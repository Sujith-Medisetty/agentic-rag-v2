// Minimal service worker so the browser will surface the install
// prompt. Network-first for HTML so redeploys are visible immediately;
// cache-first for content-hashed assets (they're immutable by hash).
// `__CACHE_VERSION__` is injected at build time by Vite's `define`
// (see vite.config.ts), so every npm run build produces a new cache
// name and the browser evicts the old assets automatically.

const CACHE = "ojas-static-v" + "__CACHE_VERSION__";
const SHELL = ["./manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  // Evict EVERY old cache (different version = different app revision).
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);

  // Never cache the SW itself — it must always be re-validated so a
  // new SW byte-stream can take over.
  if (url.pathname.endsWith("/sw.js")) return;

  // Network-first for HTML so users always see the freshest version
  // after a redeploy. Fall back to cache only when offline.
  const isHTML =
    e.request.mode === "navigate" ||
    e.request.destination === "document" ||
    url.pathname.endsWith(".html") ||
    url.pathname === "/" ||
    url.pathname.endsWith("/");

  if (isHTML) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
          }
          return res;
        })
        .catch(() => caches.match(e.request).then((c) => c || Response.error()))
    );
    return;
  }

  // Cache-first ONLY for Vite's content-hashed asset bundles
  // (/assets/*). Other same-origin GETs (manifest, icons, anything
  // else) go network-first so a redeploy is visible immediately
  // and writes to /api/* (if the app has one) are never stale.
  const isAsset = url.pathname.includes("/assets/");

  if (isAsset) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        if (cached) return cached;
        return fetch(e.request)
          .then((res) => {
            if (res.ok && url.origin === self.location.origin) {
              const clone = res.clone();
              caches.open(CACHE).then((c) => c.put(e.request, clone));
            }
            return res;
          })
          .catch(() => cached || Response.error());
      })
    );
    return;
  }

  // Network-first for everything else.
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok && url.origin === self.location.origin) {
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }
        return res;
      })
      .catch(() =>
        caches.match(e.request).then((c) => c || Response.error())
      )
  );
});
