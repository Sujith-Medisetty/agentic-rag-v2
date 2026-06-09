#!/usr/bin/env node
/**
 * Guard 1: dependency layout.
 *
 * Catches the bug that ships blank React pages to production:
 * npm hoists a duplicate `react` (or `react-dom`) into a parent
 * `node_modules` when the install was run from outside the project.
 * Vite happily bundles the result, but the browser sees TWO Reacts
 * at runtime and the first component render throws
 *   "Cannot read properties of null (reading 'useContext')"
 * which presents as a blank `<div id="root">` with no network
 * errors. This script fails the build before the duplicate can ship.
 *
 * Run before every build. Exit 1 on any of:
 *   - A `package.json` exists in any ancestor directory (would let
 *     npm hoist new installs across projects)
 *   - More than one version of a critical React-coupled dep
 *     resolves in the project's dep tree
 *
 * Usage:
 *   node scripts/check-deps.mjs
 *   # or auto-wired via `prebuild` / `npm run verify:deps`
 */
import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const issues = [];
// react + react-dom MUST be singleton. Add any other dep you call
// hooks from (e.g. react-router) so a duplicate also fails here.
const CRITICAL = ["react", "react-dom"];

// 1. No `package.json` in any ancestor that npm will actually
//    resolve against. npm will hoist into the nearest one and
//    silently break the project below it.
//
//    EXCEPTION: a "stub" package.json that exists only as a single
//    dependency declaration (no `scripts`, no `main`, no `workspaces`,
//    no actual `node_modules/` with content) is inert for npm
//    purposes. The Ojas backend root has one of these (it declares
//    stockfish.wasm for the agent's chess tool and nothing else).
//    We treat it as a no-op so the frontend project can build even
//    when the backend root is checked out alongside it.
let dir = path.dirname(projectRoot);
for (let i = 0; i < 6; i++) {
  const pjPath = path.join(dir, "package.json");
  if (fs.existsSync(pjPath)) {
    let inert = true; // default to "skip" — the common case is a stub
    try {
      const meta = JSON.parse(fs.readFileSync(pjPath, "utf-8"));
      // Any of these means the file is a real Node project, not a stub.
      const looksLikeAProject =
        meta.scripts !== undefined ||
        meta.main !== undefined ||
        meta.workspaces !== undefined ||
        meta.type === "module" ||
        meta.bin !== undefined;
      if (looksLikeAProject) inert = false;
      // Also flag if there's a real node_modules/ next to it with
      // actual installable content (more than just leftover dirs).
      const nmPath = path.join(dir, "node_modules");
      if (fs.existsSync(nmPath)) {
        try {
          const entries = fs.readdirSync(nmPath);
          // Any real install leaves more than just .package-lock.json
          // or scoped namespace dirs. Even a single non-dotfile entry
          // means the directory is a real install target.
          if (entries.some((e) => !e.startsWith("."))) inert = false;
        } catch {
          // unreadable — treat as real to be safe
          inert = false;
        }
      }
    } catch {
      // unparseable — treat as a real ancestor and flag it
      inert = false;
    }
    if (!inert) {
      issues.push(
        `package.json found at ${pjPath} -- ` +
          `npm will hoist new installs there and break this project. ` +
          `Move or delete it.`,
      );
      break;
    }
  }
  const parent = path.dirname(dir);
  if (parent === dir) break;
  dir = parent;
}

