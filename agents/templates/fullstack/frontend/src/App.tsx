import { useEffect, useState } from "react";
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

/**
 * Page chrome lives here — App.tsx owns the top bar, the
 * responsive nav sheet, ThemeToggle, and InstallButton.
 *
 * Why this file is structured this way:
 *   - The system prompt's CRITICAL rule says page chrome
 *     (app title, ThemeToggle, InstallButton, header bar)
 *     belongs to App.tsx and ONLY to App.tsx. A real feature
 *     component (Calculator, Calendar, Todo, ...) is rendered
 *     INSIDE the App.tsx <main> and never has to duplicate
 *     the chrome. The 2026-06-14 calculator build shipped two
 *     ThemeToggles because the starter Dashboard owned chrome
 *     and contradicted this rule.
 *   - The starter below renders the template's example
 *     `_starter-dashboard.example.tsx` (a /api/items dashboard)
 *     inside <main>. The agent's real build:
 *       1. Replaces the import + return body to render their
 *          feature component.
 *       2. Optionally deletes the .example file.
 *     If they forget step 1, they see a 404-from-/api/items
 *     banner inside the starter (the example has a detection
 *     branch for that), not a blank page.
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

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header
        className="sticky top-0 z-30 flex h-16 items-center gap-4 border-b border-border bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60"
        style={{ paddingTop: "env(safe-area-inset-top)" }}
      >
        <MobileNav />
        <h1 className="text-base font-semibold tracking-tight">Ojas app</h1>
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <InstallButton />
        </div>
      </header>

      <main className="flex-1">
        <StarterDashboard />
      </main>
    </div>
  );
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
          <p>Add your app&apos;s primary nav here.</p>
        </div>
      </SheetContent>
    </Sheet>
  );
}
