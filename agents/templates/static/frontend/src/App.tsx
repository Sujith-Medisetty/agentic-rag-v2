import { useEffect, useState } from "react";
import { BrowserRouter, Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { Sparkles } from "lucide-react";

import InstallButton from "@/components/install-button";
import { ThemeToggle } from "@/components/theme-toggle";
import StarterSections from "@/components/_sections-example.example";
// import StarterProduct from "@/components/_product-example.example";
import HomePage from "@/pages/home";
import NotFoundPage from "@/pages/not-found";

/**
 * Page chrome AND client-side routing live here — App.tsx owns:
 *   - the top bar, ThemeToggle, InstallButton (page chrome rule),
 *   - the <BrowserRouter> + <Routes> tree (routing rule).
 *
 * Why this file is structured this way:
 *   - The system prompt's CRITICAL rule says page chrome belongs
 *     to App.tsx and ONLY to App.tsx. A real feature component
 *     (Calculator, Calendar, Todo, ...) is rendered INSIDE the
 *     <main> of a route, never has to duplicate the chrome, and
 *     must NEVER render its own top bar / ThemeToggle.
 *   - The starter below renders the template's example
 *     `_sections-example.example.tsx` at "/". The agent's real
 *     build:
 *       1. Edits or replaces the component imported as
 *          `StarterSections` (or uncomment `StarterProduct`).
 *       2. Adds more pages under `src/pages/` and registers
 *          each one as a `<Route>` below — see `pages/home.tsx`
 *          for a copy-paste reference.
 *       3. Deletes the .example files when they're done.
 *   - Per-section anchor nav (Overview / Highlights / Connect, or
 *     Features / Pricing / FAQ) stays INSIDE the example layout,
 *     because it's feature content, not page chrome.
 *
 * Routing notes:
 *   - We use <BrowserRouter>. The Ojas deploy (Caddy) already
 *     has `try_files {path} /index.html` for BOTH the user's
 *     sub-app and the chat UI, so deep links like /settings or
 *     /items/42 always serve index.html and the client router
 *     takes over. Without the router, refresh on any non-root
 *     URL would 404.
 *   - Always declare a catch-all `<Route path="*" element={...} />`
 *     so unknown URLs render a 404 page instead of a blank
 *     document. Caddy will gladly serve the SPA at literally
 *     any path.
 *   - For in-app links use `<Link to="...">` from react-router-dom,
 *     never `<a href="...">`. A plain <a> causes a full page
 *     reload, defeating the router and resetting all state.
 */
export default function App() {
  // Register the service worker so the browser will surface the
  // install prompt. Skip in dev (Vite serves over :5180 without a
  // service worker).
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (!import.meta.env.PROD) return;
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker
      .register("./sw.js")
      .catch((err) => console.warn("SW registration failed:", err));
  }, []);

  // Gate <BrowserRouter> on the client. <BrowserRouter> reads
  // `window` / `document` on first render to construct its
  // history object, so rendering it during SSR (or in the
  // verify-render smoke test, which runs the bundle through
  // `react-dom/server` in Node) throws
  // "ReferenceError: document is not defined" and the whole
  // tree unwinds. We render the page chrome + a placeholder on
  // the server, then swap to the real router on the first
  // client effect. This is the same pattern shadcn's own
  // templates use for any client-only library.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const chrome = (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header
        className="sticky top-0 z-40 flex h-14 items-center gap-4 border-b border-border/60 bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 sm:px-6"
        style={{ paddingTop: "env(safe-area-inset-top)" }}
      >
        <Link
          to="/"
          onClick={(e) => {
            // Smooth-scroll to top on the home route; if we're
            // on another route, BrowserRouter will navigate.
            if (window.location.pathname === "/") {
              e.preventDefault();
              window.scrollTo({ top: 0, behavior: "smooth" });
            }
          }}
          className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight"
        >
          <Sparkles className="h-4 w-4 text-accent" />
          Ojas
        </Link>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <InstallButton />
        </div>
      </header>

      <main className="flex-1">
        {mounted ? (
          <Routes>
            <Route path="/" element={<StarterSections />} />
            <Route path="/home" element={<HomePage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        ) : (
          <div className="min-h-[40vh]" aria-hidden="true" />
        )}
      </main>
    </div>
  );

  if (typeof window === "undefined") return <MemoryRouter>{chrome}</MemoryRouter>;
  return <BrowserRouter>{chrome}</BrowserRouter>;
}
