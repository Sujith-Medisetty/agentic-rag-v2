/**
 * Guard 2: render smoke test.
 *
 * `tsc -b` and `vite build` validate types and bundle modules.
 * They do NOT execute the code, so they cannot catch:
 *   - Duplicate React / invalid hook call (two-React bug)
 *   - Bad runtime imports, missing exports
 *   - Throw-during-render in any component
 *
 * Renders the root <App /> to a string with `react-dom/server`.
 * Server-render needs no browser, runs in CI, and is the fastest
 * way to detect a "blank page" deployment before the user does.
 *
 * Usage:
 *   vite-node scripts/verify-render.tsx
 *   # or auto-wired via `npm run verify:render` / `npm run verify`
 */
import { renderToString } from "react-dom/server";
import App from "../src/App";

// Anything that looks like real app content. This isn't exhaustive
// coverage -- it's a smoke alarm. A real project will assert against
// its own sections ("Sujith Medisetty", "id=\"contact\"", etc.).
// We just need to know the render did not produce a stub, an
// exception, or a tiny 0-200 char fragment.
const MIN_LEN = 200;
const REQUIRED: string[] = [
  // Must have at least one element + one section-like opening tag.
  "<",
];

try {
  const html = renderToString(App({}));
  const problems: string[] = [];

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
    console.error(html?.slice(0, 500) ?? "(no output)");
    process.exit(1);
  }

  console.log(
    `verify-render OK -- ${html.length} chars rendered, smoke checks passed.`,
  );
} catch (e) {
  console.error("verify-render FAILED -- render threw:");
  console.error(e instanceof Error ? e.stack || e.message : e);
  process.exit(1);
}
