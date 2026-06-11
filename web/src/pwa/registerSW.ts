// Service-worker registration with AUTO update + reload.
//
// Earlier this dispatched a "ojas:sw-update" event for a UI toast asking
// the user to click "Refresh to update". The toast lived in <Layout>,
// which is NOT rendered on the main chat routes (Workspace). Result: SW
// updates landed silently in "waiting" state and the user kept seeing old
// code until they hard-refreshed. They almost always wanted the update
// anyway — the toast was friction with no payoff.
//
// New flow:
//   1. wb.register() installs the SW.
//   2. New SW detected → `waiting` event → we immediately tell it to
//      skipWaiting (no user click required).
//   3. New SW takes control → `controlling` event → page reloads.
//   4. We POLL for updates every 60s while the page is visible AND on
//      `visibilitychange → visible`, so a user who keeps the PWA open
//      for hours still gets the update within a minute of it being pushed.
//
// The reload is brief — current bundle is in memory so the new one paints
// almost immediately. Cost: a user typing in the chat input at the exact
// moment the SW activates loses their draft. Rare in practice; if it
// becomes a problem we can defer reload until the input is empty.
//
// Called once from src/main.tsx.

import { Workbox } from "workbox-window";

const UPDATE_CHECK_INTERVAL_MS = 60_000;   // 1 minute

export function registerSW(): void {
  if (!("serviceWorker" in navigator)) return;
  if (import.meta.env.DEV) return;   // SW disabled in dev mode

  const wb = new Workbox("/sw.js", { scope: "/" });

  // Auto-accept new versions — no user toast, no waiting state lingering.
  wb.addEventListener("waiting", () => {
    wb.messageSkipWaiting();
  });

  // New SW took control → reload silently so the page runs fresh code.
  // The `reloaded` guard prevents the very rare double-reload edge case
  // (multiple `controlling` fires) that Workbox documents.
  let reloaded = false;
  wb.addEventListener("controlling", () => {
    if (reloaded) return;
    reloaded = true;
    // Wipe ANY leftover Workbox runtime caches from older SW builds before
    // reloading. Older builds had different runtimeCaching rules — an
    // even older build may have cached /api/admin/services, and a stale
    // cache entry there makes the admin panel show "ghost" deployed apps
    // that have already been deleted server-side. The current config
    // marks /api/* as NetworkOnly, so nuking the legacy cache buckets
    // is safe — they'll just get re-created from the (now non-cached)
    // network responses. See the BookWise ghost-row bug from 2026-06.
    if ("caches" in self) {
      caches.keys()
        .then((keys) => Promise.all(
          keys
            .filter((k) => k.startsWith("workbox-precache") || k === "app-shell-html" || k === "app-shell-assets")
            .map((k) => caches.delete(k)),
        ))
        .catch(() => { /* non-fatal; reload will still happen */ });
    }
    window.location.reload();
  });

  wb.register().catch((err) => {
    console.warn("[pwa] SW registration failed:", err);
  });

  // Periodic update checks. Without this, the browser only checks for a
  // new SW on full page navigation — users with the PWA open for hours
  // miss updates entirely. We pause the interval when the page is hidden
  // so we don't burn mobile battery polling in the background.
  let interval: number | null = null;
  const startPolling = () => {
    if (interval !== null) return;
    interval = window.setInterval(() => {
      wb.update().catch(() => { /* network blip, retry next tick */ });
    }, UPDATE_CHECK_INTERVAL_MS);
  };
  const stopPolling = () => {
    if (interval !== null) {
      window.clearInterval(interval);
      interval = null;
    }
  };
  const onVisibility = () => {
    if (document.visibilityState === "visible") {
      // Catch-up check on focus — covers "user closed laptop, opened
      // hours later, an update was pushed in the meantime".
      wb.update().catch(() => { /* ignore */ });
      startPolling();
    } else {
      stopPolling();
    }
  };
  document.addEventListener("visibilitychange", onVisibility);
  if (document.visibilityState === "visible") startPolling();
}
