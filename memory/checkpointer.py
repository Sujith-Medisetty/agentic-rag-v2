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

import json
import os
from pathlib import Path
from typing import Any
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

# --- Auto-compaction thresholds ---
# COMPACT_BUDGET: when estimated message-list tokens cross this, auto-compact
# fires BEFORE the next LLM call. Lowered from 80K → 50K (with a UI warning
# tier at 25% of CONTEXT_WINDOW) so the live window stays lean for the
# 30+ turn salon-build sessions — the long tail of edits otherwise piles
# up and we'd be re-sending the same stale file contents on every call.
#
# CONTEXT_WINDOW: the working context the LLM can actually reason over. Used
# for the UI context-used percentage bar — Claude Code-style "75% used"
# indicator. MiniMax-M3-512k nominally fits 512K, but quality holds up to
# ~200K. Tune via env if needed.
DEFAULT_AUTO_COMPACT_INPUT_TOKENS = 50_000
AUTO_COMPACT_THRESHOLD_ENV_VAR = "OJAS_AUTO_COMPACT_INPUT_TOKENS"  # was CLAUDE_CODE_…
AUTO_COMPACT_THRESHOLD_LEGACY_ENV_VAR = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"
CONTEXT_WINDOW_TOKENS = 200_000  # what the UI's 100% fill represents

CHARS_PER_TOKEN = 4
# How many of the most-recent messages to keep VERBATIM when compacting.
# Was 4 (≈1 turn) — that forced constant re-derivation on long sessions.
# 80 ≈7 turns is the sweet spot: the agent can see its recent reasoning
# + tool calls + the file edits from the last few iterations without
# either re-reading them or watching them get summarised away mid-thought.
PRESERVE_RECENT = 80
PRESERVE_RECENT_ENV_VAR = "OJAS_PRESERVE_RECENT"

# --- Tool-result truncation ---
# A single `Read` of a 30k-token log file would otherwise fill most of the
# live window with stale content. The agent can re-`Read` if it needs the
# fresh content — we keep the tool CALL (path + args) verbatim, since
# that's the agent's intent, but replace the oversized result body with
# a one-line pointer. ~200 tokens / 800 chars keeps the live window lean
# while still letting the agent recognise "oh, that's the file I just
# read" via the head snippet.
TOOL_RESULT_TRUNCATE_AT_CHARS = 800
TOOL_RESULT_TRUNCATE_ENV_VAR = "OJAS_TRUNCATE_TOOL_RESULT_AT"

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
    """Auto-compaction token threshold.

    Reads `OJAS_AUTO_COMPACT_INPUT_TOKENS` (new name) first, then
    `CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS` (legacy alias, kept for
    backward compatibility with anything that still sets the old var)."""
    for var in (AUTO_COMPACT_THRESHOLD_ENV_VAR, AUTO_COMPACT_THRESHOLD_LEGACY_ENV_VAR):
        raw = os.getenv(var)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return DEFAULT_AUTO_COMPACT_INPUT_TOKENS


def _preserve_recent() -> int:
    """How many of the most-recent messages to keep verbatim when compacting.
    Override at runtime via `OJAS_PRESERVE_RECENT` (e.g. to bump down on
    especially tight budgets, or up for code-review style sessions where
    the agent loops over the same diff repeatedly)."""
    raw = os.getenv(PRESERVE_RECENT_ENV_VAR)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return PRESERVE_RECENT


def _tool_result_truncate_at() -> int:
    """Char threshold above which a ToolMessage body gets collapsed to a
    one-line pointer. Override at runtime via `OJAS_TRUNCATE_TOOL_RESULT_AT`."""
    raw = os.getenv(TOOL_RESULT_TRUNCATE_ENV_VAR)
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return TOOL_RESULT_TRUNCATE_AT_CHARS


