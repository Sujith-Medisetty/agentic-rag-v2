// TurnFooter — prominent per-turn stats card.
//
//   ┌──────────────────────────────────────────────────────────────────────┐
//   │ DURATION 23.4s · TOOLS 5 · IN 3.4k (2.6k cached · 800 new) ·         │
//   │   OUT 1.2k · COST $0.04                                              │
//   │     (800 new × $0.30) + (2.6k cached × $0.06) + (1.2k out × $1.20)   │
//   └──────────────────────────────────────────────────────────────────────┘
//
// Each metric is labelled so the user can read it at a glance instead of
// guessing which number is which. The IN parenthetical splits the input
// into cache hits (green) and "new" tokens (uncached + cache_creation),
// so the user can see how much of the prompt the cache served vs what
// the model had to read in fresh. The COST sub-line shows the full cost
// math — tokens × rate for each component — so the bill is auditable
// without having to click a tooltip.

import type { TurnSummary } from "@/lib/types";
import { formatTokens, formatCost, formatDuration, formatCostMath, formatCostMathMultiline } from "@/lib/format";
import { pricingForModel, MINIMAX_M3_PRICING } from "@/lib/pricing";

export default function TurnFooter({ summary }: { summary: TurnSummary }) {
  const totalTok = summary.input_tokens + summary.output_tokens;
  const cached = summary.cache_read_tokens ?? 0;
  const newTokens = Math.max(0, summary.input_tokens - cached);
  // Pricing is per-model; we don't have it on the wire yet (TODO: send from
  // server), so default to MiniMax-M3. When the server starts emitting a
  // per-turn `pricing` field, this is the line to swap.
  const pricing = pricingForModel(null);
  // Sub-totals from the server (per-component cost). Optional on the type —
  // older replays won't have them, in which case the cost chip just shows
  // the total without a breakdown.
  const costIn   = summary.cost_input_usd      ?? 0;
  const costOut  = summary.cost_output_usd     ?? 0;
  const costCR   = summary.cost_cache_read_usd ?? 0;
  const costCW   = summary.cost_cache_write_usd?? 0;
  // Cost math — visible inline as the parenthetical, full version in
  // tooltip. Built from the rates × the same token counts the IN/OUT
  // stats show, so the multiplication is verifiable by eye.
  const mathString = formatCostMath(pricing, {
    input:      summary.input_tokens,
    output:     summary.output_tokens,
    cacheRead:  summary.cache_read_tokens,
    cacheWrite: summary.cache_write_tokens,
  });
  const mathMultiline = formatCostMathMultiline(pricing, {
    input:      summary.input_tokens,
    output:     summary.output_tokens,
    cacheRead:  summary.cache_read_tokens,
    cacheWrite: summary.cache_write_tokens,
  }, summary.cost_usd);
  return (
    <div className="mt-5 rounded-lg border border-border/70 bg-elevated/40 px-3 py-2 font-sans backdrop-blur-sm">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-tx-xs">
        <Stat label="Duration" value={formatDuration(summary.duration_ms)} />
        <Divider />
        <Stat
          label="Tools"
          value={String(summary.tools_used)}
          valueClass="text-text"
        />
        {totalTok > 0 && (
          <>
            <Divider />
            <Stat
              label="In"
              value={formatTokens(summary.input_tokens)}
              valueClass="text-accent"
              title={
                cached > 0 || newTokens > 0
                  ? `${summary.input_tokens.toLocaleString()} in · ${cached.toLocaleString()} cached · ${newTokens.toLocaleString()} new`
                  : undefined
              }
              after={
                cached > 0 ? (
                  <span className="text-success/80" title={`${cached.toLocaleString()} cache hits`}>
                    {" "}({formatTokens(cached)} cached
                    {newTokens > 0 && <> · <span className="text-text">{formatTokens(newTokens)} new</span></>})
                  </span>
                ) : newTokens > 0 ? (
                  <span className="text-subtle"> ({formatTokens(newTokens)} new)</span>
                ) : null
              }
            />
            <Stat
              label="Out"
              value={formatTokens(summary.output_tokens)}
              valueClass="text-accent-2"
            />
          </>
        )}
        {summary.cost_usd > 0 && (
          <>
            <Divider />
            <Stat
              label="Cost"
              value={formatCost(summary.cost_usd)}
              valueClass="text-text"
              // Full math (one term per line) for hover-to-inspect; the
              // visible part below the value already shows the inline form.
              title={mathMultiline}
            />
          </>
        )}
      </div>
      {summary.cost_usd > 0 && mathString && (
        <div
          className="mt-1.5 font-mono text-[10px] leading-snug text-subtle"
          title={`${mathMultiline}\n\nRates: input $${MINIMAX_M3_PRICING.input}/M · output $${MINIMAX_M3_PRICING.output}/M · cache_read $${MINIMAX_M3_PRICING.cache_read}/M · cache_write $${MINIMAX_M3_PRICING.cache_write}/M`}
        >
          = {mathString}
        </div>
      )}
    </div>
  );
}

function Stat({
  label, value, valueClass = "text-text", title, after,
}: {
  label: string;
  value: string;
  valueClass?: string;
  title?: string;
  after?: React.ReactNode;
}) {
  return (
    <div className="inline-flex items-baseline gap-1.5" title={title}>
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-subtle">
        {label}
      </span>
      <span className={`font-mono text-tx-sm ${valueClass}`}>{value}</span>
      {after}
    </div>
  );
}

function Divider() {
  return <span className="text-subtle">·</span>;
}
