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
    // A new SW just took control. We don't reload mid-session; let the next
    // route change naturally pick up the new bundle. (If you ever DO want
    // immediate reload, call window.location.reload() here.)
  });

  wb.register().catch((err) => {
    // SW registration failures are loud in dev tools but should never block
    // the app — fall back to non-PWA mode silently.
    console.warn("[pwa] SW registration failed:", err);
  });
}
