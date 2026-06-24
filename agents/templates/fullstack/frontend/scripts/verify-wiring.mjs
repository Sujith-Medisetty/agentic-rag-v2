/**
 * Stage: Wiring.  (fullstack only)
 *
 * A STATIC check — no browser — that the frontend is actually plumbed to the
 * backend: every `/api/...` the UI calls must correspond to a route the
 * backend really exposes. This replaces walking every screen in a browser:
 * instead of clicking "submit" and watching, we read the code and confirm
 * the action is wired to a real endpoint.
 *
 * It catches the classic "button does nothing / 404s" bug: a handler that
 * fetches `/api/todos` when the backend only serves `/api/tasks`, a typo'd
 * path, or a method the route doesn't support — none of which a type-check
 * or build would flag.
 *
 *   FAIL: the frontend calls a path the backend's OpenAPI spec has no route
 *         for (after normalising dynamic `{id}`/`${id}`/`:id`/`123` segments).
 *   WARN: a backend endpoint the frontend never references (often fine —
 *         internal/admin routes — so it's a note, not a failure).
 *
 * Exposes runWiringStage(ctx). ctx.specPaths is the list of raw spec paths.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { projectRoot } from "./verify-helpers.mjs";
import { StageError } from "./verify-report-util.mjs";

// Collapse a path to its shape: every dynamic segment becomes "{}", so
// `/api/items/42`, `/api/items/{id}`, `/api/items/${x}` and `/api/items/:id`
// all compare equal.
function shape(p) {
  return p
    .replace(/\?.*$/, "")
    .replace(/\/+$/, "")
    .split("/")
    .map((seg) => {
      if (!seg) return seg;
      if (/^\{.+\}$/.test(seg)) return "{}"; // {id}
      if (/^:.+/.test(seg)) return "{}"; // :id
      if (/\$\{/.test(seg)) return "{}"; // ${id} (whole or partial)
      if (/^\d+$/.test(seg)) return "{}"; // numeric literal
      if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-/i.test(seg)) return "{}"; // uuid
      return seg;
    })
    .join("/");
}

function collectSrcFiles() {
  const srcDir = join(projectRoot, "src");
  const out = [];
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
        out.push(p);
      }
    }
  };
  walk(srcDir);
  return out;
}

// Every string/template literal that begins with /api/ in the frontend src.
function collectFrontendApiPaths() {
  const paths = new Set();
  for (const f of collectSrcFiles()) {
    let txt = "";
    try {
      txt = readFileSync(f, "utf8");
    } catch {
      continue;
    }
    for (const m of txt.matchAll(/["'`](\/api\/[^"'`\s)]*)/g)) {
      const raw = m[1].replace(/\$\{[^}]*$/, ""); // drop a half-captured ${...
      // Skip globs/route patterns (e.g. the `/api/*` proxy doc) — not real calls.
      if (raw.includes("*")) continue;
      if (raw.length > "/api/".length) paths.add(raw);
    }
  }
  return [...paths];
}

export async function runWiringStage(ctx) {
  const specPaths = ctx.specPaths || [];
  if (specPaths.length === 0) {
    ctx.log("no OpenAPI paths available — skipping wiring check.");
    return { calls: 0, missing: 0, unused: 0 };
  }
  const backendShapes = new Set(specPaths.map(shape));
  const frontendPaths = collectFrontendApiPaths();

  // A frontend path is wired if some backend route equals its shape, or has
  // it as a prefix (the UI references a base path it extends at runtime).
  const isWired = (f) => {
    const fs = shape(f);
    for (const b of backendShapes) {
      if (b === fs || b.startsWith(fs + "/")) return true;
    }
    return false;
  };

  const missing = frontendPaths.filter((f) => !isWired(f));
  if (missing.length) {
    throw new StageError(
      "wiring",
      `the frontend calls ${missing.length} API path(s) the backend does NOT expose — ` +
        `those actions will 404 at runtime:\n` +
        missing.map((m) => `    ${m}`).join("\n") +
        `\n  Fix the path in the frontend, or add the route to the backend. ` +
        `Backend exposes: ${[...backendShapes].slice(0, 20).join(", ")}` +
        (backendShapes.size > 20 ? `, …` : ``),
    );
  }

  // Backend routes the frontend never touches — informational only.
  const referenced = new Set(frontendPaths.map(shape));
  const unused = [...backendShapes].filter(
    (b) => b !== "/health" && ![...referenced].some((r) => b === r || b.startsWith(r + "/")),
  );
  if (unused.length) {
    ctx.log(
      `⚠ ${unused.length} backend endpoint(s) are never called from the frontend ` +
        `(ok if intentional): ${unused.slice(0, 10).join(", ")}${unused.length > 10 ? ", …" : ""}`,
    );
  }
  ctx.log(`${frontendPaths.length} frontend API call(s) all resolve to real backend routes.`);
  return { calls: frontendPaths.length, missing: 0, unused: unused.length };
}
