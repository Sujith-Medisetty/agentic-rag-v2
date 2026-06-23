#!/usr/bin/env node
/**
 * Staged verifier — the single done-bar for an Ojas app.
 *
 *   npm run verify  →  node scripts/verify.mjs
 *
 * Runs the stages IN ORDER and STOPS at the first one that fails, with a
 * precise, actionable message (what broke, why it matters, the fix). On a
 * full pass it writes the green sentinel `<app>/.ojas/verify-pass.json`;
 * the agent's done-gate refuses to finish until that sentinel exists and
 * is newer than every source file.
 *
 *   0. preflight   build (runs check-deps + verify-radix via prebuild) + render smoke
 *   1. auth        signup → login obtains a real session            (fullstack, if auth)
 *   2. db          schema usable, declared data seeds + loads        (fullstack)
 *   3. api         every declared endpoint does its job              (fullstack)
 *   4. browser     each screen renders, no console errors, data
 *                  visible, primary action works                     (all)
 *   5. smoke       end-to-end happy path as one session              (all)
 *   6. cleanup     delete the dummy test user                        (all)
 *
 * The whole run uses a THROWAWAY SQLite DB under node_modules/.ojas-verify/
 * — the real app DB is never touched. Servers + browser boot ONCE and are
 * shared across stages (the old suite booted a backend per script).
 */
import { spawn } from "node:child_process";
import { existsSync, rmSync, mkdirSync, writeFileSync, renameSync } from "node:fs";
import { join } from "node:path";

import {
  projectRoot,
  repoRoot,
  FRONTEND_URL,
  BACKEND_URL,
  TEST_CREDS,
  bootFullstack,
  bootStatic,
  withCleanup,
  launchBrowser,
  authenticate,
  shortFetch,
} from "./verify-helpers.mjs";
import { loadManifest, deriveManifest } from "./manifest.mjs";
import { Reporter, StageError } from "./verify-report-util.mjs";
import { runDbStage } from "./verify-db.mjs";
import { runApiStage } from "./verify-api.mjs";
import { runBrowserStage } from "./verify-browser.mjs";
import { runSmokeStage, runCleanupStage } from "./verify-smoke.mjs";

const SENTINEL_DIR = join(repoRoot, ".ojas");
const SENTINEL_PATH = join(SENTINEL_DIR, "verify-pass.json");
const VERIFY_DB = join(projectRoot, "node_modules", ".ojas-verify", "verify.db");
const isFullstack = existsSync(join(repoRoot, "backend", "main.py"));

function runChild(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { stdio: "inherit", ...opts });
    child.on("error", reject);
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new StageError(opts.stage || "preflight", `${cmd} ${args.join(" ")} exited ${code}`))));
  });
}

function writeSentinel(summary) {
  mkdirSync(SENTINEL_DIR, { recursive: true });
  const tmp = SENTINEL_PATH + ".tmp";
  writeFileSync(
    tmp,
    JSON.stringify({ passedAt: new Date().toISOString(), mode: isFullstack ? "fullstack" : "static", summary }, null, 2),
  );
  renameSync(tmp, SENTINEL_PATH);
}

async function main() {
  const reporter = new Reporter();
  // A stale sentinel must never outlive a failing run: drop it now, write
  // it back only if every stage passes.
  try {
    rmSync(SENTINEL_PATH, { force: true });
  } catch {}

  // ---- 0. preflight (child processes; build must precede serving dist) ----
  reporter.start("preflight");
  await runChild("npm", ["run", "build"], { cwd: projectRoot, stage: "preflight" });
  await runChild("node", ["scripts/verify-render.mjs"], { cwd: projectRoot, stage: "preflight" });
  reporter.pass("preflight", "build + render");

  // Fresh throwaway DB so seed counts + the dummy user are deterministic.
  try {
    rmSync(VERIFY_DB, { force: true });
  } catch {}

  const procs = isFullstack ? await bootFullstack() : await bootStatic();

  // Shared context for every stage — booted servers, creds, the (lazy)
  // browser. Declared out here so the cleanup finally can always reap it.
  const ctx = {
    mode: isFullstack ? "fullstack" : "static",
    backendBase: BACKEND_URL,
    frontendBase: FRONTEND_URL,
    creds: TEST_CREDS,
    browser: null,
    auth: { enabled: false, headers: {} },
    report: reporter.report,
    log: (m) => reporter.log(m),
  };

  const summary = {};
  await withCleanup(procs, async () => {
    try {
      await runStages(ctx, summary, reporter);
    } finally {
      // Always reap headless Chromium even if a stage threw (withCleanup
      // only knows about the server procs).
      if (ctx.browser) await ctx.browser.close().catch(() => {});
    }
  });

  writeSentinel(summary);
  process.stdout.write(`\n✅ verify GREEN — wrote ${SENTINEL_PATH}\n`);
}

async function runStages(ctx, summary, reporter) {
  // Manifest: explicit if present, else derived (bounded + loud warning).
  let manifest = loadManifest();
  if (!manifest) {
    let openapi = null;
    if (isFullstack) {
      const spec = await shortFetch(`${BACKEND_URL}/openapi.json`);
      openapi = spec.json;
    }
    manifest = deriveManifest({ openapi });
    reporter.log("⚠ " + manifest.warnings.join("\n    ⚠ "));
  } else {
    reporter.log(`manifest: ${manifest.endpoints.length} endpoint(s), ${manifest.screens.length} screen(s).`);
  }
  ctx.manifest = manifest;

  // ---- 1. auth ----
  if (isFullstack && manifest.auth.enabled) {
    reporter.start("auth");
    const state = await authenticate({ backendBase: BACKEND_URL, auth: manifest.auth, creds: TEST_CREDS });
    if (!state.ok) {
      throw new StageError(
        "auth",
        `signup/login did not work — protected features can't be reached.\n  ${state.detail}`,
      );
    }
    ctx.auth = state;
    reporter.pass("auth", "signup + login established a session");
  }

  // ---- 2. db ----
  if (isFullstack) {
    reporter.start("db");
    await runDbStage(ctx);
    reporter.pass("db", `${manifest.resources.length} resource(s)`);
  }

  // ---- 3. api ----
  if (isFullstack) {
    reporter.start("api");
    const r = await runApiStage(ctx);
    summary.api = r;
    reporter.pass("api", `${r.tested} endpoint(s) tested, ${r.skipped} skipped`);
  }

  // ---- 4. browser + 5. smoke (need a browser) ----
  const needBrowser = manifest.screens.length > 0 || manifest.happyPath.length > 0;
  if (needBrowser) ctx.browser = await launchBrowser();

  reporter.start("browser");
  const b = await runBrowserStage(ctx);
  summary.browser = b;
  reporter.pass("browser", `${b.checked} screen(s)`);

  reporter.start("smoke");
  const sm = await runSmokeStage(ctx);
  summary.smoke = sm;
  reporter.pass("smoke", `${sm.steps} step(s)`);

  // ---- 6. cleanup ----
  reporter.start("cleanup");
  await runCleanupStage(ctx);
  reporter.pass("cleanup");
}

main().catch((err) => {
  const stage = err instanceof StageError ? err.stage : "verify";
  process.stderr.write(`\n❌ verify FAILED at stage: ${stage}\n\n${err.message}\n\n`);
  process.stderr.write(
    `Fix the ROOT CAUSE above (the check is right — the app is wrong), then re-run \`npm run verify\`.\n` +
      `Nothing ships until verify is green.\n`,
  );
  process.exit(1);
});
