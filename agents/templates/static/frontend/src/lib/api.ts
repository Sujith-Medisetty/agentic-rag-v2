/**
 * Shared API base — same as the fullstack template's lib/api.ts.
 *
 * The static template doesn't ship a backend by default, but if the
 * agent later wires one in (contact form, signup, webhook), they
 * import { API } from "@/lib/api" the same way the fullstack apps do.
 *
 * Why this file exists here too:
 *   Keeping the static and fullstack templates symmetric means the
 *   agent doesn't have to learn two patterns. The cost when the
 *   app has no backend is one import the bundler tree-shakes.
 */
function readBaseUrl(): string {
  // Defensive: `import.meta.env` may be undefined in non-Vite
  // tools (esbuild, plain Node) that don't inject it.
  const env = (import.meta as { env?: { BASE_URL?: string } }).env;
  return env?.BASE_URL ?? "/";
}

const base = readBaseUrl().replace(/\/$/, "");

/** Base path to a backend (no trailing slash). */
export const API = `${base}/api`;
