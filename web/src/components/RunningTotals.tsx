// RunningTotals — pinned chip in the chat header showing session-wide totals
// computed by summing per-turn metrics.
//
// "12 turns · 184 tools · 84k in (74k cached) / 51k out · $1.23"
// The cached parenthetical + tooltip shows the dollar savings from prompt
// cache hits so the user can see how much the cache is saving them.
// Hidden entirely until the first turn finishes (totals.turns === 0).

import type { SessionTotals } from "@/lib/types";
import { formatTokens, formatCost, formatDuration } from "@/lib/format";

// MiniMax-M3 cache_read rate (USD per 1M tokens). Used to surface the
// dollar savings in the session chip title. If/when the model changes,
// pull this from a shared modelPricing constant — for now M3 is the only
// model we run and the value is stable.
const CACHE_READ_USD_PER_M = 0.06;
// And the regular input rate, for the "would have been" comparison.
const INPUT_USD_PER_M = 0.30;

export default function RunningTotals({ totals }: { totals: SessionTotals }) {
  if (totals.turns === 0) return null;
  const total = totals.inputTokens + totals.outputTokens;
  // Cache savings = tokens that hit cache_read (cheap) instead of full input
  // (expensive). The savings = (input_rate - cache_read_rate) * cached / 1M.
  const cached = totals.cacheReadTokens ?? 0;
  const cachedM = cached / 1_000_000;
  const cachedSavedUsd = cachedM * (INPUT_USD_PER_M - CACHE_READ_USD_PER_M);
  const cachedCostUsd = cachedM * CACHE_READ_USD_PER_M;
  const cacheSummary = cached > 0
    ? ` · ${cached.toLocaleString()} cached (saved $${cachedSavedUsd.toFixed(4)}, paid $${cachedCostUsd.toFixed(4)})`
    : "";
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
          <span
            title={
              `${totals.inputTokens.toLocaleString()} in (${totals.cacheReadTokens.toLocaleString()} cached, saved $${cachedSavedUsd.toFixed(4)}) / `
              + `${totals.outputTokens.toLocaleString()} out`
            }
          >
            <span className="text-text">{formatTokens(total)}</span>
            {cached > 0 && (
              <span className="text-success/80" title={cacheSummary}>
                {" "}({formatTokens(cached)} cached)
              </span>
            )}
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
