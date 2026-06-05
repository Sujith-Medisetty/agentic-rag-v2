import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import path from "node:path";

// In dev, Vite runs on :5173 and FastAPI on :8765 — we proxy /api so the
// frontend uses same-origin URLs (CORS-free) and so the WebSocket upgrade
// passes through unchanged.
//
// In production (Phase 5) FastAPI will serve the static build from web/dist,
// so this proxy is dev-only.
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      // Auto-update strategy: when a new SW is found, activate it on next
      // navigation. No user-visible "update available" prompt for v1 — quiet
      // and reliable is better for a non-technical user.
      registerType: "autoUpdate",
      injectRegister: false,         // we register manually in src/pwa/registerSW.ts
                                     // so we control timing + error handling
      includeAssets: [
        "icon.svg", "favicon.ico",
        "icons/icon-192.png", "icons/icon-512.png",
        "icons/icon-maskable-512.png", "icons/apple-touch-icon.png",
      ],
      manifest: {
        name: "Forge",
        short_name: "Forge",
        description:
          "Your local coding agent — chat with your repo from any device.",
        theme_color: "#1B1814",
        background_color: "#1B1814",
        display: "standalone",
        orientation: "any",
        scope: "/",
        start_url: "/",
        icons: [
          // SVG works everywhere modern (Chrome, Edge, Firefox, Safari 15+).
          { src: "icon.svg",                  sizes: "any", type: "image/svg+xml", purpose: "any" },
          // PNG fallbacks for older Android and for iOS home-screen quality.
          { src: "icons/icon-192.png",        sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "icons/icon-512.png",        sizes: "512x512", type: "image/png", purpose: "any" },
          // Maskable: lets Android draw the icon inside its adaptive shape
          // (circle/squircle) without clipping the meaningful content.
          { src: "icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
      },
      workbox: {
        // Don't pre-cache the API; always go to the network for it. Cache
        // static assets and the SPA shell so first paint is instant offline.
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [
          {
            // The SPA shell + JS/CSS chunks
            urlPattern: ({ request }) =>
              ["document", "script", "style", "worker"].includes(request.destination),
            handler: "StaleWhileRevalidate",
            options: { cacheName: "app-shell" },
          },
          {
            urlPattern: ({ request }) =>
              ["image", "font"].includes(request.destination),
            handler: "CacheFirst",
            options: {
              cacheName: "static-assets",
              expiration: { maxEntries: 100, maxAgeSeconds: 60 * 60 * 24 * 30 },
            },
          },
          {
            // API requests — always try the network. Phase 4 might queue
            // failed POSTs for background sync, but not yet.
            urlPattern: ({ url }) => url.pathname.startsWith("/api/"),
            handler: "NetworkOnly",
          },
        ],
      },
      devOptions: {
        // Make the SW work in `npm run dev` too so we catch PWA bugs early
        // without a full build.
        enabled: false,   // keep off by default; flip to true to test SW locally
      },
    }),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
