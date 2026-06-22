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
from agents._timeouts import (
    _call_with_wall_clock_guard,
    DEFAULT_CHECKPOINT_WRITE_TIMEOUT_S,
    CHECKPOINT_WRITE_TIMEOUT_ENV,
)

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
# NOTE: there is no "preserve last N messages" knob anymore. Compaction is
# Claude-Code-style PURE REPLACE — the entire prior conversation is folded into
# one structured summary and the raw messages are dropped; recency is carried by
# the summary, the live todo list is re-injected verbatim, and the system prompt
# is preserved out-of-band. The sole trigger is the token threshold (below);
# `_compact_messages` no-ops on a ≤1-message history so there's nothing to guard
# by count. (Removed the old PRESERVE_RECENT / OJAS_PRESERVE_RECENT.)

# Tool-result truncation: bodies over this many chars get collapsed
# to a one-line pointer. The agent can re-invoke the tool to get the
# fresh body. We keep the tool CALL (path + args) verbatim since
# that's the agent's intent.
#
# The 800-char cap was too aggressive: it collapsed the agent's
# immediate post-write verification reads (Read of a freshly written
# 16KB file) to a one-line stub, and the agent concluded the file
# was corrupt when actually the on-disk file was fine. The agent
# then ran `sed`/`python` "repairs" based on its truncated view,
# which is where the REAL corruption entered the file. With this
# cap at 4000 (~1KB of tokens), most file reads, grep outputs, and
# test results fit in one observation without being collapsed, and
# the per-message cap only catches pathological cases (multi-MB
# log dumps, `find` outputs, large `npm install` logs). Long-term
# budget control is the job of `mask_old_observations`, which
# collapses older observations while keeping the most recent K
# verbatim. `_truncate_live_history` in agents/nodes.py also
# preserves the last 4 ToolMessages verbatim regardless of size.
TOOL_RESULT_TRUNCATE_AT_CHARS = 4000
TOOL_RESULT_TRUNCATE_ENV_VAR = "OJAS_TRUNCATE_TOOL_RESULT_AT"

# Preamble + tail injected as a HumanMessage at compact time, so the
# next LLM call sees a single "this is a continuation" block followed
# by the kept tail. Borrowed from the Rust runtime/compact.rs format.
COMPACT_PREAMBLE = (
    "This session is being continued from a previous conversation that ran out "
    "of context. The summary below covers the earlier portion of the "
    "conversation.\n\n"
)
COMPACT_DIRECT_RESUME_INSTRUCTION = (
    "Continue the conversation from where it left off without asking the user "
    "any further questions. Resume directly — do not acknowledge the summary, "
    "do not recap what was happening, and do not preface with continuation text."
)

# Set OJAS_COMPACT_LLM_SUMMARY=0 to force the deterministic heuristic summary
# (no extra LLM call). Default is the LLM summary (true Claude-Code behaviour).
COMPACT_LLM_SUMMARY_ENV_VAR = "OJAS_COMPACT_LLM_SUMMARY"
# Per-message caps when rendering the transcript fed to the summariser LLM.
# Tool results (file dumps, command output) are the bulk of the bytes and the
# least useful verbatim, so they get the tightest cap; assistant reasoning is
# the most useful, so it gets the most room.
_SUMMARY_TOOL_RESULT_CAP = 1500
_SUMMARY_AI_TEXT_CAP     = 6000
_SUMMARY_USER_TEXT_CAP   = 4000

