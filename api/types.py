"""
Data types for the API layer.

Trimmed to what the LangChain-based loop actually uses:
  * TokenUsage — used by memory/token_counter.py
"""

from dataclasses import dataclass


@dataclass
class TokenUsage:
    """Token usage tracked per API call.

    `input_tokens` is the GROSS prompt size — cache_read + cache_creation are
    SUBSETS of it (OpenAI/MiniMax `prompt_tokens` and current langchain
    normalisation both report it this way). TokenCounter.cost() subtracts the
    cached portions before pricing the remainder at the input rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0   # tokens written to the prompt cache
    cache_read_tokens: int = 0       # tokens read from the prompt cache (cheaper)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens
