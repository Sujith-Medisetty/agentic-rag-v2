/**
 * Stage: Browser.  (all apps)
 *
 * A per-route headless UNIT pass — NOT a chained end-to-end session (that
 * "integration" pass was removed). For EVERY route (the manifest's screens,
 * or the router's routes when none are declared) it opens the page on its
 * own and asserts:
 *   1. it isn't a BLANK screen (200 but nothing rendered),
 *   2. ZERO console errors / promoted React warnings,
 *   3. ZERO 4xx/5xx on same-origin or /api requests,
 *   4. the declared `expectVisible` text is actually on the page,
 *   5. it actually TALKS TO THE BACKEND — a data screen that fires no /api
 *      request is using mock/hardcoded data or has a broken fetch (the
 *      "renders fine but the network tab is empty" bug),
 *   6. its ONE primary action works AND (for a write) hits the API.
 *
 * If auth is enabled it logs in through the UI once up front, so protected
 * routes load in the shared context. This is where "static screens are fine
 * but backend integration is broken" gets caught.
 *
 * Exposes runBrowserStage(ctx); plus findField/clickByText/uiLogin.
 */
import { attachCollectors, gotoSafe, isBlankScreen, textVisible } from "./verify-helpers.mjs";
import { StageError } from "./verify-report-util.mjs";

// Find an input for a manifest field key, trying label → placeholder →
// name → id → aria-label. Returns a Playwright Locator or null.
export async function findField(page, key) {
  const tries = [
    page.getByLabel(new RegExp(key, "i")),
    page.getByPlaceholder(new RegExp(key, "i")),
    page.locator(`input[name="${key}" i], textarea[name="${key}" i], select[name="${key}" i]`),
    page.locator(`[id="${key}"]`),
    page.locator(`[aria-label="${key}" i]`),
  ];
  for (const loc of tries) {
    if ((await loc.count()) > 0) return loc.first();
  }
  return null;
}

export async function clickByText(page, text) {
  const btn = page.getByRole("button", { name: new RegExp(text, "i") });
  if ((await btn.count()) > 0) return btn.first().click();
  const link = page.getByRole("link", { name: new RegExp(text, "i") });
  if ((await link.count()) > 0) return link.first().click();
  const any = page.locator(`text=${text}`);
  if ((await any.count()) > 0) return any.first().click();
  throw new StageError("browser", `could not find anything to click matching "${text}".`);
}

function guessAuthRoute(ctx, re) {
  return ctx.manifest.screens.find((s) => re.test(s.route))?.route ?? null;
}

// Best-effort UI login: navigate to the login route, fill email+password,
// submit. Establishes the session in the shared context (cookie or the
// app's own localStorage token) so protected screens load afterward.
export async function uiLogin(ctx, page) {
  const auth = ctx.manifest.auth;
  const route = auth.loginRoute || guessAuthRoute(ctx, /login|sign-?in/i) || "/login";
  await gotoSafe(page, `${ctx.frontendBase}${route}`);
  const email = await findField(page, "email");
  const pass = await findField(page, "password");
  if (!email || !pass) {
    throw new StageError(
      "browser",
      `auth is enabled but the login screen at "${route}" has no email+password ` +
        `fields the runner could find. Set auth.loginRoute in the manifest, or ` +
        `make sure the login form uses standard <input type=email/password>.`,
    );
  }
  await email.fill(ctx.creds.email);
  await pass.fill(ctx.creds.password);
  await clickByText(page, auth.loginSubmitText || "(log ?in|sign ?in|continue|submit)");
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
}

// Best-effort UI signup: fill the signup form and submit. Tolerant — the
// account may already exist (the auth stage created it via the API), so a
// failure here is not fatal; the login step is the real assertion.
async function uiSignup(ctx, page) {
  const auth = ctx.manifest.auth;
  const route = auth.signupRoute || guessAuthRoute(ctx, /sign-?up|register/i);
  if (!route) return;
  await gotoSafe(page, `${ctx.frontendBase}${route}`);
  const name = await findField(page, "name");
  if (name) await name.fill(ctx.creds.name).catch(() => {});
  const email = await findField(page, "email");
  const pass = await findField(page, "password");
  if (!email || !pass) return;
  await email.fill(ctx.creds.email);
  await pass.fill(ctx.creds.password);
  await clickByText(page, auth.signupSubmitText || "(sign ?up|register|create account|continue|submit)").catch(
    () => {},
  );
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
}

