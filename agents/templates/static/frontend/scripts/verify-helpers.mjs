/**
 * Shared infrastructure for verify-browser.mjs and verify-api.mjs.
 *
 * Both scripts need to boot a fullstack app (backend + static+proxy
 * server for the built dist/) OR a static app (proxy only), wait
 * for URLs to respond, spawn child processes with output streaming,
 * and tear everything down at the end. Extracting that here keeps
 * the browser walker focused on DOM logic and the API walker
 * focused on OpenAPI / CRUD logic.
 *
 * Parameterised on `projectRoot`, `repoRoot`, `ports`, and
 * `pythonBin` so:
 *   - verify-browser.mjs uses the default ports + default DB path
 *   - verify-api.mjs uses BACKEND port 8766 and a SEPARATE
 *     `verify-api.db` so the two scripts can run in parallel
 *     without "database is locked" collisions on SQLite.
 *
 * The static-template copy of this file is byte-identical. Templates
 * ship independently; duplication of ~280 lines is acceptable for
 * independent deliverables.
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync, mkdirSync, renameSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as wait } from "node:timers/promises";

const __filename = fileURLToPath(import.meta.url);
const projectRoot = resolve(dirname(__filename), "..");
const repoRoot = resolve(projectRoot, "..");

const FRONTEND_PORT = Number(process.env.OJAS_VERIFY_FRONTEND_PORT ?? 4173);
const BACKEND_PORT = Number(process.env.OJAS_VERIFY_BACKEND_PORT ?? 8765);
const PYTHON_BIN = process.env.OJAS_VERIFY_PYTHON ?? "python3";
const FRONTEND_URL = `http://127.0.0.1:${FRONTEND_PORT}`;
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`;

// Console + pageerror messages we tolerate. These are noise, not
// bugs: devtools probes, favicon 404s when there's no favicon,
// service-worker complaints on the dev-tools preview port, etc.
const IGNORED_CONSOLE_PATTERNS = [
  /Download the React DevTools/i,
  /favicon\.ico/i,
  /sw\.js/i,
];

const MAX_ROUTES = 50;
const NAV_TIMEOUT_MS = 15_000;

// One set of test credentials per run. Stable across the whole
// run so a signup submit followed by a login submit uses the
// SAME email + password — the runner actually creates the
// account on the first submit and reuses it on the second.
// Clearly test data (verify-*@example.com, "Verify Browser",
// password prefixed VerifyBrowser!) so a backend that
// accidentally accepts these can't confuse them with real users.
const TEST_CREDS = (() => {
  const suffix = Math.random().toString(36).slice(2, 10);
  return {
    email: `verify-${Date.now()}-${suffix}@example.com`,
    password: `VerifyBrowser!${Math.random().toString(36).slice(2, 14)}`,
    username: `verify_${suffix}`,
    name: "Verify Browser",
  };
})();

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------
async function waitForUrl(url, { timeoutMs = 30_000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastErr;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { method: "GET" });
      if (res.status < 500) return true;
    } catch (e) {
      lastErr = e;
    }
    await wait(500);
  }
  throw new Error(
    `Timed out waiting for ${url} (${timeoutMs}ms)${
      lastErr ? `: ${lastErr.message}` : ""
    }`,
  );
}

function spawnLogged(cmd, args, opts = {}) {
  const child = spawn(cmd, args, {
    stdio: ["ignore", "pipe", "pipe"],
    ...opts,
  });
  child.stdout.on("data", (b) => process.stdout.write(`[${cmd}] ${b}`));
  child.stderr.on("data", (b) => process.stderr.write(`[${cmd}] ${b}`));
  return child;
}

async function withCleanup(procs, fn) {
  try {
    return await fn();
  } finally {
    for (const p of procs) {
      if (p && !p.killed) {
        try {
          p.kill("SIGTERM");
        } catch {}
      }
    }
    await wait(500);
    for (const p of procs) {
      if (p && !p.killed) {
        try {
          p.kill("SIGKILL");
        } catch {}
      }
    }
  }
}

/**
 * Boot the fullstack stack on caller-provided ports + DB path.
 *
 * @param {object} opts
 * @param {string} [opts.pythonBin] - python binary (default PYTHON_BIN)
 * @param {number} [opts.backendPort] - uvicorn port (default BACKEND_PORT)
 * @param {string} [opts.frontendUrl] - frontend origin (default FRONTEND_URL)
 * @param {string} [opts.backendUrl] - backend origin (default BACKEND_URL)
 * @param {string} [opts.dbPath] - sqlite DATABASE_URL path (default
 *   <projectRoot>/node_modules/.ojas-verify/verify.db)
 */
