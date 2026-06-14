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
      // We use a hand-rolled no-op SW from web/public/sw.js (copied to
      // dist/ on build) — not the workbox-generated one. So:
      //   - disable the auto-generated SW (workbox precache, runtimeCaching)
      //   - just keep the manifest so the PWA is installable
      //   - src/pwa/registerSW.ts registers /sw.js (the no-op one) on load
      disable: true,
      manifest: {
        name: "Ojas",
        short_name: "Ojas",
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
        // No runtime caching at all. The server already sends
        // `Cache-Control: no-store, must-revalidate` on every response,
        // and the SW's job here is only to satisfy the installability
        // criterion (browsers won't show the install prompt without a
        // registered SW). The precache is the minimum needed for that;
        // every fetch goes straight to the network.
        navigateFallback: null,
        runtimeCaching: [],
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
