#!/usr/bin/env node
/**
 * Guard 5: endpoint ↔ UI integration check.
 *
 * `verify-api` exercises endpoints in isolation. `verify-browser`
 * walks UI routes in isolation. Neither proves the WIRING: that
 * every (method, path) the backend documents is actually called
 * from a UI source file, AND that every fetch / api.* call the UI
 * makes corresponds to a documented endpoint.
 *
 * This script closes that gap. It runs purely at the source level
 * (regex over src/**) — the runtime "data appears in the rendered
 * DOM" check already lives in verify-browser.mjs, so we don't need
 * to boot a browser here.
 *
 * For every (method, path) in /openapi.json:
 *   1. Find a UI call site in src/**\/*.{ts,tsx} that hits the
 *      endpoint (string match, with path-template normalization so
 *      `/api/products/${id}` matches `/api/products/{product_id}`).
 *   2. FAIL — orphan endpoint — if no call site exists.
 *   3. WARN — and only WARN — if a call site exists but the
 *      response is destructured-then-ignored (no .map, no setState,
 *      no destructured use). The agent may be deliberately
 *      fire-and-forget; we surface it but don't block.
 *
 * Inverse check: every fetch("/api/...") and api.{verb}("/api/...")
 * call in src/ must correspond to a documented endpoint, otherwise
 * FAIL — undocumented endpoint. Catches the agent writing ad-hoc
 * fetch calls that bypass the documented surface.
 *
 * Static template: no-op (no backend / no /openapi.json). Exits 0.
 *
 * Run order: after verify:api, before verify:browser. Wired into
 * `npm run verify` between those two.
 *
 * Output:
 *   - human-readable summary to stdout (one line per endpoint)
 *   - integration-report.json to node_modules/.ojas-verify/ so
 *     the deploy reporter can surface "X of Y endpoints proven
 *     integrated" on the deploy success card
 *
 * Exits 1 if any orphan or undocumented call is found. Exits 0
 * otherwise (warnings don't fail the run).
 */
