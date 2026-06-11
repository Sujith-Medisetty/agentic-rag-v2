// Sticky plan panel — pinned above the chat scroll area, shows the current
// TodoWrite state. NEVER hides while a plan has been emitted (the user needs
// to see the final "all done" state, not have the panel disappear).
// Collapses to a one-line summary when there are >5 items; click the header
// to expand. When the session is mid-work but no plan exists yet, shows a
// "no plan yet" hint so the missing state is visible instead of silent.

import { useState } from "react";
import type { TodoItem } from "@/lib/types";

const ICON: Record<TodoItem["status"], string> = {
  completed: "✓",
  in_progress: "▸",
  pending: "○",
};

const COLOR: Record<TodoItem["status"], string> = {
  completed: "text-success",
  in_progress: "text-accent",
  pending: "text-muted",
};

export default function PlanPanel({
  items,
  hasWorkStarted = false,
}: {
  items: TodoItem[];
  /** True once the agent has actually started working (any tool call, or
   *  a long user prompt). Used to decide whether to show the "no plan yet"
   *  hint. False on a fresh empty session. */
  hasWorkStarted?: boolean;
}) {
  // Default: collapsed if the list is long. User can toggle.
  const [open, setOpen] = useState<boolean | null>(null);

  const done = items.filter((t) => t.status === "completed").length;
  const inProgress = items.filter((t) => t.status === "in_progress").length;
  const pending = items.filter((t) => t.status === "pending").length;
  const allDone = items.length > 0 && done === items.length;

  // "No plan yet" hint: the session is actively working (tool calls have
  // started, OR the user has sent a non-trivial prompt) but the agent hasn't
  // emitted a TodoWrite. Without this, the user can't tell whether the agent
  // skipped planning or the system is broken. Renders as a single-line strip
  // with a subtle pulse so it's noticeable but not noisy.
  if (items.length === 0) {
    if (!hasWorkStarted) return null;
    return (
      <div className="border-b border-border/70 bg-elevated/60 backdrop-blur-md">
        <div className="flex items-center gap-2 px-4 py-1.5 text-xs text-muted">
          <span className="inline-block h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
          <span className="font-semibold uppercase tracking-[0.18em]">
            Plan
          </span>
          <span>— agent hasn&apos;t planned this task yet</span>
        </div>
      </div>
    );
  }

  // "Step N of M" — what the user is most likely to glance at. N is the
  // first in_progress index (1-based); M is the total. When everything is
  // done, show "N/M done" instead. When nothing is in progress, show
  // "X done, Y pending".
  const firstInProgressIdx = items.findIndex(
    (t) => t.status === "in_progress",
  );
  const stepLabel = allDone
    ? `${done}/${items.length} done`
    : firstInProgressIdx >= 0
      ? `Step ${firstInProgressIdx + 1} of ${items.length}`
      : `${done} done · ${pending} pending`;

  // If the user hasn't toggled, default to expanded for small lists.
  const expanded = open == null ? items.length <= 5 : open;

  return (
    <div
      className={
        allDone
          ? "border-b border-border/70 bg-elevated/40 backdrop-blur-md"
          : "border-b border-border/70 bg-elevated/60 backdrop-blur-md"
      }
    >
      <button
        type="button"
        onClick={() => setOpen(!expanded)}
        className="flex w-full items-center justify-between px-4 py-2 text-left transition-colors hover:bg-surface/60"
      >
        <div className="flex items-center gap-3 text-sm">
          <span className="text-[11px] font-semibold uppercase tracking-[0.18em] text-muted">
            Plan
          </span>
          <span
            className={
              allDone
                ? "font-semibold text-success"
                : "font-semibold text-text"
            }
          >
            {stepLabel}
          </span>
          {inProgress > 0 && (
            <span className="text-accent">{inProgress} in progress</span>
          )}
          {pending > 0 && !allDone && (
            <span className="text-muted">{pending} pending</span>
          )}
        </div>
        <span className="text-muted">{expanded ? "▾" : "▸"}</span>
      </button>
      {expanded && (
        <ul className="space-y-0.5 px-4 pb-3">
          {items.map((t, i) => {
            const isActive = t.status === "in_progress";
            return (
              <li
                key={`${i}-${t.content}`}
                className={
                  isActive
                    ? "flex items-stretch gap-2 rounded-sm border-l-2 border-accent bg-accent/5 py-1 pl-2 pr-2 text-sm"
                    : "flex items-baseline gap-2 text-sm"
                }
              >
                <span
                  className={
                    isActive
                      ? "inline-block h-1.5 w-1.5 animate-pulse-soft self-center rounded-full bg-accent"
                      : `${COLOR[t.status]} w-3 self-baseline`
                  }
                >
                  {isActive ? "" : ICON[t.status]}
                </span>
                <span
                  className={
                    t.status === "completed"
                      ? "text-muted line-through"
                      : isActive
                        ? "font-semibold text-accent"
                        : "text-muted"
                  }
                >
                  {isActive ? t.activeForm : t.content}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
