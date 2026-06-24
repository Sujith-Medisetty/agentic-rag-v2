/**
 * Stage: Cleanup.  (all apps)
 *
 * Verify now runs against the app's REAL database (so the running app ends
 * up properly seeded). The flip side is that the api stage creates transient
 * TEST rows ("test order", "test task", the dummy account) directly in that
 * real DB — and those must not be left behind. This stage removes them,
 * leaving only the legitimate seed data the app needs.
 *
 *   1. Delete every row the api stage created (tracked in ctx._createdRows)
 *      via each resource's DELETE endpoint. Rows already removed by a DELETE
 *      test are gone; this mops up the rest.
 *   2. Delete the dummy test user (if auth + a delete-account endpoint).
 *
 * Anything it can't remove (a resource with no delete route) is reported
 * LOUDLY so the leftover test data in the real DB is never silent.
 *
 * Exposes runCleanupStage(ctx).
 */
import { shortFetch } from "./verify-helpers.mjs";
import { StageError } from "./verify-report-util.mjs";

function listPathFor(ctx, resource) {
  return ctx.manifest.resources.find((r) => r.name === resource)?.listPath ?? `/api/${resource}`;
}

export async function runCleanupStage(ctx) {
  const authHeaders = ctx.auth?.headers ?? {};
  const created = ctx._createdRows || {};

  // 1) Remove the test rows the api stage created in the real DB.
  let deleted = 0;
  const leftover = [];
  for (const [resource, ids] of Object.entries(created)) {
    const listPath = listPathFor(ctx, resource);
    for (const id of ids) {
      const del = await shortFetch(`${ctx.backendBase}${listPath}/${encodeURIComponent(id)}`, {
        method: "DELETE",
        headers: authHeaders,
      });
      if (del.ok || del.status === 404) deleted++;
      else leftover.push(`${listPath}/${id} (DELETE → ${del.status || del.error})`);
    }
  }
  if (deleted) ctx.log(`removed ${deleted} test row(s) created during this run.`);
  if (leftover.length) {
    ctx.log(
      `⚠ could NOT remove ${leftover.length} test row(s) from the real DB — the resource has ` +
        `no working DELETE endpoint. Clean up manually or add a delete route:\n    ` +
        leftover.slice(0, 10).join("\n    "),
    );
  }

  // 2) Remove the dummy test user.
  const c = ctx.manifest.cleanup;
  if (!ctx.manifest.auth.enabled || !c.deleteTestUser) {
    ctx.log("no test user to delete.");
    return;
  }
  if (!c.deleteUserPath) {
    ctx.log(
      "deleteTestUser set but no deleteUserPath — the dummy account stays in the real DB. " +
        "Add cleanup.deleteUserPath (e.g. DELETE /api/auth/me) so verify can remove it.",
    );
    return;
  }
  const del = await shortFetch(`${ctx.backendBase}${c.deleteUserPath}`, {
    method: "DELETE",
    headers: authHeaders,
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
