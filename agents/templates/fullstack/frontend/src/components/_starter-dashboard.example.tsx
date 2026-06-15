import { useEffect, useMemo, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import {
  CheckCircle2,
  Database,
  ListTodo,
  Plus,
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
import { Skeleton } from "@/components/ui/skeleton";

import { API } from "@/lib/api";

/**
 * STARTER EXAMPLE — `frontend/src/components/_starter-dashboard.example.tsx`
 *
 * This is the fullstack template's default feature component,
 * wired to the FastAPI backend's `/api/items` endpoint. The
 * agent's real app replaces this:
 *
 *   1. Edit `frontend/src/App.tsx` to import + render YOUR
 *      component instead of `<StarterDashboard />`.
 *   2. Delete this file when you're done with it.
 *
 * It renders ONLY the feature content (no header, no theme
 * toggle, no install button — those live in App.tsx). The
 * agent's own components should follow the same shape: pure
 * feature content, no page chrome.
 *
 * The 404-from-/api/items detection below is the second line of
 * defence for "agent forgot to replace the starter" — App.tsx's
 * prompt rule is the first.
 */

interface Item {
  id: number;
  title: string;
  done: number; // 0 | 1 (SQLite convention)
}

const itemSchema = z.object({
  title: z.string().min(1, "Title is required").max(200, "Too long"),
});

type ItemFormValues = z.infer<typeof itemSchema>;

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
    /*
     * Radix invariant: <DialogTrigger> and <DialogContent> MUST be
     * descendants of the same <Dialog> provider. The trigger consumes
     * the context the Dialog creates; if it's a sibling instead of a
     * child, runtime throws "DialogTrigger must be used within Dialog"
     * and the whole app renders blank. Keep them under the same
     * <Dialog> — even if the trigger is in one visual location and the
     * panel in another. The calculator build on 2026-06-15 hit this
     * bug and shipped with a blank screen.
     */
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

export default function StarterDashboard() {
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
      // 404 means the agent's real backend doesn't expose /api/items
      // because they replaced this starter with their own component
      // but forgot to also delete the unused dashboard code, OR they
      // shipped a build that still mounts this example. Either way:
      // surface a clear hint instead of a generic "backend down".
      const msg = err instanceof Error ? err.message : "Failed to load items.";
      if (/404/.test(msg)) {
        setError(
          "This is the template's starter example (it calls /api/items). " +
          "If you see this, your real UI is probably in another component — " +
          "edit frontend/src/App.tsx to render your actual component instead of " +
          "<StarterDashboard />, then delete this file.",
        );
      } else {
        setError(msg);
      }
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
    <div className="space-y-6 p-4 sm:p-6 lg:p-8">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            Welcome to your app
          </h2>
          <p className="text-muted-foreground">
            Live data from <code>{API}/items</code>. Replace{" "}
            <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
              src/components/_starter-dashboard.example.tsx
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
    </div>
  );
}
