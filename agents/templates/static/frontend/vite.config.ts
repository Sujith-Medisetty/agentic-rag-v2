import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// CRITICAL: `base: './'` (relative) makes asset URLs work under ANY
// subpath. Ojas serves the built dist at https://<slug>.<host>/, so
// assets must be requested as ./assets/foo.js, NOT /assets/foo.js.
// Without this, every CSS/JS asset 404s on the deployed URL.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