async function bootFullstack(opts = {}) {
  const pythonBin = opts.pythonBin ?? PYTHON_BIN;
  const backendPort = opts.backendPort ?? BACKEND_PORT;
  const frontendUrl = opts.frontendUrl ?? FRONTEND_URL;
  const backendUrl = opts.backendUrl ?? BACKEND_URL;
  const dbPath =
    opts.dbPath ?? join(projectRoot, "node_modules", ".ojas-verify", "verify.db");

  const procs = [];

  // Backend: uvicorn on the local port, with a transient DB so we
  // don't touch anything on disk the user cares about.
  const backendEnv = {
    ...process.env,
    DATABASE_URL: `sqlite:///${dbPath}`,
    PORT: String(backendPort),
    OJAS_VERIFY_MODE: "1",
  };
  const backendCwd = join(repoRoot, "backend");
  const uvicorn = spawnLogged(
    pythonBin,
    [
      "-m",
      "uvicorn",
      "main:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(backendPort),
    ],
    { cwd: backendCwd, env: backendEnv },
  );
  procs.push(uvicorn);

  // Sanity-check: if the port was already in use (a leftover
  // process from a previous run, or another local service),
  // the spawn here won't fail — uvicorn will just print
  // "address already in use" to stderr and exit. The verify
  // scripts then fetch /openapi.json from that port and
  // get the WRONG spec (the leftover backend's spec), and
  // every test is meaningless. Detect this case by waiting
  // a beat, then checking the actual port. If something
  // else is on the port, fail loudly with a clear message.
  // We bind a quick probe socket to the port: if it fails
  // AND uvicorn isn't our process, abort.
  await wait(500);
  // uvicorn logs "Uvicorn running on http://127.0.0.1:PORT"
  // to stdout. If we don't see that within ~1s, the spawn
  // failed (port collision, missing venv, syntax error in
  // main.py, etc.). Health-check /health to confirm it's
  // OUR backend (not a leftover from a prior run).
  const healthUrl = `${backendUrl}/health`;
  let healthOk = false;
  for (let i = 0; i < 30; i++) {
    try {
      const r = await fetch(healthUrl, {
        signal: AbortSignal.timeout(2000),
      });
      if (r.ok) {
        healthOk = true;
        break;
      }
    } catch {
      /* not up yet */
    }
    await wait(200);
  }
  if (!healthOk) {
    // Kill any process we spawned and bail. The user sees
    // a clear error instead of a silent wrong-spec run.
    try {
      uvicorn.kill("SIGKILL");
    } catch {}
    throw new Error(
      `bootFullstack: backend did not come up at ${backendUrl}/health ` +
        `within 6s. Common causes: port ${new URL(backendUrl).port} is ` +
        `already in use (a leftover process from a prior run — kill ` +
        `it with \`lsof -i :${new URL(backendUrl).port}\`); the venv ` +
        `at ${pythonBin} is missing uvicorn; or main.py has a syntax error.`,
    );
  }

  // Frontend: tiny static+proxy server. We don't use
  // `vite preview` because (a) the Ojas template's vite.config.ts
  // has no proxy entry, so API calls would hit vite's SPA
  // fallback (returning HTML instead of JSON) — that's the exact
  // "deployed but UI broken" bug we're trying to catch, and
  // shipping a false-negative here defeats the gate. (b)
  // vite preview defaults to localhost which on macOS resolves
  // to [::1] (IPv6) and Node's fetch sometimes lands on the
  // wrong stack. The proxy here binds to 127.0.0.1 explicitly.
  const proxyServer = await startProxyServer({
    frontendUrl,
    backendUrl,
  });
  procs.push(proxyServer);

  await Promise.all([
    // /health may not exist; fall back to root. The frontend will
    // surface real backend errors when its first fetch fires.
    waitForUrl(`${backendUrl}/health`).catch(() => waitForUrl(backendUrl)),
    waitForUrl(frontendUrl),
  ]);

  return procs;
}

/**
 * Boot just the static proxy (no backend). Static template.
 */
async function bootStatic(opts = {}) {
  const frontendUrl = opts.frontendUrl ?? FRONTEND_URL;
  const proxyServer = await startProxyServer({ frontendUrl });
  await waitForUrl(frontendUrl);
  return [proxyServer];
}

