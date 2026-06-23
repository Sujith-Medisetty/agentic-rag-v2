/**
 * verify.manifest.json — the contract that tells the verifier what the
 * app is SUPPOSED to do, so every stage tests the real feature instead
 * of poking blindly.
 *
 * The agent writes `<app>/verify.manifest.json` during the build. Every
 * field is optional; this module fills sane defaults and, when the file
 * is missing entirely, DERIVES a minimal manifest from the OpenAPI spec
 * + the App.tsx route table so the app still gets a bounded smoke pass
 * (with a loud "coverage reduced" warning) rather than zero verification.
 *
 * Shape (all optional):
 * {
 *   "name": "Task tracker",
 *   "auth": {
 *     "enabled": true,
 *     "signupPath": "/api/auth/register",
 *     "loginPath":  "/api/auth/login",
 *     "logoutPath": "/api/auth/logout",        // optional
 *     "userPayload": { "name": "$NAME", "email": "$EMAIL", "password": "$PASSWORD" },
 *     "tokenField": "access_token",            // dot-path into the login JSON
 *     "tokenIn": "header",                      // "header" | "cookie"
 *     "header": "Authorization",
 *     "scheme": "Bearer"
 *   },
 *   "resources": [
 *     { "name": "tasks", "table": "tasks", "listPath": "/api/tasks",
 *       "minRows": 1, "seed": [ { "title": "Buy milk", "done": false } ] }
 *   ],
 *   "endpoints": [
 *     { "feature": "create task", "method": "POST", "path": "/api/tasks",
 *       "auth": true, "body": { "title": "Verify task" },
 *       "expectStatus": 201, "expectShape": ["id", "title"] }
 *   ],
 *   "screens": [
 *     { "feature": "task list", "route": "/", "requiresAuth": false,
 *       "expectVisible": ["Buy milk"],
 *       "primaryAction": {
 *         "kind": "fill-submit",                // "click" | "fill-submit" | "none"
 *         "fields": { "title": "E2E task" },     // matched by label/placeholder/name
 *         "submitText": "Add",
 *         "expectVisibleAfter": ["E2E task"]
 *       } }
 *   ],
 *   "happyPath": [
 *     { "step": "signup" }, { "step": "login" },
 *     { "step": "navigate", "route": "/" },
 *     { "step": "screenAction", "route": "/" },
 *     { "step": "expectVisible", "route": "/", "text": "E2E task" }
 *   ],
 *   "cleanup": { "deleteTestUser": true, "deleteUserPath": "/api/auth/me" }
 * }
 *
 * The static-template copy of this file is byte-identical.
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { repoRoot, projectRoot } from "./verify-helpers.mjs";

const MANIFEST_PATH = join(repoRoot, "verify.manifest.json");

// Bounds so a derived manifest can never explode into a 200-route walk.
const MAX_DERIVED_ENDPOINTS = 40;
const MAX_DERIVED_SCREENS = 40;

// ---------------------------------------------------------------------------
// Defaults / coercion
// ---------------------------------------------------------------------------
function coerceAuth(auth) {
  if (!auth || auth.enabled === false) return { enabled: false };
  return {
    enabled: true,
    signupPath: auth.signupPath ?? null,
    loginPath: auth.loginPath ?? null,
    logoutPath: auth.logoutPath ?? null,
    userPayload: auth.userPayload ?? null,
    tokenField: auth.tokenField ?? "access_token",
    tokenIn: auth.tokenIn === "cookie" ? "cookie" : "header",
    header: auth.header ?? "Authorization",
    scheme: auth.scheme ?? "Bearer",
  };
}

function coerceManifest(raw) {
  const m = raw && typeof raw === "object" ? raw : {};
  return {
    name: typeof m.name === "string" ? m.name : null,
    derived: Boolean(m.derived),
    warnings: Array.isArray(m.warnings) ? m.warnings : [],
    auth: coerceAuth(m.auth),
    resources: Array.isArray(m.resources)
      ? m.resources
          .filter((r) => r && typeof r.name === "string")
          .map((r) => ({
            name: r.name,
            table: r.table ?? r.name,
            listPath: r.listPath ?? `/api/${r.name}`,
            minRows: Number.isFinite(r.minRows) ? r.minRows : 0,
            seed: Array.isArray(r.seed) ? r.seed : [],
          }))
      : [],
    endpoints: Array.isArray(m.endpoints)
      ? m.endpoints
          .filter((e) => e && typeof e.path === "string")
          .map((e) => ({
            feature: e.feature ?? `${(e.method || "GET").toUpperCase()} ${e.path}`,
            method: (e.method || "GET").toUpperCase(),
            path: e.path,
            auth: Boolean(e.auth),
            body: e.body ?? null,
            query: e.query ?? null,
            expectStatus: e.expectStatus ?? null, // null => "any 2xx"
            expectShape: Array.isArray(e.expectShape) ? e.expectShape : [],
            pathParamsFrom: e.pathParamsFrom ?? null, // resource name to pull a real id from
          }))
      : [],
    screens: Array.isArray(m.screens)
      ? m.screens
          .filter((s) => s && typeof s.route === "string")
          .map((s) => ({
            feature: s.feature ?? s.route,
            route: s.route,
            requiresAuth: Boolean(s.requiresAuth),
            expectVisible: Array.isArray(s.expectVisible) ? s.expectVisible : [],
            primaryAction: coercePrimaryAction(s.primaryAction),
          }))
      : [],
    happyPath: Array.isArray(m.happyPath) ? m.happyPath : [],
    cleanup: {
      deleteTestUser: m.cleanup ? Boolean(m.cleanup.deleteTestUser) : false,
      deleteUserPath: m.cleanup?.deleteUserPath ?? null,
    },
  };
}

function coercePrimaryAction(a) {
  if (!a || a.kind === "none") return { kind: "none" };
  if (a.kind === "click") {
    return {
      kind: "click",
      target: a.target ?? a.submitText ?? null, // button text/selector to click
      expectVisibleAfter: Array.isArray(a.expectVisibleAfter) ? a.expectVisibleAfter : [],
    };
  }
  return {
    kind: "fill-submit",
    fields: a.fields && typeof a.fields === "object" ? a.fields : {},
    submitText: a.submitText ?? null,
    expectVisibleAfter: Array.isArray(a.expectVisibleAfter) ? a.expectVisibleAfter : [],
  };
}

// ---------------------------------------------------------------------------
// Load (explicit) — returns coerced manifest or null if no file on disk
// ---------------------------------------------------------------------------
function loadManifest() {
  if (!existsSync(MANIFEST_PATH)) return null;
  let raw;
  try {
    raw = JSON.parse(readFileSync(MANIFEST_PATH, "utf8"));
  } catch (e) {
    throw new Error(
      `verify.manifest.json is not valid JSON (${e.message}). Fix the syntax — ` +
        `it is the contract the verifier tests against.`,
    );
  }
  return coerceManifest(raw);
}

// ---------------------------------------------------------------------------
// Derive (fallback) — minimal, bounded, loudly flagged
// ---------------------------------------------------------------------------
const DEFAULT_STATUS = { POST: 201, PUT: 200, PATCH: 200, DELETE: 204, GET: 200 };

function deriveAuthFromSpec(spec) {
  const schemes = spec?.components?.securitySchemes;
  if (!schemes || Object.keys(schemes).length === 0) return { enabled: false };
  const paths = Object.keys(spec.paths || {});
  const find = (re) => paths.find((p) => re.test(p)) ?? null;
  return coerceAuth({
    enabled: true,
    signupPath: find(/(register|signup|sign-up|users\/?$)/i),
    loginPath: find(/(login|signin|sign-in|token|auth)/i),
    userPayload: { name: "$NAME", email: "$EMAIL", password: "$PASSWORD" },
  });
}

// Tiny, depth-capped body synth from an OpenAPI/JSON schema. Conservative:
// fills required-ish primitives so a POST doesn't 422 on a missing field.
function synthBody(schema, depth = 0) {
  if (!schema || depth > 4) return {};
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  const t = schema.type;
  if (schema.enum?.length) return schema.enum[0];
  if (t === "string") return schema.format === "email" ? "verify@example.com" : "verify";
  if (t === "integer" || t === "number") return 1;
  if (t === "boolean") return false;
  if (t === "array") return [];
  if (t === "object" || schema.properties) {
    const out = {};
    const props = schema.properties || {};
    for (const [k, v] of Object.entries(props)) out[k] = synthBody(v, depth + 1);
    return out;
  }
  return {};
}

function deriveEndpointsFromSpec(spec) {
  const out = [];
  const paths = spec.paths || {};
  for (const [path, ops] of Object.entries(paths)) {
    if (/^\/(health|docs|redoc|openapi\.json)/.test(path)) continue;
    for (const [method, op] of Object.entries(ops)) {
      const M = method.toUpperCase();
      if (!["GET", "POST", "PUT", "PATCH", "DELETE"].includes(M)) continue;
      if (out.length >= MAX_DERIVED_ENDPOINTS) break;
      const reqSchema =
        op?.requestBody?.content?.["application/json"]?.schema ?? null;
      const okStatus = Object.keys(op?.responses || {}).find((c) => /^2/.test(c));
      out.push({
        feature: op?.summary || `${M} ${path}`,
        method: M,
        path,
        auth: Array.isArray(op?.security) ? op.security.length > 0 : Boolean(spec.security?.length),
        body: reqSchema ? synthBody(reqSchema) : null,
        query: null,
        expectStatus: okStatus ? Number(okStatus) : DEFAULT_STATUS[M] ?? null,
        expectShape: [],
        // GET-with-{id} endpoints pull a real id from the matching list resource.
        pathParamsFrom: /\/api\/([a-z0-9_-]+)\//i.exec(path)?.[1] ?? null,
      });
    }
  }
  return out;
}

function deriveResourcesFromSpec(spec) {
  const out = [];
  for (const path of Object.keys(spec.paths || {})) {
    const m = /^\/api\/([a-z0-9_-]+)\/?$/i.exec(path);
    if (m && spec.paths[path].get) {
      out.push({ name: m[1], table: m[1], listPath: path, minRows: 0, seed: [] });
    }
  }
  return out;
}

// Parse <Route path="..."> out of the router source. Best-effort regex —
// only used when the agent didn't declare screens explicitly.
function deriveScreensFromSource() {
  const srcDir = join(projectRoot, "src");
  const routes = new Set();
  const files = [];
  const walk = (dir) => {
    let entries = [];
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = join(dir, e.name);
      if (e.isDirectory()) {
        if (["node_modules", "dist", "ui"].includes(e.name)) continue;
        walk(p);
      } else if (/\.(t|j)sx?$/.test(e.name)) {
        files.push(p);
      }
    }
  };
  walk(srcDir);
  for (const f of files) {
    let txt = "";
    try {
      txt = readFileSync(f, "utf8");
    } catch {
      continue;
    }
    for (const m of txt.matchAll(/<Route\s+[^>]*\bpath\s*=\s*["'`]([^"'`]+)["'`]/g)) {
      const r = m[1];
      if (r && !r.includes(":") && !r.includes("*")) routes.add(r.startsWith("/") ? r : `/${r}`);
    }
  }
  if (routes.size === 0) routes.add("/");
  return [...routes].slice(0, MAX_DERIVED_SCREENS).map((route) => ({
    feature: route,
    route,
    requiresAuth: false,
    expectVisible: [],
    primaryAction: { kind: "none" },
  }));
}

/**
 * Build a minimal manifest from the running backend's spec (may be null
 * for static apps) and the App.tsx route table. Always flagged derived
 * with a coverage warning the orchestrator surfaces to the user.
 */
function deriveManifest({ openapi = null } = {}) {
  const warnings = [
    "No verify.manifest.json found — running a DERIVED minimal smoke pass. " +
      "Coverage is reduced: endpoints are poked generically (no per-feature " +
      "expectations) and screens are only checked for render + console errors. " +
      "Write verify.manifest.json to test what each feature is actually for.",
  ];
  const auth = openapi ? deriveAuthFromSpec(openapi) : { enabled: false };
  const endpoints = openapi ? deriveEndpointsFromSpec(openapi) : [];
  const resources = openapi ? deriveResourcesFromSpec(openapi) : [];
  const screens = deriveScreensFromSource();
  return coerceManifest({
    derived: true,
    warnings,
    auth,
    resources,
    endpoints,
    screens,
    happyPath: [],
    cleanup: { deleteTestUser: false },
  });
}

export {
  MANIFEST_PATH,
  loadManifest,
  deriveManifest,
  coerceManifest,
  synthBody,
};
