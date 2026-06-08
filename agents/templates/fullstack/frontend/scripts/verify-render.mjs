#!/usr/bin/env node
/**
 * Guard 2: render smoke test.
 *
 * `tsc -b` and `vite build` validate types and bundle modules.
 * They do NOT execute the code, so they cannot catch:
 *   - Duplicate React / invalid hook call (two-React bug)
 *   - Bad runtime imports, missing exports
 *   - Throw-during-render in any component
 *
 * The big trap with server-render smoke tests is the
 * "react-dom/server is CJS, the project is ESM" issue. If you
 * `import { renderToString } from "react-dom/server"` from a
 * Node-runnable .mjs file, Node pulls react-dom/server from
 * node_modules' CJS entry, which loads its own copy of React
 * via `require('react')`. The project's own React (bundled by
 * esbuild as ESM, with its own dispatcher table) is a
 * DIFFERENT module instance, so hooks like useContext return
 * null -- the exact "two-React" bug this guard exists to catch.
 *
 * The fix: bundle the project's React, the project's
 * components, AND `react-dom/server` all into a single ESM file
 * with esbuild. The bundle shares one React module instance
 * across everything, mirroring what the browser does at runtime.
 * Then we call the bundle's exported `render` and assert on
 * the output.
 *
 * Pure Node + esbuild. No `vite-node` dependency, so the smoke
 * test still runs even if the dev toolchain breaks.
 *
 * Usage:
 *   node scripts/verify-render.mjs
 *   # or auto-wired via `npm run verify:render` / `npm run verify`
 */
import { build } from "esbuild";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const MIN_LEN = 200;
const REQUIRED = ["<"];

// This script lives at <project>/frontend/scripts/verify-render.mjs.
// Resolve the project root (<project>/frontend) once at module load.
const __filename = fileURLToPath(import.meta.url);
const projectRoot = resolve(dirname(__filename), "..");
// Prefer tsconfig.app.json (vite scaffolds use it), fall back to
// the root tsconfig.json. Either way, esbuild reads
// `compilerOptions.paths` so `@/foo` resolves to
// `<root>/src/foo` the same way Vite does.
const tsconfigApp = join(projectRoot, "tsconfig.app.json");
const tsconfig = existsSync(tsconfigApp)
  ? tsconfigApp
  : join(projectRoot, "tsconfig.json");

// Optional providers. Missing files are fine -- the smoke test
// gracefully falls back to a bare App render. The provider paths
// are relative to the project root.
const providerPaths = [
  "src/components/theme-provider.tsx", // named: ThemeProvider
  "src/components/ui/sonner.tsx",      // named: Toaster
];