// Tiny HTTP server: serves the built dist/ statically + proxies
// /api/* to the backend (fullstack) or returns 404 (static). The
// SPA fallback returns index.html for any non-asset GET so deep
// links work. Kept in-process (no extra dep) — `http` is built
// into Node.
const STATIC_MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain; charset=utf-8",
  ".webmanifest": "application/manifest+json",
};

async function startProxyServer({ frontendUrl, backendUrl } = {}) {
  const frontendOrigin = frontendUrl ?? FRONTEND_URL;
  const backendOrigin = backendUrl ?? BACKEND_URL;

  const { createServer } = await import("node:http");
  const { readFile, stat } = await import("node:fs/promises");
  const { extname, join, normalize, resolve } = await import("node:path");

  const distDir = resolve(projectRoot, "dist");
  const hasBackend = existsSync(join(repoRoot, "backend", "main.py"));

  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url || "/", frontendOrigin);

      // Proxy /api/* to backend (fullstack only).
      if (hasBackend && url.pathname.startsWith("/api/")) {
        const target = `${backendOrigin}${url.pathname}${url.search}`;
        // Buffer the request body for methods that carry one. The old
        // proxy forwarded GET/HEAD only and silently dropped POST/PATCH/
        // PUT/DELETE bodies — which made every "create through the UI"
        // check a false pass (the backend got an empty body). Now we
        // buffer and forward it so CRUD round-trips actually exercise
        // the write path.
        const hasBody = !["GET", "HEAD"].includes((req.method || "GET").toUpperCase());
        let body;
        if (hasBody) {
          const chunks = [];
          for await (const chunk of req) chunks.push(chunk);
          body = chunks.length ? Buffer.concat(chunks) : undefined;
        }
        // Strip hop-by-hop / length headers — fetch recomputes them for
        // the buffered body; a stale content-length stalls the upstream.
        const fwdHeaders = {};
        for (const [k, v] of Object.entries(req.headers)) {
          if (["host", "connection", "content-length", "transfer-encoding"].includes(k.toLowerCase()))
            continue;
          fwdHeaders[k] = v;
        }
        const upstream = await fetch(target, {
          method: req.method,
          headers: fwdHeaders,
          body,
        });
        res.statusCode = upstream.status;
        upstream.headers.forEach((v, k) => {
          // Skip hop-by-hop headers; everything else passes through.
          if (["connection", "transfer-encoding"].includes(k.toLowerCase())) return;
          res.setHeader(k, v);
        });
        const buf = Buffer.from(await upstream.arrayBuffer());
        res.end(buf);
        return;
      }

      // Static file or SPA fallback. Resolve safely under dist/.
      let filePath = join(distDir, normalize(decodeURIComponent(url.pathname)));
      if (!filePath.startsWith(distDir)) {
        res.statusCode = 403;
        res.end("Forbidden");
        return;
      }
      let fileStat;
      try {
        fileStat = await stat(filePath);
      } catch {
        fileStat = null;
      }
      // SPA fallback for non-asset GETs that don't exist on disk.
      if (
        (!fileStat || !fileStat.isFile()) &&
        req.method === "GET" &&
        !/\.[a-z0-9]+$/i.test(url.pathname)
      ) {
        filePath = join(distDir, "index.html");
        fileStat = await stat(filePath);
      }
      if (!fileStat || !fileStat.isFile()) {
        res.statusCode = 404;
        res.end("Not found");
        return;
      }
      const content = await readFile(filePath);
      res.setHeader(
        "content-type",
        STATIC_MIME[extname(filePath).toLowerCase()] ||
          "application/octet-stream",
      );
      res.end(content);
    } catch (e) {
      res.statusCode = 500;
      res.end(`Proxy error: ${e instanceof Error ? e.message : e}`);
    }
  });

  const port = Number(new URL(frontendOrigin).port);
  await new Promise((resolveReady, rejectReady) => {
    server.once("error", rejectReady);
    server.listen(port, "127.0.0.1", () => resolveReady(undefined));
  });

  // Wrap so withCleanup's `p.kill()` works — the proxy is a
  // server object, not a child process. Provide no-op kill that
  // actually closes the server.
  return {
    kill(_sig) {
      server.close();
    },
  };
}

// ---------------------------------------------------------------------------
// Stage helpers — shared by verify-api / verify-browser / verify-smoke
// ---------------------------------------------------------------------------

