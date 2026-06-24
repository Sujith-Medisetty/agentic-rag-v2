/**
 * Stage: DB / schema / data.  (fullstack only)
 *
 * Runs right after auth, against the app's REAL database (not a throwaway).
 * Its job is the #1 reason a generated app shows an empty screen: the data
 * was never loaded. This stage loads it, idempotently, so the running app
 * actually has content — and proves the data layer round-trips:
 *
 *   1. For each declared resource, GET its list. If it's already populated
 *      (≥ the rows we need), leave it alone — re-running verify must NOT
 *      pile up duplicate seed rows in the real DB.
 *   2. If it's empty, seed the declared fixtures through the REAL create
 *      endpoint (so the write path is exercised AND the app is populated).
 *   3. Assert the list endpoint now returns a JSON array with ≥ the needed
 *      rows, so the screen that maps over it won't render blank.
 *
 * Seeded rows are APP DATA — they persist after verify (that's the point).
 * Only the transient rows the api stage creates are torn down in cleanup.
 *
 * Exposes runDbStage(ctx); ctx is the shared orchestrator context.
 */
import { shortFetch, substituteCreds } from "./verify-helpers.mjs";
import { StageError } from "./verify-report-util.mjs";

// Pull the row array out of a list response in any of the common shapes.
function rowsOf(json) {
  if (Array.isArray(json)) return json;
  if (Array.isArray(json?.items)) return json.items;
  if (Array.isArray(json?.data)) return json.data;
  return null;
}

export async function runDbStage(ctx) {
  const { backendBase, manifest, auth } = ctx;
  const authHeaders = auth?.headers ?? {};
  const resources = manifest.resources;

  if (resources.length === 0) {
    ctx.log("no resources declared — schema/data check limited to a boot probe (already green).");
    return;
  }

  for (const r of resources) {
    const need = Math.max(r.minRows, r.seed.length);

    // 1) How much data does the real DB already hold?
    const before = await shortFetch(`${backendBase}${r.listPath}`, { headers: authHeaders });
    if (!before.ok) {
      throw new StageError(
        "db",
        `GET ${r.listPath} returned ${before.status || before.error} — the list endpoint ` +
          `for resource "${r.name}" is broken, so its screen will render empty.\n` +
          `  response: ${before.text.slice(0, 200)}`,
      );
    }
    const existing = rowsOf(before.json);
    if (existing === null) {
      throw new StageError(
        "db",
        `GET ${r.listPath} did not return a JSON array (got ${typeof before.json}). ` +
          `List endpoints must return an array (or {items|data: [...]}) so the UI can map over it.`,
      );
    }

    // 2) Seed only when empty — keeps the real DB idempotent across re-runs.
    let seeded = 0;
    if (existing.length === 0 && r.seed.length > 0) {
      for (const row of r.seed) {
        const body = substituteCreds(row, ctx.creds);
        const res = await shortFetch(`${backendBase}${r.listPath}`, {
          method: "POST",
          headers: { "content-type": "application/json", ...authHeaders },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          throw new StageError(
            "db",
            `Seeding "${r.name}" failed: POST ${r.listPath} → ${res.status || res.error}.\n` +
              `  body sent: ${JSON.stringify(body).slice(0, 200)}\n` +
              `  response:  ${res.text.slice(0, 200)}\n` +
              `Fix: make the create endpoint accept this shape, or correct the ` +
              `seed in verify.manifest.json so it matches the model.`,
          );
        }
        seeded++;
      }
    } else if (existing.length > 0) {
      ctx.log(`resource "${r.name}": ${existing.length} row(s) already in the DB — not re-seeding.`);
    }

    // 3) Assert the data actually loads.
    const after = seeded > 0
      ? await shortFetch(`${backendBase}${r.listPath}`, { headers: authHeaders })
      : before;
    const rows = rowsOf(after.json) ?? [];
    if (rows.length < need) {
      throw new StageError(
        "db",
        `GET ${r.listPath} returned ${rows.length} row(s) but expected ≥ ${need} ` +
          `(${seeded} seeded${r.minRows ? `, minRows ${r.minRows}` : ""}). The write didn't ` +
          `persist or the read filters them out — data won't load on the screen.\n` +
          (r.seed.length === 0
            ? `  This resource declares no "seed" in verify.manifest.json, so the real app ` +
              `ships with an EMPTY ${r.name} list. Add a seed so the app has data on first load.`
            : ``),
      );
    }
    ctx.log(`resource "${r.name}": ${rows.length} row(s) load via ${r.listPath} (seeded ${seeded}).`);
  }
}
