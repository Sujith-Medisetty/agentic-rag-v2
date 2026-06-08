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

// 1. No `package.json` in any ancestor. npm will hoist into the
//    nearest one and silently break the project below it.
let dir = path.dirname(projectRoot);
for (let i = 0; i < 6; i++) {
  if (fs.existsSync(path.join(dir, "package.json"))) {
    issues.push(
      `package.json found at ${path.join(dir, "package.json")} -- ` +
        `npm will hoist new installs there and break this project. ` +
        `Move or delete it.`,
    );
    break;
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
