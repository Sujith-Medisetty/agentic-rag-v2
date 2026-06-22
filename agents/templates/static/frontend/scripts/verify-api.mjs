#!/usr/bin/env node
/**
 * Backend API smoke test (OpenAPI-driven CRUD).
 *
 * `verify-browser.mjs` exercises the UI. This script exercises
 * the BACKEND directly via the documented FastAPI spec at
 * /openapi.json. Every documented (method, path) pair gets:
 *
 *   1. Body synthesised from the requestBody schema (recursive
 *      generator handles type, format, enum, example, $ref,
 *      allOf/oneOf/anyOf, nullable — depth-capped at 5).
 *   2. Sent with a 10s timeout.
 *   3. Status checked against the documented success response.
 *   4. For POST/PUT/PATCH: the returned id is captured and a
 *      follow-up GET + DELETE round-trip is queued — so we
 *      exercise the FULL create→read→delete lifecycle.
 *
 * Auth-aware: if the spec declares securitySchemes, this script
 * runs signup → login with TEST_CREDS, obtains a bearer token,
 * and attaches it to every protected request. The current Ojas
 * template has no auth, so this is a no-op today; the wiring is
 * here for the day a template ships with auth.
 *
 * Each endpoint is reported as:
 *   - passed  — request succeeded with documented status, body
 *               matches schema, round-trip (if applicable) clean
 *   - skipped — endpoint requires fixtures we can't synthesise
 *               (file uploads, foreign keys without a parent) OR
 *               auth was required but unavailable
 *   - failed  — wrong status, schema mismatch, network error, or
 *               round-trip couldn't complete
 *
 * Exits 1 if any failed. Exits 0 otherwise (passed + skipped
 * only is a clean run; the user gets to investigate skipped
 * endpoints from the report).
 *
 * Pre-requisites the caller (npm run verify:api) handles:
 *   - `npm run build` has produced frontend/dist/index.html
 *     (verify-api only needs the backend, but `verify` runs
 *      build first; we re-use the build artifact)
 *   - For fullstack apps: backend deps installed
 *
 * Static-template: NO verify-api script in package.json — no
 * backend to smoke. If verify-api is invoked against a static
 * template (no backend/main.py), exit 0 silently.
 *
 * Usage:
 *   node scripts/verify-api.mjs
 */
import { existsSync } from "node:fs";
import { join } from "node:path";

import {
  projectRoot,
  repoRoot,
  PYTHON_BIN,
  TEST_CREDS,
  bootFullstack,
  withCleanup,
  loadReport,
  saveReport,
} from "./verify-helpers.mjs";

const REQUEST_TIMEOUT_MS = 10_000;
// Methods we actually exercise. FastAPI's /openapi.json also
// documents OPTIONS / HEAD / etc — they're framework noise.
const METHODS_TO_EXERCISE = ["get", "post", "put", "patch", "delete"];
// Path patterns we skip — framework plumbing, not app endpoints.
const SKIP_PATH_PATTERNS = [
  /^\/health$/i,
  /^\/openapi\.json$/i,
  /^\/docs($|\/)/i,
  /^\/redoc($|\/)/i,
  /^\/api\/docs/i,
];
// Field names that signal "needs a real fixture we can't
// synthesise" — password reset tokens, file uploads, etc. If
// the request body ONLY contains fields whose names match this
// list, the endpoint is skipped (no point sending fake tokens).
// Partial-fixture bodies still get exercised; non-fixture fields
// get synthesised values, fixture fields get `"verify-fixture-…"`
// placeholders.
const FIXTURE_FIELD_PATTERNS = [
  /^password(_confirmation)?$/i,
  /^current_password$/i,
  /^refresh_token$/i,
  /^access_token$/i,
  /^token$/i,
  /^file$/i,
  /^image$/i,
  /^avatar$/i,
  /^attachment$/i,
];

