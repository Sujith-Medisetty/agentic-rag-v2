"""
Token counter and cost tracker.

Tracks cumulative token usage across all turns in a session.
Calculates cost in USD per model pricing.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.types import TokenUsage


# Model pricing per million tokens (USD).
MODEL_PRICING: dict[str, dict] = {
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
        pricing = MODEL_PRICING.get(self.model)
        if not pricing:
            # unknown model — estimate using a mid-tier price
            pricing = {"input": 3.0, "output": 15.0, "cache_write": 0, "cache_read": 0}

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
