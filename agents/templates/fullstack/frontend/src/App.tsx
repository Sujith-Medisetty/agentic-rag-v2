import { useEffect, useState } from "react";
import { BrowserRouter, Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { Menu } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import InstallButton from "@/components/install-button";
import { ThemeToggle } from "@/components/theme-toggle";
import StarterDashboard from "@/components/_starter-dashboard.example";
import HomePage from "@/pages/home";
import NotFoundPage from "@/pages/not-found";

/**
 * Page chrome AND client-side routing live here — App.tsx owns:
 *   - the top bar, mobile nav sheet, ThemeToggle, InstallButton
 *     (page chrome rule from the system prompt),
 *   - the <BrowserRouter> + <Routes> tree (routing rule).
 *
 * Why this file is structured this way:
 *   - The system prompt's CRITICAL rule says page chrome belongs
 *     to App.tsx and ONLY to App.tsx. A real feature component
 *     (Calculator, Calendar, Todo, ...) is rendered INSIDE the
 *     <main> of a route, never has to duplicate the chrome, and
 *     must NEVER render its own top bar / ThemeToggle. The
 *     2026-06-14 calculator build shipped two ThemeToggles
 *     because the starter Dashboard owned chrome and contradicted
 *     this rule.
 *
 *   - The starter below renders the template's example
 *     `_starter-dashboard.example.tsx` (a /api/items dashboard)
 *     at "/". The agent's real build:
 *       1. Edits or replaces the component imported as
 *          `StarterDashboard` (or swap the import for their own
 *          feature).
 *       2. Adds more pages under `src/pages/` and registers
 *          each one as a `<Route>` below — see `pages/home.tsx`
 *          for a copy-paste reference.
 *       3. Optionally deletes the .example file.
 *     If they forget step 1, they see a 404-from-/api/items
 *     banner inside the starter (the example has a detection
 *     branch for that), not a blank page.
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
  // install prompt. Skip in dev (Vite serves without a service worker).
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
  // client effect. The first paint shows the layout briefly
  // with no route content; a single useState/useEffect pair
  // runs after hydration. This is the same pattern shadcn's
  // own templates use for any client-only library.
  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  const chrome = (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header
        className="sticky top-0 z-30 flex h-16 items-center gap-4 border-b border-border bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60"
        style={{ paddingTop: "env(safe-area-inset-top)" }}
      >
        <MobileNav />
        <Link
          to="/"
          className="text-base font-semibold tracking-tight"
          aria-label="Home"
        >
          Ojas app
        </Link>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <InstallButton />
        </div>
      </header>

      <main className="flex-1">
        {/*
          Route content. After hydration we mount <Routes>
          inside <BrowserRouter>; before hydration we render a
          placeholder of the same height so the page chrome
          doesn't shift on first paint.
        */}
        {mounted ? (
          <Routes>
            <Route path="/" element={<StarterDashboard />} />
            <Route path="/home" element={<HomePage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
        ) : (
          <div className="min-h-[40vh]" aria-hidden="true" />
        )}
      </main>
    </div>
  );

  // On the server (or any environment without `window`) skip
  // the router entirely and render the chrome + placeholder.
  // On the client, wrap in <BrowserRouter> so refresh on a
  // deep URL works.
  if (typeof window === "undefined") return <MemoryRouter>{chrome}</MemoryRouter>;
  return <BrowserRouter>{chrome}</BrowserRouter>;
}

function MobileNav() {
  // The mobile sheet opens a simple nav panel. A real app would
  // pass its own navigation links in here via props/context;
  // the starter intentionally keeps it empty so the agent has
  // an obvious place to add their own.
  const [open, setOpen] = useState(false);
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      {/*
        Radix invariant: <SheetTrigger> and <SheetContent> MUST
        be descendants of the same <Sheet> provider. The trigger
        consumes the context the Sheet creates; if it's a sibling
        instead of a child, runtime throws
        "DialogTrigger must be used within Dialog" and the whole
        app renders blank. Keep them under the same <Sheet> —
        even if the trigger button is in one visual location
        and the panel in another. The calculator build on
        2026-06-15 hit this bug and shipped with a blank screen.
      */}
      <SheetTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden"
          aria-label="Open menu"
        >
          <Menu className="h-5 w-5" />
        </Button>
      </SheetTrigger>
      <SheetContent side="left" className="w-72">
        <SheetHeader className="sr-only">
          <SheetTitle>Navigation</SheetTitle>
          <SheetDescription>Primary navigation</SheetDescription>
        </SheetHeader>
        <div className="flex h-full flex-col gap-6 p-4 text-sm text-muted-foreground">
          <Link to="/" onClick={() => setOpen(false)}>
            Home
          </Link>
        </div>
      </SheetContent>
    </Sheet>
  );
}
