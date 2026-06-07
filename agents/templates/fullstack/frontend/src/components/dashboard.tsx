import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  BarChart3,
  CheckCircle2,
  CheckSquare,
  Database,
  LayoutDashboard,
  ListTodo,
  Menu,
  Plus,
  Settings as SettingsIcon,
  Sparkles,
  TrendingUp,
  Trash2,
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
import { Skeleton } from "@/components/ui/skeleton";
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

// Backend API root. In dev, Vite's proxy (configured below) forwards
// /api/* to 127.0.0.1:8000. In production, the deploy pipeline puts
// the FastAPI backend behind Caddy at /api/* of the same host.
const API = ((import.meta as unknown as { env: { BASE_URL: string } }).env.BASE_URL ?? "/")
  .replace(/\/$/, "")
  + "/api";

interface Item {
  id: number;
  title: string;
  done: number; // 0 | 1 (SQLite convention)
}

type NavItem = { label: string; icon: typeof LayoutDashboard; active?: boolean };

const NAV: readonly NavItem[] = [
  { label: "Dashboard", icon: LayoutDashboard, active: true },
  { label: "Tasks", icon: CheckSquare },
  { label: "Stats", icon: BarChart3 },
  { label: "Settings", icon: SettingsIcon },
] as const;

const itemSchema = z.object({
  title: z.string().min(1, "Title is required").max(200, "Too long"),
});

type ItemFormValues = z.infer<typeof itemSchema>;

function SidebarContent({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <div className="flex h-full flex-col gap-6">
      <div className="flex items-center gap-2 px-2">
        <div className="grid h-8 w-8 place-items-center rounded-md bg-primary text-primary-foreground">
          <Sparkles className="h-4 w-4" />
        </div>
        <div>
          <div className="text-sm font-semibold leading-tight">Ojas app</div>
          <div className="text-xs text-muted-foreground">React + FastAPI</div>
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

function CreateItemDialog({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isSubmitting },
  } = useForm<ItemFormValues>({
    resolver: zodResolver(itemSchema),
    defaultValues: { title: "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    try {
      const res = await fetch(`${API}/items`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(values),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      toast.success("Item created.");
      reset();
      setOpen(false);
      onCreated();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Failed to create item.",
      );
    }
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus className="h-4 w-4" />
          New item
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create a new item</DialogTitle>
          <DialogDescription>
            Saved to the FastAPI backend at <code>{API}/items</code>.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="title">Title</Label>
            <Input
              id="title"
              autoFocus
              placeholder="e.g. Ship the new landing page"
              {...register("title")}
            />
            {errors.title && (
              <p className="text-sm text-destructive">
                {errors.title.message}
              </p>
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
              {isSubmitting ? "Creating…" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function Dashboard() {
  const [items, setItems] = useState<Item[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API}/items`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: Item[] = await res.json();
      setItems(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load items.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const counts = useMemo(() => {
    const total = items.length;
    const done = items.filter((i) => i.done === 1).length;
    return { total, done, open: total - done };
  }, [items]);

  const onDelete = async (id: number) => {
    try {
      const res = await fetch(`${API}/items/${id}`, { method: "DELETE" });
      if (!res.ok && res.status !== 204) throw new Error(`HTTP ${res.status}`);
      toast.success("Item deleted.");
      void load();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Failed to delete item.",
      );
    }
  };

  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <aside className="hidden w-60 shrink-0 border-r border-border bg-card/30 p-4 md:block">
        <SidebarContent />
      </aside>

      <div className="flex min-h-screen flex-1 flex-col">
        <header
          className="sticky top-0 z-30 flex h-16 items-center gap-4 border-b border-border bg-background/80 px-4 backdrop-blur supports-[backdrop-filter]:bg-background/60"
          style={{ paddingTop: "env(safe-area-inset-top)" }}
        >
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

        <main className="flex-1 space-y-6 p-4 sm:p-6 lg:p-8">
          <div className="flex flex-wrap items-end justify-between gap-2">
            <div>
              <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
                Welcome to your app
              </h2>
              <p className="text-muted-foreground">
                Live data from <code>{API}/items</code>. Replace{" "}
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                  src/components/dashboard.tsx
                </code>{" "}
                with your real UI.
              </p>
            </div>
            <CreateItemDialog onCreated={() => void load()} />
          </div>

          {error && (
            <div className="rounded-md border border-destructive/50 bg-destructive/10 p-4 text-sm text-destructive">
              Could not reach the backend: {error}. Make sure the FastAPI
              service is running.
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {[
              { label: "Total", value: counts.total, icon: Database, accent: "text-muted-foreground" },
              { label: "Done", value: counts.done, icon: CheckCircle2, accent: "text-success" },
              { label: "Open", value: counts.open, icon: TrendingUp, accent: "text-primary" },
            ].map((s, i) => {
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
                      <Icon className={`h-4 w-4 ${s.accent}`} />
                    </CardHeader>
                    <CardContent>
                      {loading ? (
                        <Skeleton className="h-8 w-20" />
                      ) : (
                        <div className="text-2xl font-bold tracking-tight">
                          {s.value}
                        </div>
                      )}
                      <p className="mt-1 text-xs text-muted-foreground">
                        <span className={s.accent}>Live from /api/items</span>
                      </p>
                    </CardContent>
                  </Card>
                </motion.div>
              );
            })}
          </div>

          <Card>
            <CardHeader className="flex flex-row items-center justify-between">
              <div>
                <CardTitle>Items</CardTitle>
                <CardDescription>
                  Created via POST, removed via DELETE.
                </CardDescription>
              </div>
              <ListTodo className="h-5 w-5 text-muted-foreground" />
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="space-y-2">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              ) : items.length === 0 ? (
                <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
                  No items yet — click <strong>New item</strong> to add one.
                </div>
              ) : (
                <ul className="divide-y divide-border overflow-hidden rounded-md border border-border">
                  <AnimatePresence initial={false}>
                    {items.map((it) => (
                      <motion.li
                        key={it.id}
                        layout
                        initial={{ opacity: 0, x: -8 }}
                        animate={{ opacity: 1, x: 0 }}
                        exit={{ opacity: 0, x: 8 }}
                        transition={{ duration: 0.18 }}
                        className="flex items-center gap-3 bg-card px-4 py-3 text-sm"
                      >
                        <span className="flex-1 truncate">{it.title}</span>
                        <span
                          className={
                            "rounded-full px-2 py-0.5 text-xs " +
                            (it.done === 1
                              ? "bg-success/15 text-success"
                              : "bg-muted text-muted-foreground")
                          }
                        >
                          {it.done === 1 ? "done" : "open"}
                        </span>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => void onDelete(it.id)}
                          aria-label={`Delete "${it.title}"`}
                          className="text-muted-foreground hover:text-destructive"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </motion.li>
                    ))}
                  </AnimatePresence>
                </ul>
              )}
            </CardContent>
          </Card>
        </main>
      </div>
    </div>
  );
}
