/**
 * Tiny shared error + reporter for the staged verifier.
 *
 * StageError carries the stage name + an ACTIONABLE message (what failed,
 * why it matters, and the fix). The orchestrator catches it, prints the
 * message verbatim, records it in verify-report.json, and exits non-zero
 * WITHOUT writing the green sentinel — so the agent fixes the root cause
 * and re-runs rather than thrashing against a flaky check.
 */
import { loadReport, saveReport } from "./verify-helpers.mjs";

export class StageError extends Error {
  constructor(stage, message) {
    super(message);
    this.name = "StageError";
    this.stage = stage;
  }
}

export class Reporter {
  constructor() {
    this.report = loadReport();
    this.report.stages = this.report.stages || {};
  }
  start(stage) {
    this._t = Date.now();
    this._stage = stage;
    process.stdout.write(`\n▶ ${stage}\n`);
  }
  log(msg) {
    process.stdout.write(`  · ${msg}\n`);
  }
  pass(stage, summary = "") {
    this.report.stages[stage] = { status: "pass", ms: Date.now() - (this._t || Date.now()), summary };
    saveReport(this.report);
    process.stdout.write(`  ✓ ${stage} ok${summary ? ` — ${summary}` : ""}\n`);
  }
  fail(stage, message) {
    this.report.stages[stage] = { status: "fail", ms: Date.now() - (this._t || Date.now()), error: message };
    saveReport(this.report);
    process.stdout.write(`  ✗ ${stage} FAILED\n`);
  }
  skip(stage, why) {
    this.report.stages[stage] = { status: "skip", why };
    saveReport(this.report);
    process.stdout.write(`  – ${stage} skipped (${why})\n`);
  }
}
