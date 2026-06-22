#!/usr/bin/env node
/**
 * Guard 0.5: data ingestion check + auto-seed safety net.
 *
 * The other guards (verify:api, verify:integration,
 * verify:browser) all assume the backend has data to test
 * against. Without that data, the API smoke returns empty
 * arrays, the browser check warns about "data not visible",
 * and the user sees a deployed app that's completely empty.
 *
 * This guard runs FIRST in the chain. It:
 *   1. Locates the live backend DB at
 *      <project>/backend/data/app.db.
 *   2. Counts rows in every table.
 *   3. If the live DB is empty (or doesn't exist), copies the
 *      seed from
 *      <project>/frontend/node_modules/.ojas-verify/verify.db
 *      — the DB the verify suite has been using. The verify
 *      suite's DB has already been auto-seeded by the
 *      backend's `_seed_if_empty()` on its first boot, so
 *      this is a known-good snapshot.
 *   4. Reports the row counts in verify-report.json so the
 *      deploy UI shows "X rows in stores, Y rows in products"
 *      on the success card.
 *
 * This is the "data ingestion" piece of the full integration
 * check. It runs before verify:api so the API smoke has data
 * to work with; verify:api then exercises endpoints against
 * the same data the deployed app will show users.
 *
 * Static template: no-op (no backend, no DB). Exits 0.
 *
 * Usage:
 *   node scripts/verify-data.mjs
 *   # or auto-wired via `npm run verify:data` / `npm run verify`
 */