def _truncate_tool_result(content) -> str:
    """Replace an oversized tool result body with a one-line pointer so the
    live window stays small. The agent can re-`Read` / re-invoke the tool
    if it needs the actual content again.

    Keeping the first ~200 chars in the pointer is enough for the agent
    to recognise the content (file header, first error line, command
    output's first row). Non-string content (lists, dicts) is returned
    unchanged — that's typically structured tool output (file diffs, JSON
    responses) that should not be silently truncated.
    """
    if not isinstance(content, str):
        return content
    limit = _tool_result_truncate_at()
    if len(content) <= limit:
        return content
    head = content[:limit].replace("\n", " ⏎ ")
    approx_tokens = len(content) // CHARS_PER_TOKEN
    return (
        f"[output truncated: {len(content):,} chars (~{approx_tokens:,} tokens); "
        f"first {limit} chars: {head!r}… "
        f"re-invoke the tool to see the full body]"
    )

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
    """Build a rich text summary of the messages being compacted.

    Preserves what matters for the agent to keep working without re-reading
    every file it just touched:
      - the most recent user request (constraints / "make it nicer" cues)
      - file edits (path + what changed)
      - errors hit + how they were fixed
      - test results
      - key shell commands run
      - all files touched in the compacted slice
    """
    sections: dict[str, Any] = {
        "edits_made":       [],
        "errors_and_fixes": [],
        "test_results":     [],
        "key_commands":     [],
        "files_touched":    set(),
        "tools_used":       [],
    }
    last_user_msg: str | None = None

    for msg in messages:
        cls = type(msg).__name__
        content = msg.content if hasattr(msg, "content") else str(msg)

        # Keep only the most recent HumanMessage verbatim — earlier user asks
        # are summarised by being the source of all the work that followed.
        if cls == "HumanMessage" and isinstance(content, str) and content.strip():
            last_user_msg = content.strip()
            continue

        if cls == "AIMessage":
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                name = (tc.get("name") if isinstance(tc, dict) else None) or "?"
                sections["tools_used"].append(name)
                args = (tc.get("args") if isinstance(tc, dict) else None) or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                if not isinstance(args, dict):
                    continue
                if name == "edit_file":
                    path = args.get("path", "?")
                    new = (args.get("new_string") or "")[:200]
                    sections["edits_made"].append(f"{path}: → {new!r}")
                    sections["files_touched"].add(path)
                elif name == "write_file":
                    path = args.get("path", "?")
                    sections["edits_made"].append(
                        f"{path}: (new file, {len(args.get('content', '') or '')} bytes)"
                    )
                    sections["files_touched"].add(path)
                elif name == "bash":
                    cmd = (args.get("command") or "")[:100]
                    sections["key_commands"].append(cmd)

        if cls == "ToolMessage":
            c = content if isinstance(content, str) else str(content)
            snippet = c[:500].replace("\n", " ")
            name = getattr(msg, "name", "tool") or "tool"
            low = c.lower()
            if "error" in low or "traceback" in low or "enoent" in low or "eacces" in low:
                sections["errors_and_fixes"].append(f"{name}: {snippet}")
            elif "pass" in c or "✓" in c or " ok\n" in c or low.endswith(" ok"):
                sections["test_results"].append(f"{name}: {snippet}")

    parts: list[str] = []
    if last_user_msg:
        parts.append(
            f"User's most recent request that was summarised (the very latest "
            f"is in the kept tail below, verbatim):\n  {last_user_msg[:500]}"
        )
    if sections["edits_made"]:
        parts.append("\nFiles edited:")
        parts.extend(f"  {e}" for e in sections["edits_made"][:15])
    if sections["errors_and_fixes"]:
        parts.append("\nErrors hit + how they were resolved:")
        parts.extend(f"  {e}" for e in sections["errors_and_fixes"][:8])
    if sections["test_results"]:
        parts.append("\nTest results:")
        parts.extend(f"  {r}" for r in sections["test_results"][:8])
    if sections["key_commands"]:
        parts.append("\nKey shell commands run:")
        parts.extend(f"  {c}" for c in sections["key_commands"][:10])
    if sections["files_touched"]:
        parts.append(
            "\nAll files referenced in this slice: "
            + ", ".join(sorted(sections["files_touched"])[:20])
        )
    if sections["tools_used"]:
        from collections import Counter
        ctr = Counter(sections["tools_used"])
        parts.append(
            "\nTools used (top): "
            + ", ".join(f"{n}×{name}" for name, n in ctr.most_common(8))
        )

    # Pointer to the durable per-workspace fix log on disk. The regex
    # summary above is lossy (15-edit cap), so a long session — say
    # 100 bug fixes — would lose ~85% of the trail here. The full trail
    # lives in `<workspace>/.ojas-fixlog.md` and is one `Read` away;
    # the dynamic system-prompt suffix surfaces the tail automatically,
    # but mentioning the path here means the next turn has a fallback
    # even if the dynamic section was missed.
    parts.append(
        "\nFix trail: see `.ojas-fixlog.md` in the workspace for "
        "one-line summaries of every `edit_file` call (this list is "
        "auto-appended and survives compaction)."
    )

    return "\n".join(parts) or "Previous conversation."

