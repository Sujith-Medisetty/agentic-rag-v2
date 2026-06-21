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
 *   - Console errors / unhandled promise rejections
 *   - Routes that load on first nav but break on F5
 *   - React key warnings, validateDOMNesting, memory leaks
 *
 * This guard boots the built static app, opens it in headless
 * Chromium via Playwright, and exercises every visible route,
 * button, link, and form. It captures:
 *
 *   1. Console errors + pageerror events.
 *   2. Any network response >= 400 on the app's origin.
 *   3. Click handlers that throw — caught as console errors
 *     rather than as a separate signal, because the browser
 *     already gives us that surface.
 *   4. Blank-screen-of-death: route returns 200 but the rendered
 *     DOM has no semantic content nodes and almost no text —
 *     typical of an error boundary returning null or a list
 *     with no items and no empty-state copy.
 *   5. Auth forms (signup / login) are detected by the email +
 *     password field combo and filled with TEST_CREDS generated
 *     once per run. The same creds are reused across the whole
 *     run, so signup → login exercises the SAME account.
 *   6. Refresh survival — after the click+form pass on each
 *     route, page.reload() re-checks blank-screen against the
 *     reloaded DOM.
 *   7. Console warnings — captured in addition to errors; five
 *     specific React / DOM patterns promote to failures
 *     (missing keys, validateDOMNesting, setState during render,
 *      memory leaks, act() wrapper).
 *   8. CRUD button classification — buttons matching add/create/
 *     save/update/edit/delete are tagged; list-count snapshots
 *     before vs after the click+form pass are reported as
 *     evidence (informational, not pass/fail).
 *
 * NOTE — no dynamic-route enumeration for the static template:
 * the static template has no backend, so /openapi.json isn't
 * available. The dynamic-route block in exerciseDynamicRoutes()
 * is gated on `isFullstack`, which is false here. The browser
 * walker itself is otherwise identical to the fullstack copy.
 *
 * Exits 0 if every route loads cleanly, every interactive
 * element responds without error, no network request 4xxs, every
 * route renders actual content, and every route survives refresh.
 * Exits 1 with a structured failure report otherwise.
 *
 * Pre-requisites the caller (npm run verify:browser) handles:
 *   - Playwright + chromium browser installed
 *     (`npm install` then `npx playwright install chromium`)
 *   - `npm run build` has produced frontend/dist/index.html
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
  IGNORED_CONSOLE_PATTERNS,
  MAX_ROUTES,
  NAV_TIMEOUT_MS,
  TEST_CREDS,
  bootStatic,
  withCleanup,
} from "./verify-helpers.mjs";

const failures = [];
const consoleErrors = [];
const consoleWarnings = [];
const networkFailures = [];
const blankScreenFailures = [];
const refreshFailures = [];
const crudEvidence = [];
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
  // and re-check blank-screen. A page that worked once but
  // breaks on F5 (state held in memory, missing hydration,
  // etc.) is a real deployed-app failure mode. Try/catch so a
  // hung reload doesn't fail the whole run — it gets recorded
  // as a refreshFailure instead.
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

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
async function main() {
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

  const procs = await bootStatic();

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

    // BFS over routes starting at "/".
    const queue = ["/"];
    while (queue.length > 0) {
      const route = queue.shift();
      if (visitedRoutes.has(route)) continue;
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
  if (problems.length) {
    console.error("verify-browser FAILED:");
    for (const p of problems) console.error("  - " + p);
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
    console.error(`\nVisited ${visitedRoutes.size} route(s).`);
    process.exit(1);
  }

  console.log(
    `verify-browser OK -- ${visitedRoutes.size} route(s) exercised cleanly.`,
  );
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
}

main().catch((e) => {
  console.error("verify-browser FAILED -- unexpected error:");
  console.error(e instanceof Error ? e.stack || e.message : e);
  process.exit(1);
});
