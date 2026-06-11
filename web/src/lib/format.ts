// Tiny formatting helpers shared across the metric components.
// Kept here (vs inline) so the UI feels consistent across TurnFooter,
// RunningTotals, BranchBadge, etc.

import type { ModelPricing } from "./pricing";
import { formatRate } from "./pricing";

/** 1234 → "1.2k", 1234567 → "1.2M". Plain integers under 1000 unchanged. */
export function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + "k";
  return (n / 1_000_000).toFixed(1) + "M";
}

/** USD; under $0.01 shown to 4 decimals so very-small turns aren't "$0.00". */
export function formatCost(usd: number): string {
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  if (usd < 1)    return `$${usd.toFixed(3)}`;
  return `$${usd.toFixed(2)}`;
}

/** 1230 → "1.2s", 90000 → "1m 30s". Always rounds DOWN to avoid jitter. */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// ============================================================================
// Cost math — render the (tokens × rate) multiplication that produced the
// cost figure, so the bill is auditable at a glance.
// ============================================================================

export interface CostTokens {
  input: number;        // total input tokens (uncached + cache_read + cache_creation)
  output: number;
  cacheRead: number;    // cache_read tokens (served from cache, cheap)
  cacheWrite: number;   // cache_creation tokens (newly written, M3 = 0)
}

/**
 * Per-component cost sub-totals. Optional so older replays that pre-date
 * the cost_*_usd fields just produce a $0.00 / no-breakdown display.
 */
export interface CostSubtotals {
  in: number;       // cost of (input - cache_read) — the "new" part
  cache: number;    // cost of cache_read tokens
  out: number;
  write?: number;   // cost of cache_creation tokens (M3 = 0, Claude > 0)
}

/**
 * Format the cost math visibly: `(87k new × $0.30) + (1.6M cached × $0.06) + (18k out × $1.20)`.
 * Drops zero/empty terms. Used as the inline parenthetical on the cost
 * stat, with a tooltip variant below for the full version.
 */
export function formatCostMath(
  pricing: ModelPricing,
  tokens: CostTokens,
): string {
  const newTokens = Math.max(0, tokens.input - tokens.cacheRead - tokens.cacheWrite);
  const parts: string[] = [];
  if (newTokens > 0 && pricing.input > 0) {
    parts.push(`(${formatTokens(newTokens)} new × ${formatRate(pricing.input)})`);
  }
  if (tokens.cacheRead > 0 && pricing.cache_read > 0) {
    parts.push(`(${formatTokens(tokens.cacheRead)} cached × ${formatRate(pricing.cache_read)})`);
  }
  if (tokens.cacheWrite > 0 && pricing.cache_write > 0) {
    parts.push(`(${formatTokens(tokens.cacheWrite)} write × ${formatRate(pricing.cache_write)})`);
  }
  if (tokens.output > 0 && pricing.output > 0) {
    parts.push(`(${formatTokens(tokens.output)} out × ${formatRate(pricing.output)})`);
  }
  return parts.join(" + ");
}

/**
 * Compact cost split: `in $0.04 · cache $0.004 · out $0.06`. Drops zero
 * terms. Used on the session chip where the full math doesn't fit.
 */
export function formatCostComponents(subtotals: CostSubtotals): string {
  const parts: string[] = [];
  if (subtotals.in > 0)       parts.push(`in ${formatCost(subtotals.in)}`);
  if (subtotals.cache > 0)    parts.push(`cache ${formatCost(subtotals.cache)}`);
  if (subtotals.write && subtotals.write > 0) parts.push(`write ${formatCost(subtotals.write)}`);
  if (subtotals.out > 0)      parts.push(`out ${formatCost(subtotals.out)}`);
  return parts.join(" · ");
}

/**
 * Multi-line math breakdown for tooltips — one term per line, ending with
 * the total. Useful when the inline form gets truncated on narrow screens.
 */
export function formatCostMathMultiline(
  pricing: ModelPricing,
  tokens: CostTokens,
  totalUsd: number,
): string {
  const newTokens = Math.max(0, tokens.input - tokens.cacheRead - tokens.cacheWrite);
  const lines: string[] = [];
  if (newTokens > 0 && pricing.input > 0) {
    lines.push(`${newTokens.toLocaleString()} new × ${formatRate(pricing.input)} = ${formatCost((newTokens / 1_000_000) * pricing.input)}`);
  }
  if (tokens.cacheRead > 0 && pricing.cache_read > 0) {
    lines.push(`${tokens.cacheRead.toLocaleString()} cached × ${formatRate(pricing.cache_read)} = ${formatCost((tokens.cacheRead / 1_000_000) * pricing.cache_read)}`);
  }
  if (tokens.cacheWrite > 0 && pricing.cache_write > 0) {
    lines.push(`${tokens.cacheWrite.toLocaleString()} write × ${formatRate(pricing.cache_write)} = ${formatCost((tokens.cacheWrite / 1_000_000) * pricing.cache_write)}`);
  }
  if (tokens.output > 0 && pricing.output > 0) {
    lines.push(`${tokens.output.toLocaleString()} out × ${formatRate(pricing.output)} = ${formatCost((tokens.output / 1_000_000) * pricing.output)}`);
  }
  lines.push(`= ${formatCost(totalUsd)} total`);
  return lines.join("\n");
}