async function runPrimaryAction(ctx, page, screen) {
  const a = screen.primaryAction;
  if (a.kind === "none") return;
  if (a.kind === "click") {
    if (a.target) await clickByText(page, a.target);
    await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
  } else if (a.kind === "fill-submit") {
    for (const [key, value] of Object.entries(a.fields)) {
      const field = await findField(page, key);
      if (!field) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}): primary action can't find a field ` +
            `matching "${key}". Use the input's label/placeholder/name in the manifest.`,
        );
      }
      await field.fill(String(value));
    }
    await clickByText(page, a.submitText || "(submit|save|add|create|send|post)");
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
  }
  for (const text of a.expectVisibleAfter) {
    if (!(await textVisible(page, text))) {
      throw new StageError(
        "browser",
        `screen "${screen.feature}" (${screen.route}): after the primary action, expected to ` +
          `see "${text}" but it never appeared. The action didn't take effect in the UI ` +
          `(write fired but list didn't refresh, optimistic update missing, or it errored).`,
      );
    }
  }
}

export async function runBrowserStage(ctx) {
  const screens = ctx.manifest.screens;
  if (!ctx.browser || screens.length === 0) {
    ctx.log("no screens to walk — skipping browser stage.");
    return { checked: 0 };
  }
  const isFullstack = ctx.mode === "fullstack";
  const context = await ctx.browser.newContext({ baseURL: ctx.frontendBase });
  context.setDefaultTimeout(8000);

  // ONE persistent page + context for the whole walk. This matters for the
  // session: cookies & localStorage live on the context, but sessionStorage
  // is per-TAB — it would be lost if we opened a fresh page per route. Logging
  // in on this page and then NAVIGATING it (same tab) to each route preserves
  // cookie, localStorage AND sessionStorage tokens. Collectors attach once;
  // we snapshot their lengths per route so each route is judged on its own
  // new console errors / failed requests / api calls.
  const page = await context.newPage();
  const c = attachCollectors(page);

  // Log in through the UI once up front (so protected routes render). The
  // auth stage already proved the API accepts these creds; this proves the
  // UI login wires that session into the app shell — and the session now
  // rides along on this same tab for every route below.
  if (ctx.manifest.auth.enabled) {
    await uiSignup(ctx, page);
    await uiLogin(ctx, page);
  }

  let checked = 0;
  try {
    for (const screen of screens) {
      const base = { console: c.consoleErrors.length, net: c.networkErrors.length, api: c.apiCalls.length };
      await gotoSafe(page, `${ctx.frontendBase}${screen.route}`);
      // Let late data fetches settle before judging "no API call".
      await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});

      if (await isBlankScreen(page)) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}) rendered a BLANK page (200 but no ` +
            `content). Causes: error boundary returned null, a list with no items and no ` +
            `empty state, a routing bug, or a crash on mount.`,
        );
      }
      for (const text of screen.expectVisible) {
        if (!(await textVisible(page, text))) {
          throw new StageError(
            "browser",
            `screen "${screen.feature}" (${screen.route}): expected to see "${text}" but it's ` +
              `not on the page. The data didn't load, or the UI dropped it (API returned it ` +
              `but the component didn't render it).`,
          );
        }
      }

      // Did the screen actually talk to the backend on load? (count this
      // route's NEW api calls, not the login's.)
      const apiOnLoad = c.apiCalls.length - base.api;
      if (isFullstack && apiOnLoad === 0) {
        if (screen.expectVisible.length > 0 || screen.expectsApi) {
          throw new StageError(
            "browser",
            `screen "${screen.feature}" (${screen.route}) rendered but made ZERO calls to /api — ` +
              `it's showing mock/hardcoded data or its fetch never fires. This is the "looks ` +
              `fine but the network tab is empty" bug: wire the component to the real endpoint ` +
              `(useEffect fetch / react-query / loader) instead of static data.`,
          );
        }
        ctx.log(`⚠ screen "${screen.feature}" (${screen.route}) made no /api calls on load (ok if static).`);
      } else if (isFullstack) {
        ctx.log(`screen "${screen.feature}" (${screen.route}): ${apiOnLoad} /api call(s) on load.`);
      }

      // Primary action — and, for a write, prove it pinged the backend.
      const beforeAction = c.apiCalls.length;
      await runPrimaryAction(ctx, page, screen);
      const a = screen.primaryAction;
      if (
        isFullstack &&
        a.kind !== "none" &&
        a.expectVisibleAfter.length > 0 &&
        c.apiCalls.length === beforeAction
      ) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}): the "${a.kind}" action ran but fired NO ` +
            `/api request, even though it's expected to change backend data ` +
            `(expectVisibleAfter is set). The handler isn't calling the API — the change lives ` +
            `only in client state and is lost on reload.`,
        );
      }

      const newConsole = c.consoleErrors.slice(base.console);
      if (newConsole.length) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}) logged ${newConsole.length} console ` +
            `error(s):\n    ${newConsole.slice(0, 5).join("\n    ")}`,
        );
      }
      const newNet = c.networkErrors.slice(base.net);
      if (newNet.length) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}) hit ${newNet.length} failing ` +
            `request(s):\n    ${newNet.slice(0, 5).join("\n    ")}`,
        );
      }
      checked++;
      ctx.log(`screen "${screen.feature}" (${screen.route}) ✓`);
    }
  } finally {
    await page.close();
    await context.close();
  }
  return { checked };
}
