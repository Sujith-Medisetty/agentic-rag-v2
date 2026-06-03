"""
Compaction-aware LangGraph checkpointer.

Uses SqliteSaver (not MemorySaver) so checkpoints survive restarts.
Same workspace → same thread_id → resumes from last phase on restart.

When the estimated context exceeds the auto-compaction threshold → summarise
old messages → continue.

 - runtime/src/conversation.rs (auto-compaction trigger: 100_000 input tokens,
 env CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS)
 - runtime/src/compact.rs (CompactionConfig: preserve_recent=4,
 max_estimated_tokens=10_000; estimate_message_tokens = len/4 + 1 per block)
"""

import os
from pathlib import Path
from typing import Any
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

# Auto-compaction trigger — rs:
# DEFAULT_AUTO_COMPACTION_INPUT_TOKENS_THRESHOLD = 100_000
# AUTO_COMPACTION_THRESHOLD_ENV_VAR = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"
DEFAULT_AUTO_COMPACT_INPUT_TOKENS = 100_000
AUTO_COMPACT_THRESHOLD_ENV_VAR = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"

CHARS_PER_TOKEN = 4
PRESERVE_RECENT = 4  # CompactionConfig.preserve_recent_messages

# COMPACT_DIRECT_RESUME_INSTRUCTION (compact.rs)
COMPACT_PREAMBLE = (
    "This session is being continued from a previous conversation that ran out "
    "of context. The summary below covers the earlier portion of the "
    "conversation.\n\n"
)
COMPACT_RECENT_MESSAGES_NOTE = "Recent messages are preserved verbatim."
COMPACT_DIRECT_RESUME_INSTRUCTION = (
    "Continue the conversation from where it left off without asking the user "
    "any further questions. Resume directly — do not acknowledge the summary, "
    "do not recap what was happening, and do not preface with continuation text."
)

def _auto_compact_threshold() -> int:
    """Auto-compaction token threshold."""
    raw = os.getenv(AUTO_COMPACT_THRESHOLD_ENV_VAR)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_AUTO_COMPACT_INPUT_TOKENS

def _estimate_block_tokens(block: Any) -> int:
    """Per content-block estimate.
    every block contributes `len // 4 + 1`."""
    if isinstance(block, str):
        return len(block) // CHARS_PER_TOKEN + 1
    if isinstance(block, dict):
        btype = block.get("type")
        if btype == "tool_use":
            name = str(block.get("name", ""))
            inp = str(block.get("input", ""))
            return (len(name) + len(inp)) // CHARS_PER_TOKEN + 1
        if btype == "tool_result":
            name = str(block.get("name", block.get("tool_name", "")))
            out = str(block.get("content", block.get("output", "")))
            return (len(name) + len(out)) // CHARS_PER_TOKEN + 1
        # text / thinking / other dict blocks
        text = str(block.get("text", block.get("thinking", "")))
        if text:
            return len(text) // CHARS_PER_TOKEN + 1
        return len(str(block)) // CHARS_PER_TOKEN + 1
    return len(str(block)) // CHARS_PER_TOKEN + 1

def _estimate_tokens(messages: list) -> int:
    """Estimate the token footprint of a message list.

    `len // 4 + 1`.
    """
    total = 0
    for msg in messages:
        content = msg.content if hasattr(msg, "content") else str(msg)
        if isinstance(content, list):
            for block in content:
                total += _estimate_block_tokens(block)
        else:
            total += _estimate_block_tokens(content if isinstance(content, str) else str(content))
    return total

def _summarize_messages(messages: list) -> str:
    """Build a text summary of messages being compacted."""
    lines = []
    tools_used = []
    files_mentioned = set()

    for msg in messages:
        content = msg.content if hasattr(msg, "content") else str(msg)
        role = type(msg).__name__.replace("Message", "").lower()

        if isinstance(content, str) and content.strip():
            lines.append(f"[{role}]: {content.strip()[:300]}")
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        inp = block.get("input", {})
                        info = name
                        if "path" in inp:
                            info += f"({inp['path']})"
                            files_mentioned.add(inp["path"])
                        elif "command" in inp:
                            info += f"({str(inp['command'])[:60]})"
                        tools_used.append(info)
                    elif block.get("type") == "text":
                        lines.append(f"[{role}]: {block.get('text','')[:300]}")

    parts = []
    if lines:
        parts.append("Conversation history:")
        parts.extend(f" {l}" for l in lines[:20])
    if tools_used:
        parts.append(f"\nTools used: {', '.join(tools_used[:10])}")
    if files_mentioned:
        parts.append(f"\nFiles referenced: {', '.join(sorted(files_mentioned)[:10])}")

    return "\n".join(parts) or "Previous conversation."

def _compact_messages(messages: list) -> list:
    """
    Summarise old messages, keep recent tail.
    Returns new message list with summary injected as SystemMessage.
    """
    if len(messages) <= PRESERVE_RECENT:
        return messages

    cut = len(messages) - PRESERVE_RECENT

    # never split a tool-use / tool-result pair
    while cut > 0:
        msg = messages[cut]
        content = msg.content if hasattr(msg, "content") else []
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            cut -= 1
        else:
            break

    to_summarise = messages[:cut]
    to_keep = messages[cut:]

    if not to_summarise:
        return messages

    summary_text = _summarize_messages(to_summarise)
    continuation = (
        f"{COMPACT_PREAMBLE}Summary:\n{summary_text.strip()}\n\n"
        f"{COMPACT_RECENT_MESSAGES_NOTE} {COMPACT_DIRECT_RESUME_INSTRUCTION}"
    )

    return [SystemMessage(content=continuation)] + to_keep

class CompactingCheckpointer(SqliteSaver):
    """
    SqliteSaver + automatic context compaction.

    SqliteSaver: checkpoints survive process restarts — same thread_id
    on next run resumes from the last completed node,
    not from Phase 1 again.

    Compaction: when the estimated context exceeds the auto-compaction
    threshold (default 100k tokens, env
    CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS), old messages are
    summarised before saving so context never explodes.

    DB location: ~/.agent/checkpoints.db
    """

    def __init__(self):
        import sqlite3
        db_path = Path.home() / ".agent" / "checkpoints.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        super().__init__(conn)

    def put(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> dict:
        """Intercept checkpoint save — compact messages if needed, then persist."""
        messages = checkpoint.get("channel_values", {}).get("messages", [])

        # preserve_recent messages AND the estimate crosses the threshold.
        if (
            len(messages) > PRESERVE_RECENT
            and _estimate_tokens(messages) >= _auto_compact_threshold()
        ):
            compacted = _compact_messages(messages)
            removed = len(messages) - len(compacted)
            if removed > 0:
                print(
                    f"\033[2m[auto-compact: {removed} messages summarised, "
                    f"~{removed * 200} tokens freed]\033[0m"
                )
            checkpoint = {
                **checkpoint,
                "channel_values": {
                    **checkpoint.get("channel_values", {}),
                    "messages": compacted,
                },
            }

        return super().put(config, checkpoint, metadata, new_versions)
