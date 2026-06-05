// Manual SW registration via workbox-window.
//
// We could let vite-plugin-pwa do `injectRegister: 'auto'` and get a
// 5-line auto-register, but doing it manually lets us:
//   - Avoid throwing on dev (where the SW is disabled by devOptions)
//   - Catch and log registration errors (Safari is fussy about scopes)
//   - Surface "update available" to the UI instead of silently reloading
//     (so the user knows their new code is live instead of wondering why
//     the page just refreshed)
//
// Called once from src/main.tsx. The update-available UI lives in
// Layout.tsx and listens for the "ojas:sw-update" CustomEvent on window.

import { Workbox } from "workbox-window";

export function registerSW(): void {
  if (!("serviceWorker" in navigator)) return;
  if (import.meta.env.DEV) return;   // SW disabled in dev mode

  const wb = new Workbox("/sw.js", { scope: "/" });

  // A new SW has installed and is waiting. Notify the UI so it can show
  // a "refresh to update" toast. We do NOT auto-skip-waiting here — the
  // user clicking the toast is the explicit signal.
  wb.addEventListener("waiting", (event) => {
    const installing = (event as any).sw;
    window.dispatchEvent(
      new CustomEvent("ojas:sw-update", { detail: { sw: installing } }),
    );
  });

  // User accepted the update (clicked the toast). Tell the waiting SW to
  // skip waiting so it activates now. The `controlling` listener below
  // will then reload the page to run the new bundle.
  window.addEventListener("ojas:sw-apply", () => {
    wb.messageSkipWaiting();
  });

  // New SW took control — reload so the page runs the fresh bundle.
  wb.addEventListener("controlling", () => {
    window.location.reload();
  });

  wb.register().catch((err) => {
    // SW registration failures are loud in dev tools but should never block
    // the app — fall back to non-PWA mode silently.
    console.warn("[pwa] SW registration failed:", err);
  });
}
