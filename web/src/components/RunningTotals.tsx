// RunningTotals — pinned chip in the chat header showing session-wide totals
// computed by summing per-turn metrics.
//
// "12 turns · 184 tools · 84k in (74k cached · 10k new) / 51k out · $1.23 (in $0.04 · cache $0.004 · out $0.06)"
//
//   - The In/Out pair is the headline token count.
//   - The parenthetical splits input into cache hits (cheap, green) and
//     "new" tokens (uncached + cache_creation = the rest). Together they
//     add up to total input — so the user can see at a glance what fraction
//     of the model's input was served from the prompt cache vs paid for
//     at full price.
//   - The cost comes from the server's per-component sub-totals summed
//     across turns. The compact parenthetical (in $X · cache $Y · out $Z)
//     makes the per-component contribution visible inline; hover the chip
//     for the full (tokens × rate) math.
//   - Hidden entirely until the first turn finishes (totals.turns === 0).

import type { SessionTotals } from "@/lib/types";
import {
  formatTokens, formatCost, formatDuration,
  formatCostMath, formatCostMathMultiline, formatCostComponents,
} from "@/lib/format";
import { pricingForModel, MINIMAX_M3_PRICING } from "@/lib/pricing";

export default function RunningTotals({ totals }: { totals: SessionTotals }) {
  if (totals.turns === 0) return null;
  const total = totals.inputTokens + totals.outputTokens;
  // "new" = input tokens that were NOT served from the prompt cache. This
  // includes both uncached input AND cache_creation (newly written to
  // cache). Both are billed at the regular input rate, so grouping them
  // here matches the cost math.
  const cached = totals.cacheReadTokens ?? 0;
  const newTokens = Math.max(0, totals.inputTokens - cached);
  // Sub-totals the server gave us per turn. Default to 0 for older sessions
  // whose summaries pre-date the cost_*_usd fields — display still works,
  // just without the per-component cost breakdown.
  const cacheCost = totals.costCacheReadUsd ?? 0;
  const inCost    = totals.costInputUsd     ?? 0;
  const outCost   = totals.costOutputUsd    ?? 0;
  const hasSubCosts = cacheCost > 0 || inCost > 0 || outCost > 0;
  // Cost math — full multiline form goes in the tooltip; the chip shows
  // the compact component breakdown inline. Pricing is per-model; default
  // to MiniMax-M3 since that's what the server runs.
  const pricing = pricingForModel(null);
  const mathMultiline = formatCostMathMultiline(pricing, {
    input:      totals.inputTokens,
    output:     totals.outputTokens,
    cacheRead:  totals.cacheReadTokens,
    cacheWrite: totals.cacheWriteTokens,
  }, totals.costUsd);
  const inlineBreakdown = hasSubCosts
    ? formatCostComponents({ in: inCost, cache: cacheCost, out: outCost })
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
              `${totals.inputTokens.toLocaleString()} in `
              + `(${cached.toLocaleString()} cached · ${newTokens.toLocaleString()} new) `
              + `/ ${totals.outputTokens.toLocaleString()} out`
            }
          >
            <span className="text-text">{formatTokens(total)}</span>
            {cached > 0 && (
              <span
                className="text-success/80"
                title={`${cached.toLocaleString()} cache hits`}
              >
                {" "}({formatTokens(cached)} cached
                {newTokens > 0 && <> · <span className="text-text">{formatTokens(newTokens)} new</span></>})
              </span>
            )}
            {cached === 0 && newTokens > 0 && totals.inputTokens > 0 && (
              <span className="text-subtle" title={`${newTokens.toLocaleString()} new input tokens`}>
                {" "}({formatTokens(newTokens)} new)
              </span>
            )}
            <span className="text-subtle"> tok</span>
          </span>
        </>
      )}
      {totals.costUsd > 0 && (
        <>
          <span className="text-subtle">·</span>
          <span
            className="text-text"
            title={`${mathMultiline}\n\nRates: input $${MINIMAX_M3_PRICING.input}/M · output $${MINIMAX_M3_PRICING.output}/M · cache_read $${MINIMAX_M3_PRICING.cache_read}/M · cache_write $${MINIMAX_M3_PRICING.cache_write}/M`}
          >
            {formatCost(totals.costUsd)}
            {inlineBreakdown && (
              <span className="text-subtle" title={mathMultiline}>
                {" "}({inlineBreakdown})
              </span>
            )}
          </span>
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