def _compact_messages(messages: list) -> list:
    """Summarise old messages, keep recent tail, return a new message list.

    Bug fixes vs the prior version (round 1 of this refactor):

    Bug A — system prompt preservation: the system prompt is NOT in
    `state["messages"]`; it's rebuilt each turn from `system_prompt_pair`.
    So `_compact_messages` doesn't need to (and shouldn't) preserve any
    SystemMessage. The summary itself is injected as a `HumanMessage`
    (see Bug B).

    Bug B — three SystemMessages in a row: prior versions injected the
    summary as a `SystemMessage`, which combined with the static +
    dynamic SystemMessages at the start of the next LLM call produced
    three consecutive system messages. Anthropic rejects this; some
    OpenAI-compatible providers tolerate it but inconsistently. Fix:
    the summary is now a `HumanMessage` (synthetic), so it lands in the
    user/AI/tool flow naturally — between the dynamic system message
    and the recent tool calls.

    Bug F — dangling ToolMessage in the kept tail: prior versions walked
    the cut BACKWARDS past tool_use/tool_result pairs but never walked
    FORWARDS to ensure the kept tail doesn't start with a `ToolMessage`
    whose `AIMessage` got summarised away. `_repair_orphan_tool_calls`
    catches it downstream but at the cost of dropping the unsatisfied
    `tool_calls` from the kept AIMessage — losing context. Fix: walk
    the cut forward past any leading ToolMessage in the kept tail too,
    so the AIMessage that owns them stays in the kept window.

    Bug G — already-correct orphan-repair is preserved (it runs in
    `node_agent` after compaction, so the result is consistent).

    Returns a new list: `[HumanMessage(summary), ...recent_kept]`. If
    there's nothing to summarise (cut walked all the way to 0), returns
    the original list unchanged.
    """
    preserve = _preserve_recent()
    if len(messages) <= preserve:
        return messages

    # Pre-filter: drop any SystemMessage in the history. They don't belong
    # in `state["messages"]` in the first place (the actual system prompt
    # is in `state["system_prompt_pair"]` and rebuilt every turn), but if
    # a stale one slipped in we don't want to leak it through compaction.
    messages = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(messages) <= preserve:
        return messages

    cut = len(messages) - preserve

    # Walk the cut BACKWARDS past any tool-result blocks so we don't
    # summarise the AIMessage that owns them while keeping its result
    # (or vice versa). Two equivalent shapes to detect:
    #   - Anthropic: a single message whose content list contains a
    #     `tool_result` block (paired with the preceding `tool_use` block).
    #   - OpenAI / MiniMax: a separate ToolMessage whose `tool_call_id`
    #     points back to a tool_calls entry on the preceding AIMessage.
    while cut > 0:
        msg = messages[cut]
        if isinstance(msg, ToolMessage):
            cut -= 1
            continue
        content = msg.content if hasattr(msg, "content") else []
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            cut -= 1
            continue
        break

    # Walk the cut FORWARDS past any leading ToolMessage in the kept tail.
    # If the kept window starts with a ToolMessage, the AIMessage that
    # owns it has been summarised away — push the cut forward to keep
    # both, or `_repair_orphan_tool_calls` will strip the tool_call and
    # the agent loses context.
    while cut < len(messages) - 1 and cut > 0:
        nxt = messages[cut]
        if isinstance(nxt, ToolMessage):
            cut += 1
            continue
        content = nxt.content if hasattr(nxt, "content") else []
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in content
        ):
            cut += 1
            continue
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

    # Bug B fix: inject as HumanMessage, not SystemMessage, so we don't
    # produce three consecutive SystemMessages at the start of the next
    # LLM call. The summary is a synthetic "user" turn saying "below is
    # what we talked about before; please continue."
    return [HumanMessage(content=continuation)] + to_keep


def maybe_compact(messages: list) -> tuple[list, bool]:
    """Compact the message list NOW if it crosses the auto-compaction threshold.

    Called by the agent loop BEFORE `model.invoke(messages)`, so the turn that
    crosses the threshold pays for the smaller compacted context rather than
    the giant one. The old `put()`-time check fired AFTER the LLM had already
    been billed for the full history — late fire, paid twice.

    Returns `(messages, did_compact)`. If `did_compact` is True, the caller
    should publish a `compacting` event to the UI.
    """
    if len(messages) <= _preserve_recent():
        return messages, False
    if _estimate_tokens(messages) < _auto_compact_threshold():
        return messages, False
    compacted = _compact_messages(messages)
    removed = len(messages) - len(compacted)
    if removed <= 0:
        return messages, False
    print(
        f"\033[2m[auto-compact: {removed} messages summarised, "
        f"~{removed * 200} tokens freed]\033[0m"
    )
    return compacted, True


class CompactingCheckpointer(SqliteSaver):
    """
    SqliteSaver + automatic context compaction.

    SqliteSaver: checkpoints survive process restarts — same thread_id
    on next run resumes from the last completed node,
    not from Phase 1 again.

    Compaction: the agent loop calls `maybe_compact(messages)` BEFORE each
    `model.invoke`, so the turn that crosses the threshold pays for the
    smaller compacted context rather than the giant one. This `put()` hook
    is a safety net for restarts and any code path that bypasses the loop.

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
        """Intercept checkpoint save — compact messages if needed, then persist.

        Safety net only — the primary compaction now happens in
        `maybe_compact()` called from the agent loop before each LLM call.
        """
        messages = checkpoint.get("channel_values", {}).get("messages", [])

        # preserve_recent messages AND the estimate crosses the threshold.
        if (
            len(messages) > _preserve_recent()
            and _estimate_tokens(messages) >= _auto_compact_threshold()
        ):
            compacted = _compact_messages(messages)
            removed = len(messages) - len(compacted)
            if removed > 0:
                print(
                    f"\033[2m[auto-compact (safety net): {removed} messages summarised, "
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
