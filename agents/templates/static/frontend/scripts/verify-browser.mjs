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
 *   - Routes that load on first nav but break on F5
 *   - React key warnings, validateDOMNesting, memory leaks
 *   - Dynamic routes the UI doesn't expose as anchors
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
 *   7. Refresh survival — after the click+form pass on each
 *     route, page.reload() re-checks blank-screen and API
 *     visibility against the reloaded DOM.
 *   8. Console warnings — captured in addition to errors; five
 *     specific React / DOM patterns promote to failures
 *     (missing keys, validateDOMNesting, setState during render,
 *      memory leaks, act() wrapper).
 *   9. Dynamic routes discovered from /openapi.json — patterns
 *     like /api/{name}/{idParam} are walked, the corresponding
 *     list endpoint is queried, the first 3 IDs are visited via
 *     Playwright (so the UI rendering is exercised, not just the
 *     API).
 *  10. CRUD button classification — buttons matching add/create/
 *     save/update/edit/delete are tagged; list-count snapshots
 *     before vs after the click+form pass are reported as
 *     evidence (informational, not pass/fail).
 *
 * Exits 0 if every route loads cleanly, every interactive
 * element responds without error, no network request 4xxs, every
 * route renders actual content, every route survives refresh,
 * any non-empty /api/* response has its data visible in the
 * rendered DOM, and every dynamic detail route loads cleanly.
 * Exits 1 with a structured failure report otherwise.
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
import { existsSync } from "node:fs";
import { join } from "node:path";
import { setTimeout as wait } from "node:timers/promises";

import {
  projectRoot,
  repoRoot,
  FRONTEND_URL,
  BACKEND_URL,
  IGNORED_CONSOLE_PATTERNS,
  MAX_ROUTES,
  NAV_TIMEOUT_MS,
  TEST_CREDS,
  bootFullstack,
  bootStatic,
  withCleanup,
  loadReport,
  saveReport,
} from "./verify-helpers.mjs";

const failures = [];
const consoleErrors = [];
const consoleWarnings = [];
const networkFailures = [];
const apiEmptyResponses = [];
const apiVisibilityFailures = [];
const blankScreenFailures = [];
const refreshFailures = [];
const crudEvidence = [];
const dynamicRouteErrors = [];
const dynamicRoutesVisited = new Set();
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

// Console WARNING patterns that promote to FAILURES. These are
// not transient devtools noise — each one indicates a real
// React/DOM bug the agent should fix in the app, not silence in
// the test. The promote happens before the IGNORED_CONSOLE_PATTERNS
// filter is consulted, so even "favicon"-shaped noise can't
// swallow a missing-key warning.
const PROMOTED_WARNING_PATTERNS = [
  { re: /each child in a list should have a unique "key" prop/i, tag: "react-missing-key" },
  { re: /validateDOMNesting|validate dom nesting/i, tag: "validate-dom-nesting" },
  { re: /cannot update a component while rendering a different component/i, tag: "setstate-during-render" },
  { re: /memory leak|can't perform a react state update on an unmounted component/i, tag: "react-memory-leak" },
  { re: /not wrapped in act\(/i, tag: "react-act-wrapper" },
];

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

function recordConsoleWarning(page, text) {
  if (shouldIgnoreConsole(text)) return;
  // Promote specific React/DOM warning patterns to errors.
  for (const { re, tag } of PROMOTED_WARNING_PATTERNS) {
    if (re.test(text)) {
      consoleErrors.push({
        route: page.url(),
        text: `[${tag}] ${text}`,
      });
      return;
    }
  }
  consoleWarnings.push({ route: page.url(), text });
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

// Aria-aware CRUD button classification. `positive` matches any
// button that looks like it mutates data; `destructive` is a
// subset that fires without confirmation (delete/remove/etc.).
// The list-count snapshot in `walkPage` uses these to attribute
// before/after deltas to CRUD intent.
const CRUD_KEYWORDS = /\b(add|create|new|save|update|edit|delete|remove|submit)\b/i;
const DESTRUCTIVE_KEYWORDS = /\b(delete|remove|destroy|purge|wipe|drop|kill|clear|reset)\b/i;

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

// Best-effort count of list rows currently rendered. Heuristic:
// count repeated structural patterns that look like a list —
// <li> inside <ul>/<ol>, <tr> inside <tbody>, articles under
// <main>. The intent is "did the row count change after clicks",
// not "did we find every possible list component". Returns -1
// when no list-like container is found so the caller can skip
// the snapshot rather than reporting 0/0 as "no CRUD fired".
async function countListItems(page) {
  return page.evaluate(() => {
    const main = document.querySelector("main") || document.body;
    if (!main) return -1;
    const counts = [];
    const ul = main.querySelectorAll("ul > li").length;
    const ol = main.querySelectorAll("ol > li").length;
    const tr = main.querySelectorAll("tbody > tr").length;
    const art = main.querySelectorAll("article").length;
    const cards = main.querySelectorAll(
      '[data-testid*="item"], [data-testid*="row"], [class*="Card"]',
    ).length;
    for (const c of [ul, ol, tr, art, cards]) if (c > 0) counts.push(c);
    if (counts.length === 0) return -1;
    // Use the maximum count — list components usually render ONE
    // of these patterns. Reporting the max avoids 0/0 false
    // negatives when an app uses <li> for nav and <Card> for
    // items.
    return Math.max(...counts);
  });
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

// Returns the URL paths of dynamic detail endpoints in the spec,
// e.g. [{ name: "items", detailPath: "/api/items/{item_id}" }].
// A path is "detail-like" if (a) it starts with /api/, (b) it
// has exactly one path parameter, and (c) the corresponding
// list endpoint (/api/{name}) is also documented. The single-id
// requirement excludes bulk routes like /api/items/bulk-delete
// that take an `op` parameter, not an entity id.
async function collectDetailPaths(spec) {
  if (!spec?.paths) return [];
  const out = [];
  for (const [path, methods] of Object.entries(spec.paths)) {
    if (!path.startsWith("/api/")) continue;
    const params = [...path.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
    if (params.length !== 1) continue;
    // Skip paths where the param is a non-id (filter / op / type).
    if (!/id$/i.test(params[0])) continue;
    // Derive list endpoint: strip the last segment.
    const listPath = path.replace(/\/\{[^}]+\}$/, "");
    if (!spec.paths[listPath]) continue;
    // The list endpoint should support GET.
    const listMethods = spec.paths[listPath] || {};
    if (!listMethods.get) continue;
    out.push({ name: listPath.replace(/^\/api\//, ""), detailPath: path, idParam: params[0] });
  }
  return out;
}

// Fetch a list endpoint and return up to `n` ids. Walks the
// response for `id`, `Id`, `_id`, `uuid`, or any `*_id`-suffixed
// field. Empty / non-JSON responses return [].
async function fetchIdsFromList(url, n) {
  try {
    const res = await fetch(url, { headers: { accept: "application/json" } });
    if (res.status >= 400) return [];
    const ct = res.headers.get("content-type") || "";
    if (!ct.includes("application/json")) return [];
    const body = await res.json();
    const arr = Array.isArray(body)
      ? body
      : Array.isArray(body?.items)
        ? body.items
        : Array.isArray(body?.results)
          ? body.results
          : [];
    const ids = [];
    for (const item of arr) {
      if (!item || typeof item !== "object") continue;
      const idField = Object.keys(item).find((k) => /(^|_)(id|uuid)$/i.test(k));
      if (idField) ids.push(item[idField]);
      if (ids.length >= n) break;
    }
    return ids;
  } catch {
    return [];
  }
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

  // Snapshot list-count BEFORE the click+form pass so we can
  // attribute before/after deltas to CRUD intent. Skipped on
  // detail pages (no list pattern visible).
  const beforeCount = await countListItems(page);

  const elements = await enumerateElements(page);
  // After clicking a button, new forms may appear in the DOM
  // (e.g. a Dialog opens and its form mounts). Re-fill/submit
  // every visible form after each click so we exercise forms
  // that only exist after their trigger was clicked. Auth-aware
  // — see fillAndSubmitForms.
  const crudButtonsFired = new Set();
  for (const btn of elements.buttons) {
    if (!btn.text) continue;
    const isCrud = CRUD_KEYWORDS.test(btn.text);
    const isDestructive = DESTRUCTIVE_KEYWORDS.test(btn.text);
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
    if (isCrud && !isDestructive) crudButtonsFired.add(btn.text);
  }

  // Snapshot AFTER + report delta as evidence. We only emit
  // evidence when (a) we fired at least one CRUD button AND
  // (b) the count was observable on this route (≠ -1).
  if (crudButtonsFired.size > 0) {
    const afterCount = await countListItems(page);
    if (beforeCount >= 0 && afterCount >= 0 && beforeCount !== afterCount) {
      crudEvidence.push({
        route,
        buttons: [...crudButtonsFired],
        beforeCount,
        afterCount,
      });
    }
  }

  // B1: Refresh survival. After the destructive pass, reload
  // and re-check blank-screen + API visibility. A page that
  // worked once but breaks on F5 (state held in memory,
  // missing hydration, etc.) is a real deployed-app failure
  // mode. Try/catch so a hung reload doesn't fail the whole
  // run — it gets recorded as a refreshFailure instead.
  try {
    await page.reload({ waitUntil: "networkidle", timeout: NAV_TIMEOUT_MS });
  } catch (e) {
    refreshFailures.push({ route, kind: "reload-timeout", message: e.message });
    return collectInternalLinks(elements.links, FRONTEND_URL);
  }
  const blankAfter = await checkBlankScreen(page);
  if (blankAfter.blank) {
    refreshFailures.push({
      route,
      kind: "blank-after-reload",
      reason: blankAfter.reason,
    });
  }

  return collectInternalLinks(elements.links, FRONTEND_URL);
}

// B3: Dynamic route enumeration via OpenAPI. After BFS finishes,
// walk spec.paths for /api/{name}/{idParam} patterns, fetch the
// first 3 IDs from the corresponding list endpoint, and navigate
// Playwright to each detail URL so the UI rendering is exercised.
// Blank or errored detail pages feed into the existing failure
// arrays.
async function exerciseDynamicRoutes(page, isFullstack) {
  if (!isFullstack) return;
  let spec;
  try {
    const res = await fetch(`${BACKEND_URL}/openapi.json`);
    if (!res.ok) return;
    spec = await res.json();
  } catch {
    return;
  }
  const detailPaths = await collectDetailPaths(spec);
  if (detailPaths.length === 0) return;

  const DYNAMIC_ID_COUNT = 3;
  for (const { name, detailPath, idParam } of detailPaths) {
    const listUrl = `${BACKEND_URL}/api/${name}`;
    const ids = await fetchIdsFromList(listUrl, DYNAMIC_ID_COUNT);
    if (ids.length === 0) continue;
    for (const id of ids) {
      // Build the SPA URL: strip the /api prefix (the frontend
      // typically mounts detail routes under /<name>/<id>).
      const uiDetailPath = `/${name}/${encodeURIComponent(id)}`;
      const route = uiDetailPath;
      if (visitedRoutes.has(route)) {
        dynamicRoutesVisited.add(route);
        continue;
      }
      dynamicRoutesVisited.add(route);
      visibilityCheckedForRoute = false;
      currentRouteApiResponses = [];
      currentRouteApiEmpty = [];
      try {
        await page.goto(route, {
          waitUntil: "networkidle",
          timeout: NAV_TIMEOUT_MS,
        });
      } catch (e) {
        dynamicRouteErrors.push({
          route,
          kind: "navigation",
          message: e.message,
        });
        continue;
      }
      const blank = await checkBlankScreen(page);
      if (blank.blank) {
        dynamicRouteErrors.push({
          route,
          kind: "blank",
          reason: blank.reason,
        });
      }
      // Cap dynamic-route visits at the same MAX_ROUTES limit
      // to avoid runaway loops in degenerate specs.
      if (dynamicRoutesVisited.size + visitedRoutes.size > MAX_ROUTES) return;
    }
  }
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
      const type = msg.type();
      if (type === "error") recordConsole(page, msg.text());
      else if (type === "warning") recordConsoleWarning(page, msg.text());
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

    // B3: dynamic route enumeration via OpenAPI (fullstack only).
    await exerciseDynamicRoutes(page, isFullstack);

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
  if (refreshFailures.length) {
    problems.push(
      `Refresh failures — route loaded on first navigation but broke on F5 (${refreshFailures.length}):\n` +
        refreshFailures
          .map(
            (r) =>
              `  ${r.route}: [${r.kind}] ${r.message ?? r.reason}. ` +
              `Likely state held in memory, missing hydration, or ` +
              `an effect that doesn't re-run after mount.`,
          )
          .join("\n"),
    );
  }
  if (dynamicRouteErrors.length) {
    problems.push(
      `Dynamic detail routes — first 3 IDs per /api/{name}/{id} pattern failed (${dynamicRouteErrors.length}):\n` +
        dynamicRouteErrors
          .map(
            (d) =>
              `  ${d.route}: [${d.kind}] ${d.message ?? d.reason}. ` +
              `The route renders for some IDs but not others — ` +
              `check for missing loaders or 404 fallbacks.`,
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
    if (consoleWarnings.length) {
      console.error(
        `\nNote: ${consoleWarnings.length} console warning(s) (informational):`,
      );
      for (const w of consoleWarnings) console.error(`  - ${w.route} :: ${w.text}`);
    }
    if (crudEvidence.length) {
      console.error(
        `\nNote: ${crudEvidence.length} CRUD snapshot(s) — UI clicks changed list counts:`,
      );
      for (const e of crudEvidence) {
        console.error(
          `  - ${e.route}: buttons=[${e.buttons.join(", ")}] count ${e.beforeCount} → ${e.afterCount}`,
        );
      }
    }
    console.error(
      `\nVisited ${visitedRoutes.size} static route(s) + ${dynamicRoutesVisited.size} dynamic route(s).`,
    );
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
  if (dynamicRoutesVisited.size > 0) {
    console.log(
      `verify-browser dynamic detail routes: ${dynamicRoutesVisited.size} (first 3 IDs per /api/{name}/{id} pattern).`,
    );
  }
  if (consoleWarnings.length) {
    console.log(
      `\nConsole warnings (${consoleWarnings.length}, informational):`,
    );
    for (const w of consoleWarnings) console.log(`  - ${w.route} :: ${w.text}`);
  }
  if (crudEvidence.length) {
    console.log(
      `\nCRUD evidence — UI clicks that changed list counts (${crudEvidence.length}):`,
    );
    for (const e of crudEvidence) {
      console.log(
        `  - ${e.route}: buttons=[${e.buttons.join(", ")}] count ${e.beforeCount} → ${e.afterCount}`,
      );
    }
  }
  console.log(
    `verify-browser test creds (used for any auth forms encountered): ` +
      `email=${TEST_CREDS.email} password=${TEST_CREDS.password}`,
  );

  // Contribute structured evidence to the unified
  // verify-report.json so the deploy UI can show "X routes
  // walked, Y console errors, Z dynamic detail pages" on
  // the success card. verify-browser owns
  // `report.guards.browser`.
  const report = loadReport();
  report.guards.browser = {
    status: problems.length === 0 ? "pass" : "fail",
    routes_walked: visitedRoutes.size,
    dynamic_routes_walked: dynamicRoutesVisited.size,
    console_errors: consoleErrors.length,
    console_warnings: consoleWarnings.length,
    network_failures: networkFailures.length,
    blank_routes: blankScreenFailures.length,
    refresh_failures: refreshFailures.length,
    api_visibility_failures: apiVisibilityFailures.length,
    empty_api_responses: apiEmptyResponses.length,
    crud_evidence: crudEvidence.length,
    test_creds: {
      email: TEST_CREDS.email,
      password: TEST_CREDS.password,
    },
    sample_console_errors: consoleErrors.slice(0, 3),
    sample_blank_routes: blankScreenFailures.slice(0, 3),
  };
  saveReport(report);
}

main().catch((e) => {
  console.error("verify-browser FAILED -- unexpected error:");
  console.error(e instanceof Error ? e.stack || e.message : e);
  process.exit(1);
});
