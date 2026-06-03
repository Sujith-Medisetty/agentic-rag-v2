// TurnFooter — one quiet line in em-dashes summarising the finished turn.
//
//     ── 5 tools · 23.4s · 3.4k tok · $0.07 ──
//
// Centered, subtle text, no border chrome. Mirrors how a terminal closes a
// section with a faint divider.

import type { TurnSummary } from "@/lib/types";
import { formatTokens, formatCost, formatDuration } from "@/lib/format";

export default function TurnFooter({ summary }: { summary: TurnSummary }) {
  const parts: string[] = [];
  parts.push(`${summary.tools_used} tool${summary.tools_used === 1 ? "" : "s"}`);
  parts.push(formatDuration(summary.duration_ms));
  const totalTok = summary.input_tokens + summary.output_tokens;
  if (totalTok > 0) {
    parts.push(`${formatTokens(totalTok)} tok`);
  }
  if (summary.cache_read_tokens > 0) {
    parts.push(`${formatTokens(summary.cache_read_tokens)} cache`);
  }
  if (summary.cost_usd > 0) {
    parts.push(formatCost(summary.cost_usd));
  }
  return (
    <div className="mt-3 text-center text-tx-xs text-subtle">
      ── {parts.join(" · ")} ──
    </div>
  );
}
