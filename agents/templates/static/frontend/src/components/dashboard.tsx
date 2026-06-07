import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  Activity,
  BarChart3,
  CheckCircle2,
  CheckSquare,
  LayoutDashboard,
  Menu,
  Settings as SettingsIcon,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
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

type NavItem = { label: string; icon: typeof LayoutDashboard; active?: boolean };

const NAV: readonly NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, active: true },
  { label: "Tasks", icon: CheckSquare },
  { label: "Stats", icon: BarChart3 },
  { label: "Settings", icon: SettingsIcon },
] as const;

const STATS = [
  {
    label: "Active projects",
    value: "12",
    delta: "+2 from last week",
    icon: Activity,
    positive: true,
  },
  {
    label: "Tasks done",
    value: "48",
    delta: "+8 today",
    icon: CheckCircle2,
    positive: true,
  },
  {
    label: "Streak",
    value: "7 days",
    delta: "Keep it up",
    icon: TrendingUp,
    positive: true,
  },
];

const emailSchema = z.object({
  email: z.string().email("Please enter a valid email."),
});

type EmailFormValues = z.infer<typeof emailSchema>;

/**
 * Sidebar — used in both the desktop aside and the mobile Sheet.
 */
function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-6">
      <div className="flex items-center gap-2 px-2">
        <div className="grid h-8 w-8 place-items-center rounded-md bg-primary text-primary-foreground">
          <Sparkles className="h-4 w-4" />
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight">Ojas app</div>
          <div className="text-xs text-muted-foreground">Deployed by Ojas</div>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1">
        {NAV.map(({ label, icon: Icon, active }) => (
          <a
            key={label}
            href="#"
            onClick={(e) => {
              e.preventDefault();
              onNavigate?.();
            }}
            className={
              "group flex h-11 items-center gap-3 rounded-md px-3 text-sm font-medium transition-all " +
              (active
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/60 hover:text-foreground hover:translate-x-0.5")
            }
            aria-current={active ? "page" : undefined}
          >
            <Icon className="h-4 w-4" />
            {label}
          </a>
        ))}
      </nav>

      <Separator />

      <div className="flex items-center gap-2 px-1">
        <ThemeToggle />
        <InstallButton />
      </div>
    </div>
  );
}

/**
 * Demo modal — the email signup form. The static template uses
 * localStorage-style state only; the fullstack variant of this
 * component will swap the submit handler for a POST to /api/items.
 */
function EmailDialog() {
  const [open, setOpen] = useState(false);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<EmailFormValues>({
    resolver: zodResolver(emailSchema),
  });

  const onSubmit = handleSubmit(async () => {
    // Static template just toasts — the fullstack variant hits /api/items.
    toast.success("Subscribed — check your inbox.");
    reset();
    setOpen(false);
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline">Open modal</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Stay in the loop</DialogTitle>
          <DialogDescription>
            Subscribe for product updates. We send roughly one email a month.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">Email address</Label>
            <Input
              id="email"
              type="email"
              placeholder="you@domain.com"
              autoComplete="email"
              {...register("email")}
            />
            {errors.email && (
              <p className="text-sm text-destructive">{errors.email.message}</p>
            )}
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? "Subscribing…" : "Subscribe"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function Dashboard() {
  return (
    <div className="flex min-h-screen bg-background text-foreground">
      {/* ── Desktop sidebar ────────────────────────────────────── */}
      <aside className="hidden w-60 shrink-0 border-r border-border bg-card/30 p-4 md:block">
        <SidebarContent />
      </aside>

      {/* ── Main column ────────────────────────────────────────── */}
      <div className="flex min-h-screen flex-1 flex-col">
        {/* Sticky header with safe-area inset */}
        <header
          className="sticky top-0 z-30 flex h-16 items-center gap-4 border-b border-border bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60"
          style={{ paddingTop: "env(safe-area-inset-top)" }}
        >
          {/* Mobile menu trigger */}
          <Sheet>
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
              <SidebarContent />
            </SheetContent>
          </Sheet>

          <h1 className="text-base font-semibold tracking-tight">Dashboard</h1>

          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle />
            <InstallButton />
          </div>
        </header>

        {/* Page content */}
        <main className="flex-1 space-y-6 p-4 sm:p-6 lg:p-8">
          {/* Greeting */}
          <div>
            <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
              Welcome to your app
            </h2>
            <p className="text-muted-foreground">
              This dashboard ships with the Ojas template. Replace{" "}
              <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                src/components/dashboard.tsx
              </code>{" "}
              with your real UI.
            </p>
          </div>

          {/* Stat cards — staggered entrance */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <AnimatePresence>
              {STATS.map((s, i) => {
                const Icon = s.icon;
                return (
                  <motion.div
                    key={s.label}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.3, delay: i * 0.08, ease: "easeOut" }}
                  >
                    <Card>
                      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                        <CardTitle className="text-sm font-medium text-muted-foreground">
                          {s.label}
                        </CardTitle>
                        <Icon className="h-4 w-4 text-muted-foreground" />
                      </CardHeader>
                      <CardContent>
                        <div className="text-2xl font-bold tracking-tight">
                          {s.value}
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          <span
                            className={
                              s.positive
                                ? "text-success"
                                : "text-muted-foreground"
                            }
                          >
                            {s.delta}
                          </span>
                        </p>
                      </CardContent>
                    </Card>
                  </motion.div>
                );
              })}
            </AnimatePresence>
          </div>

          {/* Action row */}
          <Card>
            <CardHeader>
              <CardTitle>Try the design system</CardTitle>
              <CardDescription>
                A toast, a modal with a validated form, and animations — all
                wired to the same Tailwind tokens.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="flex flex-wrap gap-3">
                <Button onClick={() => toast.success("Saved successfully!")}>
                  Show toast
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => toast.error("Something went wrong.")}
                >
                  Show error toast
                </Button>
                <EmailDialog />
              </div>
            </CardContent>
          </Card>
        </main>
      </div>
    </div>
  );
}
