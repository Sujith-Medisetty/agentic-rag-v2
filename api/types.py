"""
Data types for the API layer.

Trimmed to what the LangChain-based loop actually uses:
  * TokenUsage — used by memory/token_counter.py
"""

from dataclasses import dataclass


@dataclass
class TokenUsage:
    """Token usage tracked per API call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0   # tokens written to Anthropic cache
    cache_read_tokens: int = 0       # tokens read from Anthropic cache (cheaper)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billed_input(self) -> int:
        """Cache reads are billed at 10%; track separately."""
        return self.input_tokens + self.cache_creation_tokens