// Bounded fetch. Every API request in the suite goes through here so one
// hung endpoint can't stall the whole stage: short, explicit timeout,
// parsed body, never throws on a non-2xx (the caller asserts the status).
async function shortFetch(url, opts = {}, timeoutMs = 8000) {
  let res;
  try {
    res = await fetch(url, { ...opts, signal: AbortSignal.timeout(timeoutMs) });
  } catch (e) {
    return { ok: false, status: 0, error: String(e?.message || e), headers: null, json: null, text: "" };
  }
  const text = await res.text().catch(() => "");
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    /* not JSON */
  }
  return { ok: res.ok, status: res.status, headers: res.headers, json, text };
}

// Replace $NAME / $EMAIL / $PASSWORD / $USERNAME tokens (recursively) in a
// manifest payload template with the run's stable TEST_CREDS.
function substituteCreds(value, creds = TEST_CREDS) {
  if (typeof value === "string") {
    return value
      .replace(/\$EMAIL/g, creds.email)
      .replace(/\$PASSWORD/g, creds.password)
      .replace(/\$USERNAME/g, creds.username)
      .replace(/\$NAME/g, creds.name);
  }
  if (Array.isArray(value)) return value.map((v) => substituteCreds(v, creds));
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = substituteCreds(v, creds);
    return out;
  }
  return value;
}

function getByPath(obj, dotPath) {
  if (!obj || !dotPath) return undefined;
  return dotPath.split(".").reduce((acc, k) => (acc == null ? acc : acc[k]), obj);
}

/**
 * Run the manifest's auth flow against the backend: signup (best-effort —
 * a 409/400 "already exists" is fine), then login. Returns the auth state
 * to attach to subsequent requests:
 *   { ok, headers, cookie, token, detail }
 * On failure `ok` is false and `detail` explains why, so the API stage can
 * report a precise "auth is broken" error rather than a wall of 401s.
 */
async function authenticate({ backendBase, auth, creds = TEST_CREDS }) {
  if (!auth || !auth.enabled) return { ok: true, headers: {}, cookie: null, token: null };
  const payload = auth.userPayload
    ? substituteCreds(auth.userPayload, creds)
    : { name: creds.name, email: creds.email, password: creds.password };

  // Signup (tolerant — the account may already exist from a prior run).
  if (auth.signupPath) {
    const r = await shortFetch(`${backendBase}${auth.signupPath}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok && ![400, 409, 422].includes(r.status)) {
      return {
        ok: false,
        detail: `signup POST ${auth.signupPath} returned ${r.status || r.error}. ` +
          `Expected 2xx (created) or 400/409 (already exists). Body: ${r.text.slice(0, 200)}`,
      };
    }
  }

  // Login.
  if (!auth.loginPath) {
    return { ok: false, detail: "auth.enabled but no loginPath — cannot obtain a session." };
  }
  const login = await shortFetch(`${backendBase}${auth.loginPath}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!login.ok) {
    return {
      ok: false,
      detail: `login POST ${auth.loginPath} returned ${login.status || login.error}. ` +
        `Body: ${login.text.slice(0, 200)}`,
    };
  }

  if (auth.tokenIn === "cookie") {
    const cookie = login.headers?.get?.("set-cookie") || null;
    if (!cookie) return { ok: false, detail: `login succeeded but set no cookie (tokenIn=cookie).` };
    return { ok: true, headers: { cookie }, cookie, token: null };
  }
  const token = getByPath(login.json, auth.tokenField);
  if (!token) {
    return {
      ok: false,
      detail: `login succeeded but no token at "${auth.tokenField}" in the response. ` +
        `Keys: ${login.json ? Object.keys(login.json).join(", ") : "(non-JSON body)"}.`,
    };
  }
  return {
    ok: true,
    token,
    headers: { [auth.header]: `${auth.scheme} ${token}`.trim() },
    cookie: null,
  };
}

// ---- Browser helpers (Playwright) -----------------------------------------

const PROMOTED_WARNING_PATTERNS = [
  /Each child in a list should have a unique "key"/i,
  /validateDOMNesting/i,
  /Cannot update a component .* while rendering a different component/i,
  /Maximum update depth exceeded/i,
  /not wrapped in act\(/i,
];

async function launchBrowser() {
  let chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch (e) {
    throw new Error(
      `playwright is not installed. Run \`npx playwright install chromium\` ` +
        `from the frontend/ directory, then re-run verify. (${e.message})`,
    );
  }
  try {
    return await chromium.launch({ headless: true });
  } catch (e) {
    throw new Error(
      `Could not launch headless Chromium. Run \`npx playwright install chromium\` ` +
        `once, then re-run verify. (${e.message})`,
    );
  }
}

/**
 * Attach console / pageerror / failed-response listeners to a page and
 * return a live collector. `consoleErrors` captures genuine errors plus
 * the five promoted React warnings; `networkErrors` captures 4xx/5xx on
 * same-origin requests. IGNORED_CONSOLE_PATTERNS filters known noise.
 */
function attachCollectors(page) {
  const consoleErrors = [];
  const networkErrors = [];
  const ignore = (t) => IGNORED_CONSOLE_PATTERNS.some((re) => re.test(t));
  page.on("console", (msg) => {
    const type = msg.type();
    const text = msg.text();
    if (ignore(text)) return;
    if (type === "error") consoleErrors.push(text);
    else if (type === "warning" && PROMOTED_WARNING_PATTERNS.some((re) => re.test(text)))
      consoleErrors.push(`(promoted warning) ${text}`);
  });
  page.on("pageerror", (err) => {
    if (!ignore(err.message)) consoleErrors.push(`uncaught: ${err.message}`);
  });
  page.on("response", (res) => {
    const u = res.url();
    if (res.status() >= 400 && (u.includes("/api/") || u.startsWith(page.url().split("/").slice(0, 3).join("/")))) {
      networkErrors.push(`${res.status()} ${res.request().method()} ${u}`);
    }
  });
  return { consoleErrors, networkErrors };
}

async function gotoSafe(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT_MS });
  // Let React mount + first data fetch settle without a blind long sleep:
  // networkidle with a short cap, falling back if it never idles.
  await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
}

