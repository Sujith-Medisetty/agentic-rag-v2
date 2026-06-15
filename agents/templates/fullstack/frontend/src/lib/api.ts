/**
 * Shared API base for the fullstack template.
 *
 * Why this file exists:
 *   1. `(import.meta as unknown as { env: { BASE_URL: string } }).env.BASE_URL`
 *      is a code smell. It works around a missing `vite/client` types
 *      reference — but our `vite-env.d.ts` already has
 *      `/// <reference types="vite/client" />`, which exposes
 *      `import.meta.env.BASE_URL` as a typed string with NO cast.
 *      Defining the API base here once means feature components
 *      just `import { API } from "@/lib/api"` and never have to
 *      touch `import.meta.env` themselves.
 *   2. esbuild (used by `npm run verify` and the verify-render
 *      smoke test) does NOT inject `import.meta.env` like Vite
 *      does. The cast pattern failed at runtime in the
 *      2026-06-15 Full Stack Todo App Build smoke test.
 *      Centralising the env read here also lets us swap to a
 *      safer default if `import.meta.env` is missing in any tool.
 *
 * Convention:
 *   `API` is the absolute path to the FastAPI app's `/api` prefix
 *   relative to the current host. In dev, Vite's proxy forwards
 *   `/api/*` to 127.0.0.1:8000. In production, Caddy strips the
 *   app's slug prefix and the FastAPI app is mounted at `/api/*`
 *   on the same hostname.
 */
function readBaseUrl(): string {
  // `import.meta.env.BASE_URL` is typed by `vite/client` and is
  // always a string — defaults to "/" in Vite. The fallbacks
  // below are defensive for non-Vite tools (esbuild running
  // the verify-render smoke test, plain Node, vitest without
  // the vite plugin) that may not inject the field at all.
  const env = (import.meta as { env?: { BASE_URL?: string } }).env;
  return env?.BASE_URL ?? "/";
}

const base = readBaseUrl().replace(/\/$/, "");

/** Base path to the FastAPI backend (no trailing slash). */
export const API = `${base}/api`;
