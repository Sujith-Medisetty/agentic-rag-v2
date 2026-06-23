/**
 * Stage: API.  (fullstack only)
 *
 * Tests every endpoint the manifest declares FOR WHAT IT IS DESIGNED TO
 * DO — not by blindly poking synthesized bodies. For each endpoint:
 *   - attach auth if it's protected (the session from the auth stage),
 *   - resolve {id}-style path params from REAL rows (so detail/update/
 *     delete routes hit existing data instead of 404-ing),
 *   - send the declared body,
 *   - assert the declared status (or any 2xx if unspecified),
 *   - assert the declared response shape (required keys present).
 *
 * Every request is bounded (shortFetch's timeout) so one hung handler
 * can't stall the stage. A failure names the feature, the request, and
 * the expected-vs-actual — an actionable "this endpoint is wrong" report.
 *
 * Exposes runApiStage(ctx).
 */
import { shortFetch, substituteCreds } from "./verify-helpers.mjs";
import { StageError } from "./verify-report-util.mjs";

// Pull a usable id from a resource's list endpoint, memoised per run.
async function idFor(ctx, resourceName) {
  ctx._idPool = ctx._idPool || {};
  if (ctx._idPool[resourceName]?.length) return ctx._idPool[resourceName][0];
  const r = ctx.manifest.resources.find((x) => x.name === resourceName);
  const listPath = r?.listPath ?? `/api/${resourceName}`;
  const res = await shortFetch(`${ctx.backendBase}${listPath}`, { headers: ctx.auth?.headers ?? {} });
  const rows = Array.isArray(res.json) ? res.json : res.json?.items ?? res.json?.data ?? [];
  const ids = rows.map((x) => x?.id ?? x?._id ?? x?.uuid).filter((v) => v != null);
  ctx._idPool[resourceName] = ids;
  return ids[0];
}

// Replace {param} tokens with a real id. Returns { url, skip } — skip is
// set with a reason when no id is available (can't fairly test a detail
// route with no data; reported as a skip, not a failure).
async function resolvePath(ctx, ep) {
  const tokens = [...ep.path.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
  if (tokens.length === 0) return { url: `${ctx.backendBase}${ep.path}` };
  let path = ep.path;
  for (const tok of tokens) {
    const resource =
      ep.pathParamsFrom ||
      /\/api\/([a-z0-9_-]+)\//i.exec(ep.path)?.[1] ||
      tok.replace(/_?id$/i, "");
    const id = await idFor(ctx, resource);
    if (id == null) {
      return { skip: `no "${resource}" row available to fill {${tok}} — seed one in the manifest.` };
    }
    path = path.replace(`{${tok}}`, encodeURIComponent(id));
  }
  return { url: `${ctx.backendBase}${path}` };
}

function shapeMissing(json, requiredKeys) {
  if (requiredKeys.length === 0) return null;
  const obj = Array.isArray(json) ? json[0] : json;
  if (!obj || typeof obj !== "object") return `response is not an object/array-of-objects`;
  const missing = requiredKeys.filter((k) => !(k in obj));
  return missing.length ? `missing key(s): ${missing.join(", ")}` : null;
}

export async function runApiStage(ctx) {
  const endpoints = ctx.manifest.endpoints;
  if (endpoints.length === 0) {
    ctx.log("no endpoints declared — skipping API stage.");
    return { tested: 0, skipped: 0 };
  }
  let tested = 0;
  let skipped = 0;
  for (const ep of endpoints) {
    const { url, skip } = await resolvePath(ctx, ep);
    if (skip) {
      ctx.log(`skip ${ep.method} ${ep.path}: ${skip}`);
      skipped++;
      continue;
    }
    const headers = { ...(ep.auth ? ctx.auth?.headers ?? {} : {}) };
    const init = { method: ep.method, headers };
    if (ep.body != null && !["GET", "HEAD", "DELETE"].includes(ep.method)) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(substituteCreds(ep.body, ctx.creds));
    }
    const finalUrl = ep.query
      ? `${url}?${new URLSearchParams(ep.query).toString()}`
      : url;

    const res = await shortFetch(finalUrl, init);

    const ok2xx = res.status >= 200 && res.status < 300;
    const statusOk = ep.expectStatus != null ? res.status === ep.expectStatus : ok2xx;
    if (!statusOk) {
      throw new StageError(
        "api",
        `Feature "${ep.feature}" is broken.\n` +
          `  ${ep.method} ${finalUrl}\n` +
          `  expected ${ep.expectStatus != null ? `status ${ep.expectStatus}` : "any 2xx"}, ` +
          `got ${res.status || res.error}.\n` +
          `  response: ${res.text.slice(0, 300)}\n` +
          (ep.auth && (res.status === 401 || res.status === 403)
            ? `  (endpoint is marked auth:true — confirm the session token is accepted.)`
            : ``),
      );
    }
    const shapeErr = shapeMissing(res.json, ep.expectShape);
    if (shapeErr) {
      throw new StageError(
        "api",
        `Feature "${ep.feature}" returned the wrong shape.\n` +
          `  ${ep.method} ${finalUrl} → ${res.status}\n` +
          `  ${shapeErr}\n` +
          `  expected keys: ${ep.expectShape.join(", ")}\n` +
          `  response: ${res.text.slice(0, 300)}`,
      );
    }
    // Cache ids from create responses so later detail routes can use them.
    if (ep.method === "POST" && res.json?.id != null) {
      const resource = /\/api\/([a-z0-9_-]+)/i.exec(ep.path)?.[1];
      if (resource) {
        ctx._idPool = ctx._idPool || {};
        ctx._idPool[resource] = [res.json.id, ...(ctx._idPool[resource] || [])];
      }
    }
    tested++;
    ctx.log(`${ep.method} ${ep.path} → ${res.status} ✓ (${ep.feature})`);
  }
  return { tested, skipped };
}
