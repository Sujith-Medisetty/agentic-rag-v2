// Strict sub-app service worker.
//
// THREE caching rules (in order of precedence):
//   1. /api/*       → NetworkOnly, NEVER cached, NEVER written to cache
//   2. /assets/*    → Cache-first (Vite content-hashed; safe to cache
//                     forever because the filename changes per build)
//   3. HTML         → Network-first, cache as last-resort offline fallback
//
// Everything else (manifest, icons, anything not /api/, not /assets/,
// not HTML) is NetworkOnly — the server's Cache-Control headers
// (no-cache on /apps/*) are authoritative for the HTTP cache.
//
// CACHE NAME: `ojas-static-v<__CACHE_VERSION__>`. The version is
// injected at build time by the `injectCacheVersion` Vite plugin
// (see vite.config.ts) so every build produces a new cache name →
// the activate handler evicts every OLD cache → no stale /api/*
// entries can survive a redeploy.

const CACHE = "ojas-static-v" + "__CACHE_VERSION__";
const SHELL = ["./manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    Promise.all([
      caches.keys().then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      ),
      self.clients.claim(),
    ])
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Never cache the SW itself — it must always re-validate so a new
  // SW byte-stream can take over.
  if (url.pathname.endsWith("/sw.js")) return;

  // /api/* is NetworkOnly. NO cache read, NO cache write. This is
  // the single most important rule in this file — sub-apps fetch their
  // data from /api/* and a stale entry there breaks every feature.
  if (url.pathname.startsWith("/api/")) return;

  // Cross-origin: never cache. The browser's HTTP cache handles
  // its own revalidation via Cache-Control.
  if (url.origin !== self.location.origin) return;

  // Vite content-hashed assets. Safe to cache aggressively because
  // a content change always means a new URL (new hash in filename) =
  // a fresh fetch.
  if (url.pathname.includes("/assets/")) {
    e.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(req, clone));
          }
          return res;
        });
      })
    );
    return;
  }

  // HTML / navigations. Network-first so a redeploy is visible
  // immediately. Cache only the network response.
  const isHTML =
    req.mode === "navigate" ||
    req.destination === "document" ||
    url.pathname.endsWith(".html") ||
    url.pathname === "/" ||
    url.pathname.endsWith("/");

  if (isHTML) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(req, clone));
          }
          return res;
        })
        .catch(() =>
          caches.match(req).then((c) =>
            c || caches.match("/").then((c2) => c2 || Response.error())
          )
        )
    );
    return;
  }

  // Everything else: NetworkOnly.
});
