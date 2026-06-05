// Manual SW registration via workbox-window.
//
// We could let vite-plugin-pwa do `injectRegister: 'auto'` and get a
// 5-line auto-register, but doing it manually lets us:
//   - Avoid throwing on dev (where the SW is disabled by devOptions)
//   - Catch and log registration errors (Safari is fussy about scopes)
//   - Hook the autoUpdate flow so a fresh build silently activates
//
// Called once from src/main.tsx.

import { Workbox } from "workbox-window";

export function registerSW(): void {
  if (!("serviceWorker" in navigator)) return;
  if (import.meta.env.DEV) return;   // SW disabled in dev mode

  const wb = new Workbox("/sw.js", { scope: "/" });

  // autoUpdate strategy: when a new SW reports `waiting`, tell it to skip
  // waiting and activate immediately. The next navigation will pick up the
  // fresh assets — no user-visible reload prompt for v1.
  wb.addEventListener("waiting", () => {
    wb.messageSkipWaiting();
  });

  wb.addEventListener("controlling", () => {
    // New SW took control — reload so the page runs the fresh bundle.
    // This fires at most once per deployment and only if the user was already
    // on the page when the SW updated (rare). The reload is silent and instant.
    window.location.reload();
  });

  wb.register().catch((err) => {
    // SW registration failures are loud in dev tools but should never block
    // the app — fall back to non-PWA mode silently.
    console.warn("[pwa] SW registration failed:", err);
  });
}
