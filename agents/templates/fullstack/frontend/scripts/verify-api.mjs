/**
 * Stage: API.  (fullstack only)
 *
 * Tests EVERY endpoint the backend exposes — the merged manifest is the
 * full OpenAPI surface (see manifest.mergeManifests), so nothing the agent
 * forgot to declare ships untested. For each endpoint:
 *   - attach the real session token if it's protected (from the auth stage),
 *   - resolve {id}-style path params:
 *       · read-only (GET/HEAD): from any existing row (seed data is fine),
 *       · mutating (PUT/PATCH/DELETE): from a row WE created this run, so a
 *         destructive test can never touch real seed/app data,
 *   - send the declared body (or a spec-synthesised one for undeclared eps),
 *   - assert the status (declared status for agent-declared endpoints; any
 *     2xx for auto-derived ones), and the declared response shape.
 *
 * Endpoints that create rows record them in ctx._createdRows so the cleanup
 * stage can delete the test data afterwards, leaving the real DB holding
 * only its proper seed data.
 *
 * Auth endpoints (signup/login/logout) are covered by the auth stage and
 * skipped here — re-hitting them generically would mint junk accounts.
 *
 * Exposes runApiStage(ctx).
 */
import { shortFetch, substituteCreds } from "./verify-helpers.mjs";
import { synthBody } from "./manifest.mjs";
import { StageError } from "./verify-report-util.mjs";

// First /api/<name> (or /<name>) segment of a path → the resource it acts on.
function resourceOf(path) {
  return (
    /^\/api\/([a-z0-9_-]+)/i.exec(path)?.[1] ??
    /^\/([a-z0-9_-]+)/i.exec(path)?.[1] ??
    null
  );
}

// The auth flow's own endpoints — tested by the auth stage, not here.
function isAuthEndpoint(ctx, ep) {
  const a = ctx.manifest.auth;
  if (!a?.enabled) return false;
  return [a.signupPath, a.loginPath, a.logoutPath].filter(Boolean).includes(ep.path);
}

// Pull usable ids from a resource's list endpoint, memoised per run. Read-only
// callers (GET detail) may use these — they include real seed rows.
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