// 2. Single version of each critical dep resolves inside the project.
//    `npm ls <pkg> --all` walks the whole tree (including hoisted
//    ancestors if any) and reports each version it finds.
for (const pkg of CRITICAL) {
  try {
    const out = execSync(`npm ls ${pkg} --all --json`, {
      cwd: projectRoot,
      encoding: "utf-8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    const versions = new Set();
    const walk = (node) => {
      if (!node) return;
      if (node.version && node.name === pkg) versions.add(node.version);
      if (node.dependencies) {
        for (const child of Object.values(node.dependencies)) walk(child);
      }
    };
    walk(JSON.parse(out));
    if (versions.size > 1) {
      issues.push(
        `${pkg} has multiple versions in the tree: ${[...versions].join(", ")}`,
      );
    }
  } catch (e) {
    // `npm ls` exits non-zero when it finds duplicates -- parse the
    // human-readable text out of stdout to extract the versions.
    const stdout = e.stdout?.toString() || "";
    const versions = new Set(
      [
        ...stdout.matchAll(
          new RegExp(`${pkg.replace("/", "\\/")}@(\\d+\\.\\d+\\.\\d+)`, "g"),
        ),
      ].map((m) => m[1]),
    );
    if (versions.size > 1) {
      issues.push(
        `${pkg} has multiple versions in the tree: ${[...versions].join(", ")}`,
      );
    }
  }
}

// 3. Walk node_modules/ directly and count physical copies of each
//    critical dep. `npm ls` reports the deduped top-level copy and
//    hides nested duplicates behind "deduped" markers -- but a
//    physical duplicate under e.g.
//    `node_modules/@radix-ui/react-dialog/node_modules/react/` is
//    the actual two-React bug: at build time npm/bundler sees one
//    (the top-level), but at runtime the package's require()
//    resolves to the nested one. This was the exact bug from
//    session `62a020f6` (event #41): "react@18.3.1 deduped" next
//    to "react@19.2.7" in `npm ls`, with the 18.3.1 living
//    physically under a transitive dep's node_modules.
function findPhysicalCopies(root, pkg) {
  const out = [];
  // BFS over node_modules dirs. We start at the project's own
  // node_modules, and every time we find a package we ALSO queue
  // that package's nested node_modules/ to be searched -- which
  // is exactly where nested duplicates hide.
  const stack = [path.join(root, "node_modules")];
  while (stack.length) {
    const dir = stack.pop();
    if (!fs.existsSync(dir)) continue;
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e of entries) {
      if (!e.isDirectory()) continue;
      const full = path.join(dir, e.name);
      if (e.name === pkg) {
        // Direct hit: read the version.
        const pj = path.join(full, "package.json");
        try {
          const meta = JSON.parse(fs.readFileSync(pj, "utf-8"));
          out.push({ version: meta.version, path: full });
        } catch {
          // ignore -- broken copy
        }
      } else if (e.name.startsWith("@")) {
        // Scoped namespace dir (`@radix-ui`, etc). For each
        // `<scope>/<pkg>/` child, check both the pkg itself AND
        // descend into its nested node_modules.
        let scoped;
        try {
          scoped = fs.readdirSync(full, { withFileTypes: true });
        } catch {
          continue;
        }
        for (const s of scoped) {
          if (!s.isDirectory()) continue;
          const subPkg = path.join(full, s.name, pkg);
          if (fs.existsSync(subPkg) && fs.statSync(subPkg).isDirectory()) {
            const pj = path.join(subPkg, "package.json");
            try {
              const meta = JSON.parse(fs.readFileSync(pj, "utf-8"));
              out.push({ version: meta.version, path: subPkg });
            } catch {
              // ignore
            }
          }
          // Recurse into this scoped pkg's nested node_modules
          // for the case where the duplicate is one or more
          // levels deeper.
          const nestedNm = path.join(full, s.name, "node_modules");
          if (fs.existsSync(nestedNm)) stack.push(nestedNm);
        }
      }
      // For non-`@`, non-`pkg` entries (regular package dirs),
      // also descend into their nested node_modules to find
      // deeper duplicates.
      const nestedNm = path.join(full, "node_modules");
      if (fs.existsSync(nestedNm)) stack.push(nestedNm);
    }
  }
  return out;
}

for (const pkg of CRITICAL) {
  const copies = findPhysicalCopies(projectRoot, pkg);
  if (copies.length > 1) {
    const summary = copies
      .map((c) => `${c.version} @ ${path.relative(projectRoot, c.path)}`)
      .join("\n      ");
    issues.push(
      `${pkg} has ${copies.length} physical copies under node_modules:\n      ${summary}\n` +
        `Even if "npm ls" shows one "deduped" version, a nested copy will load ` +
        `a second React at runtime, breaking hooks.`,
    );
  }
}

if (issues.length) {
  console.error("check-deps FAILED:");
  for (const msg of issues) console.error("  - " + msg);
  console.error(
    "\nFix: delete any ancestor package.json, then `rm -rf node_modules " +
      "package-lock.json && npm install` from inside the project.",
  );
  process.exit(1);
}
console.log("check-deps OK -- single React, no hoisting parent.");