async function main() {
  // 1. Build a stub that re-exports App + any optional providers
  //    + a `render` function that runs the wrapped tree through
  //    `react-dom/server.renderToString`. Bundling renderToString
  //    into the same ESM file as the App is the only way to
  //    guarantee a single React instance -- see the file header.
  //
  //    The entry file AND the output bundle both live INSIDE the
  //    project root (in `node_modules/.ojas-verify/`) so that:
  //    - esbuild's node_modules resolution starts from the right
  //      place and can find `react`, `react-dom/server`, etc.
  //    - the resulting ESM bundle can `import "react"` etc. at
  //      runtime and Node's module resolver finds them via the
  //      same project node_modules.
  //    `node_modules/.ojas-verify/` is gitignored and ignored by
  //    npm; nothing else sees it.
  const workDir = join(projectRoot, "node_modules", ".ojas-verify");
  mkdirSync(workDir, { recursive: true });
  const entryStub = join(workDir, "entry.mjs");
  const bundleOut = join(workDir, "app.bundle.mjs");

  const lines = [];
  // Always include the root App.
  lines.push(
    `export { default as App } from ${JSON.stringify(
      join(projectRoot, "src", "App.tsx"),
    )};`,
  );
  // Include providers if the files exist. Each provider's TSX is
  // bundled, so its imports (next-themes, sonner, etc.) resolve
  // through esbuild + the project's tsconfig paths the same way
  // Vite would.
  for (const rel of providerPaths) {
    const abs = join(projectRoot, rel);
    if (existsSync(abs)) {
      lines.push(`export * from ${JSON.stringify(abs)};`);
    }
  }
  // Bundle `react-dom/server` AND a `render` function so the
  // entire smoke test runs inside one esbuild module graph.
  lines.push(`import { renderToString } from "react-dom/server";`);
  lines.push(`import { createElement, Fragment } from "react";`);
  lines.push(`export function render(App, ThemeProvider, Toaster) {`);
  lines.push(`  const children = [createElement(App)];`);
  lines.push(`  if (Toaster) children.push(createElement(Toaster));`);
  lines.push(`  const tree = ThemeProvider`);
  lines.push(`    ? createElement(`);
  lines.push(`        ThemeProvider,`);
  lines.push(`        { attribute: "class", defaultTheme: "system", enableSystem: true, disableTransitionOnChange: true },`);
  lines.push(`        ...children,`);
  lines.push(`      )`);
  lines.push(`    : createElement(Fragment, null, ...children);`);
  lines.push(`  return renderToString(tree);`);
  lines.push(`}`);
  writeFileSync(entryStub, lines.join("\n") + "\n");

  await build({
    entryPoints: [entryStub],
    bundle: true,
    format: "esm",
    platform: "node",
    target: ["node20"],
    jsx: "automatic",
    outfile: bundleOut,
    logLevel: "silent",
    // Pick up `compilerOptions.paths` (e.g. `"@/*": ["./src/*"]`) from
    // the project's tsconfig so path aliases resolve the same way
    // they do under Vite. esbuild reads this natively.
    tsconfig,
    // Match Vite's default extension inference so imports like
    // `@/components/ui/button` (no `.tsx` suffix) resolve. Order
    // matters: tsx before ts.
    resolveExtensions: [".tsx", ".ts", ".jsx", ".js", ".mjs", ".json"],
    // `react-dom/server` (CJS) calls `require("util")` etc. for
    // Node builtins. Mark them external so the CJS bridge stays
    // intact AND the bundle can still use them via Node's module
    // resolver. Banner provides a `createRequire` shim so the
    // bundled CJS code can find its builtins at runtime.
    external: ["react", "react-dom", "react-dom/server"],
    banner: {
      js: [
        "import { createRequire as __ojasCR } from 'node:module';",
        "const require = __ojasCR(import.meta.url);",
      ].join("\n"),
    },
  });

  // 2. Import the bundle and call its exported `render` function.
  //    Everything -- App, providers, react-dom/server, react --
  //    comes from the same esbuild module graph, so React is a
  //    single instance and hooks work the way they do in the
  //    browser.
  const bundleUrl = pathToFileURL(bundleOut).href;
  const mod = await import(bundleUrl);
  const { App, ThemeProvider, Toaster, render } = mod;
  if (typeof App !== "function") {
    throw new Error(
      `App is not a function -- got ${typeof App}. Did src/App.tsx ` +
        `export default a component?`,
    );
  }
  const html = render(App, ThemeProvider, Toaster);

  // 3. Assert non-trivial output.
  const problems = [];
  if (typeof html !== "string" || html.length < MIN_LEN) {
    problems.push(
      `render is suspiciously short: ${html?.length ?? 0} chars ` +
        `(expected >= ${MIN_LEN}). Likely cause: a component threw ` +
        `during render, or the wrong root component was imported.`,
    );
  }
  for (const needle of REQUIRED) {
    if (!html.includes(needle)) {
      problems.push(`render is missing expected content: "${needle}"`);
    }
  }
  if (problems.length) {
    console.error("verify-render FAILED:");
    for (const p of problems) console.error("  - " + p);
    console.error("--- first 500 chars of output ---");
    console.error(
      typeof html === "string" ? html.slice(0, 500) : "(no output)",
    );
    process.exit(1);
  }
  console.log(
    `verify-render OK -- ${html.length} chars rendered, smoke checks passed.`,
  );
}

main().catch((e) => {
  console.error("verify-render FAILED -- render threw:");
  console.error(e instanceof Error ? e.stack || e.message : e);
  process.exit(1);
});
