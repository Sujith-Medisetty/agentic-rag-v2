import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * Replace `__CACHE_VERSION__` in the built `dist/sw.js` with the
 * current build's epoch (base-36). Vite's `public/` files are copied
 * to `dist/` verbatim, so `define` doesn't touch them — we have to
 * patch after the bundle closes. Because the cache name changes on
 * every build, the new SW evicts the old cache on `activate` and
 * users see the new version on the next page load. No manual
 * Ctrl+Shift-R, no clearing site data.
 */
const cacheVersion = Date.now().toString(36);
const injectCacheVersion = () => ({
  name: "ojas:inject-cache-version",
  closeBundle() {
    const swPath = path.resolve(__dirname, "dist/sw.js");
    if (!fs.existsSync(swPath)) return;
    const content = fs.readFileSync(swPath, "utf8");
    if (!content.includes("__CACHE_VERSION__")) return;
    fs.writeFileSync(
      swPath,
      content.replace(/__CACHE_VERSION__/g, cacheVersion),
    );
  },
});

// CRITICAL: `base: './'` (relative) makes asset URLs work under ANY
// subpath. Ojas serves the built dist at https://<slug>.<host>/, so
// assets must be requested as ./assets/foo.js, NOT /assets/foo.js.
// Without this, every CSS/JS asset 404s on the deployed URL.
export default defineConfig({
  base: "./",
  plugins: [react(), injectCacheVersion()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
