// RunningTotals — pinned chip in the chat header showing session-wide totals
// computed by summing per-turn metrics.
//
// "12 turns · 184 tools · 84k in / 51k out · $1.23"
// Hidden entirely until the first turn finishes (totals.turns === 0).

import type { SessionTotals } from "@/lib/types";
import { formatTokens, formatCost, formatDuration } from "@/lib/format";

export default function RunningTotals({ totals }: { totals: SessionTotals }) {
  if (totals.turns === 0) return null;
  const total = totals.inputTokens + totals.outputTokens;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 rounded-md border border-border bg-elevated/50 px-2 py-1 text-[11px] text-muted">
      <span className="text-text">{totals.turns}</span>
      <span className="text-subtle">turn{totals.turns === 1 ? "" : "s"}</span>
      <span className="text-subtle">·</span>
      <span className="text-text">{totals.tools}</span>
      <span className="text-subtle">tools</span>
      {total > 0 && (
        <>
          <span className="text-subtle">·</span>
          <span title={`${totals.inputTokens.toLocaleString()} in / ${totals.outputTokens.toLocaleString()} out`}>
            <span className="text-text">{formatTokens(total)}</span>
            <span className="text-subtle"> tok</span>
          </span>
        </>
      )}
      {totals.costUsd > 0 && (
        <>
          <span className="text-subtle">·</span>
          <span className="text-text">{formatCost(totals.costUsd)}</span>
        </>
      )}
      {totals.durationMs > 0 && (
        <>
          <span className="text-subtle">·</span>
          <span className="text-subtle">{formatDuration(totals.durationMs)}</span>
        </>
      )}
    </div>
  );
}
