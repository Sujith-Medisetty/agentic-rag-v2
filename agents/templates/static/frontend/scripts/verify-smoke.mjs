/**
 * Stage: Smoke (end-to-end happy path) + Cleanup.  (all apps)
 *
 * The browser stage proves each screen works in isolation; this stage
 * proves the FULL journey works as ONE continuous session — the thing a
 * real user does: sign up → log in → use the core feature → see the
 * result → log out. It runs the manifest's `happyPath` steps in order in
 * a single persistent context, so state (auth, created data) carries
 * across steps exactly like a real session.
 *
 * Cleanup then removes the dummy test user so nothing test-shaped is left
 * behind. (Verify always runs against a THROWAWAY DB under
 * node_modules/.ojas-verify/ — the real app DB is never touched — but if
 * the app exposes account deletion we exercise it here too, which both
 * cleans up and proves the delete-account feature works.)
 *
 * Exposes runSmokeStage(ctx) and runCleanupStage(ctx).
 */
import { gotoSafe, textVisible, attachCollectors, shortFetch } from "./verify-helpers.mjs";
import { findField, clickByText, uiLogin } from "./verify-browser.mjs";
import { StageError } from "./verify-report-util.mjs";

function authRoute(ctx, re, fallback) {
  return ctx.manifest.screens.find((s) => re.test(s.route))?.route ?? fallback;
}

async function uiSignup(ctx, page) {
  const auth = ctx.manifest.auth;
  const route = auth.signupRoute || authRoute(ctx, /sign-?up|register/i, "/signup");
  await gotoSafe(page, `${ctx.frontendBase}${route}`);
  const name = await findField(page, "name");
  if (name) await name.fill(ctx.creds.name).catch(() => {});
  const email = await findField(page, "email");
  const pass = await findField(page, "password");
  if (!email || !pass) {
    throw new StageError(
      "smoke",
      `happy path "signup": no email+password fields found at "${route}". ` +
        `Set auth.signupRoute in the manifest or use standard input types.`,
    );
  }
  await email.fill(ctx.creds.email);
  await pass.fill(ctx.creds.password);
  await clickByText(page, auth.signupSubmitText || "(sign ?up|register|create account|continue|submit)");
  await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
}

export async function runSmokeStage(ctx) {
  const steps = ctx.manifest.happyPath;
  if (!steps.length) {
    ctx.log("no happyPath declared — per-screen browser checks already cover each screen.");
    return { steps: 0 };
  }
  const context = await ctx.browser.newContext({ baseURL: ctx.frontendBase });
  context.setDefaultTimeout(8000);
  const page = await context.newPage();
  const { consoleErrors } = attachCollectors(page);

  try {
    for (let i = 0; i < steps.length; i++) {
      const s = steps[i];
      const where = `happy-path step ${i + 1} (${s.step})`;
      switch (s.step) {
        case "signup":
          await uiSignup(ctx, page);
          break;
        case "login":
          await uiLogin(ctx, page);
          break;
        case "logout":
          await clickByText(page, s.target || "(log ?out|sign ?out)").catch(() => {
            throw new StageError("smoke", `${where}: no logout control found.`);
          });
          await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
          break;
        case "navigate":
          await gotoSafe(page, `${ctx.frontendBase}${s.route || "/"}`);
          break;
        case "click":
          if (s.route) await gotoSafe(page, `${ctx.frontendBase}${s.route}`);
          await clickByText(page, s.target);
          await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
          break;
        case "fillSubmit": {
          if (s.route) await gotoSafe(page, `${ctx.frontendBase}${s.route}`);
          for (const [k, v] of Object.entries(s.fields || {})) {
            const f = await findField(page, k);
            if (!f) throw new StageError("smoke", `${where}: no field matching "${k}".`);
            await f.fill(String(v));
          }
          await clickByText(page, s.submitText || "(submit|save|add|create|send)");
          await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
          break;
        }
        case "expectVisible":
          if (!(await textVisible(page, s.text))) {
            throw new StageError(
              "smoke",
              `${where}: expected "${s.text}" to be visible but it wasn't. The journey broke here.`,
            );
          }
          break;
        case "expectGone":
          if (await textVisible(page, s.text)) {
            throw new StageError("smoke", `${where}: expected "${s.text}" to be gone but it's still shown.`);
          }
          break;
        default:
          ctx.log(`unknown happy-path step "${s.step}" — ignored.`);
      }
      // expectVisibleAfter is allowed on any interactive step.
      for (const text of s.expectVisibleAfter || []) {
        if (!(await textVisible(page, text))) {
          throw new StageError("smoke", `${where}: expected "${text}" after this step; not found.`);
        }
      }
    }
    if (consoleErrors.length) {
      throw new StageError(
        "smoke",
        `the happy path logged ${consoleErrors.length} console error(s):\n    ` +
          consoleErrors.slice(0, 5).join("\n    "),
      );
    }
  } finally {
    await page.close();
    await context.close();
  }
  return { steps: steps.length };
}

export async function runCleanupStage(ctx) {
  const c = ctx.manifest.cleanup;
  if (!ctx.manifest.auth.enabled || !c.deleteTestUser) {
    ctx.log("nothing to delete — verify ran against a throwaway DB; the real app DB is untouched.");
    return;
  }
  if (!c.deleteUserPath) {
    ctx.log(
      "deleteTestUser set but no deleteUserPath — the dummy user lives only in the throwaway " +
        "verify DB (discarded after this run), so nothing leaks into the real app.",
    );
    return;
  }
  const del = await shortFetch(`${ctx.backendBase}${c.deleteUserPath}`, {
    method: "DELETE",
    headers: ctx.auth?.headers ?? {},
  });
  if (!del.ok && del.status !== 404) {
    throw new StageError(
      "cleanup",
      `DELETE ${c.deleteUserPath} returned ${del.status || del.error} — account deletion ` +
        `is broken (or the path is wrong). Response: ${del.text.slice(0, 200)}`,
    );
  }
  ctx.log(`dummy test user deleted via ${c.deleteUserPath}.`);
}