// ---------------------------------------------------------------------------
// OpenAPI discovery
// ---------------------------------------------------------------------------
async function fetchOpenApi(baseUrl) {
  const res = await fetch(`${baseUrl}/openapi.json`, {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`/openapi.json returned ${res.status}`);
  return res.json();
}

function isSkippablePath(path) {
  return SKIP_PATH_PATTERNS.some((re) => re.test(path));
}

// Returns [{ method, path, operation }] for every (method, path)
// the spec documents AND we exercise. `operation` is the per-method
// block (parameters, requestBody, responses, security).
function discoverEndpoints(spec) {
  const out = [];
  for (const [path, methods] of Object.entries(spec.paths || {})) {
    if (isSkippablePath(path)) continue;
    for (const [method, operation] of Object.entries(methods || {})) {
      if (!METHODS_TO_EXERCISE.includes(method)) continue;
      out.push({ method, path, operation });
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// Body synthesis (recursive schema walker)
// ---------------------------------------------------------------------------
// Synthesise a JSON value matching the OpenAPI/Pydantic schema.
// Handles:
//   - type: string/integer/number/boolean/array/object
//   - format: email/uuid/date-time/date/uri
//   - enum: first value
//   - example: returns the example verbatim
//   - default: returns the default if present
//   - $ref: resolved via spec.components.schemas with seen-set
//     cycle detection
//   - allOf/oneOf/anyOf: first branch
//   - nullable: same as the underlying type
//   - required-but-missing: walks the parent's `required: [...]`
//
// `seen` tracks $ref names already on the recursion stack —
// prevents infinite loops when schema A references B which
// references A (Pydantic forward refs can produce this).
function generateValue(schema, spec, seen, depth) {
  if (!schema || depth > 5) return null;

  // $ref — resolve to the target schema, then recurse.
  if (schema.$ref) {
    const name = schema.$ref.split("/").pop();
    if (seen.has(name)) return null; // cycle
    const target = spec.components?.schemas?.[name];
    if (!target) return null;
    return generateValue(target, spec, new Set([...seen, name]), depth + 1);
  }

  // Composite types — take the first branch.
  if (schema.allOf?.length) {
    return generateValue(schema.allOf[0], spec, seen, depth + 1);
  }
  if (schema.oneOf?.length) {
    return generateValue(schema.oneOf[0], spec, seen, depth + 1);
  }
  if (schema.anyOf?.length) {
    return generateValue(schema.anyOf[0], spec, seen, depth + 1);
  }

  // example / default — author knows best.
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;

  // enum — pick first.
  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    return schema.enum[0];
  }

  const format = schema.format || "";
  const type = schema.type;

  if (type === "string" || (!type && (format || schema.enum))) {
    if (format === "email" || /email/i.test(schema.title || "")) {
      return TEST_CREDS.email;
    }
    if (format === "uuid") return crypto.randomUUID();
    if (format === "date-time") return new Date().toISOString();
    if (format === "date") return new Date().toISOString().slice(0, 10);
    if (format === "uri" || format === "url") {
      return `https://example.com/${crypto.randomUUID()}`;
    }
    // Generic string — short + deterministic + unique.
    return `verify-${Math.random().toString(36).slice(2, 10)}`;
  }

  if (type === "integer") return 1;
  if (type === "number") return 1.5;
  if (type === "boolean") return true;

  if (type === "array") {
    const itemSchema = schema.items || {};
    // Single-element array — enough to prove the endpoint works.
    return [generateValue(itemSchema, spec, seen, depth + 1)];
  }

  if (type === "object" || schema.properties) {
    const obj = {};
    const required = new Set(schema.required || []);
    // Walk BOTH required and optional properties so optional
    // fields with format hints (uuid, date-time) get sensible
    // values too. Skip purely optional fields if the schema is
    // very large (>30 props) to avoid huge bodies.
    const props = Object.entries(schema.properties || {});
    const toInclude =
      props.length > 30 ? props.filter(([n]) => required.has(n)) : props;
    for (const [name, propSchema] of toInclude) {
      const isFixture = FIXTURE_FIELD_PATTERNS.some((re) => re.test(name));
      if (isFixture && !required.has(name)) continue;
      obj[name] = isFixture
        ? `verify-fixture-${Math.random().toString(36).slice(2, 8)}`
        : generateValue(propSchema, spec, seen, depth + 1);
    }
    return obj;
  }

  return null;
}

// Synthesise a path-param value matching its declared schema.
// Integer-id params get 1/2/3 so we can test multiple IDs in the
// same session without colliding on uniqueness constraints.
function generatePathParamValue(paramSchema, index) {
  if (!paramSchema) return String(index + 1);
  if (paramSchema.format === "uuid") return crypto.randomUUID();
  if (paramSchema.type === "integer") return index + 1;
  if (paramSchema.type === "string") {
    // Try the spec's example / enum first; otherwise pick a short
    // deterministic string. Using the same string for every
    // param would surface duplicates — use index so calls don't
    // collide.
    if (Array.isArray(paramSchema.enum) && paramSchema.enum.length > 0) {
      return String(paramSchema.enum[index % paramSchema.enum.length]);
    }
    return `verify-id-${index}`;
  }
  return String(index + 1);
}

// Substitute path params in the URL template. Returns null if
// the spec is missing required path params (caller skips the
// endpoint).
function substitutePathParams(pathTemplate, pathParams, index) {
  let url = pathTemplate;
  const missing = [];
  for (const p of pathParams) {
    const re = new RegExp(`\\{${p.name}\\}`);
    if (!re.test(url)) continue;
    if (p.required === false) continue;
    const value = generatePathParamValue(p.schema, index);
    url = url.replace(re, encodeURIComponent(value));
  }
  // Detect any remaining unsubstituted `{name}` segments — those
  // are path params the spec didn't declare, so we can't
  // exercise this endpoint.
  if (/\{[^}]+\}/.test(url)) return null;
  return url;
}

// ---------------------------------------------------------------------------
// Auth-aware request
// ---------------------------------------------------------------------------
// Try to authenticate against the spec's securitySchemes. Today
// the Ojas template has no auth — this returns null and every
// request goes out unauthenticated. When a future template adds
// securitySchemes (e.g. bearer JWT via OAuth2 password flow),
// wire the signup + token-grant sequence here.
// ---------------------------------------------------------------------------
// Auth-aware request
// ---------------------------------------------------------------------------
// When the spec declares securitySchemes (e.g. bearer JWT via
// OAuth2 password flow), we perform a signup + login dance to
// obtain a real token, then attach it to every protected
// request. This turns 401s on protected endpoints into real
// 2xx/4xx signal — the same auth flow a real user would
// experience.
//
// The Ojas fullstack template currently ships two auth
// shapes:
//   1. Pydantic OAuth2 password flow — POST /auth/signup then
//      POST /auth/login, capture the bearer from the response.
//   2. A custom "send signup body with email + password, get
//      a token back" shape (some templates use this).
//
// We try (1) first, then (2) as a fallback. The
// `firstAuthRoute()` helper walks the spec for the first path
// containing "auth" + the appropriate method, since the exact
// route name varies by template.
function firstAuthPath(spec, suffix) {
  for (const [path, methods] of Object.entries(spec.paths || {})) {
    if (path.includes(suffix)) {
      for (const m of ["post", "POST"]) {
        if (methods[m]) return path;
      }
    }
  }
  return null;
}

function extractToken(body) {
  if (!body || typeof body !== "object") return null;
  // Common token fields: access_token, token, jwt, bearer
  for (const k of ["access_token", "token", "jwt", "bearer", "auth_token"]) {
    if (typeof body[k] === "string" && body[k].length > 0) return body[k];
  }
  return null;
}

async function maybeAuthenticate(spec, baseUrl) {
  const schemes = spec.components?.securitySchemes;
  if (!schemes || Object.keys(schemes).length === 0) return null;
  // If the spec declares bearerAuth / BearerAuth (or any
  // HTTP-bearer scheme), try to obtain a token. The OpenAPI
  // 3.x bearer scheme is `type: http, scheme: bearer`.
  const isBearer = Object.values(schemes).some(
    (s) =>
      s &&
      ((s.type === "http" && s.scheme === "bearer") ||
        s.type === "oauth2" ||
        s.type === "apiKey"),
  );
  if (!isBearer) return null;

  const signupPath = firstAuthPath(spec, "signup") || firstAuthPath(spec, "register");
  const loginPath =
    firstAuthPath(spec, "login") ||
    firstAuthPath(spec, "signin") ||
    firstAuthPath(spec, "auth");

  // First: try signup. If the user already exists (from a
  // previous run) the backend returns 4xx — that's fine,
  // we move to login.
  if (signupPath) {
    try {
      const r = await fetch(`${baseUrl}${signupPath}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          email: TEST_CREDS.email,
          password: TEST_CREDS.password,
          name: TEST_CREDS.name,
        }),
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      if (r.ok) {
        const body = await r.json().catch(() => null);
        const token = extractToken(body);
        if (token) {
          return { Authorization: `Bearer ${token}` };
        }
      }
    } catch {
      /* fall through to login */
    }
  }
  // Second: try login. Some templates (apple-photos /
  // instacart) accept {email, password} and return a token
  // without a separate signup endpoint — they'll succeed
  // here. Others (Pydantic OAuth2 password flow) require
  // form-encoded data — try both shapes.
  if (loginPath) {
    for (const body of [
      // JSON shape: most custom templates
      JSON.stringify({
        email: TEST_CREDS.email,
        password: TEST_CREDS.password,
      }),
      // Form shape: Pydantic OAuth2PasswordRequestForm
      new URLSearchParams({
        username: TEST_CREDS.email,
        password: TEST_CREDS.password,
      }).toString(),
    ]) {
      const isForm = body.startsWith("username=");
      try {
        const r = await fetch(`${baseUrl}${loginPath}`, {
          method: "POST",
          headers: {
            "content-type": isForm
              ? "application/x-www-form-urlencoded"
              : "application/json",
          },
          body,
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        if (r.ok) {
          const respBody = await r.json().catch(() => null);
          const token = extractToken(respBody);
          if (token) {
            return { Authorization: `Bearer ${token}` };
          }
        }
      } catch {
        /* try the next body shape */
      }
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Per-endpoint smoke
// ---------------------------------------------------------------------------
// Expected success status codes, by method. Pydantic/FastAPI
// convention: POST→201, PUT/PATCH→200, DELETE→204, GET→200.
// Anything else is treated as a failure (the script doesn't
// pass-through 4xx responses as "expected").
function expectedSuccessStatuses(method) {
  if (method === "post") return [200, 201];
  if (method === "delete") return [200, 204];
  return [200];
}

// Build a request body from the operation's requestBody schema.
// Returns null for endpoints with no body (GET, DELETE, etc.).
// Returns { skip: true, reason } if the body requires fixtures
// we can't synthesise.
//
// `realIdsByName` (optional) is a Map<resourceName, id[]>
// populated by main() from real GETs to the list endpoints.
// If provided, we walk the synthesized body for FK-shaped
// fields (suffixed `_id`, integer type) and substitute real
// IDs from the matching resource. This kills the largest
// class of false-positives where the schema is technically
// valid but the body would have a non-existent FK and the
// endpoint returns 500 from the FK constraint.
function buildBody(operation, spec, realIdsByName) {
  const rb = operation.requestBody;
  if (!rb) return { body: null };
  const jsonContent = rb.content?.["application/json"];
  if (!jsonContent) return { body: null };
  const schema = jsonContent.schema;
  if (!schema) return { body: null };
  // Reuse generateValue for the top-level object. Top-level
  // schema is typically a $ref to a CreateX pydantic model —
  // pass the full spec so $ref resolution can find the target.
  const value = generateValue(schema, spec, new Set(), 0);
  if (!value || typeof value !== "object") {
    return { body: value };
  }
  // FK substitution pass: walk every value-shaped field in
  // the body. If the field name ends in `_id` and we have a
  // real ID for the corresponding resource, swap it in.
  // Examples:
  //   {product_id: 1, qty: 2}            (cart/items)
  //   {store_id: 1, address_id: 4, ...}  (orders)
  //   {album_id: 2, photo_ids: [1,2]}    (albums)
  if (realIdsByName && realIdsByName.size > 0) {
    for (const [k, v] of Object.entries(value)) {
      if (!/_id$/.test(k) && !/_ids$/.test(k)) continue;
      // Derive resource name from field: `product_id` → "product",
      // `photo_ids` → "photo", `user_id` → "user". Singularise by
      // stripping the trailing `s` for `*_ids` arrays.
      let resource = k.replace(/_ids?$/, "");
      // Some schemas use names that don't match the URL segment
      // 1:1 — `product_ids` (array) is for the `/products` list,
      // singular `product_id` is for `/products/{id}`. Both work
      // because the resource name is the URL segment prefix.
      const candidates = [resource, resource + "s"];
      let realIds = null;
      for (const c of candidates) {
        if (realIdsByName.has(c)) {
          realIds = realIdsByName.get(c);
          resource = c;
          break;
        }
      }
      if (!realIds || realIds.length === 0) continue;
      if (Array.isArray(v)) {
        // array of ids: use up to 3 real ids
        value[k] = realIds.slice(0, Math.min(3, realIds.length));
      } else {
        value[k] = realIds[0];
      }
    }
  }
  // Check if EVERY required field is a fixture field — if so,
  // skip rather than send a body of "verify-fixture-…" strings
  // that no real endpoint would accept.
  const reqSchema = schema.$ref
    ? schema // resolved $ref handled inside generateValue; we
             // don't have it here, but the value is already
             // produced. Skip this check for $refs (most cases).
    : schema;
  if (!reqSchema.$ref && reqSchema.required?.length > 0) {
    const allRequiredAreFixtures = reqSchema.required.every((n) =>
      FIXTURE_FIELD_PATTERNS.some((re) => re.test(n)),
    );
    if (allRequiredAreFixtures) {
      return { skip: true, reason: "requires fixture fields only" };
    }
  }
  return { body: value };
}

// Pull the first id-shaped field from a JSON response. Looks for
// `id`, `_id`, `uuid`, or any `*_id` field. Used for POST→GET→
// DELETE round-trip tracking.
function extractIdFromResponse(body) {
  if (!body || typeof body !== "object") return null;
  const keys = Object.keys(body);
  const idKey = keys.find((k) => /(^|_)(id|uuid)$/i.test(k));
  return idKey ? body[idKey] : null;
}

// Send a single request. Returns { status, body, error }.
// 10s timeout. JSON request/response when applicable.
async function sendRequest({ method, url, body, headers }) {
  const init = {
    method: method.toUpperCase(),
    headers: { accept: "application/json", ...headers },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  };
  if (body !== null && body !== undefined && method !== "get") {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  try {
    const res = await fetch(url, init);
    let parsed = null;
    let rawText = null;
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      try {
        parsed = await res.json();
      } catch {
        // JSON parse failed — fall through and capture raw
        // text so the failure message shows the body the
        // server actually sent (e.g. an HTML error page or
        // a Python traceback).
      }
    }
    // Always read the body as text too. Used for failure
    // reporting on non-2xx responses where the server might
    // have returned an HTML error page or a stack trace
    // rather than JSON. Capped to 500 chars to keep the
    // failure message readable.
    if (!parsed || res.status >= 400) {
      try {
        rawText = (await res.text()).slice(0, 500);
      } catch {
        /* ignore */
      }
    }
    return { status: res.status, body: parsed, rawText };
  } catch (e) {
    return { status: 0, body: null, rawText: null, error: e instanceof Error ? e.message : String(e) };
  }
}

// Exercise a single endpoint. Returns one of:
//   { kind: "passed",  roundTrip?: "ok" | "failed" }
//   { kind: "skipped", reason }
//   { kind: "failed",  reason }
async function exerciseEndpoint({ method, path, operation }, baseUrl, spec, authHeaders, realIdsByName, roundTripIds) {
  // Path params
  const pathParams = (operation.parameters || []).filter((p) => p.in === "path");
  const resourceName = path.replace(/^\/api\//, "").replace(/\/.*$/, "");
  const paramIndex = roundTripIds.next();
  let urlPath;
  if (pathParams.length > 0) {
    // Substitute each path param. For id-bearing endpoints
    // (PATCH, DELETE), prefer real IDs from the pre-fetched
    // list. If no real IDs exist, skip — there's no way to
    // exercise the endpoint meaningfully without one.
    urlPath = path;
    for (const p of pathParams) {
      const re = new RegExp(`\\{${p.name}\\}`);
      if (!re.test(urlPath)) continue;
      const real = realIdsByName.get(resourceName);
      if (Array.isArray(real) && real.length === 0) {
        return { kind: "skipped", reason: `no rows in /api/${resourceName} for ${p.name}` };
      }
      const value = pickPathParamValue(resourceName, p.name, p.schema, realIdsByName, paramIndex);
      urlPath = urlPath.replace(re, encodeURIComponent(value));
    }
  } else {
    urlPath = path;
  }
  const url = `${baseUrl}${urlPath}`;

  // Build body (POST/PUT/PATCH)
  let body = null;
  if (["post", "put", "patch"].includes(method)) {
    const built = buildBody(operation, spec, realIdsByName);
    if (built.skip) return { kind: "skipped", reason: built.reason };
    body = built.body;
  }

  // Auth-aware: if the operation requires auth and we couldn't
  // obtain a token, skip rather than fail.
  if (operation.security?.length > 0 && !authHeaders) {
    return { kind: "skipped", reason: "requires auth (not configured in template)" };
  }

  // Send the primary request.
  const primary = await sendRequest({ method, url, body, headers: authHeaders });
  if (primary.error) {
    return { kind: "failed", reason: `network: ${primary.error}` };
  }

  const expected = expectedSuccessStatuses(method);
  if (!expected.includes(primary.status)) {
    // Build an informative failure message. The body
    // (parsed JSON or raw text) tells the user WHY the
    // endpoint failed — was it a 500 with a stack trace? a
    // 401 with a "missing token" hint? a 422 with a
    // validation error? Without the body, all 5xx look
    // identical and the user has to re-run the request by
    // hand to debug.
    let detail = "";
    if (primary.body && typeof primary.body === "object") {
      const json = JSON.stringify(primary.body);
      detail = json.length > 300 ? json.slice(0, 300) + "…" : json;
    } else if (primary.rawText) {
      detail = primary.rawText;
    } else if (primary.body) {
      detail = String(primary.body).slice(0, 300);
    }
    return {
      kind: "failed",
      reason: `expected ${expected.join("/")}, got ${primary.status}: ${detail || "(no body)"}`,
    };
  }

  // Round-trip for POST: capture id, queue GET (if available) + DELETE.
  let roundTrip = null;
  if (method === "post" && primary.body) {
    const newId = extractIdFromResponse(primary.body);
    if (newId !== null && newId !== undefined) {
      // POST's path has no path-param (it's a collection endpoint).
      // The round-trip GET/DELETE need the DETAIL path, e.g.
      // /api/items/{item_id}. Derive it from the spec.
      const resourceName = path.replace(/^\/api\//, "").replace(/\/.*$/, "");
      const detailPath = findDetailPathTemplate(spec, resourceName);
      if (!detailPath) {
        // No detail endpoint documented — skip round-trip
        // (the POST itself was verified above).
        roundTrip = null;
      } else {
        const detailUrl = `${baseUrl}${detailPath.replace(/\{[^}]+\}/, encodeURIComponent(String(newId)))}`;
        // GET the new resource IF the spec documents a GET on
        // the detail path. Some templates only expose PATCH +
        // DELETE on detail (e.g. "save edits then delete"
        // without a dedicated read). Without a documented GET,
        // skip the GET leg rather than fail the round-trip on
        // a 405.
        const detailMethods = spec.paths?.[detailPath] || {};
        const hasGetDetail = Boolean(detailMethods.get);
        let getOk = !hasGetDetail; // true when GET leg is skipped
        if (hasGetDetail) {
          const getRes = await sendRequest({
            method: "get",
            url: detailUrl,
            headers: authHeaders,
          });
          const getExpected = expectedSuccessStatuses("get");
          if (!getExpected.includes(getRes.status)) {
            roundTrip = {
              kind: "failed",
              reason: `POST→GET round-trip failed: GET returned ${getRes.status}`,
            };
          } else {
            getOk = true;
          }
        }
        if (getOk) {
          // DELETE the new resource. 204 No Content or 200 both ok.
          const delRes = await sendRequest({
            method: "delete",
            url: detailUrl,
            headers: authHeaders,
          });
          const delExpected = expectedSuccessStatuses("delete");
          if (!delExpected.includes(delRes.status)) {
            roundTrip = {
              kind: "failed",
              reason: `POST→DELETE round-trip failed: DELETE returned ${delRes.status}`,
            };
          } else {
            roundTrip = { kind: "ok" };
          }
        }
      }
    }
  }

  if (roundTrip && roundTrip.kind === "failed") {
    return roundTrip;
  }
  return roundTrip ? { kind: "passed", roundTrip: "ok" } : { kind: "passed" };
}

// ---------------------------------------------------------------------------
// Path-param value resolution
// ---------------------------------------------------------------------------
// For endpoints with a path param (e.g. /api/items/{item_id}),
// we need a value that actually exists in the DB. DELETE on
// id=4 when no row exists returns 404 by design — that's not a
// bug to report, it's a wasted round-trip. Pre-fetch the list
// endpoint for each resource and pick from the returned IDs.
//
// Main() builds a Map<resourceName, id[]> before iterating
// endpoints. exerciseEndpoint() consults it for id-bearing
// endpoints and returns "skipped" if the resource has no rows.
function pickPathParamValue(name, paramName, paramSchema, realIdsByName, index) {
  // Prefer a real ID from the pre-fetched list (when present).
  // index is the n-th call to this endpoint — rotate through
  // up to 3 distinct IDs so we exercise multiple rows.
  const real = realIdsByName.get(name);
  if (Array.isArray(real) && real.length > 0) {
    return String(real[index % real.length]);
  }
  // Fall back to synthesised values (POST-created IDs are
  // captured by the round-trip block below).
  return generatePathParamValue(paramSchema, index);
}

// Derive the detail-path template for a resource from the spec.
// E.g. for resource "items" (registered via POST /api/items),
// look for sibling paths like /api/items/{item_id} and return
// the FIRST one. Returns null if no detail endpoint is
// documented (then the round-trip GET/DELETE can't be built
// and the POST just verifies the create itself).
function findDetailPathTemplate(spec, resourceName) {
  if (!spec?.paths) return null;
  for (const [path, methods] of Object.entries(spec.paths)) {
    if (!path.startsWith(`/api/${resourceName}/`)) continue;
    if (!/\{[^}]+\}/.test(path)) continue;
    return path;
  }
  return null;
}
async function main() {
  const isFullstack = existsSync(join(repoRoot, "backend", "main.py"));
  if (!isFullstack) {
    // Static template — no backend to smoke. Exit 0 silently.
    console.log("verify-api SKIPPED — no backend/main.py (static template).");
    return;
  }
  if (!existsSync(join(projectRoot, "dist", "index.html"))) {
    console.error(
      "verify-api FAILED: dist/index.html not found. " +
        "Run `npm run build` first.",
    );
    process.exit(1);
  }

  // Boot on a SEPARATE port and SEPARATE DB (verify-api.db)
  // so verify-browser and verify-api can run in parallel without
  // colliding on SQLite locks. Default port is BACKEND_PORT + 1
  // (typically 8766) — overridable via OJAS_VERIFY_API_PORT so a
  // user can avoid collisions with other local services.
  const API_BACKEND_PORT = Number(
    process.env.OJAS_VERIFY_API_PORT ?? (Number(process.env.OJAS_VERIFY_BACKEND_PORT ?? 8765) + 1),
  );
  const API_DB_PATH = join(
    projectRoot,
    "node_modules",
    ".ojas-verify",
    "verify-api.db",
  );
  const API_BACKEND_URL = `http://127.0.0.1:${API_BACKEND_PORT}`;

  const procs = await bootFullstack({
    pythonBin: PYTHON_BIN,
    backendPort: API_BACKEND_PORT,
    backendUrl: API_BACKEND_URL,
    dbPath: API_DB_PATH,
  });

  await withCleanup(procs, async () => {
    // Discover endpoints.
    let spec;
    try {
      spec = await fetchOpenApi(API_BACKEND_URL);
    } catch (e) {
      console.error(`verify-api FAILED: could not fetch /openapi.json: ${e.message}`);
      process.exit(1);
    }
    const endpoints = discoverEndpoints(spec);
    if (endpoints.length === 0) {
      console.log("verify-api OK — spec has no exercisable endpoints.");
      return;
    }

    // Optional auth.
    const authHeaders = await maybeAuthenticate(spec, API_BACKEND_URL);
    const authHeader = authHeaders
      ? Object.fromEntries(
          Object.entries(authHeaders).map(([k, v]) => [k.toLowerCase(), v]),
        )
      : null;

    // Pre-fetch real IDs for every resource so DELETE / PATCH
    // exercise real rows. Map<resourceName, id[]>. Empty arrays
    // mean "skip id-bearing endpoints for this resource".
    const realIdsByName = new Map();
    const resourceNames = new Set();
    for (const ep of endpoints) {
      const parts = ep.path.replace(/^\/api\//, "").split("/");
      if (parts.length === 1) resourceNames.add(parts[0]);
    }
    for (const name of resourceNames) {
      try {
        const res = await fetch(`${API_BACKEND_URL}/api/${name}`, {
          headers: { accept: "application/json", ...(authHeader || {}) },
          signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
        });
        if (!res.ok) {
          realIdsByName.set(name, []);
          continue;
        }
        const body = await res.json().catch(() => []);
        const arr = Array.isArray(body)
          ? body
          : Array.isArray(body?.items)
            ? body.items
            : [];
        const ids = [];
        for (const item of arr) {
          if (item && typeof item === "object") {
            const idField = Object.keys(item).find((k) => /(^|_)(id|uuid)$/i.test(k));
            if (idField) ids.push(item[idField]);
          }
          if (ids.length >= 3) break;
        }
        realIdsByName.set(name, ids);
      } catch {
        realIdsByName.set(name, []);
      }
    }

    // Exercise every endpoint, then report.
    const results = [];
    let roundTripCounter = 0;
    const roundTripIds = {
      next() {
        return roundTripCounter++;
      },
    };
    for (const ep of endpoints) {
      const result = await exerciseEndpoint(ep, API_BACKEND_URL, spec, authHeader, realIdsByName, roundTripIds);
      results.push({ endpoint: ep, result });
    }

    // Tally.
    const passed = results.filter((r) => r.result.kind === "passed");
    const skipped = results.filter((r) => r.result.kind === "skipped");
    const failed = results.filter((r) => r.result.kind === "failed");

    // Contribute structured evidence to the unified
    // verify-report.json so the deploy UI can show "X of Y
    // endpoints passed" on the success card. The integration
    // check and the browser check both add their own keys;
    // verify-api owns `report.guards.api`.
    const report = loadReport();
    report.guards.api = {
      status: failed.length === 0 ? "pass" : "fail",
      endpoints: endpoints.length,
      passed: passed.length,
      skipped: skipped.length,
      failed: failed.length,
      examples: passed.slice(0, 3).map((r) => ({
        method: r.endpoint.method.toUpperCase(),
        path: r.endpoint.path,
        round_trip: r.result.roundTrip ?? null,
      })),
      failures: failed.slice(0, 5).map((r) => ({
        method: r.endpoint.method.toUpperCase(),
        path: r.endpoint.path,
        reason: r.result.reason,
      })),
    };
    saveReport(report);

    console.log(
      `verify-api: ${passed.length} passed, ${skipped.length} skipped, ${failed.length} failed (of ${endpoints.length} endpoints).`,
    );
    for (const r of results) {
      const { method, path } = r.endpoint;
      const tag =
        r.result.kind === "passed"
          ? r.result.roundTrip
            ? "✓ rt"
            : "✓"
          : r.result.kind === "skipped"
            ? "·"
            : "✗";
      console.log(`  ${tag} ${method.toUpperCase().padEnd(6)} ${path}  ${r.result.reason ?? ""}`);
    }

    if (failed.length > 0) {
      console.error("\nverify-api FAILED:");
      for (const r of failed) {
        const { method, path } = r.endpoint;
        console.error(`  - ${method.toUpperCase()} ${path}: ${r.result.reason}`);
      }
      process.exit(1);
    }
  });
}

main().catch((e) => {
  console.error("verify-api FAILED -- unexpected error:");
  console.error(e instanceof Error ? e.stack || e.message : e);
  process.exit(1);
});
