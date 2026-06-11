// Pricing — USD per million tokens for the models Ojas runs.
//
// Source of truth on the server is `memory/token_counter.py:MODEL_PRICING`.
// The web uses these constants to render the cost math visibly (the user
// wants to see `(in tokens × rate) + (cache × rate) + (out × rate)` next
// to the cost figure so the bill is auditable at a glance). When we add
// more models, the resolution rule below should be replaced by a per-turn
// `pricing` payload from the server — for now MiniMax-M3 is the only
// model in production and the rates are stable.
//
// IMPORTANT: keep these in sync with MODEL_PRICING in
// memory/token_counter.py. Drift here means the visible math won't match
// the server's authoritative cost_usd.

export interface ModelPricing {
  /** USD per 1M input tokens (uncached + cache_creation). */
  input: number;
  /** USD per 1M output tokens. */
  output: number;
  /** USD per 1M cache_read tokens (served from the prompt cache). */
  cache_read: number;
  /** USD per 1M cache_creation tokens (newly written to cache). */
  cache_write: number;
}

/** MiniMax-M3 — the default orchestrator + sub-agent model. */
export const MINIMAX_M3_PRICING: ModelPricing = {
  input:      0.30,
  output:     1.20,
  cache_read: 0.06,
  cache_write: 0.00,
};

/** Default to MiniMax-M3 — the only model in production today. */
export function pricingForModel(_model: string | undefined | null): ModelPricing {
  return MINIMAX_M3_PRICING;
}

/** Format a per-million rate as a short dollar string. `$0.30`, `$1.20`. */
export function formatRate(n: number): string {
  if (n === 0) return "free";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}
