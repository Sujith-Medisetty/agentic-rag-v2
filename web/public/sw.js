// No-op service worker.
//
// The user wants ZERO caching across the stack. The server already sends
// `Cache-Control: no-store, must-revalidate` on every response, so the
// browser's HTTP cache stays empty. This SW exists for ONE reason only:
// to satisfy the PWA installability criterion. Browsers (Chrome, Edge,
// Safari iOS) won't show the "Add to Home Screen" prompt without a
// registered service worker — even if the manifest is otherwise valid.
//
// What this SW does NOT do:
//   - No `fetch` event handler → every request goes to the network.
//   - No `caches.open(...)` → no cache buckets exist.
//   - No `caches.match` / `caches.put` → nothing to read or write.
//
// What it DOES do:
//   - install/activate → take over from any previous SW.
//   - skipWaiting + clients.claim → take over immediately, not on the
//     next page navigation. Means a redeploy that ships a new SW
//     doesn't need a manual refresh to be effective.
//   - On activate, wipe any cache buckets left by older SWs (in case
//     a previous build had a real cache and we want the new policy to
//     take effect on the very next load).

self.addEventListener("install", (e) => {
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(keys.map((k) => caches.delete(k)))
      ),
      self.clients.claim(),
    ])
  );
});

// No fetch handler. Every request bypasses the SW and goes to the
// network untouched. The browser's HTTP cache is the only cache layer
// in play, and it's set to no-store by the server, so effectively
// every request hits the origin.
