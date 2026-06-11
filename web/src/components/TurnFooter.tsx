// TurnFooter — prominent per-turn stats card.
//
//   ┌──────────────────────────────────────────────────────────────────┐
//   │ DURATION 23.4s · TOOLS 5 · IN 3.4k (2.6k cached · 800 new) ·    │
//   │   OUT 1.2k · COST $0.04                                          │
//   └──────────────────────────────────────────────────────────────────┘
//
// Each metric is labelled so the user can read it at a glance instead of
// guessing which number is which. The IN parenthetical splits the input
// into cache hits (green) and "new" tokens (uncached + cache_creation),
// so the user can see how much of the prompt the cache served vs what
// the model had to read in fresh. Cost tooltip shows the in/out/cache
// sub-totals when the server sent them.

import type { TurnSummary } from "@/lib/types";
import { formatTokens, formatCost, formatDuration } from "@/lib/format";

export default function TurnFooter({ summary }: { summary: TurnSummary }) {
  const totalTok = summary.input_tokens + summary.output_tokens;
  const cached = summary.cache_read_tokens ?? 0;
  const newTokens = Math.max(0, summary.input_tokens - cached);
  // Sub-totals from the server (per-component cost). Optional on the type —
  // older replays won't have them, in which case the cost chip just shows
  // the total without a breakdown tooltip.
  const costIn   = summary.cost_input_usd      ?? 0;
  const costOut  = summary.cost_output_usd     ?? 0;
  const costCR   = summary.cost_cache_read_usd ?? 0;
  const costCW   = summary.cost_cache_write_usd?? 0;
  const hasSubCosts = costIn > 0 || costOut > 0 || costCR > 0 || costCW > 0;
  const costTitle = hasSubCosts
    ? `Cost split — in: ${formatCost(costIn)} · out: ${formatCost(costOut)} · cache_read: ${formatCost(costCR)} · cache_write: ${formatCost(costCW)}`
    : undefined;
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
              title={costTitle}
            />
          </>
        )}
      </div>
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
