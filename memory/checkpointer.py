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
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

# LLM-reported input_tokens from the most recent call (per-context).
# The local `len//4` estimator underestimates by 5-6× (no system prompt,
# no tool defs, no JSON envelope), so we trust the LLM's own count when
# we have one. The module-level dict below survives `copy_context()` —
# without it the per-context value resets to 0 every turn (run_turn does
# `ctx = copy_context()` per turn) and the trigger never fires.
_LAST_LLM_INPUT_TOKENS: ContextVar[int] = ContextVar(
    "ojas_last_llm_input_tokens", default=0,
)
_LAST_INPUT_TOKENS_BY_SESSION: dict[str, int] = {}

# Default auto-compact threshold: 80K. 50K was too tight — system prompt
# + tool defs alone are ~16K and a few file-edits push the rest over.
# CONTEXT_WINDOW_TOKENS is what the chip's "100% used" represents
# (the working context the model can reason over — quality holds up to
# ~200K for MiniMax-M3 even though its nominal window is 512K).
DEFAULT_AUTO_COMPACT_INPUT_TOKENS = 80_000
AUTO_COMPACT_THRESHOLD_ENV_VAR = "OJAS_AUTO_COMPACT_INPUT_TOKENS"
AUTO_COMPACT_THRESHOLD_LEGACY_ENV_VAR = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"
CONTEXT_WINDOW_TOKENS = 200_000

CHARS_PER_TOKEN = 4
# Most-recent messages to keep verbatim when compacting. 4 was too small
# (the agent lost the thread of mid-edit context); 80 was too large
# (each edit_file tool_call embeds a 1-3KB new_string arg, so 80
# messages could be 80K+ tokens — bigger than the threshold itself, and
# the compact stopped actually reducing anything). 30 ≈ 2-3 turns is
# the sweet spot: post-mask/strip/truncate, ~15-30K tokens.
PRESERVE_RECENT = 30
PRESERVE_RECENT_ENV_VAR = "OJAS_PRESERVE_RECENT"

# Tool-result truncation: bodies over 800 chars get collapsed to a
# one-line pointer. The agent can re-invoke the tool to get the fresh
# body. We keep the tool CALL (path + args) verbatim since that's
# the agent's intent.
TOOL_RESULT_TRUNCATE_AT_CHARS = 800
TOOL_RESULT_TRUNCATE_ENV_VAR = "OJAS_TRUNCATE_TOOL_RESULT_AT"

# Preamble + tail injected as a HumanMessage at compact time, so the
# next LLM call sees a single "this is a continuation" block followed
# by the kept tail. Borrowed from the Rust runtime/compact.rs format.
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
    """Auto-compaction token threshold. New var takes precedence over the
    legacy CLAUDE_CODE_* alias for backward compatibility."""
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
    """Replace an oversized tool result body with a one-line pointer.
    Non-string content (lists, dicts) is returned unchanged — those are
    typically structured tool output (diffs, JSON) that should not be
    silently truncated."""
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


# --- Observation masking ---
# JetBrains research (2024) + Claude Code / Cursor production behavior:
# after N turns, the same tool result is being re-sent on every subsequent
# turn (via Anthropic's automatic prefix cache), but the agent almost never
# actually re-reads the result after it's been a few turns away. The
# tool result just bloats the cache_read and inflates the per-turn cost.
#
# Masking replaces old tool results with a one-line stub ("obsolete — re-invoke
# if you need the content"). The agent can always re-call the tool to get
# the fresh body, so masking is lossless for the agent's actual capability
# but cuts the per-turn cache_read dramatically on long sessions.
#
# Without masking, the 5.9M-token calculator session saw cache_read grow to
# 240K per turn (the entire previous history), driving cost to $2.22 per
# build. With masking at KEEP_RECENT_OBSERVATIONS=12, the same build would
# cap cache_read at ~30-50K (the recent window only) — a 5-7× reduction
# in cache_read and a comparable drop in cost.
KEEP_RECENT_OBSERVATIONS = 12
KEEP_RECENT_OBSERVATIONS_ENV_VAR = "OJAS_KEEP_RECENT_OBSERVATIONS"
_MASKED_RESULT_STUB = (
    "[observation masked — this tool result is from an earlier turn and has "
    "been collapsed to save context. If you need the full content, re-invoke "
    "the tool.]"
)


