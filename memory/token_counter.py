"""
Token counter and cost tracker.

Tracks cumulative token usage across all turns in a session.
Calculates cost in USD per model pricing.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.types import TokenUsage


# Model pricing per million tokens (USD).
#
# Two-tier lookup:
#   1. Exact key match in MODEL_PRICING (e.g. "claude-opus-4-8")
#   2. Provider-family prefix match (e.g. "claude-opus-*" → Opus pricing)
#      — used when a model is current but not yet listed by exact name, so
#      cost accounting still lands in the right ballpark instead of
#      silently mispricing as Sonnet-class.
#   3. If still unknown, _lookup_pricing() logs a warning and returns zeros
#      (was: silently used Sonnet-class prices, which wildly mispriced
#      Opus calls as ~5x cheaper than they really are).
MODEL_PRICING: dict[str, dict] = {
    # ---- Claude 4.x — current (June 2026) ----
    "claude-opus-4-8": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 3.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 3.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-6": {
        "input": 15.00,
        "output": 75.00,
        "cache_write": 3.75,
        "cache_read": 1.50,
    },
    "claude-sonnet-4-7": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 0.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 0.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5-20251001": {
        "input": 0.80,
        "output": 4.00,
        "cache_write": 0.20,
        "cache_read": 0.08,
    },
    "MiniMax-M3": {
        "input": 0.30,
        "output": 1.20,
        "cache_write": 0.00,
        "cache_read": 0.06,
    },
    "gpt-4o": {
        "input": 5.00,
        "output": 15.00,
        "cache_write": 0.00,
        "cache_read": 2.50,
    },
    "gpt-4-turbo": {
        "input": 10.00,
        "output": 30.00,
        "cache_write": 0.00,
        "cache_read": 0.00,
    },
    # Ollama / local models — free
    "llama3":         {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "codellama":      {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "mistral":        {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "deepseek-coder": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
}


# Provider-family fallback — applied when the exact model name isn't in
# MODEL_PRICING. Order matters: most specific prefix wins. Used by
# _lookup_pricing() below. Keep this aligned with the MODEL_PRICING keys.
_PROVIDER_FAMILY_PRICING: list[tuple[str, dict]] = [
    # Claude families
    ("claude-opus-4-8",  {"input": 15.00, "output": 75.00, "cache_write": 3.75, "cache_read": 1.50}),
    ("claude-opus-4-7",  {"input": 15.00, "output": 75.00, "cache_write": 3.75, "cache_read": 1.50}),
    ("claude-opus-4-6",  {"input": 15.00, "output": 75.00, "cache_write": 3.75, "cache_read": 1.50}),
    ("claude-opus-",     {"input": 15.00, "output": 75.00, "cache_write": 3.75, "cache_read": 1.50}),
    ("claude-sonnet-",   {"input": 3.00,  "output": 15.00, "cache_write": 0.75, "cache_read": 0.30}),
    ("claude-haiku-",    {"input": 0.80,  "output": 4.00,  "cache_write": 0.20, "cache_read": 0.08}),
    # OpenAI families
    ("gpt-4o-",          {"input": 5.00,  "output": 15.00, "cache_write": 0.00, "cache_read": 2.50}),
    ("gpt-4-turbo",      {"input": 10.00, "output": 30.00, "cache_write": 0.00, "cache_read": 0.00}),
    ("gpt-4",            {"input": 10.00, "output": 30.00, "cache_write": 0.00, "cache_read": 0.00}),
    # Local models — assume free
    ("llama",            {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("codellama",        {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("mistral",          {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("deepseek",         {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("qwen",             {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
]


def _lookup_pricing(model: str) -> dict:
    """Find pricing for `model` — exact match, then provider-family prefix,
    then a logged-warning zero fallback. The previous behaviour silently
    used Sonnet-class prices for any unknown model, which dramatically
    undercounted Opus (~$3/15 vs real $15/75) and similarly mispriced any
    unfamiliar name. Unknown models now show $0.00 with a one-line warning
    so the operator notices and adds the entry to MODEL_PRICING."""
    if not model:
        return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
    # 1. Exact match
    p = MODEL_PRICING.get(model)
    if p is not None:
        return p
    # 2. Provider-family prefix (most specific first)
    low = model.lower()
    for prefix, pricing in _PROVIDER_FAMILY_PRICING:
        if low.startswith(prefix):
            return pricing
    # 3. Unknown — log once, then return zeros so the cost doesn't lie.
    import logging, sys
    logging.getLogger(__name__).warning(
        "token_counter: unknown model %r — add an entry to MODEL_PRICING "
        "(or _PROVIDER_FAMILY_PRICING for a family fallback). Showing $0.00.",
        model,
    )
    print(
        f"[token_counter] WARNING: unknown model {model!r} — cost shown as $0.00. "
        f"Add an entry to MODEL_PRICING in memory/token_counter.py.",
        file=sys.stderr, flush=True,
    )
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


@dataclass
class CostEstimate:
    input_cost: float = 0.0
    output_cost: float = 0.0
    cache_write_cost: float = 0.0
    cache_read_cost: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.input_cost
            + self.output_cost
            + self.cache_write_cost
            + self.cache_read_cost
        )

    def format(self) -> str:
        if self.total == 0:
            return "free (local model)"
        # When cache_read contributed a non-trivial fraction, break it out
        # so the user can see how much prompt caching saved them. The total
        # is still the authoritative number — this is just a label.
        if self.cache_read_cost > 0 and self.cache_read_cost / max(self.total, 1e-9) > 0.05:
            return (
                f"${self.total:.4f}"
                f" (cache_read ${self.cache_read_cost:.4f})"
            )
        return f"${self.total:.4f}"


class TokenCounter:
    """Tracks cumulative token usage across all turns in a session."""

    def __init__(self, model: str):
        self.model = model
        self._input = 0
        self._output = 0
        self._cache_write = 0
        self._cache_read = 0
        self._turns = 0

    def record(self, usage: TokenUsage) -> None:
        """Record token usage from one API call."""
        self._input += usage.input_tokens
        self._output += usage.output_tokens
        self._cache_write += usage.cache_creation_tokens
        self._cache_read += usage.cache_read_tokens
        self._turns += 1

    @property
    def cumulative(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self._input,
            output_tokens=self._output,
            cache_creation_tokens=self._cache_write,
            cache_read_tokens=self._cache_read,
        )

    def cost(self) -> CostEstimate:
        """Calculate cost in USD for all tokens used so far."""
        pricing = _lookup_pricing(self.model)

        per_m = 1_000_000  # pricing is per million tokens

        return CostEstimate(
            input_cost=(self._input / per_m) * pricing["input"],
            output_cost=(self._output / per_m) * pricing["output"],
            cache_write_cost=(self._cache_write / per_m) * pricing["cache_write"],
            cache_read_cost=(self._cache_read / per_m) * pricing["cache_read"],
        )

    def summary(self) -> str:
        """Human readable summary for display."""
        c = self.cost()
        return (
            f"tokens: {self._input:,} in / {self._output:,} out"
            + (f" | cache: {self._cache_read:,} read" if self._cache_read else "")
            + f" | cost: {c.format()}"
            + f" | turns: {self._turns}"
        )

    def estimate_tokens_in_text(self, text: str) -> int:
        """Rough token estimate for a string."""
        return len(text) // 4 + 1
