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
        const upstream = await fetch(target, {
          method: req.method,
          headers: req.headers,
          // fetch() requires ReadableStream; for simple bodies
          // we let it use GET/HEAD default. Other methods with
          // bodies would need explicit buffering — none of the
          // Ojas verify suite uses them.
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
// Verify report
// ---------------------------------------------------------------------------
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
};
