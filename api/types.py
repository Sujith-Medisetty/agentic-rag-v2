"""
Data types for the API layer.
Ported from Rust: api/src/types.rs

Trimmed to the types actually used by the LangChain-based loop:
  * TokenUsage          — used by memory/token_counter.py
  * TextBlock / ToolUseContentBlock / ToolResultBlock — used by ui/slash_commands.py

The original port also carried hand-rolled stream-event types (DeltaType,
TextDelta, …), request/response types (ApiRequest, AssistantResponse, …) and the
ConversationMessage model. Those were superseded by ChatAnthropic / LangChain
message types and had zero references, so they were removed.
"""

from dataclasses import dataclass


@dataclass
class TokenUsage:
    """
    Token usage tracked per API call.
    Ported from Rust: runtime/src/usage.rs
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0   # tokens written to Anthropic cache
    cache_read_tokens: int = 0       # tokens read from Anthropic cache (cheaper)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billed_input(self) -> int:
        """Cache reads are billed at 10% so track separately."""
        return self.input_tokens + self.cache_creation_tokens


# ---------------------------------------------------------------------------
# Content blocks surfaced by the session transcript tooling (ui/slash_commands).
# ---------------------------------------------------------------------------

@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseContentBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    tool_name: str
    output: str
    is_error: bool = False