import { existsSync, readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { join, resolve, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  projectRoot,
  repoRoot,
  BACKEND_URL,
  bootFullstack,
  withCleanup,
  loadReport,
  saveReport,
} from "./verify-helpers.mjs";

const __filename = fileURLToPath(import.meta.url);

// Methods we treat as API verbs. Matches FastAPI's /openapi.json
// (and most OpenAPI 3.x specs).
const HTTP_METHODS = ["get", "post", "put", "patch", "delete"];

// Path params we ignore when normalising — they may appear as
// `${id}`, `{product_id}`, `:productId`, etc. We replace ANY
// segment matching these shapes with a single sentinel so two
// paths compare equal if their non-param segments match.
const PATH_PARAM_SENTINEL = "{PARAM}";

// Heuristic patterns for "the call site consumed the response".
// If any of these match within ~200 chars after the call, we treat
// the response as used. None matching = warning, not failure.
const RESPONSE_USE_PATTERNS = [
  /\.then\s*\(\s*res\s*=>\s*res\.json/i,           // fetch().then(res => res.json())
  /\.then\s*\(\s*\w+\s*=>\s*\w+\.json/i,           // any -> .json()
  /await\s+api\.\w+\s*\(/i,                          // const x = await api.get(...)
  /\.map\s*\(/i,                                    // data.map(...)
  /\.forEach\s*\(/i,                                // data.forEach(...)
  /\.length\b/i,                                    // data.length
  /set[A-Z]\w*\s*\(\s*\w+\s*\)/i,                   // setProducts(data)
  /set[A-Z]\w*\s*\(\s*\[\.\.\./i,                  // setItems([...data])
  /const\s*\{[^}]+\}\s*=\s*await/i,                 // const {x} = await ...
  /=\s*await\s+api\.\w+/i,                          // = await api.*
  /=\s*await\s+fetch\s*\(\s*[`"]/i,                 // = await fetch(`/api/...`)
];

// Heuristic patterns for "this is an API call site in the source".
// Both axios-style (`api.get("/api/...")`) and fetch-style
// (`fetch("/api/...", {...method: "POST"})`) are recognised.
const CALL_SITE_PATTERNS = [
  // api.{verb}("/api/...") — method is encoded in the verb
  /\bapi\.(get|post|put|patch|delete)\s*\(\s*([`"'])(\/api\/[^"'`]*)\2/gi,
  // fetch("/api/...") — method may be a default or in the options object
  /\bfetch\s*\(\s*([`"'])(\/api\/[^"'`]*)\1/g,
];

// ---------------------------------------------------------------------------
// Static-template shortcut
// ---------------------------------------------------------------------------
// If there's no backend on disk, this is a static app. No API to
// integrate against — exit 0 with a stub report so the deploy
// pipeline still has something to surface.
const hasBackend = existsSync(join(repoRoot, "backend", "main.py"));
if (!hasBackend) {
  const report = loadReport();
  report.guards.integration = {
    status: "skip",
    reason: "static template — no backend to integrate against",
    endpoints: 0,
    orphan: 0,
    undocumented: 0,
    warnings: 0,
  };
  saveReport(report);
  console.log("✓ verify:integration (static template — skipped)");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Source walker
// ---------------------------------------------------------------------------
const SRC = join(projectRoot, "src");
const SKIP_DIRS = new Set(["ui", "node_modules", "dist", "scripts"]);

/** Yield every .ts / .tsx file under SRC, recursively. Skip the
 * `ui/` primitive library (no app-level API calls live there) and
 * files prefixed with `_` (template examples like
 * `_starter-dashboard.example.tsx` that aren't meant to ship). */
function* walkSrc(dir) {
  if (!existsSync(dir)) return;
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name.startsWith(".")) continue;
    if (entry.name.startsWith("_")) continue;
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walkSrc(full);
    } else if (entry.isFile() && /\.(t|j)sx?$/.test(entry.name)) {
      yield full;
    }
  }
}

/**
 * Read every source file, return [{file, line, method, path, raw,
 * consumed}] for every API call site we can identify. The
 * `consumed` flag is a best-effort check: if any of
 * RESPONSE_USE_PATTERNS matches within 200 chars after the call,
 * we mark consumed=true.
 *
 * Patterns we look for:
 *   - fetch(`/api/...`)  →  literal-string fetch
 *   - fetch(`${API}/...`)  →  template-literal fetch using the
 *     API base constant defined in src/lib/api.ts
 *   - api.{verb}(`/api/...`)  →  axios-style wrapper, method in
 *     the verb name
 *
 * The `${API}` template substitution is a common Ojas pattern
 * (see `src/lib/api.ts`) so we look for it explicitly and treat
 * the rest of the template as the path.
 */
function findCallSites() {
  const out = [];
  // Regex for the URL argument: backtick template, single or
  // double quote string. Capture group 1 = the URL content.
  const URL_RE = "(?:`([^`]*)`|\"([^\"]*)\"|'([^']*)')";

  for (const file of walkSrc(SRC)) {
    const src = readFileSync(file, "utf8");

    // {anyName}.{verb}(URL) — verb encodes the method. Catches
    // both `api.get("/api/...")` (instacart-style) AND
    // `apiPhotos.get(\`/photos\`)` (apple-photos-style wrapper).
    // The {anyName} pattern matches any identifier; the verb
    // list keeps it specific enough to avoid false positives.
    //
    // Capture groups: 1=identifier, 2=verb, 3=backtick URL,
    // 4=double-quote URL, 5=single-quote URL.
    const apiRe = new RegExp(
      `\\b(\\w+)\\.(get|post|put|patch|delete)\\s*\\(\\s*${URL_RE}`,
      "gi",
    );
    let m;
    while ((m = apiRe.exec(src)) !== null) {
      const url = m[3] ?? m[4] ?? m[5] ?? "";
      const path = resolveApiBase(url);
      if (!path) continue;
      const method = m[2].toUpperCase();
      const idx = m.index;
      const tail = src.slice(idx, idx + 250);
      const consumed = RESPONSE_USE_PATTERNS.some((re) => re.test(tail));
      out.push({
        file: relative(projectRoot, file),
        line: src.slice(0, idx).split("\n").length,
        method,
        path,
        raw: m[0],
        consumed,
      });
    }

    // fetch(URL) — method is either default (GET) or in the
    // options object. Scan a window after the call for
    // `method: "POST"` / `method: 'PUT'` and default to GET.
    const fetchRe = new RegExp(`\\bfetch\\s*\\(\\s*${URL_RE}`, "g");
    while ((m = fetchRe.exec(src)) !== null) {
      const url = m[1] ?? m[2] ?? m[3] ?? "";
      const path = resolveApiBase(url);
      if (!path) continue;
      const idx = m.index;
      const tail = src.slice(idx, idx + 400);
      const methodMatch = tail.match(/method\s*:\s*["']([A-Z]+)["']/i);
      const method = methodMatch ? methodMatch[1].toUpperCase() : "GET";
      const consumed = RESPONSE_USE_PATTERNS.some((re) => re.test(tail));
      out.push({
        file: relative(projectRoot, file),
        line: src.slice(0, idx).split("\n").length,
        method,
        path,
        raw: m[0],
        consumed,
      });
    }

    // request<T>(URL, opts?) — wrapper pattern used by
    // apple-photos (and any future Ojas app that puts a typed
    // API client in src/lib/api*.ts). The function is named
    // `request` here, but the leading pattern is intentionally
    // permissive: any identifier immediately followed by an
    // optional `<...>` generic + `(URL, ...)`. This catches
    // both `request(`/photos/${id}`)` and
    // `request<Photo>("/photos", { method: "POST" })`.
    //
    // The non-greedy `<[^>]+?>` is critical: a greedy match
    // would extend past `request<Photo[]>` into the next
    // generic (e.g. `Record<string, ...>` inside an assertion)
    // and fail to find the closing `>`. The `+?` keeps the
    // span as short as possible while still satisfying
    // `<TypeName>`.
    //
    // The method-extraction rule is the same as for fetch() —
    // scan a window after the call for `method: "POST"` / etc.
    // and treat the second arg if it contains `jsonBody(` as
    // POST (the Ojas `jsonBody` helper always sets
    // `method: "POST"`).
    const requestRe = new RegExp(
      `\\b(\\w+)\\s*(?:<[^>]+?>)?\\s*\\(\\s*${URL_RE}`,
      "g",
    );
    while ((m = requestRe.exec(src)) !== null) {
      const calledName = m[1] ?? "";
      const url = m[2] ?? m[3] ?? m[4] ?? "";
      // The api.* and fetch patterns are handled by their own
      // loops above — skip them here so we don't double-count.
      // The request() walker's purpose is the generic wrapper
      // pattern (apple-photos's `request<Photo>(\`/photos\`)`
      // and any future Ojas app that puts a typed API client
      // in src/lib/api*.ts).
      if (/^(api|fetch)$/i.test(calledName)) continue;
      // request() wrappers (like apple-photos's
      // `request<Photo>(\`/photos/${id}\`)`) pass paths
      // RELATIVE to the API base — the leading `/api` is
      // added by `${API}` in the inner `fetch()` call. To
      // match these against /openapi.json paths, we prepend
      // `/api` if the URL doesn't already start with it.
      let path;
      if (url.startsWith("/api/") || url === "/api") {
        path = url;
      } else if (url.startsWith("/") && !url.startsWith("//")) {
        // Looks like a relative path to the API base —
        // typical wrapper pattern.
        path = "/api" + url.replace(/\$\{[^}]+\}/g, PATH_PARAM_SENTINEL);
      } else {
        path = resolveApiBase(url);
      }
      if (!path) continue;
      // Path hygiene: strip trailing slashes (e.g.
      // `request<Photo>("/photos/")` should match
      // `/api/photos` in the spec, not generate a phantom
      // `/api/photos/`).
      path = path.replace(/\/+$/, "") || "/";
      if (!path) continue;
      const idx = m.index;
      const tail = src.slice(idx, idx + 500);
      let method = "GET";
      const methodMatch = tail.match(/method\s*:\s*["']([A-Z]+)["']/i);
      if (methodMatch) {
        method = methodMatch[1].toUpperCase();
      } else if (/jsonBody\s*\(/.test(tail.slice(0, 80))) {
        // The Ojas `jsonBody` helper always sets
        // `method: "POST"`. If the call's immediate 80 chars
        // contain `jsonBody(`, treat as POST without needing
        // to scan the helper definition.
        method = "POST";
      }
      // Source-level "consumed" detection has a known
      // limitation: when the call lives in src/lib/* (an API
      // wrapper), the response is consumed by the wrapper's
      // CALLER, not by the immediate surroundings of the
      // `request(...)` call. Mark these as consumed=true so
      // they don't pollute the warnings list.
      const inLib = file.includes("/lib/");
      const consumed = inLib || RESPONSE_USE_PATTERNS.some((re) => re.test(tail));
      out.push({
        file: relative(projectRoot, file),
        line: src.slice(0, idx).split("\n").length,
        method,
        path,
        raw: m[0],
        consumed,
      });
    }
  }
  return out;
}

/**
 * Given a URL fragment as it appears in a `fetch()` or `api.*`
 * call, extract the API path. The common forms:
 *   "/api/stores"               →  "/api/stores"
 *   "${API}/stores"             →  "/api/stores"  (API is the base
 *                                            constant; we treat
 *                                            it as /api)
 *   "${API}/stores/${id}/items" →  "/api/stores/{PARAM}/items"
 *   "/health"                   →  null  (not an API call)
 *   "https://example.com/..."   →  null  (external, skip)
 */
function resolveApiBase(url) {
  if (!url) return null;
  // External URL — skip.
  if (/^https?:\/\//i.test(url)) return null;
  // Strip query string + hash so `fetch(\`${API}/products?q=${q}\`)`
  // normalises to `/api/products`, not `/api/products?q={PARAM}`.
  const pathOnly = url.split(/[?#]/)[0];
  // Must contain an /api reference somehow.
  // Form A: literal "/api/..." — return as-is.
  if (pathOnly.includes("/api/") || pathOnly === "/api") {
    return pathOnly.replace(/^\$\{API\}/, "").replace(/^\/+/, "/");
  }
  // Form B: "${API}/..." with no inline /api/ — assume the API
  // base IS /api. (This is the Ojas convention; src/lib/api.ts
  // exports `API = ${base}/api`.)
  if (pathOnly.includes("${API}")) {
    return (
      "/api" +
      pathOnly
        .replace(/^\$\{API\}/, "")
        // Normalise template-literal params: ${id}, ${productId}
        .replace(/\$\{[^}]+\}/g, PATH_PARAM_SENTINEL)
    );
  }
  return null;
}

// ---------------------------------------------------------------------------
// Path-template normalisation
// ---------------------------------------------------------------------------
/**
 * Convert any of `/api/products/${id}`, `/api/products/{id}`,
 * `/api/products/:id` to `/api/products/{PARAM}` so two paths
 * compare equal if their non-param segments match.
 */
function normalisePath(p) {
  // ${...} template literal substitution
  p = p.replace(/\$\{[^}]+\}/g, PATH_PARAM_SENTINEL);
  // :id colon-style (React Router, Express)
  p = p.replace(/:[A-Za-z_]\w*/g, PATH_PARAM_SENTINEL);
  // {id} / {product_id} / etc. — already a template var in the
  // OpenAPI spec; this normalises src/ paths. Strip the param so
  // it doesn't double-normalise.
  p = p.replace(/\{[A-Za-z_]\w*\}/g, PATH_PARAM_SENTINEL);
  // collapse consecutive slashes just in case
  p = p.replace(/\/{2,}/g, "/");
  return p;
}

/** Does the literal path (as it appears in src/) match the
 * template path (from /openapi.json)?  Templates may have
 * `{product_id}`; literals may have `${id}`. After
 * normalisePath both reduce to /api/products/{PARAM}. */
function pathMatches(templatePath, literalPath) {
  return normalisePath(templatePath) === normalisePath(literalPath);
}

// ---------------------------------------------------------------------------
// OpenAPI walker
// ---------------------------------------------------------------------------
async function fetchSpec(baseUrl) {
  const res = await fetch(`${baseUrl}/openapi.json`);
  if (!res.ok) throw new Error(`/openapi.json returned ${res.status}`);
  return res.json();
}

function discoverEndpoints(spec) {
  const out = [];
  for (const [path, methods] of Object.entries(spec.paths || {})) {
    if (!path.startsWith("/api/")) continue;
    // Skip framework plumbing: /health, /docs, /openapi.json
    if (/^\/(health|openapi\.json|docs|redoc)(\/|$)/.test(path)) continue;
    for (const [method, op] of Object.entries(methods || {})) {
      if (!HTTP_METHODS.includes(method)) continue;
      out.push({ method: method.toUpperCase(), path, operation: op });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
const failures = [];
const warnings = [];
const report = loadReport();
report.guards.integration = {
  status: "pass",
  endpoints: 0,
  with_caller: 0,
  orphan: 0,
  undocumented: 0,
  warnings: 0,
  per_endpoint: [],
};

const procs = await bootFullstack();
try {
  const callSites = findCallSites();
  console.log(
    `verify:integration: found ${callSites.length} API call site(s) in src/`,
  );

  const spec = await fetchSpec(BACKEND_URL);
  const endpoints = discoverEndpoints(spec);
  report.guards.integration.endpoints = endpoints.length;

  // 1. For each documented endpoint, find a UI caller.
  for (const { method, path } of endpoints) {
    const matching = callSites.filter(
      (c) => c.method === method && pathMatches(path, c.path),
    );
    if (matching.length === 0) {
      failures.push(
        `orphan endpoint: ${method} ${path} — no UI caller found in src/`,
      );
      report.guards.integration.orphan += 1;
      report.guards.integration.per_endpoint.push({
        method,
        path,
        status: "orphan",
        caller: null,
      });
    } else {
      report.guards.integration.with_caller += 1;
      const caller = matching[0];
      const consumed = matching.some((c) => c.consumed);
      report.guards.integration.per_endpoint.push({
        method,
        path,
        status: consumed ? "ok" : "ok-unused",
        caller: `${caller.file}:${caller.line}`,
      });
      if (!consumed) {
        warnings.push(
          `${method} ${path} called at ${caller.file}:${caller.line} but response appears unused`,
        );
        report.guards.integration.warnings += 1;
      }
    }
  }

  // 2. Inverse: every UI call must be in the spec.
  const specKeys = new Set(
    endpoints.map((e) => `${e.method} ${normalisePath(e.path)}`),
  );
  const seenUndocumented = new Set();
  for (const c of callSites) {
    const key = `${c.method} ${normalisePath(c.path)}`;
    if (!specKeys.has(key) && !seenUndocumented.has(key)) {
      seenUndocumented.add(key);
      failures.push(
        `undocumented endpoint: ${c.method} ${c.path} called from ${c.file}:${c.line} but not in /openapi.json`,
      );
      report.guards.integration.undocumented += 1;
    }
  }

  // 3. Tally and report.
  const total = endpoints.length;
  const orphans = report.guards.integration.orphan;
  const undoc = report.guards.integration.undocumented;
  const warns = report.guards.integration.warnings;
  const ok = report.guards.integration.with_caller - warns;

  console.log("");
  console.log(
    `  ${ok}/${total} endpoints have a UI caller that consumes the response`,
  );
  if (warns > 0)
    console.log(
      `  ${warns} warning(s) — caller exists but response appears unused`,
    );
  if (orphans > 0)
    console.log(`  ${orphans} orphan(s) — endpoint has no UI caller`);
  if (undoc > 0)
    console.log(`  ${undoc} undocumented call(s) — fetch in src/ but not in spec`);

  if (failures.length > 0) {
    report.guards.integration.status = "fail";
    console.log("");
    console.log("  failures:");
    for (const f of failures) console.log(`    ✗ ${f}`);
  } else {
    console.log("");
    console.log("✓ verify:integration passed");
  }
  if (warnings.length > 0 && failures.length === 0) {
    console.log("");
    console.log("  warnings (not blocking):");
    for (const w of warnings) console.log(`    ⚠ ${w}`);
  }

  saveReport(report);

  process.exitCode = failures.length > 0 ? 1 : 0;
} finally {
  await withCleanup(procs, async () => {});
}
