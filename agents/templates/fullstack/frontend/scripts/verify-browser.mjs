#!/usr/bin/env node
/**
 * Guard 4: end-to-end browser smoke test (Playwright).
 *
 * The other guards (check-deps, verify-render, verify-radix) catch
 * compile-time + SSR-render issues. NONE of them execute the app
 * in a real browser, so they cannot catch:
 *   - Routes that 4xx/5xx on first navigation (wrong API path,
 *     CORS, missing service worker, etc.)
 *   - Click handlers that throw or do nothing
 *   - Forms that submit but never update the UI
 *   - Backends that return empty arrays (no seed data)
 *   - Console errors / unhandled promise rejections
 *
 * This guard boots the built app (fullstack: backend + vite
 * preview; static: vite preview only), opens it in headless
 * Chromium via Playwright, and exercises every visible route,
 * button, link, and form. It captures:
 *
 *   1. Console errors + pageerror events.
 *   2. Any network response >= 400 on the app's origin.
 *   3. Any fetch/XHR to /api/* that returns an empty array body
 *     (downgraded to a warning — fresh apps legitimately have
 *     no data on first run, but a deployed app should have
 *     seeded data).
 *   4. Click handlers that throw — caught as console errors
 *     rather than as a separate signal, because the browser
 *     already gives us that surface.
 *   5. Blank-screen-of-death: route returns 200 but the rendered
 *     DOM has no semantic content nodes and almost no text —
 *     typical of an error boundary returning null or a list
 *     with no items and no empty-state copy.
 *   6. Auth forms (signup / login) are detected by the email +
 *     password field combo and filled with TEST_CREDS generated
 *     once per run. The same creds are reused across the whole
 *     run, so signup → login exercises the SAME account.
 *
 * Exits 0 if every route loads cleanly, every interactive
 * element responds without error, no network request 4xxs, every
 * route renders actual content, and any non-empty /api/*
 * response has its data visible in the rendered DOM. Exits 1
 * with a structured failure report otherwise.
 *
 * Pre-requisites the caller (npm run verify:browser) handles:
 *   - Playwright + chromium browser installed
 *     (`npm install` then `npx playwright install chromium`)
 *   - `npm run build` has produced frontend/dist/index.html
 *   - For fullstack apps: backend deps installed
 *
 * Usage:
 *   node scripts/verify-browser.mjs
 *   # or auto-wired via `npm run verify:browser` / `npm run verify`
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
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

const failures = [];
const consoleErrors = [];
const networkFailures = [];
const apiEmptyResponses = [];
const apiVisibilityFailures = [];
const blankScreenFailures = [];
// /api/* responses captured on the CURRENT route, BEFORE the
// destructive walker runs. Both arrays reset before each
// navigation so the "data visible" + "data not empty" checks
// only see what fired during the page's initial load.
let currentRouteApiResponses = [];
let currentRouteApiEmpty = [];
// True once we've run the visibility check for the current
// route. While set, the walker is free to delete items and
// trigger empty /api/* responses without those polluting the
// "missing seed data" warning.
let visibilityCheckedForRoute = false;
const visitedRoutes = new Set();

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

async function bootFullstack() {
  const procs = [];

  // Backend: uvicorn on the local port, with a transient DB so we
  // don't touch anything on disk the user cares about.
  const dbPath = join(projectRoot, "node_modules", ".ojas-verify", "verify.db");
  const backendEnv = {
    ...process.env,
    DATABASE_URL: `sqlite:///${dbPath}`,
    PORT: String(BACKEND_PORT),
    OJAS_VERIFY_MODE: "1",
  };
  const backendCwd = join(repoRoot, "backend");
  const uvicorn = spawnLogged(
    PYTHON_BIN,
    ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)],
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
  const proxyServer = await startProxyServer();
  procs.push(proxyServer);

  await Promise.all([
    // /health may not exist; fall back to root. The frontend will
    // surface real backend errors when its first fetch fires.
    waitForUrl(`${BACKEND_URL}/health`).catch(() => waitForUrl(BACKEND_URL)),
    waitForUrl(FRONTEND_URL),
  ]);

  return procs;
}

async function bootStatic() {
  // No backend; serve dist/ + SPA fallback over the proxy.
  const proxyServer = await startProxyServer();
  await waitForUrl(FRONTEND_URL);
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

async function startProxyServer() {
  const { createServer } = await import("node:http");
  const { readFile, stat } = await import("node:fs/promises");
  const { extname, join, normalize, resolve } = await import("node:path");

  const distDir = resolve(projectRoot, "dist");
  const backendOrigin = BACKEND_URL;
  const hasBackend = existsSync(join(repoRoot, "backend", "main.py"));

  const server = createServer(async (req, res) => {
    try {
      const url = new URL(req.url || "/", FRONTEND_URL);

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

  await new Promise((resolveReady, rejectReady) => {
    server.once("error", rejectReady);
    server.listen(FRONTEND_PORT, "127.0.0.1", () => resolveReady(undefined));
  });

  // Wrap so withCleanup's `p.kill()` works — the proxy is a
  // server object, not a child process. Provide no-op kill that
  // actually closes the server.
  return {
    kill(sig) {
      server.close();
    },
  };
}

// ---------------------------------------------------------------------------
// Recording helpers
// ---------------------------------------------------------------------------
function shouldIgnoreConsole(text) {
  return IGNORED_CONSOLE_PATTERNS.some((re) => re.test(text));
}

function recordConsole(page, text) {
  if (shouldIgnoreConsole(text)) return;
  consoleErrors.push({ route: page.url(), text });
}

function recordNetwork(resp) {
  const url = resp.url();
  const status = resp.status();
  if (status < 400) return;
  // Ignore cross-origin noise (CDN font 404s, etc.).
  if (!url.startsWith(FRONTEND_URL)) return;
  networkFailures.push({ url, status });
}

function recordApiResponse(resp) {
  // Track two things per /api/* response:
  //   1. Empty arrays → warning (likely missing seed data).
  //   2. Non-empty arrays → store the body so the per-route walker
  //      can check whether the items actually appear in the
  //      rendered DOM (an API call that returns data the UI never
  //      displays is just as broken as a 500).
  const url = resp.url();
  if (!url.includes("/api/")) return;
  if (resp.status() >= 400) return;
  const ct = resp.headers()["content-type"] || "";
  if (!ct.includes("application/json")) return;
  resp
    .json()
    .then((body) => {
      if (!Array.isArray(body)) return;
      // After the walker has run its destructive clicks,
      // /api/* responses may legitimately shrink to [] (we just
      // deleted every item). Only count responses that fired
      // BEFORE the visibility check — those reflect the page's
      // actual initial-load state.
      if (visibilityCheckedForRoute) return;
      if (body.length === 0) {
        currentRouteApiEmpty.push({ url });
      } else {
        // Cap stored body at 50 items / 20 KB to keep memory
        // bounded; we only need a few items to confirm
        // visibility in the DOM.
        const capped = body.slice(0, 50);
        let approxSize = 0;
        const trimmed = [];
        for (const item of capped) {
          const s = JSON.stringify(item);
          if (approxSize + s.length > 20_000) break;
          approxSize += s.length;
          trimmed.push(item);
        }
        currentRouteApiResponses.push({ url, items: trimmed });
      }
    })
    .catch(() => {});
}

// ---------------------------------------------------------------------------
// Discovery helpers
// ---------------------------------------------------------------------------
function isInternalHref(href) {
  if (!href) return false;
  if (href.startsWith("#")) return false;
  if (href.startsWith("mailto:") || href.startsWith("tel:")) return false;
  if (href.startsWith("http://") || href.startsWith("https://")) return false;
  return href.startsWith("/");
}

// ---------------------------------------------------------------------------
// Page walker
// ---------------------------------------------------------------------------
async function enumerateElements(page) {
  return page.evaluate(() => {
    const make = (el) => {
      const tag = el.tagName.toLowerCase();
      const text = (el.innerText || el.getAttribute("aria-label") || "")
        .trim()
        .slice(0, 60);
      return {
        tag,
        text,
        type: el.getAttribute("type"),
        name: el.getAttribute("name"),
        href: el.getAttribute("href"),
      };
    };
    return {
      buttons: [...document.querySelectorAll("button")].map(make).filter(
        // Skip icon-only buttons without aria-label — they're
        // likely destructive controls (delete, close) that we
        // don't want to fire blindly. We still observe their
        // effect via console-error capture if they ever fire.
        (b) => !!(b.text || b.name),
      ),
      links: [...document.querySelectorAll("a[href]")].map(make),
      inputs: [...document.querySelectorAll("input, textarea, select")].map(
        make,
      ),
      forms: [...document.querySelectorAll("form")].map(make),
    };
  });
}

async function clickButtons(page, buttons) {
  for (const btn of buttons) {
    if (!btn.text) continue;
    // Use page.getByRole to tolerate re-rendered lists — text
    // selectors that include arbitrary quotes/escapes are
    // brittle. getByRole('button', { name }) does the right thing.
    const handle = page.getByRole("button", { name: btn.text, exact: false }).first();
    try {
      await handle.click({ timeout: 2_000 });
      await wait(300);
      // Close any modal/sheet that opened so the next button click
      // hits the page underneath.
      await page.keyboard.press("Escape").catch(() => {});
      await wait(200);
    } catch {
      // Click intercepted, element detached, modal blocking — not
      // necessarily a bug. Real failures surface as console errors.
    }
  }
}

// Auth-form detection. Two signals: (1) the form's action/name
// hints at auth (signup / login / register / signin / auth), or
// (2) the form has BOTH an email-like and a password-like input.
// The field combo is the stronger signal — it catches auth forms
// even when the action URL is generic (e.g. React Router's
// <Form action="/">). Non-auth forms (settings, contact, search)
// keep using a generic "verify-browser test" filler.
function isAuthForm(formInfo, inputs) {
  const action = (formInfo.action || "").toLowerCase();
  const name = (formInfo.name || "").toLowerCase();
  if (/signup|sign.?up|register|login|sign.?in|auth/.test(action + " " + name)) {
    return true;
  }
  let hasEmailLike = false;
  let hasPasswordLike = false;
  for (const inp of inputs) {
    const probe = `${(inp.type || "").toLowerCase()} ${(inp.name || "").toLowerCase()} ${(inp.id || "").toLowerCase()} ${(inp.placeholder || "").toLowerCase()}`;
    if (/email|e_?mail/.test(probe)) hasEmailLike = true;
    if (/password|\bpwd\b|\bpass\b/.test(probe)) hasPasswordLike = true;
  }
  return hasEmailLike && hasPasswordLike;
}

// Match a single field to a TEST_CREDS value. Returns null for
// non-text inputs (the caller skips them). "Confirm password",
// "verify email", etc. fall through to the same mapping as their
// base field so confirmations match.
function valueForField(inp) {
  const type = (inp.type || "").toLowerCase();
  if (
    type === "checkbox" ||
    type === "radio" ||
    type === "submit" ||
    type === "file" ||
    type === "hidden"
  ) {
    return null;
  }
  const probe = `${type} ${(inp.name || "").toLowerCase()} ${(inp.id || "").toLowerCase()} ${(inp.autocomplete || "").toLowerCase()} ${(inp.placeholder || "").toLowerCase()}`;
  if (type === "email" || /email|e_?mail/.test(probe)) return TEST_CREDS.email;
  if (type === "password" || /password|\bpwd\b|\bpass\b/.test(probe))
    return TEST_CREDS.password;
  if (/username|user.?name|\buser\b|handle|login.?name/.test(probe))
    return TEST_CREDS.username;
  if (/full.?name|display.?name|first.?name|last.?name|\bname\b/.test(probe))
    return TEST_CREDS.name;
  return "verify-browser test";
}

// Fill + submit every form currently mounted in the DOM. Called
// after each button click so forms that appear in modals/dialogs
// also get exercised. Auth-aware: signup/login forms are filled
// with TEST_CREDS (matching email → email, password → password,
// etc.) so a real signup → login round-trip uses the SAME
// credentials. Playwright's context persists cookies and
// localStorage across navigations, so a successful login carries
// forward to protected routes automatically.
async function fillAndSubmitForms(page) {
  const forms = await page.$$("form");
  for (let i = 0; i < forms.length; i++) {
    const formSel = `form >> nth=${i}`;
    try {
      const inputHandles = await page.$$(`${formSel} input, ${formSel} textarea`);
      const inputInfo = await Promise.all(
        inputHandles.map(async (el) => ({
          type: await el.getAttribute("type"),
          name: await el.getAttribute("name"),
          id: await el.getAttribute("id"),
          autocomplete: await el.getAttribute("autocomplete"),
          placeholder: await el.getAttribute("placeholder"),
        })),
      );
      const formInfo = await page.evaluate((sel) => {
        const f = document.querySelector(sel);
        if (!f) return { action: "", name: "" };
        return {
          action: f.getAttribute("action") || "",
          name: f.getAttribute("name") || "",
        };
      }, formSel);
      const isAuth = isAuthForm(formInfo, inputInfo);
      for (let j = 0; j < inputHandles.length; j++) {
        const value = isAuth
          ? valueForField(inputInfo[j])
          : valueForField(inputInfo[j]) === null
            ? null
            : "verify-browser test";
        if (value === null) continue;
        await inputHandles[j].fill(value).catch(() => {});
      }
      await page.evaluate((sel) => {
        const f = document.querySelector(sel);
        if (f && typeof f.requestSubmit === "function") f.requestSubmit();
        else if (f) f.submit();
      }, formSel);
      await wait(500);
    } catch {
      // Same rationale as buttons: click intercepted, modal
      // blocking, element detached. Real failures surface as
      // console errors.
    }
  }
}

function collectInternalLinks(links, baseUrl) {
  const out = [];
  for (const link of links) {
    if (!isInternalHref(link.href)) continue;
    try {
      const target = new URL(link.href, baseUrl);
      const path = target.pathname + target.search;
      if (!visitedRoutes.has(path) && !out.includes(path)) out.push(path);
    } catch {}
  }
  return out;
}

// Catches "white screen of death" cases where the route returns
// 200 but renders effectively empty content: a React error
// boundary that returned null, a list component with no items
// AND no empty-state copy, a routing bug that landed on the
// wrong page, or an unhandled promise rejection in render. HTTP
// 200 alone is not enough — the page must actually render
// meaningful content.
//
// Scope: we inspect the route's content area, NOT the whole
// document. The page chrome (header/nav/footer) is shared across
// every route and is always present — counting it would let a
// blank route sneak past because the nav still renders text and
// links. We look inside <main> when present (the Ojas template's
// App.tsx puts every route inside <main>) and fall back to
// <body> for apps without a <main> wrapper.
//
// Two-signal heuristic:
//   1. Zero high-signal semantic nodes (h1-h6, article, section,
//      form, table, ul, ol, p) inside the route's content area.
//      A real page has at least one of these. <main> itself is
//      NOT in the selector list — it's the wrapper, not content.
//   2. Tiny inner text (< 30 chars after trim) inside the same
//      scope. Nav-only chrome doesn't produce real text inside
//      <main>.
//
// Both signals firing together = blank. Either alone is fine —
// a settings page with only forms (no headings, ~100 chars of
// labels) passes the second but has semanticCount > 0; a page
// that's mostly images + captions passes the first but has
// plenty of text.
async function checkBlankScreen(page) {
  const probe = await page.evaluate(() => {
    // Prefer <main>; fall back to <body>. Use the FIRST <main>
    // — multi-main pages are unusual, and the Ojas template
    // uses exactly one.
    const scope =
      document.querySelector("main") || document.body || document.documentElement;
    const text = (scope?.innerText || "").trim();
    return {
      textLength: text.length,
      textSample: text.slice(0, 160),
      semanticCount: scope.querySelectorAll(
        'h1, h2, h3, h4, h5, h6, article, section, form, table, ul, ol, p',
      ).length,
    };
  });
  if (probe.semanticCount === 0 && probe.textLength < 30) {
    return {
      blank: true,
      reason: `no semantic content nodes inside <main> and only ${probe.textLength} char(s) of text (sample: ${JSON.stringify(probe.textSample)})`,
    };
  }
  return { blank: false };
}

async function walkPage(page) {
  const url = new URL(page.url());
  const route = url.pathname + url.search;
  visitedRoutes.add(route);

  // Blank-screen check: the route returned 200, but does the
  // page actually render content? A page with no semantic
  // nodes and almost no text is broken even if navigation
  // succeeded — typical failure mode is an error boundary
  // returning null, a list component with no items and no
  // empty state, or a routing bug. Run BEFORE the API
  // visibility check so we don't waste time scanning DOM
  // text that doesn't exist.
  const blank = await checkBlankScreen(page);
  if (blank.blank) {
    blankScreenFailures.push({ route, reason: blank.reason });
  }

  // Visibility check: any /api/* response captured during this
  // route's INITIAL load (before we started clicking buttons
  // that may destroy data) should have its string fields visible
  // in the rendered DOM. If the backend returns items but none
  // appear on screen, the UI is silently dropping them — a
  // "data shows up empty" bug, the failure mode the user
  // explicitly called out.
  if (currentRouteApiResponses.length > 0) {
    const pageText = await page.evaluate(() => document.body.innerText || "");
    // Dedupe by URL — keep only the LATEST response per
    // endpoint so an intermediate "after a delete" snapshot
    // doesn't fail the check against the original "full list".
    const latestByUrl = new Map();
    for (const captured of currentRouteApiResponses) {
      latestByUrl.set(captured.url, captured);
    }
    for (const captured of latestByUrl.values()) {
      const { url: apiUrl, items } = captured;
      if (items.length === 0) continue;
      let visibleCount = 0;
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        for (const val of Object.values(item)) {
          if (
            typeof val === "string" &&
            val.length >= 3 &&
            pageText.includes(val)
          ) {
            visibleCount++;
            break;
          }
        }
      }
      if (visibleCount === 0) {
        apiVisibilityFailures.push({
          route,
          url: apiUrl,
          itemCount: items.length,
        });
      } else if (visibleCount < items.length / 2) {
        // Partial visibility — could be pagination or filtering.
        // Warn but don't fail; the user can investigate.
        apiEmptyResponses.push({
          url: `${apiUrl} (only ${visibleCount}/${items.length} items visible on ${route})`,
        });
      }
    }
  }
  // Drain the empty-array captures for THIS route's initial
  // load into the report list. They reflect the page's first
  // paint state, not the post-walker state.
  for (const empty of currentRouteApiEmpty) {
    apiEmptyResponses.push(empty);
  }

  // Clear for the next route's initial load.
  currentRouteApiResponses = [];
  currentRouteApiEmpty = [];
  // Lock out further /api/* recording until the next route
  // navigates. The walker below may destroy data (delete items,
  // empty the list); responses fired during destruction are not
  // a signal of "missing seed data" and must not pollute the
  // empty-array warning.
  visibilityCheckedForRoute = true;

  const elements = await enumerateElements(page);
  // After clicking a button, new forms may appear in the DOM
  // (e.g. a Dialog opens and its form mounts). Re-fill/submit
  // every visible form after each click so we exercise forms
  // that only exist after their trigger was clicked. Auth-aware
  // — see fillAndSubmitForms.
  for (const btn of elements.buttons) {
    if (!btn.text) continue;
    const handle = page.getByRole("button", { name: btn.text, exact: false }).first();
    try {
      await handle.click({ timeout: 2_000 });
      await wait(300);
    } catch {
      // Click intercepted or element detached — real failures
      // surface as console errors.
    }
    await fillAndSubmitForms(page);
    // Close any modal that opened so the next click lands on the
    // page underneath.
    await page.keyboard.press("Escape").catch(() => {});
    await wait(200);
  }

  return collectInternalLinks(elements.links, FRONTEND_URL);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
  const isFullstack = existsSync(join(repoRoot, "backend", "main.py"));
  if (!existsSync(join(projectRoot, "dist", "index.html"))) {
    console.error(
      "verify-browser FAILED: dist/index.html not found. " +
        "Run `npm run build` first.",
    );
    process.exit(1);
  }

  let chromium;
  try {
    ({ chromium } = await import("playwright"));
  } catch {
    console.error(
      "verify-browser FAILED: playwright is not installed. " +
        "Run `npm install` then `npx playwright install chromium`.",
    );
    process.exit(1);
  }

  const procs = isFullstack ? await bootFullstack() : await bootStatic();

  await withCleanup(procs, async () => {
    const browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ baseURL: FRONTEND_URL });
    const page = await context.newPage();

    page.on("console", (msg) => {
      if (msg.type() === "error") recordConsole(page, msg.text());
    });
    page.on("pageerror", (err) =>
      recordConsole(page, `pageerror: ${err.message}`),
    );
    page.on("response", recordNetwork);
    page.on("response", recordApiResponse);

    // BFS over routes starting at "/".
    const queue = ["/"];
    while (queue.length > 0) {
      const route = queue.shift();
      if (visitedRoutes.has(route)) continue;
      // Reset per-route API capture so the visibility check
      // only sees responses fired during THIS route's load.
      currentRouteApiResponses = [];
      try {
        await page.goto(route, { waitUntil: "networkidle", timeout: NAV_TIMEOUT_MS });
      } catch (e) {
        failures.push({ route, kind: "navigation", message: e.message });
        continue;
      }
      const newRoutes = await walkPage(page);
      queue.push(...newRoutes);
      // Cap BFS — guards against runaway link loops in degenerate
      // apps. A normal app has <10 routes.
      if (visitedRoutes.size + queue.length > MAX_ROUTES) break;
    }

    await browser.close();
  });

  // Give the apiEmptyResponses promises a beat to resolve.
  await wait(500);

  // Report.
  const problems = [];
  if (failures.length) {
    for (const f of failures) {
      problems.push(`[${f.kind}] ${f.route}: ${f.message}`);
    }
  }
  if (consoleErrors.length) {
    problems.push(
      `Console errors (${consoleErrors.length}):\n` +
        consoleErrors.map((c) => `  ${c.route} :: ${c.text}`).join("\n"),
    );
  }
  if (networkFailures.length) {
    problems.push(
      `Network failures (${networkFailures.length}):\n` +
        networkFailures.map((n) => `  ${n.status} ${n.url}`).join("\n"),
    );
  }
  if (apiVisibilityFailures.length) {
    problems.push(
      `API data not visible in UI (${apiVisibilityFailures.length}):\n` +
        apiVisibilityFailures
          .map(
            (v) =>
              `  ${v.route}: ${v.url} returned ${v.itemCount} item(s) but ` +
              `none of their text appears in the rendered page. ` +
              `Either the UI isn't reading the response, or the seed ` +
              `data doesn't match what the page renders.`,
          )
          .join("\n"),
    );
  }
  if (blankScreenFailures.length) {
    problems.push(
      `Blank screens — route returned 200 but rendered no content (${blankScreenFailures.length}):\n` +
        blankScreenFailures
          .map(
            (b) =>
              `  ${b.route}: ${b.reason}. ` +
              `Check for an error boundary returning null, a list ` +
              `component with no items and no empty state, or a ` +
              `routing bug landing on the wrong page.`,
          )
          .join("\n"),
    );
  }
  if (problems.length) {
    console.error("verify-browser FAILED:");
    for (const p of problems) console.error("  - " + p);
    if (apiEmptyResponses.length) {
      console.error(
        `\nNote: ${apiEmptyResponses.length} /api/ endpoint(s) returned ` +
          `empty arrays (downgraded to warning — seed your DB before deploy):`,
      );
      for (const e of apiEmptyResponses) console.error("  - " + e.url);
    }
    console.error(`\nVisited ${visitedRoutes.size} route(s).`);
    process.exit(1);
  }

  if (apiEmptyResponses.length) {
    console.log(
      `verify-browser OK with warning: ${apiEmptyResponses.length} ` +
        `endpoint(s) returned empty arrays — seed your DB before deploy.`,
    );
    for (const e of apiEmptyResponses) console.log("  - " + e.url);
  } else {
    console.log(
      `verify-browser OK -- ${visitedRoutes.size} route(s) exercised cleanly.`,
    );
  }
  console.log(
    `verify-browser test creds (used for any auth forms encountered): ` +
      `email=${TEST_CREDS.email} password=${TEST_CREDS.password}`,
  );
}

main().catch((e) => {
  console.error("verify-browser FAILED -- unexpected error:");
  console.error(e instanceof Error ? e.stack || e.message : e);
  process.exit(1);
});