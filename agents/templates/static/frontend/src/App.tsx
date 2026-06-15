import { useEffect } from "react";
import { Sparkles } from "lucide-react";

import InstallButton from "@/components/install-button";
import { ThemeToggle } from "@/components/theme-toggle";
import StarterSections from "@/components/_sections-example.example";
// import StarterProduct from "@/components/_product-example.example";

/**
 * Page chrome lives here — App.tsx owns the top bar, ThemeToggle,
 * and InstallButton.
 *
 * Why this file is structured this way:
 *   - The system prompt's CRITICAL rule says page chrome
 *     (app title, ThemeToggle, InstallButton, header bar) belongs
 *     to App.tsx and ONLY to App.tsx. A real feature component
 *     (Calculator, Calendar, Todo, ...) is rendered INSIDE the
 *     App.tsx <main> and never has to duplicate the chrome.
 *   - The starter below renders one of the template's example
 *     layouts (`_sections-example.example.tsx`) inside <main>.
 *     The agent's real build:
 *       1. Replaces the import + return body to render their
 *          feature component.
 *       2. Optionally uncomments `StarterProduct` to try the
 *          pricing-tier example instead.
 *       3. Deletes the .example files when they're done.
 *   - Per-section anchor nav (Overview / Highlights / Connect, or
 *     Features / Pricing / FAQ) stays INSIDE the example layout,
 *     because it's feature content, not page chrome.
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

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header
        className="sticky top-0 z-40 flex h-14 items-center gap-4 border-b border-border/60 bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60 sm:px-6"
        style={{ paddingTop: "env(safe-area-inset-top)" }}
      >
        <a
          href="#top"
          onClick={(e) => {
            e.preventDefault();
            window.scrollTo({ top: 0, behavior: "smooth" });
          }}
          className="inline-flex items-center gap-2 text-sm font-semibold tracking-tight"
        >
          <Sparkles className="h-4 w-4 text-accent" />
          Ojas
        </a>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <InstallButton />
        </div>
      </header>

      <main className="flex-1">
        <StarterSections />
      </main>
    </div>
  );
}