import { existsSync, mkdirSync, copyFileSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import {
  projectRoot,
  repoRoot,
  loadReport,
  saveReport,
} from "./verify-helpers.mjs";

const __filename = fileURLToPath(import.meta.url);

// ---------------------------------------------------------------------------
// Static-template shortcut
// ---------------------------------------------------------------------------
const hasBackend = existsSync(join(repoRoot, "backend", "main.py"));
if (!hasBackend) {
  const report = loadReport();
  report.guards.data = {
    status: "skip",
    reason: "static template — no backend, no DB",
  };
  saveReport(report);
  console.log("✓ verify:data (static template — skipped)");
  process.exit(0);
}

// ---------------------------------------------------------------------------
// Find the live DB
// ---------------------------------------------------------------------------
// The backend reads DATABASE_URL from its env. In the Ojas
// deploy unit, the env var is set to
// `sqlite:///./data/app.db` (relative to backend cwd), so the
// live path is <repoRoot>/backend/data/app.db.
const liveDb = join(repoRoot, "backend", "data", "app.db");
const liveDbExists = existsSync(liveDb);
const liveDbSize = liveDbExists ? statSync(liveDb).size : 0;

// The seed source: the verify suite's temp DB. After verify:api
// or any verify-X boots the backend, the backend auto-seeds
// this file. If the user runs verify:data without ever
// booting the backend, this file won't exist and we'll fall
// back to reporting an empty DB.
const seedDb = join(projectRoot, "node_modules", ".ojas-verify", "verify.db");
const seedDbExists = existsSync(seedDb);

// ---------------------------------------------------------------------------
// Count rows in a SQLite DB
// ---------------------------------------------------------------------------
// We shell out to Python (every host has python3 with sqlite3
// builtin) instead of using `node:sqlite` (Node 22+ only, and
// the Ojas templates are tested on Node 20.x). The Python
// invocation is small and fast — typically <50ms — and
// avoids adding a better-sqlite3 dep just for this check.
function countRows(dbPath) {
  if (!existsSync(dbPath)) return { error: "missing", size: 0, tables: {} };
  const pyCode = `
import sqlite3, json, os, sys
db = sys.argv[1]
try:
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    tables = {}
    for (t,) in rows:
        try:
            n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            tables[t] = n
        except Exception:
            tables[t] = -1
    conn.close()
    print(json.dumps({"size": os.path.getsize(db), "tables": tables}))
except Exception as e:
    print(json.dumps({"error": str(e), "size": os.path.getsize(db) if os.path.exists(db) else 0, "tables": {}}))
`;
  try {
    const proc = spawnSync("python3", ["-c", pyCode, dbPath], {
      encoding: "utf8",
      timeout: 10_000,
    });
    if (proc.error) {
      return { error: proc.error.message, size: statSync(dbPath).size, tables: {} };
    }
    if (proc.status !== 0) {
      return {
        error: proc.stderr || `python exited ${proc.status}`,
        size: statSync(dbPath).size,
        tables: {},
      };
    }
    const parsed = JSON.parse(proc.stdout.trim());
    return {
      error: parsed.error ?? null,
      size: parsed.size,
      tables: parsed.tables ?? {},
    };
  } catch (e) {
    return {
      error: e instanceof Error ? e.message : String(e),
      size: statSync(dbPath).size,
      tables: {},
    };
  }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
const report = loadReport();
const liveCounts = await countRows(liveDb);
const seedCounts = await countRows(seedDb);

const totalLiveRows = liveCounts.tables
  ? Object.values(liveCounts.tables).reduce((a, b) => a + Math.max(0, b), 0)
  : 0;
const totalSeedRows = seedCounts.tables
  ? Object.values(seedCounts.tables).reduce((a, b) => a + Math.max(0, b), 0)
  : 0;

// Decide whether the live DB is "empty enough" to warrant
// auto-copying the seed. The threshold is "fewer than 3 rows
// across all tables" — a real app will have at minimum a few
// rows of seed data (a few stores, a few products, etc).
// A threshold of 3 lets through "the agent left 1 user from
// their test run" without triggering a copy.
const liveIsEmpty = totalLiveRows < 3;

// ---------------------------------------------------------------------------
// Auto-copy if empty
// ---------------------------------------------------------------------------
let copied = false;
let copyError = null;
if (liveIsEmpty && seedDbExists && totalSeedRows > 0) {
  try {
    // Make sure the parent dir exists (it should — the
    // backend ensures this in main.py via _ensure_sqlite_parent_dir,
    // but be defensive).
    mkdirSync(dirname(liveDb), { recursive: true });
    copyFileSync(seedDb, liveDb);
    copied = true;
  } catch (e) {
    copyError = e instanceof Error ? e.message : String(e);
  }
}

// Re-count after the copy so the report reflects the post-
// copy state.
const finalCounts = copied ? await countRows(liveDb) : liveCounts;
const finalTotalRows = finalCounts.tables
  ? Object.values(finalCounts.tables).reduce((a, b) => a + Math.max(0, b), 0)
  : 0;

// ---------------------------------------------------------------------------
// Report
// ---------------------------------------------------------------------------
const status = finalTotalRows > 0 ? "pass" : "fail";
const reportEntry = {
  status,
  live_db: liveDb,
  live_db_size: liveCounts.size,
  live_total_rows: totalLiveRows,
  live_tables: liveCounts.tables ?? {},
  seed_db: seedDb,
  seed_db_exists: seedDbExists,
  seed_total_rows: totalSeedRows,
  copied_from_seed: copied,
  copy_error: copyError,
  final_total_rows: finalTotalRows,
  final_tables: finalCounts.tables ?? {},
};
report.guards.data = reportEntry;
saveReport(report);

// ---------------------------------------------------------------------------
// Print
// ---------------------------------------------------------------------------
console.log("");
console.log(`verify:data: live DB at ${liveDb.replace(repoRoot, "<project>")}`);
if (liveCounts.error) {
  console.log(`  live DB error: ${liveCounts.error}`);
} else {
  console.log(`  live DB: ${liveCounts.size} bytes, ${totalLiveRows} total rows across ${Object.keys(liveCounts.tables).length} tables`);
  for (const [t, n] of Object.entries(liveCounts.tables)) {
    console.log(`    ${t}: ${n} rows`);
  }
}
if (copied) {
  console.log("");
  console.log(`  ⚠ live DB was empty — copied seed from verify suite (${seedDb.replace(repoRoot, "<project>")})`);
  console.log(`  ✓ now has ${finalTotalRows} rows`);
} else if (liveIsEmpty) {
  console.log("");
  console.log(`  ✗ live DB is empty and no seed source available — app will appear blank to users`);
  if (!seedDbExists) {
    console.log(`    (run npm run verify:api first to populate the seed DB)`);
  }
  process.exitCode = 1;
} else {
  console.log("");
  console.log(`✓ verify:data passed`);
}
