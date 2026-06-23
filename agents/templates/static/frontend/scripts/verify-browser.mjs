/**
 * Stage: Browser.  (all apps)
 *
 * ONE targeted pass per declared screen — no BFS over 200 routes, no
 * click-every-button, no reload-everything. For each screen in the
 * manifest it:
 *   1. navigates (logging in first through the UI if the screen needs auth),
 *   2. asserts the page actually rendered (not a blank <main>),
 *   3. asserts ZERO console errors / promoted React warnings / 4xx-5xx,
 *   4. asserts the declared `expectVisible` text is on the page (the data
 *      the screen is supposed to show is actually shown),
 *   5. runs the screen's ONE primary action (fill+submit a form, or click
 *      a control) and asserts the declared `expectVisibleAfter` outcome.
 *
 * Deterministic and bounded: a screen either renders its feature or it
 * fails with a precise reason. Shares the orchestrator's single browser
 * + one persistent context (so a UI login carries across screens).
 *
 * Exposes runBrowserStage(ctx).
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

function guessAuthRoute(ctx, re) {
  return ctx.manifest.screens.find((s) => re.test(s.route))?.route ?? null;
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
  if (screens.length === 0) {
    ctx.log("no screens declared — skipping browser stage.");
    return { checked: 0 };
  }
  const context = await ctx.browser.newContext({ baseURL: ctx.frontendBase });
  context.setDefaultTimeout(8000);

  // Log in once up front if any screen needs auth.
  const needsAuth = ctx.manifest.auth.enabled && screens.some((s) => s.requiresAuth);
  if (needsAuth) {
    const page = await context.newPage();
    attachCollectors(page);
    await uiLogin(ctx, page);
    await page.close();
  }

  let checked = 0;
  for (const screen of screens) {
    const page = await context.newPage();
    const { consoleErrors, networkErrors } = attachCollectors(page);
    try {
      await gotoSafe(page, `${ctx.frontendBase}${screen.route}`);

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
      await runPrimaryAction(ctx, page, screen);

      if (consoleErrors.length) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}) logged ${consoleErrors.length} console ` +
            `error(s):\n    ${consoleErrors.slice(0, 5).join("\n    ")}`,
        );
      }
      if (networkErrors.length) {
        throw new StageError(
          "browser",
          `screen "${screen.feature}" (${screen.route}) hit ${networkErrors.length} failing ` +
            `request(s):\n    ${networkErrors.slice(0, 5).join("\n    ")}`,
        );
      }
      checked++;
      ctx.log(`screen "${screen.feature}" (${screen.route}) ✓`);
    } finally {
      await page.close();
    }
  }
  await context.close();
  return { checked };
}