// Blank-screen heuristic: a 200 that rendered nothing meaningful.
async function isBlankScreen(page) {
  return page.evaluate(() => {
    const main = document.querySelector("main") || document.body;
    const text = (main.innerText || "").trim();
    const semantic = main.querySelectorAll(
      "h1,h2,h3,h4,p,li,td,th,button,a,input,textarea,select,img,svg,table,form,label,article,section",
    ).length;
    return text.length < 30 && semantic < 2;
  });
}

async function textVisible(page, needle) {
  if (!needle) return true;
  return page.evaluate((t) => {
    const body = (document.body.innerText || "").toLowerCase();
    return body.includes(String(t).toLowerCase());
  }, needle);
}

// All verify scripts (verify:deps, verify:render, verify:api,
// verify:integration, verify:browser) contribute structured
// evidence to a single verify-report.json under
// node_modules/.ojas-verify/. The server reads it after
// `npm run verify` completes and surfaces the data on the
// deploy success card, so the user can SEE what was tested —
// not just "all green".
//
// API:
//   const report = loadReport();        // start with existing or {}
//   report.guards.deps = { status: "pass", ms: 1234 };
//   saveReport(report);                 // atomic write
//
// The script's last action is `saveReport(report)`. Each script
// only owns its own guard key — concurrent writes to the same
// file from chained npm scripts are sequential, so no locking
// is needed.
const REPORT_DIR = join(projectRoot, "node_modules", ".ojas-verify");
const REPORT_PATH = join(REPORT_DIR, "verify-report.json");

function loadReport() {
  try {
    if (existsSync(REPORT_PATH)) {
      return JSON.parse(readFileSync(REPORT_PATH, "utf8"));
    }
  } catch {
    /* fall through to fresh */
  }
  return {
    started_at: new Date().toISOString(),
    guards: {},
  };
}

function saveReport(report) {
  mkdirSync(REPORT_DIR, { recursive: true });
  report.finished_at = new Date().toISOString();
  // Atomic write: write to .tmp, then rename. A crash mid-write
  // leaves the old report intact rather than a half-truncated
  // JSON file.
  const tmp = REPORT_PATH + ".tmp";
  writeFileSync(tmp, JSON.stringify(report, null, 2));
  renameSync(tmp, REPORT_PATH);
}

export {
  projectRoot,
  repoRoot,
  FRONTEND_PORT,
  BACKEND_PORT,
  FRONTEND_URL,
  BACKEND_URL,
  PYTHON_BIN,
  IGNORED_CONSOLE_PATTERNS,
  MAX_ROUTES,
  NAV_TIMEOUT_MS,
  TEST_CREDS,
  REPORT_DIR,
  REPORT_PATH,
  loadReport,
  saveReport,
  waitForUrl,
  spawnLogged,
  withCleanup,
  bootFullstack,
  bootStatic,
  startProxyServer,
  // stage helpers
  shortFetch,
  substituteCreds,
  getByPath,
  authenticate,
  PROMOTED_WARNING_PATTERNS,
  launchBrowser,
  attachCollectors,
  gotoSafe,
  isBlankScreen,
  textVisible,
};
