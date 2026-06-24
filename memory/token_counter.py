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
    # ---- OpenAI GPT-5 (current generation) ----
    "gpt-5":         {"input": 5.00,  "output": 20.00, "cache_write": 0.00, "cache_read": 1.25},
    "gpt-5-mini":    {"input": 1.00,  "output": 4.00,  "cache_write": 0.00, "cache_read": 0.25},
    # ---- Google Gemini (via OpenAI-compat endpoint) ----
    "gemini-2.5-pro":     {"input": 1.25, "output": 5.00,  "cache_write": 0.00, "cache_read": 0.31},
    "gemini-2.5-flash":   {"input": 0.30, "output": 1.20,  "cache_write": 0.00, "cache_read": 0.08},
    "gemini-2.0-pro":     {"input": 1.25, "output": 5.00,  "cache_write": 0.00, "cache_read": 0.31},
    # ---- DeepSeek ----
    "deepseek-chat":      {"input": 0.27, "output": 1.10,  "cache_write": 0.00, "cache_read": 0.07},
    "deepseek-reasoner":  {"input": 0.55, "output": 2.19,  "cache_write": 0.00, "cache_read": 0.14},
    # ---- Groq (mostly free at small scale; assume free for safety) ----
    "llama-3.3-70b-versatile":  {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "llama-3.1-8b-instant":     {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "mixtral-8x7b-32768":       {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    # ---- xAI Grok ----
    "grok-3":         {"input": 3.00,  "output": 15.00, "cache_write": 0.00, "cache_read": 0.75},
    "grok-3-mini":    {"input": 0.30,  "output": 0.50,  "cache_write": 0.00, "cache_read": 0.08},
    "grok-2":         {"input": 2.00,  "output": 10.00, "cache_write": 0.00, "cache_read": 0.50},
    # ---- Mistral ----
    "mistral-large-latest":   {"input": 2.00, "output": 6.00,  "cache_write": 0.00, "cache_read": 0.50},
    "mistral-small-latest":   {"input": 0.20, "output": 0.60,  "cache_write": 0.00, "cache_read": 0.05},
    "codestral-latest":       {"input": 0.30, "output": 0.90,  "cache_write": 0.00, "cache_read": 0.08},
    # ---- Together (per-model; pick a representative) ----
    "meta-llama/Llama-3.3-70B-Instruct-Turbo":  {"input": 0.88, "output": 0.88, "cache_write": 0.00, "cache_read": 0.00},
    # Ollama / local models — free
    "llama3":         {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "codellama":      {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "mistral":        {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
    "deepseek-coder": {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
}


# Provider-family fallback — applied when the exact model name isn't in
# MODEL_PRICING. Order matters: most specific prefix wins. Used by
# _lookup_pricing() below. These are TRUE PREFIXES only — exact-name matches
# are handled by MODEL_PRICING first (see _lookup_pricing), so listing a full
# model name here would be dead. Keep aligned with the MODEL_PRICING families.
_PROVIDER_FAMILY_PRICING: list[tuple[str, dict]] = [
    # Claude families
    ("claude-opus-",     {"input": 15.00, "output": 75.00, "cache_write": 3.75, "cache_read": 1.50}),
    ("claude-sonnet-",   {"input": 3.00,  "output": 15.00, "cache_write": 0.75, "cache_read": 0.30}),
    ("claude-haiku-",    {"input": 0.80,  "output": 4.00,  "cache_write": 0.20, "cache_read": 0.08}),
    # OpenAI families
    ("gpt-5-",           {"input": 1.00,  "output": 4.00,  "cache_write": 0.00, "cache_read": 0.25}),
    ("gpt-5",            {"input": 5.00,  "output": 20.00, "cache_write": 0.00, "cache_read": 1.25}),
    ("gpt-4o-",          {"input": 5.00,  "output": 15.00, "cache_write": 0.00, "cache_read": 2.50}),
    ("gpt-4-turbo",      {"input": 10.00, "output": 30.00, "cache_write": 0.00, "cache_read": 0.00}),
    ("gpt-4",            {"input": 10.00, "output": 30.00, "cache_write": 0.00, "cache_read": 0.00}),
    # Google Gemini family
    ("gemini-",          {"input": 0.30,  "output": 1.20,  "cache_write": 0.00, "cache_read": 0.08}),
    # xAI Grok family
    ("grok-",            {"input": 0.30,  "output": 0.50,  "cache_write": 0.00, "cache_read": 0.08}),
    # Local / OpenAI-compat free-tier families — assume $0
    ("llama",            {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("codellama",        {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("mistral",          {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("deepseek",         {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
    ("qwen",             {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}),
]


def _lookup_pricing(model: str) -> dict:
    """Find pricing for `model` — DB override → exact in-code match →
    provider-family prefix → logged-warning zero fallback.

    Lookup order:
      1. `model_pricing` table (admin-set via the Providers UI). An override
         here beats everything else. Results are cached in-process so the
         DB is hit at most once per model name per backend lifetime.
      2. `MODEL_PRICING` exact match (canonical in-code defaults).
      3. `_PROVIDER_FAMILY_PRICING` prefix (most specific first) — covers
         new minor versions of known providers that aren't in the exact
         dict yet.
      4. Unknown — log a one-time warning and return zeros. The previous
         behaviour silently used Sonnet-class prices for any unknown
         model, which dramatically undercounted Opus (~$3/15 vs real
         $15/75). Showing $0.00 + a warning is more honest and nudges the
         admin to add a price."""
    if not model:
        return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}

    # 1. DB override (cached in-process)
    db_price = _db_override_lookup(model)
    if db_price is not None:
        return db_price

    # 2. Exact in-code match
    p = MODEL_PRICING.get(model)
    if p is not None:
        return p
    # 3. Provider-family prefix (most specific first)
    low = model.lower()
    for prefix, pricing in _PROVIDER_FAMILY_PRICING:
        if low.startswith(prefix):
            return pricing
    # 4. Unknown — log once, then return zeros so the cost doesn't lie.
    import logging, sys
    logging.getLogger(__name__).warning(
        "token_counter: unknown model %r — add an entry to MODEL_PRICING "
        "(or _PROVIDER_FAMILY_PRICING for a family fallback), or set a price "
        "via the Providers admin page. Showing $0.00.",
        model,
    )
    print(
        f"[token_counter] WARNING: unknown model {model!r} — cost shown as $0.00. "
        f"Add an entry to MODEL_PRICING in memory/token_counter.py, or set a price "
        f"via the Providers admin page.",
        file=sys.stderr, flush=True,
    )
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


# Cache of DB-sourced pricing lookups. Key = model name. Value = the
# pricing dict (when found) or the sentinel `False` (when the table has
# no row for that model — distinguishes "looked up and found nothing"
# from "never looked up"). Avoids hitting SQLite on every record() call.
_db_price_cache: dict[str, dict | bool] = {}


def _db_override_lookup(model: str) -> dict | None:
    """Return an admin-set price for `model`, or None if no override
    exists. Cached in-process so the DB is hit at most once per name.
    Imported lazily — `server.db` would create a cycle on cold start if
    pulled in at module top."""
    cached = _db_price_cache.get(model)
    if cached is False:
        return None
    if cached:
        return cached
    try:
        from server import db as _db
        row = _db.get_model_price(model)
    except Exception:
        # If the DB is unreachable, fall through to the in-code defaults
        # — never let pricing lookup break the agent loop.
        return None
    if row is None:
        _db_price_cache[model] = False
        return None
    pricing = {
        "input":       float(row["input"]),
        "output":      float(row["output"]),
        "cache_write": float(row.get("cache_write") or 0),
        "cache_read":  float(row.get("cache_read") or 0),
    }
    _db_price_cache[model] = pricing
    return pricing


def invalidate_price_cache() -> None:
    """Clear the in-process DB-override cache. Called when the admin
    updates pricing via the Providers page so the next LLM call's cost
    uses the new value without a backend restart."""
    _db_price_cache.clear()


def lookup_pricing_silent(model: str) -> dict | None:
    """Like `_lookup_pricing`, but never logs warnings. Used by the
    admin UI to enumerate the catalog without spamming the log for every
    model that doesn't have a built-in price.

    DB override > exact MODEL_PRICING > family prefix > None.
    Returns None if no entry exists."""
    if not model:
        return None
    db_price = _db_override_lookup(model)
    if db_price is not None:
        return db_price
    p = MODEL_PRICING.get(model)
    if p is not None:
        return p
    low = model.lower()
    for prefix, pricing in _PROVIDER_FAMILY_PRICING:
        if low.startswith(prefix):
            return pricing
    return None


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
        """Calculate cost in USD for all tokens used so far.

        `self._input` is the GROSS input count (cache_read + cache_creation
        INCLUDED) — that's the number the UI displays and the diff it shows
        per turn. For COST, though, the cached portions must be billed at
        their own (cheaper) rates, not the full input rate, so we subtract
        them out and price only the truly-new input here. This mirrors the
        web client's formatCostMath (`new = input − cache_read − cache_write`)
        exactly, so the server's authoritative cost_usd and the UI's visible
        token×rate math finally agree. Billing the gross input at the full
        rate AND the cache counts separately (the old behaviour) double-charged
        every cached token — inflating the cost the user saw.
        """
        pricing = _lookup_pricing(self.model)

        per_m = 1_000_000  # pricing is per million tokens

        billable_input = max(0, self._input - self._cache_read - self._cache_write)

        return CostEstimate(
            input_cost=(billable_input / per_m) * pricing["input"],
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
