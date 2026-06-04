// Sticky plan panel — pinned above the chat scroll area, shows the current
// TodoWrite state. Collapses to a one-line summary when there are >5 items;
// click the header to expand. Auto-hides when there are zero todos OR when
// every item is completed (cleaner UX — once the work is done there's no
// reason for a static "all green" widget to keep occupying chrome).

import { useState } from "react";
import type { TodoItem } from "@/lib/types";

const ICON: Record<TodoItem["status"], string> = {
  completed: "✓",
  in_progress: "•",
  pending: "○",
};

const COLOR: Record<TodoItem["status"], string> = {
  completed: "text-success",
  in_progress: "text-accent",
  pending: "text-muted",
};

export default function PlanPanel({ items }: { items: TodoItem[] }) {
  // Default: collapsed if the list is long. User can toggle.
  const [open, setOpen] = useState<boolean | null>(null);
  if (!items || items.length === 0) return null;

  const done = items.filter((t) => t.status === "completed").length;
  const inProgress = items.filter((t) => t.status === "in_progress").length;
  const pending = items.filter((t) => t.status === "pending").length;

  // Hide once the plan is fully done. The events log still keeps every
  // todo_update for replay; this is a pure presentation choice so the
  // chrome doesn't carry a stale "all green" widget after the work is over.
  // The panel reappears automatically if the agent emits a new TodoWrite
  // (e.g. starting a follow-up phase).
  if (done === items.length && inProgress === 0 && pending === 0) {
    return null;
  }

  // If the user hasn't toggled, default to expanded for small lists.
  const expanded = open == null ? items.length <= 5 : open;

  return (
    <div className="border-b border-border/70 bg-elevated/60 backdrop-blur-md">
      <button
        type="button"
        onClick={() => setOpen(!expanded)}
        className="flex w-full items-center justify-between px-4 py-2 text-left transition-colors hover:bg-surface/60"
      >
        <div className="flex items-center gap-3 text-sm">
          <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted">
            Plan
          </span>
          <span className="text-success">{done} done</span>
          {inProgress > 0 && (
            <span className="text-accent">{inProgress} in progress</span>
          )}
          {pending > 0 && (
            <span className="text-muted">{pending} pending</span>
          )}
        </div>
        <span className="text-muted">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && (
        <ul className="space-y-0.5 px-4 pb-3">
          {items.map((t, i) => (
            <li
              key={`${i}-${t.content}`}
              className="flex items-baseline gap-2 text-sm"
            >
              <span className={`${COLOR[t.status]} w-3`}>{ICON[t.status]}</span>
              <span
                className={
                  t.status === "completed"
                    ? "text-muted line-through"
                    : t.status === "in_progress"
                      ? "text-text"
                      : "text-muted"
                }
              >
                {t.status === "in_progress" ? t.activeForm : t.content}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
