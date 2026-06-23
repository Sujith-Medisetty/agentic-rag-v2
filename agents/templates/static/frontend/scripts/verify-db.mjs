/**
 * Stage: DB / schema / data.  (fullstack only)
 *
 * Comes right after auth. By the time this runs the backend has booted,
 * so SQLAlchemy `create_all()` / migrations have already created the
 * schema against a throwaway verify DB. This stage proves the schema is
 * USABLE and that the app's data actually LOADS:
 *
 *   1. Seed the deterministic fixtures declared per resource (via the
 *      real create endpoint — so the write path is exercised too).
 *   2. Assert each resource's list endpoint returns a JSON array with at
 *      least `minRows` (and at least the rows we just seeded).
 *
 * A failure here means "the data layer is broken" — the #1 reason a
 * generated app shows an empty screen — and is reported as such before
 * any UI is opened.
 *
 * Exposes runDbStage(ctx); ctx is the shared orchestrator context.
 */
import { shortFetch, substituteCreds } from "./verify-helpers.mjs";
import { StageError } from "./verify-report-util.mjs";

export async function runDbStage(ctx) {
  const { backendBase, manifest, auth } = ctx;
  const authHeaders = auth?.headers ?? {};
  const resources = manifest.resources;

  if (resources.length === 0) {
    ctx.log("no resources declared — schema/data check limited to a boot probe (already green).");
    return;
  }

  for (const r of resources) {
    // 1) Seed declared fixtures through the create endpoint.
    let seeded = 0;
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

    // 2) Assert the list endpoint loads the data.
    const list = await shortFetch(`${backendBase}${r.listPath}`, { headers: authHeaders });
    if (!list.ok) {
      throw new StageError(
        "db",
        `GET ${r.listPath} returned ${list.status || list.error} — the list endpoint ` +
          `for resource "${r.name}" is broken, so its screen will render empty.\n` +
          `  response: ${list.text.slice(0, 200)}`,
      );
    }
    const rows = Array.isArray(list.json)
      ? list.json
      : Array.isArray(list.json?.items)
        ? list.json.items
        : Array.isArray(list.json?.data)
          ? list.json.data
          : null;
    if (rows === null) {
      throw new StageError(
        "db",
        `GET ${r.listPath} did not return a JSON array (got ${typeof list.json}). ` +
          `List endpoints must return an array (or {items|data: [...]}) so the UI can map over it.`,
      );
    }
    const need = Math.max(r.minRows, seeded);
    if (rows.length < need) {
      throw new StageError(
        "db",
        `GET ${r.listPath} returned ${rows.length} row(s) but expected ≥ ${need} ` +
          `(${seeded} seeded${r.minRows ? `, minRows ${r.minRows}` : ""}). The write didn't ` +
          `persist or the read filters them out — data won't load on the screen.`,
      );
    }
    ctx.log(`resource "${r.name}": ${rows.length} row(s) load via ${r.listPath} (seeded ${seeded}).`);
  }
}