def _keep_recent_observations() -> int:
    raw = os.getenv(KEEP_RECENT_OBSERVATIONS_ENV_VAR)
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return KEEP_RECENT_OBSERVATIONS


def mask_old_observations(messages: list) -> list:
    """Replace `ToolMessage.content` (and list-shaped AIMessage
    `tool_result` blocks) for any observation older than the most recent
    `KEEP_RECENT_OBSERVATIONS`. Tool CALLS on the preceding AIMessage
    are preserved — the agent's *intent* matters, the result body
    doesn't. Per-turn transformation; the on-disk checkpoint keeps the
    full body.
    """
    keep = _keep_recent_observations()

    # Collect indices of all ToolMessage / tool_result-bearing messages,
    # in order. Walk back from the end; the last `keep` are kept verbatim.
    obs_indices: list[int] = []
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage):
            obs_indices.append(i)
            continue
        content = getattr(m, "content", None)
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in content
        ):
            obs_indices.append(i)

    if len(obs_indices) <= keep:
        return messages

    to_mask = set(obs_indices[:-keep])

    masked_count = 0
    out: list = []
    for i, m in enumerate(messages):
        if i not in to_mask:
            out.append(m)
            continue

        if isinstance(m, ToolMessage):
            # Replace ToolMessage.content with the stub. Preserve
            # tool_call_id so the agent's preceding AIMessage still
            # sees its result slot satisfied (no orphan-repair needed).
            out.append(
                m.model_copy(update={"content": _MASKED_RESULT_STUB})
            )
            masked_count += 1
            continue

        # List-shaped AIMessage content with a tool_result block: replace
        # the block's content with the stub; leave the rest of the message
        # (other blocks, the preceding text) alone.
        content = m.content
        if isinstance(content, list):
            new_blocks = []
            changed = False
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    nb = dict(b)
                    nb["content"] = _MASKED_RESULT_STUB
                    new_blocks.append(nb)
                    changed = True
                else:
                    new_blocks.append(b)
            if changed:
                out.append(m.model_copy(update={"content": new_blocks}))
                masked_count += 1
                continue
        out.append(m)

    if masked_count:
        import logging
        logging.getLogger(__name__).info(
            "[observation-mask] masked %d old tool result(s) (keeping most recent %d)",
            masked_count, keep,
        )
    return out


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
    """Summarise old messages, keep recent tail. Returns a new list:
    `[HumanMessage(summary), ...recent_kept]`. The summary is injected
    as a HumanMessage (not SystemMessage) so it doesn't produce three
    consecutive SystemMessages at the start of the next LLM call.
    """
    preserve = _preserve_recent()
    threshold = _auto_compact_threshold()
    # Bail only when both count AND estimate are small. A 1.8 MB
    # user-pasted short story is 4 messages but ~450K tokens, so the
    # length check alone would skip a session that's already over
    # threshold.
    if len(messages) <= preserve and _estimate_tokens(messages) < threshold:
        return messages

    # Drop any stray SystemMessage (the real system prompt is rebuilt
    # each turn from `system_prompt_pair`).
    messages = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(messages) <= preserve and _estimate_tokens(messages) < threshold:
        return messages

    # Keep the last `preserve` messages verbatim, summarise the rest.
    # If the list is shorter than `preserve` but still over threshold
    # (the 4-message / 1.8MB case), cut at least 1 from the front so
    # the giant message gets replaced with a summary.
    cut = max(1, len(messages) - preserve) if len(messages) > preserve else 1

    # Walk cut BACKWARDS past tool_result blocks so we don't summarise
    # an AIMessage while keeping its result, or vice versa. Two shapes
    # to detect: separate ToolMessage (OpenAI/MiniMax) or list-shaped
    # AIMessage with a `tool_result` content block (Anthropic).
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
        # Walk past any HumanMessage at the cut boundary so the
        # most recent user question is always in the kept tail,
        # not in the summary. Without this, the cut can land on a
        # HumanMessage (e.g. the user's "what about dark mode?"
        # sitting right after a long tool-call sequence) and the
        # summariser absorbs it into a one-line note — the LLM
        # never sees it as a discrete user turn. Stop when we
        # land on a non-HumanMessage (an AI response, which is
        # what should follow a user question in conversation
        # order).
        if isinstance(nxt, HumanMessage):
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