// Find the collection-create (POST /api/<resource>, no path param) endpoint.
function createEndpointFor(ctx, resourceName) {
  return ctx.manifest.endpoints.find(
    (e) => e.method === "POST" && !/\{/.test(e.path) && resourceOf(e.path) === resourceName,
  );
}

// Get an id for a DESTRUCTIVE test that is guaranteed to be a row WE created
// (never real seed data). Creates one on demand via the resource's POST
// endpoint and records it for cleanup. Returns null if it can't make one.
async function testRowId(ctx, resourceName) {
  ctx._createdRows = ctx._createdRows || {};
  const pool = (ctx._createdRows[resourceName] = ctx._createdRows[resourceName] || []);
  if (pool.length) return pool[pool.length - 1];

  const create = createEndpointFor(ctx, resourceName);
  if (!create) return null;
  // Prefer the create endpoint's declared body, then the resource's seed
  // shape, then a generic synth — whatever's most likely to pass validation.
  const seed = ctx.manifest.resources.find((x) => x.name === resourceName)?.seed?.[0];
  const template = create.body ?? seed ?? synthBody({ type: "object", properties: {} });
  const body = substituteCreds(template, ctx.creds);
  const res = await shortFetch(`${ctx.backendBase}${create.path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...(create.auth ? ctx.auth?.headers ?? {} : {}) },
    body: JSON.stringify(body),
  });
  const id = res.json?.id ?? res.json?._id ?? res.json?.uuid;
  if (!res.ok || id == null) return null;
  pool.push(id);
  return id;
}

// Replace {param} tokens. read-only callers pass mutating=false (any id);
// mutating callers pass mutating=true (a test-created id only). Returns
// { url } or { skip } with a reason.
async function resolvePath(ctx, ep) {
  const tokens = [...ep.path.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
  if (tokens.length === 0) return { url: `${ctx.backendBase}${ep.path}` };
  const mutating = !["GET", "HEAD"].includes(ep.method);
  let path = ep.path;
  for (const tok of tokens) {
    const resource = ep.pathParamsFrom || resourceOf(ep.path) || tok.replace(/_?id$/i, "");
    const id = mutating ? await testRowId(ctx, resource) : await idFor(ctx, resource);
    if (id == null) {
      return {
        skip: mutating
          ? `no test row could be created for "${resource}" (no usable POST endpoint) — ` +
            `can't safely exercise ${ep.method} ${ep.path} without risking real data.`
          : `no "${resource}" row available to fill {${tok}} — seed one in the manifest.`,
      };
    }
    path = path.replace(`{${tok}}`, encodeURIComponent(id));
  }
  return { url: `${ctx.backendBase}${path}`, resource: resourceOf(ep.path) };
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
    return { tested: 0, skipped: 0, total: 0 };
  }
  ctx._createdRows = ctx._createdRows || {};
  let tested = 0;
  let skipped = 0;
  for (const ep of endpoints) {
    if (isAuthEndpoint(ctx, ep)) {
      ctx.log(`skip ${ep.method} ${ep.path}: covered by the auth stage.`);
      skipped++;
      continue;
    }

    const { url, skip } = await resolvePath(ctx, ep);
    if (skip) {
      ctx.log(`⚠ skip ${ep.method} ${ep.path}: ${skip}`);
      skipped++;
      continue;
    }
    const headers = { ...(ep.auth ? ctx.auth?.headers ?? {} : {}) };
    const init = { method: ep.method, headers };
    if (ep.body != null && !["GET", "HEAD", "DELETE"].includes(ep.method)) {
      headers["content-type"] = "application/json";
      init.body = JSON.stringify(substituteCreds(ep.body, ctx.creds));
    }
    const finalUrl = ep.query ? `${url}?${new URLSearchParams(ep.query).toString()}` : url;

    const res = await shortFetch(finalUrl, init);

    const ok2xx = res.status >= 200 && res.status < 300;
    // Agent-declared endpoints are strict (use the declared status). Auto-
    // derived ones accept any 2xx, and tolerate a 400/422 (the spec didn't
    // fully describe the body we'd need) as a loud skip rather than a fail.
    const statusOk = !ep.generic && ep.expectStatus != null ? res.status === ep.expectStatus : ok2xx;
    if (!statusOk) {
      if (ep.generic && [400, 422].includes(res.status)) {
        ctx.log(
          `⚠ skip ${ep.method} ${ep.path}: ${res.status} (undeclared endpoint; the synthesised ` +
            `body didn't satisfy validation — declare it in verify.manifest.json to test it).`,
        );
        skipped++;
        continue;
      }
      throw new StageError(
        "api",
        `Feature "${ep.feature}" is broken.\n` +
          `  ${ep.method} ${finalUrl}\n` +
          `  expected ${ep.expectStatus != null && !ep.generic ? `status ${ep.expectStatus}` : "any 2xx"}, ` +
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

    // Track test data for cleanup: a successful create records the new id;
    // a successful delete of a test row drops it from the pool (it's gone).
    const resource = resourceOf(ep.path);
    if (resource) {
      ctx._createdRows[resource] = ctx._createdRows[resource] || [];
      if (ep.method === "POST" && res.json?.id != null) {
        ctx._createdRows[resource].push(res.json.id);
        ctx._idPool = ctx._idPool || {};
        ctx._idPool[resource] = [res.json.id, ...(ctx._idPool[resource] || [])];
      } else if (ep.method === "DELETE") {
        const usedId = decodeURIComponent(url.split("/").pop() || "");
        ctx._createdRows[resource] = ctx._createdRows[resource].filter((x) => String(x) !== usedId);
      }
    }

    tested++;
    ctx.log(`${ep.method} ${ep.path} → ${res.status} ✓ (${ep.feature})`);
  }
  return { tested, skipped, total: endpoints.length };
}