# The Claude-Code compaction prompt: an explicit 7-section structure so the
# summary carries INTENT + decisions + files/code + errors+fixes + in-flight
# work, not just a vague paragraph. The next turn reads this in place of the
# raw messages, so anything omitted here is genuinely forgotten.
#
# Pending tasks are intentionally NOT a section here — `_compact_messages`
# appends the verbatim todo list right after this summary, so any prose
# description of pending work would risk contradicting the live list and
# misleading the next turn. The summariser must describe current work, not
# queue work.
COMPACT_SUMMARY_SYSTEM_PROMPT = (
    "You are summarising a coding-assistant conversation that is about to be "
    "truncated for context. Your summary REPLACES the omitted messages — the "
    "assistant will rely on it alone to keep working, so it must be detailed "
    "and faithful. Do not invent anything; only record what is in the "
    "transcript. Write the summary under these exact numbered headings:\n\n"
    "1. Primary request and intent — what the user ultimately wants, in their "
    "own framing, including every explicit instruction and constraint.\n"
    "2. Key technical concepts, decisions, and rationale — the approaches "
    "chosen and WHY (the reasoning, trade-offs, and rejected alternatives).\n"
    "3. Files and code sections — every file created or edited, with the path "
    "and a precise description of what changed and why. Include key code/"
    "signatures where they matter for continuing the work.\n"
    "4. Errors encountered and how they were fixed — pair each error with its "
    "resolution and any user feedback on it.\n"
    "5. Problem solving — what has been solved and any ongoing troubleshooting.\n"
    "6. All user messages — list every non-tool message from the user verbatim "
    "or near-verbatim, in order. This preserves intent and is critical.\n"
    "7. Current work — exactly what was happening at the moment of truncation "
    "(the file, function, or command in flight). Do NOT list pending tasks, "
    "next steps, or what remains to be done — the current todo list is "
    "appended verbatim right after this summary, and any prose description "
    "of pending work risks contradicting the live list.\n\n"
    "Be specific and concrete (real paths, names, values). Omit a heading only "
    "if it genuinely has no content. Output only the summary."
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


def _truncate_tool_result(content):
    """Replace an oversized tool result body with a one-line pointer.
    Non-string content (lists, dicts) is returned unchanged — those are
    typically structured tool output (diffs, JSON) that should not be
    silently truncated. Returns a `str` for string input, otherwise the
    original object untouched (hence no return annotation)."""
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

def _llm_summary_enabled() -> bool:
    raw = os.getenv(COMPACT_LLM_SUMMARY_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _render_transcript(messages: list) -> str:
    """Flatten the messages being compacted into a plain-text transcript for
    the summariser LLM. Tool results are truncated hard (they're the bulk of
    the bytes and least useful verbatim); assistant reasoning gets the most
    room. Robust to both content shapes — string content (OpenAI/MiniMax) and
    list-of-blocks content (Anthropic)."""
    def _text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            out = []
            for b in content:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        out.append(str(b.get("text", "")))
                    elif b.get("type") == "tool_use":
                        out.append(f"[calls {b.get('name','?')}({json.dumps(b.get('input', {}), default=str)[:300]})]")
                    elif b.get("type") == "tool_result":
                        out.append(str(b.get("content", ""))[:_SUMMARY_TOOL_RESULT_CAP])
                else:
                    out.append(str(b))
            return "\n".join(out)
        return str(content)

    lines: list[str] = []
    for msg in messages:
        cls = type(msg).__name__
        content = msg.content if hasattr(msg, "content") else str(msg)
        body = _text(content).strip()
        if cls == "HumanMessage":
            if body:
                lines.append(f"User: {body[:_SUMMARY_USER_TEXT_CAP]}")
        elif cls == "AIMessage":
            if body:
                lines.append(f"Assistant: {body[:_SUMMARY_AI_TEXT_CAP]}")
            for tc in (getattr(msg, "tool_calls", None) or []):
                name = (tc.get("name") if isinstance(tc, dict) else None) or "?"
                args = (tc.get("args") if isinstance(tc, dict) else None) or {}
                lines.append(f"Assistant → tool {name}({json.dumps(args, default=str)[:300]})")
        elif cls == "ToolMessage":
            name = getattr(msg, "name", "tool") or "tool"
            lines.append(f"Tool[{name}] result: {body[:_SUMMARY_TOOL_RESULT_CAP]}")
    return "\n\n".join(lines)


def _summarize_messages_llm(messages: list) -> str:
    """LLM-generated structured summary (the Claude-Code approach). Makes one
    non-streaming, tool-free model call with COMPACT_SUMMARY_SYSTEM_PROMPT.
    Raises on any failure so the caller can fall back to the heuristic — this
    function never silently returns a degraded summary."""
    transcript = _render_transcript(messages)
    if not transcript.strip():
        raise ValueError("empty transcript")
    # Lazy import to avoid a circular import (agents.nodes imports maybe_compact
    # from this module at load time).
    from agents.nodes import _get_llm
    llm = _get_llm(streaming=False, thinking=False)
    resp = llm.invoke([
        SystemMessage(content=COMPACT_SUMMARY_SYSTEM_PROMPT),
        HumanMessage(content="Here is the conversation to summarise:\n\n" + transcript),
    ])
    text = resp.content if isinstance(resp.content, str) else _render_transcript([resp])
    text = (text or "").strip()
    if not text:
        raise ValueError("empty LLM summary")
    return text


def _summarize_messages(messages: list) -> str:
    """Summary of the messages being compacted. Uses the LLM (true Claude-Code
    behaviour: rich, structured, captures intent + decisions + files + fixes)
    when enabled, and falls back to the deterministic heuristic extractor on
    any error (no langchain, network failure, timeout) or when disabled via
    OJAS_COMPACT_LLM_SUMMARY=0."""
    if _llm_summary_enabled():
        try:
            summary = _summarize_messages_llm(messages)
            # The on-disk fix log is the uncapped edit trail; point at it so a
            # long session's full history is always one Read away even though
            # the summary itself is necessarily lossy.
            return (
                summary
                + "\n\nFix trail: see `.ojas-fixlog.md` in the workspace for "
                "one-line summaries of every `edit_file` call (auto-appended, "
                "survives compaction)."
            )
        except Exception as exc:  # noqa: BLE001 — any failure → heuristic fallback
            logging.getLogger(__name__).warning(
                "LLM compaction summary failed (%s); using heuristic fallback", exc
            )
    return _summarize_messages_heuristic(messages)


def _summarize_messages_heuristic(messages: list) -> str:
    """Deterministic, LLM-free fallback summary of the messages being compacted.

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
    first_user_msg: str | None = None
    last_user_msg: str | None = None

    for msg in messages:
        cls = type(msg).__name__
        content = msg.content if hasattr(msg, "content") else str(msg)

        # Track the FIRST real user request (the goal) and the most recent one.
        # Skip a prior compaction's continuation block (it starts with
        # COMPACT_PREAMBLE) so we pin the actual original ask, not a summary of
        # a summary — this is how the goal survives across repeated compactions.
        if cls == "HumanMessage" and isinstance(content, str) and content.strip():
            text = content.strip()
            if not text.startswith(COMPACT_PREAMBLE.strip()[:40]):
                if first_user_msg is None:
                    first_user_msg = text
                last_user_msg = text
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
    if first_user_msg:
        parts.append(
            f"Original request (the goal — keep building toward this):\n"
            f"  {first_user_msg[:600]}"
        )
    if last_user_msg and last_user_msg != first_user_msg:
        parts.append(
            f"\nMost recent request that was summarised (the very latest is in "
            f"the kept tail below, verbatim):\n  {last_user_msg[:500]}"
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

def _render_todo_block(todos: list | None) -> str:
    """Render the live todo list verbatim, for re-injection after a compaction
    summary. Returns '' when there are no todos.

    The agent normally only ever *sees* its todo list via the TodoWrite tool
    results in the message history. Pure-replace compaction deletes those, so
    without this the agent would lose its exact plan (each item's content +
    status) and have only the summary's prose description of pending work. We
    re-surface the structured list so the agent keeps working against it —
    matching how Claude Code keeps the todo list alive across compaction."""
    if not todos:
        return ""
    lines = []
    for t in todos:
        if not isinstance(t, dict):
            continue
        content = (t.get("content") or "").strip()
        if not content:
            continue
        status = (t.get("status") or "pending").strip()
        lines.append(f"- [{status}] {content}")
    if not lines:
        return ""
    return (
        "Current todo list (your live plan — preserved across compaction; keep "
        "updating it with TodoWrite as you make progress):\n" + "\n".join(lines)
    )


def _load_todos_from_disk() -> list | None:
    """Read the live todo list from `.clawd-todos.json` on disk. Returns
    `None` when the file is missing or unreadable.

    Why disk over `state["last_todos"]`: `todo_write()` updates the on-disk
    file on EVERY call (tools/utils.py:104-105), so the disk snapshot is
    strictly fresher than the in-memory one, which only refreshes when
    TodoWrite lands in the latest tool batch (agents/nodes.py:1359). Using
    disk makes the re-injected todo block match what the UI panel is
    displaying, even when the agent has done many turns of work between
    TodoWrite calls.

    When all todos are completed, `todo_write` unlinks the file
    (tools/utils.py:98-102) and we return `None` — callers fall back to the
    in-memory list, which is also empty in that case.
    """
    # Lazy import: avoid pulling tools.utils into the checkpointer import
    # graph; this function only runs on the compact path.
    from tools.utils import _todo_store_path
    try:
        path = _todo_store_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return None
        return data
    except Exception:
        # Malformed JSON, permission error, anything — fall back silently.
        # Never let a todo-read failure break compaction.
        return None


def _compact_messages(messages: list, todos: list | None = None) -> list:
    """Summarise the ENTIRE prior conversation into one structured summary
    message and DISCARD the raw messages — Claude-Code-style "pure replace".
    Returns `[HumanMessage(summary + todo block)]`.

    The summary becomes the sole carrier of conversation history; recency lives
    in its 'Current work' and 'All user messages' sections. Pending tasks are
    intentionally NOT summarised here — they are re-injected verbatim below as
    a single source of truth. Two things that must NOT be lost are preserved
    out-of-band:
      - The SYSTEM PROMPT is not in this list at all (it's rebuilt every turn
        from `system_prompt_pair` and prepended ahead of this message), so it
        always survives AND stays at the front of the prompt → cache hit.
      - The LIVE TODO LIST is re-injected verbatim after the summary (see
        `_render_todo_block`), so the agent keeps its EXACT plan, not just the
        summary's prose.

    Cache shape: the returned summary message sits right after the (cached)
    system prompt, so it's the single new region charged once at compaction;
    from the next turn `[system][summary]` is itself a stable cached prefix.
    """
    threshold = _auto_compact_threshold()
    # Drop any stray SystemMessage (the real system prompt is rebuilt each turn
    # from `system_prompt_pair`).
    messages = [m for m in messages if not isinstance(m, SystemMessage)]
    # Nothing worth compacting: an empty/one-message history that's also under
    # threshold. (A single 1.8 MB pasted message IS worth compacting, hence the
    # estimate check rather than count alone.)
    if len(messages) <= 1 and _estimate_tokens(messages) < threshold:
        return messages

    summary_text = _summarize_messages(messages)
    continuation = (
        f"{COMPACT_PREAMBLE}Summary:\n{summary_text.strip()}\n\n"
        f"{COMPACT_DIRECT_RESUME_INSTRUCTION}"
    )
    # Disk is authoritative at compact time — todo_write() updates the file
    # on every call (tools/utils.py:104), so the disk snapshot matches what
    # the UI panel shows. The in-memory `todos` argument only refreshes
    # when TodoWrite lands in the latest tool batch, so it can lag many
    # turns behind. Fall back to the in-memory list only when disk is
    # missing (e.g., all todos completed → todo_write unlinked it).
    effective_todos = _load_todos_from_disk()
    if effective_todos is None:
        effective_todos = todos
    todo_block = _render_todo_block(effective_todos)
    if todo_block:
        continuation += f"\n\n{todo_block}"

    # Inject as HumanMessage (not SystemMessage) so we don't produce two
    # consecutive SystemMessages at the start of the next LLM call — the real
    # system prompt is prepended separately. The summary is a synthetic "user"
    # turn: "here is everything we did; continue from where we left off."
    return [HumanMessage(content=continuation)]


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


def maybe_compact(messages: list, session_id: str | None = None,
                  todos: list | None = None) -> tuple[list, bool, dict]:
    """Compact the message list NOW if it crosses the auto-compaction
    threshold. Called by the agent loop BEFORE `model.invoke(messages)`
    so the turn that crosses threshold pays for the smaller compacted
    context, not the giant one.

    `todos` is the agent's current todo list (`state["last_todos"]`); it is
    re-injected verbatim after the summary so the plan survives the pure-replace
    compaction (the summary alone would only describe it in prose).

    Returns `(messages, did_compact, info)`. `info` is the payload for
    the chat-visible `context_compacted` event — `removed`, `kept`,
    `tokens_before`, `tokens_after`, and `summary_preview` (first 280
    chars of the injected summary).
    """
    threshold = _auto_compact_threshold()
    # `_estimate_tokens` walks the entire history. After turn 1 the trigger
    # below is the LLM's own reported input_tokens, so the local estimate is
    # usually computed and thrown away. Make it lazy: compute at most once,
    # and only on the paths that actually need it (turn 1, or the tokens_before
    # baseline when we really compact).
    _est_cache: list[int] = []
    def _est() -> int:
        if not _est_cache:
            _est_cache.append(_estimate_tokens(messages))
        return _est_cache[0]

    # Trigger purely on the token threshold (Claude-Code style — no message-count
    # guard). Primary signal is the LLM's own reported input_tokens (same number
    # the chip shows, so chip + trigger stay in lockstep). Local estimate
    # underestimates by 5-6× (no system prompt, no tool defs, no JSON envelope),
    # so it's only the fallback for the very first turn.
    last_input = _last_llm_input_tokens(session_id)
    if last_input > 0:
        if last_input < threshold:
            return messages, False, {}
    elif _est() < threshold:
        return messages, False, {}

    tokens_before = _est()
    compacted = _compact_messages(messages, todos)
    removed = len(messages) - len(compacted)
    # "Did the compact shrink the prompt?" Two ways: (a) message count
    # went down (many small messages), or (b) tokens went down (one
    # giant message replaced by a summary — the 1.8MB user-paste case:
    # 4 messages in, 4 out, but msg[0] is now 1KB instead of 1.8MB).
    tokens_after = _estimate_tokens(compacted)
    if removed <= 0 and tokens_after >= tokens_before:
        return messages, False, {}

    # First 280 chars of the summary, with the preamble + trailing resume
    # instruction stripped (both are noise for the chat-visible preview).
    summary_preview = ""
    if compacted and isinstance(compacted[0], HumanMessage):
        first = compacted[0].content if isinstance(compacted[0].content, str) else ""
        if "Summary:" in first:
            first = first.split("Summary:", 1)[1]
        if COMPACT_DIRECT_RESUME_INSTRUCTION in first:
            first = first.split(COMPACT_DIRECT_RESUME_INSTRUCTION, 1)[0]
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
        # Explicit super(SubClass, self) form — robust against refactors that
        # move this body into a closure (where bare `super()` would raise
        # `RuntimeError: super(): no arguments` because the implicit
        # `__class__` cell is missing outside a method).
        super(CompactingCheckpointer, self).__init__(conn)

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

        Wall-clock guarded: the body runs inside _call_with_wall_clock_guard
        with a budget of OJAS_CHECKPOINT_WRITE_TIMEOUT_S (default 30s). The
        base SqliteSaver holds `self.lock` for the full INSERT+commit, so a
        stuck thread on the other end of that lock stalls us indefinitely on
        `acquire()` — that's the failure mode for the "testing..!" stall
        (6+ hours of no new checkpoints after the LLM call completed
        cleanly). On timeout, TimeoutError bubbles out of the LangGraph
        stream into `run_turn:318`'s `except Exception` arm, which fires the
        full error pipeline (`reporter.error` + `assistant_text(done=True)`
        + persisted `[error]` + `turn_summary`). The previous good
        checkpoint is preserved — the next user message resumes from it.

        Why hard-fail (raise) not soft-fail (synthesize a config pointing at
        the parent checkpoint)? Soft-fail would tell LangGraph the write
        succeeded when it didn't — the next node invocation would re-execute
        from the parent checkpoint, potentially re-triggering the same hang.
        Hard-fail aborts the turn cleanly; the next attempt's first
        checkpoint write is on a fresh execution that doesn't share the stuck
        lock-holder.
        """
        try:
            timeout_s = float(
                os.getenv(
                    CHECKPOINT_WRITE_TIMEOUT_ENV,
                    str(DEFAULT_CHECKPOINT_WRITE_TIMEOUT_S),
                )
                or DEFAULT_CHECKPOINT_WRITE_TIMEOUT_S
            )
        except ValueError:
            timeout_s = DEFAULT_CHECKPOINT_WRITE_TIMEOUT_S
        timeout_s = max(1.0, timeout_s)

        def _do_put() -> dict:
            # Bind the outer-scope `checkpoint` to a fresh local name. The
            # body used to reassign to `checkpoint` directly, which made
            # Python treat the name as a LOCAL for the whole inner function —
            # so the first read below raised UnboundLocalError before the
            # assignment ever ran (`cannot access local variable 'checkpoint'
            # where it is not associated with a value`). That bug aborted
            # every checkpoint write and surfaced as the "task abandoned
            # mid-build" / "deploy_step fails after the agent thinks it
            # finished" symptom on the wire.
            ckpt = checkpoint
            messages = ckpt.get("channel_values", {}).get("messages", [])

            # Over the token threshold → compact. (_compact_messages no-ops on a
            # ≤1-message history, so no count guard is needed here.)
            if _estimate_tokens(messages) >= _auto_compact_threshold():
                todos = ckpt.get("channel_values", {}).get("last_todos", [])
                compacted = _compact_messages(messages, todos)
                removed = len(messages) - len(compacted)
                if removed > 0:
                    logging.getLogger(__name__).info(
                        "auto-compact (safety net): %d messages summarised, ~%d tokens freed",
                        removed, removed * 200,
                    )
                ckpt = {
                    **ckpt,
                    "channel_values": {
                        **ckpt.get("channel_values", {}),
                        "messages": compacted,
                    },
                }

            return super(CompactingCheckpointer, self).put(config, ckpt, metadata, new_versions)

        return _call_with_wall_clock_guard(
            _do_put, timeout_s, label="checkpoint_put"
        )