def record_llm_input_tokens(input_tokens: int, session_id: str | None = None) -> None:
    """Stores the LLM-reported `input_tokens` so maybe_compact() can use
    it as the trigger. Writes to a module-level dict keyed by
    `session_id` (the ContextVar alone is per-context and resets to 0
    at the start of every turn — run_turn does `copy_context()`)."""
    value = 0
    try:
        value = max(0, int(input_tokens))
    except (TypeError, ValueError):
        pass
    _LAST_LLM_INPUT_TOKENS.set(value)
    if session_id:
        _LAST_INPUT_TOKENS_BY_SESSION[session_id] = value


def _last_llm_input_tokens(session_id: str | None = None) -> int:
    """LLM-reported input_tokens from the most recent call. 0 if no
    call has happened yet (maybe_compact falls back to the local
    estimate). Per-session module dict first (cross-turn), then the
    ContextVar (per-context, for tests)."""
    if session_id:
        v = _LAST_INPUT_TOKENS_BY_SESSION.get(session_id)
        if v is not None:
            return v
    return _LAST_LLM_INPUT_TOKENS.get()


def maybe_compact(messages: list, session_id: str | None = None) -> tuple[list, bool, dict]:
    """Compact the message list NOW if it crosses the auto-compaction
    threshold. Called by the agent loop BEFORE `model.invoke(messages)`
    so the turn that crosses threshold pays for the smaller compacted
    context, not the giant one.

    Returns `(messages, did_compact, info)`. `info` is the payload for
    the chat-visible `context_compacted` event — `removed`, `kept`,
    `tokens_before`, `tokens_after`, and `summary_preview` (first 280
    chars of the injected summary).
    """
    threshold = _auto_compact_threshold()
    est = _estimate_tokens(messages)
    if len(messages) <= _preserve_recent() and est < threshold:
        return messages, False, {}

    # Primary trigger: the LLM's own reported input_tokens (same number
    # the chip shows, so chip + trigger stay in lockstep). Local
    # estimate underestimates by 5-6× (no system prompt, no tool defs,
    # no JSON envelope), so the LLM-count is the only reliable signal.
    # Falls back to the local estimate for the very first turn.
    last_input = _last_llm_input_tokens(session_id)
    if last_input > 0:
        if last_input < threshold:
            return messages, False, {}
    elif est < threshold:
        return messages, False, {}

    tokens_before = est
    compacted = _compact_messages(messages)
    removed = len(messages) - len(compacted)
    # "Did the compact shrink the prompt?" Two ways: (a) message count
    # went down (many small messages), or (b) tokens went down (one
    # giant message replaced by a summary — the 1.8MB user-paste case:
    # 4 messages in, 4 out, but msg[0] is now 1KB instead of 1.8MB).
    tokens_after = _estimate_tokens(compacted)
    if removed <= 0 and tokens_after >= tokens_before:
        return messages, False, {}

    # First 280 chars of the summary, with the preamble + tail stripped
    # (both are noise for the chat-visible preview).
    summary_preview = ""
    if compacted and isinstance(compacted[0], HumanMessage):
        first = compacted[0].content if isinstance(compacted[0].content, str) else ""
        if "Summary:" in first:
            first = first.split("Summary:", 1)[1]
        if "Recent messages are preserved verbatim" in first:
            first = first.split("Recent messages are preserved verbatim", 1)[0]
        summary_preview = first.strip()[:280]

    info = {
        "removed": removed,
        "kept": len(compacted),
        "tokens_before": tokens_before,
        "tokens_after": tokens_after,
        "summary_preview": summary_preview,
    }
    logging.getLogger(__name__).info(
        "auto-compact: %d messages summarised, ~%d tokens freed",
        removed, removed * 200,
    )
    return compacted, True, info


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
                logging.getLogger(__name__).info(
                    "auto-compact (safety net): %d messages summarised, ~%d tokens freed",
                    removed, removed * 200,
                )
            checkpoint = {
                **checkpoint,
                "channel_values": {
                    **checkpoint.get("channel_values", {}),
                    "messages": compacted,
                },
            }

        return super().put(config, checkpoint, metadata, new_versions)
