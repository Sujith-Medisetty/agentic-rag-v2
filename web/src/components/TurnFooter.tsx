// TurnFooter — prominent per-turn stats card.
//
//   ┌─────────────────────────────────────────────────────────────────┐
//   │ DURATION 23.4s · TOOLS 5 · IN 3.4k · OUT 1.2k · CACHE 800 · $.07 │
//   └─────────────────────────────────────────────────────────────────┘
//
// Each metric is labelled so the user can read it at a glance instead of
// guessing which number is which. Mono digits, sans labels, distinct colors
// for in/out so the eye splits them automatically.

import type { TurnSummary } from "@/lib/types";
import { formatTokens, formatCost, formatDuration } from "@/lib/format";

export default function TurnFooter({ summary }: { summary: TurnSummary }) {
  const totalTok = summary.input_tokens + summary.output_tokens;
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
            />
            <Stat
              label="Out"
              value={formatTokens(summary.output_tokens)}
              valueClass="text-accent-2"
            />
          </>
        )}
        {summary.cache_read_tokens > 0 && (
          <>
            <Divider />
            <Stat
              label="Cache"
              value={formatTokens(summary.cache_read_tokens)}
              valueClass="text-success"
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
            />
          </>
        )}
      </div>
    </div>
  );
}

function Stat({
  label, value, valueClass = "text-text",
}: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="inline-flex items-baseline gap-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-subtle">
        {label}
      </span>
      <span className={`font-mono text-tx-sm ${valueClass}`}>{value}</span>
    </div>
  );
}

function Divider() {
  return <span className="text-subtle">·</span>;
}
