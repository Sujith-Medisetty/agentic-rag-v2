"""
Session runner — drives the LangGraph agent loop for a single chat turn,
piping events through a per-session WebReporter.

Threading model:
  - FastAPI handlers are async, running on the main event loop.
  - The LangGraph runner is SYNC. We offload it to a worker thread via
    loop.run_in_executor so the event loop stays responsive for WebSocket
    fan-out and other requests.
  - ContextVars (the reporter scope) are copied into the executor thread by
    asyncio automatically, so the WebReporter set on the parent context is
    the one the agent sees.

Per-session config:
  - thread_id == session_id → LangGraph's SqliteSaver checkpointer picks up
    where the previous turn left off.
  - workspace path comes from the project's stored workspace_path.
  - Safety / model / hooks / sandbox are configured once at server startup
    (see server.app.lifespan) — they're process-wide singletons today.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import re
import sys
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.reporter import reporter_scope
from server import db
from server.git_autocommit import (
    autocommit as run_autocommit,
    push_to_remote,
)
from server.reporter import WebReporter, get_bus


def session_state_dir(session_id: str) -> Path:
    """Per-session private directory for sub-agent records, todo store, and
    any other agent-loop artifacts. Lives under `~/.agent/sessions/<id>/`
    so it's outside any user workspace (no pollution of project trees) and
    deletable as a unit when a session is removed.

    Without this, the agent's stores (`.clawd-agents/`, `.clawd-todos.json`)
    used `Path.cwd()` and were SHARED across every project + session —
    sub-agent records from one session leaked into another, and deleting a
    session left orphan files forever.
    """
    base = Path.home() / ".agent" / "sessions" / session_id
    base.mkdir(parents=True, exist_ok=True)
    return base


def _default_max_iterations() -> int:
    # Unlimited by default — this is an autonomous coding agent, and a hard
    # iteration cap was cutting off legitimate long builds mid-way. The real
    # safety net is elsewhere:
    #   - no_progress_limit=8 catches actual stalls (8 identical tool calls)
    #   - per-LLM-call timeout catches model hangs
    #   - the cancel button is your manual escape hatch
    # If you DO want a cap (e.g. cost control in a deployment), set
    # AGENT_MAX_ITERATIONS to a positive integer. 0 or unset = no cap.
    try:
        return max(0, int(os.getenv("AGENT_MAX_ITERATIONS", "0")))
    except ValueError:
        return 0


async def run_turn(
    session_id: str,
    project_id: str,
    workspace: str,
    user_prompt: str,
    max_iterations: int | None = None,
) -> None:
    """Persist the user message, run one agent turn end-to-end, persist the
    final assistant text. Designed to be fire-and-forgotten from the HTTP
    handler — all progress is streamed via the SessionBus.

    Turn lifecycle invariant:
      every turn — success OR failure — ends with exactly one
      `turn_summary` event AND one `assistant_text(done=True)` event.
      The UI uses these to freeze its live indicators (Thinking…, elapsed
      timer, streaming dot). Skipping either on any exit path will hang
      the live counters forever; that's how the "still counting after
      error" bug previously happened.
    """
    if max_iterations is None:
        max_iterations = _default_max_iterations()
    # Persist the user message immediately so the chat history is up to date
    # even before the model produces anything.
    db.append_message(session_id, "user", user_prompt)

    reporter = WebReporter(session_id)
    reporter.user_message(user_prompt)

    started = time.monotonic()
    loop = asyncio.get_running_loop()

    # Make sure the bus is bound to THIS loop before we hand work to the
    # executor (publishes from the agent thread need a live loop to
    # schedule onto).
    bus = get_bus(session_id)
    if not bus.is_bound():
        bus.bind_loop(loop)

    # Token snapshots are captured INSIDE `_drive()` (see below), not here at
    # the top of `run_turn`. The `tc` counter is a process-wide singleton
    # shared across every concurrent session — capturing `before` here would
    # race with other sessions' `_drive`s queued behind this one in the
    # single-threaded executor: their LLM calls would land in `tc` between
    # our `before` snapshot and our `_drive` actually starting, making
    # `after - before` include tokens that were never this turn's. The
    # executor runs drives strictly sequentially, so the snapshot taken as
    # the first action inside `_drive` is guaranteed to be immediately
    # before this turn's LLM calls (and nothing else's).
    _drive_snapshots: dict = {}

    iters = 0
    turn_failed = False
    _turn_cancelled = False  # set to True when the asyncio Task is cancelled

    def _drive() -> int:
        """Sync body that runs in a worker thread. Returns iteration count.
        Raises whatever the graph raised — we let it propagate so the outer
        try/except in run_turn can synthesize the right end-of-turn events
        from a single place."""
        # Snapshot the token counter AT THE TOP of `_drive` — i.e. as soon as
        # the single-threaded executor starts running this turn. See the
        # block comment above for why this isn't done in `run_turn` itself.
        # Lazy import so this file can be imported in tests without langchain.
        from agents.nodes import get_token_counter
        _tc = get_token_counter()
        if _tc is not None:
            _drive_snapshots["before"] = _tc.cumulative
            # Keep the full CostEstimate (not just .total) so we can emit
            # per-component cost sub-totals — the UI uses them to show
            # "cost of in vs cost of out" and the cache-savings split
            # without re-pricing on the client.
            _drive_snapshots["cost_before"] = _tc.cost()

        # Tool-count snapshot. LangGraph keeps cumulative message history
        # across turns (same thread_id), so we diff before vs. after to get
        # "tools used in THIS turn" rather than "tools used in the whole
        # session". Done here (inside `_drive`) for symmetry with the token
        # snapshot, even though LangGraph state is per-thread and doesn't
        # suffer the same cross-session race.
        _tools_before = 0
        try:
            from agents.graph import runner_graph as _rg
            _state_before = _rg.get_state({
                "configurable": {"thread_id": session_id},
            }).values
            _tools_before = _count_tool_uses(_state_before.get("messages", []) or [])
        except Exception:
            pass
        _drive_snapshots["tools_before"] = _tools_before

        from agents.graph import runner_graph
        from agents.nodes import reset_run_budget

        # Pin the sub-agent + todo stores to THIS session's private dir BEFORE
        # we hand work to the graph. Without this, both stores fall back to
        # `Path.cwd()` and every project/session shares one folder — sub-agent
        # records leak across sessions and deletes leave orphan files forever.
        # We set the env vars (the existing override hooks in
        # tools/multi_agent._agent_store_dir and tools/utils._todo_store_path)
        # right before invoking the graph. The default asyncio executor is
        # single-threaded so concurrent turns don't race on these env vars in
        # practice; if you ever raise the executor pool, swap this for a
        # ContextVar.
        session_root = session_state_dir(session_id)
        os.environ["CLAWD_AGENT_STORE"] = str(session_root / "clawd-agents")
        os.environ["CLAWD_TODO_STORE"]  = str(session_root / "clawd-todos.json")

        # Activate the workspace jail for this turn. Look up the session's
        # user role so root bypasses the jail. The sandbox is per-ContextVar,
        # so the executor thread sees it because we wrap _drive in
        # `ctx.run(...)` below.
        from tools.sandbox import set_session_sandbox
        from server import db
        is_root = False
        try:
            sess_row = db.get_session(session_id)
            if sess_row and sess_row.get("user_id"):
                u = db.get_user(sess_row["user_id"])
                if u and u.get("role") == "root":
                    is_root = True
        except Exception:
            pass
        set_session_sandbox(workspace, is_root, session_id)

        reset_run_budget(
            max_iters=max_iterations,
            max_tokens=0,         # token/time budgets are off by default in web mode
            max_seconds=0,
            no_progress_limit=8,
        )
        config = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": (
                (max_iterations * 2 + 10) if max_iterations > 0 else 100_000
            ),
        }
        # Append the new user message to BOTH channels: `messages`
        # (add_messages reducer, appends to the accumulator) and
        # `live_messages` (REPLACE reducer — we read the prior turn's
        # working set, append, write the full list). Without this,
        # `live_messages` stays stale and the LLM fires with the prior
        # turn's context, never seeing the user's new question.
        prior_live: list = []
        try:
            prior_live = list(
                runner_graph.get_state(config).values.get("live_messages") or []
            )
        except Exception:
            pass
        initial_state = {
            "messages":        [HumanMessage(content=user_prompt)],
            "live_messages":   list(prior_live) + [HumanMessage(content=user_prompt)],
            "task":            user_prompt,
            "workspace":       workspace,
            "repo":            "",
            "project_context": _load_claude_md(workspace),
            "mode":            "auto",
            "iterations":      0,
            "max_iterations":  max_iterations,
            # Plumbed so node_agent can key the cross-turn
            # maybe_compact / record_llm_input_tokens cache by session.
            "session_id":      session_id,
            # Fresh todo state for the new turn. The TodoWrite tool
            # will overwrite `last_todos` after its first call of
            # this turn. `todo_sync_nudged` resets so the end-of-
            # task sync gate can fire once on THIS turn if needed.
            "last_todos":      [],
            "todo_sync_nudged": False,
        }
        for _ in runner_graph.stream(
            initial_state, config=config, stream_mode="updates",
        ):
            pass

        # Happy path — persist final assistant text and close the stream.
        # Skip if the asyncio Task was already cancelled (the cancel path
        # handles persistence itself; writing here too creates a duplicate
        # message because the executor thread can't be interrupted mid-run).
        if _turn_cancelled:
            return 0
        final = runner_graph.get_state(config)
        final_text = _final_assistant_text(final.values.get("messages", []))
        if final_text:
            db.append_message(session_id, "assistant", final_text)
        reporter.assistant_text(final_text or "", done=True)
        return final.values.get("iterations", 0)

    # (Previously a `_watch_preview` task ran here and emitted preview_ready
    # events on every build. Removed when we replaced the temporary preview
    # URL flow with the explicit Deploy button — the chat now shows a
    # deploy strip from on-mount lookup of `deployed_apps`, not from
    # build-time event emission.)

    try:
        with reporter_scope(reporter):
            # `loop.run_in_executor` does NOT propagate ContextVars to the
            # worker thread, so without this snapshot the agent loop would see
            # the no-op default reporter — every tool_start / tool_done /
            # token_update / assistant_text chunk would silently vanish, and
            # the user would only see the final flushed text + turn_summary
            # (which fire from this async context where the scope IS active).
            # Wrapping `_drive` in `ctx.run(...)` carries the scope through.
            ctx = contextvars.copy_context()
            iters = await loop.run_in_executor(None, ctx.run, _drive)
    except asyncio.CancelledError:
        # Cancel path — user hit cancel (or the task was aborted). MUST be a
        # separate clause because CancelledError inherits from BaseException,
        # not Exception, so the `except Exception` below would miss it and
        # the turn_summary block would never run — leaving the UI's live
        # indicators frozen and the turn unclosed in the persisted event log.
        # Don't re-raise: we want the finalizer below to publish turn_summary
        # and the function to return cleanly.
        # Set the flag BEFORE doing anything else so the still-running
        # executor thread sees it and skips its own db.append_message call
        # (avoiding the duplicate-message-on-cancel bug).
        _turn_cancelled = True
        turn_failed = True
        reporter.error("cancelled by user")
        reporter.assistant_text("", done=True)
        db.append_message(session_id, "assistant", "[cancelled by user]")

        # Persist the user message that started this turn into the
        # LangGraph state. Without this, a turn that was cancelled
        # mid-stream leaves no checkpoint (the LLM was still streaming
        # when the cancel landed) and the user message is LOST — the
        # next turn reads the pre-cancel state and never sees the
        # interrupted question.
        #
        # IMPORTANT: only append if the user prompt is NOT already in
        # `live_messages`. The happy-path `_drive` already seeds the
        # initial state with `prior_live + [HumanMessage(user_prompt)]`
        # before streaming, so if the cancel lands AFTER that seed
        # (which is almost always — node_agent hasn't had a chance to
        # run yet) the prompt is already there. The earlier version
        # of this code unconditionally appended, which duplicated
        # every cancelled user prompt and the second copy later got
        # swept away by `maybe_compact` — taking neighboring human
        # messages (i.e. the prior turn's real question) with it.
        # Content-match dedup is good enough here; the seed and the
        # append use the same user_prompt string and the next turn
        # doesn't re-emit the same string verbatim.
        try:
            from agents.graph import runner_graph
            cfg = {"configurable": {"thread_id": session_id}}
            prior_live = list(
                runner_graph.get_state(cfg).values.get("live_messages") or []
            )
            already_present = any(
                getattr(m, "content", None) == user_prompt
                and getattr(m, "type", None) == "human"
                for m in prior_live
            )
            if not already_present:
                new_live = prior_live + [HumanMessage(content=user_prompt)]
                runner_graph.update_state(
                    cfg,
                    {
                        "messages": [HumanMessage(content=user_prompt)],
                        "live_messages": new_live,
                    },
                )
        except Exception:
            pass
    except Exception as e:
        # Error path — surface the error, close the stream so the UI stops
        # showing "Thinking…", and persist a system message so the user
        # sees what went wrong on reload too.
        turn_failed = True
        msg = str(e) or e.__class__.__name__
        reporter.error(msg)
        reporter.assistant_text("", done=True)
        db.append_message(session_id, "assistant", f"[error] {msg}")

    # ALWAYS publish turn_summary — success OR failure. Token diff covers
    # whatever the model actually consumed before this turn ended (zero if
    # it crashed before the first model call).
    #
    # The `before` / `cost_before` snapshots were captured INSIDE `_drive`
    # (see the snapshot block at the top of `_drive`) and stashed in
    # `_drive_snapshots` so the executor boundary didn't race them against
    # other concurrent sessions sharing the same process-wide counter.
    # If `_drive` raised before reaching the snapshot, both stay absent
    # and the diff falls back to 0 — correct behaviour for a turn that
    # crashed before its first model call.
    tc_after = get_token_counter()
    after = tc_after.cumulative if tc_after else None
    cost_after = tc_after.cost() if tc_after else None
    before = _drive_snapshots.get("before")
    cost_before = _drive_snapshots.get("cost_before")
    turn_in   = (after.input_tokens          - before.input_tokens)          if before and after else 0
    turn_out  = (after.output_tokens         - before.output_tokens)         if before and after else 0
    turn_cr   = (after.cache_read_tokens     - before.cache_read_tokens)     if before and after else 0
    turn_cw   = (after.cache_creation_tokens - before.cache_creation_tokens) if before and after else 0
    # Per-component cost diffs. Use max(0, …) because CostEstimate can
    # theoretically dip between snapshots if the same model were re-priced
    # mid-turn (e.g. a tool swapped the active model). Total is the sum of
    # the four sub-totals, not a separate read of cost_after.total.
    turn_cost_in  = max(0.0, cost_after.input_cost      - cost_before.input_cost)      if cost_after and cost_before else 0.0
    turn_cost_out = max(0.0, cost_after.output_cost     - cost_before.output_cost)     if cost_after and cost_before else 0.0
    turn_cost_cr  = max(0.0, cost_after.cache_read_cost - cost_before.cache_read_cost) if cost_after and cost_before else 0.0
    turn_cost_cw  = max(0.0, cost_after.cache_write_cost - cost_before.cache_write_cost) if cost_after and cost_before else 0.0
    turn_cost = turn_cost_in + turn_cost_out + turn_cost_cr + turn_cost_cw

    # `tools_used` should be the count of tool invocations in THIS turn, not
    # cumulative across the session. ToolMessages-in-state diff is the most
    # reliable signal (iter count over-counts by 1 for the final no-tool turn).
    tools_before = _drive_snapshots.get("tools_before", 0)
    tool_count = max(0, iters - 1)  # fallback if state lookup fails
    try:
        from agents.graph import runner_graph
        _state_after = runner_graph.get_state({
            "configurable": {"thread_id": session_id},
        }).values
        tools_after = _count_tool_uses(_state_after.get("messages", []) or [])
        tool_count = max(0, tools_after - tools_before)
    except Exception:
        pass

    # (Preview watcher teardown removed alongside the watcher itself —
    # nothing to stop, nothing to flush.)

    reporter.turn_summary(
        tools_used           = tool_count,
        duration_ms          = int((time.monotonic() - started) * 1000),
        input_tokens         = max(0, turn_in),
        output_tokens        = max(0, turn_out),
        cache_read_tokens    = max(0, turn_cr),
        cache_write_tokens   = max(0, turn_cw),
        cost_usd             = turn_cost,
        cost_input_usd       = turn_cost_in,
        cost_output_usd      = turn_cost_out,
        cost_cache_read_usd  = turn_cost_cr,
        cost_cache_write_usd = turn_cost_cw,
    )

    # Background LLM-suggested rename. After a turn finishes, if the
    # session still has a default-looking name (e.g. "Session 6/5/2026,
    # 10:34:21 PM" — the placeholder the project view creates), ask the
    # LLM for a short, descriptive title and PATCH it. Best-effort +
    # fire-and-forget: never blocks the turn flow, never raises into
    # the turn lifecycle, never visible to the user if the LLM fails.
    if not turn_failed:
        try:
            import asyncio as _aio
            _aio.create_task(_maybe_auto_rename(session_id))
        except Exception:
            pass

    # Failed turns skip auto-commit — there might be half-written files,
    # and the user wants to see the failure rather than have it silently
    # committed.
    if turn_failed:
        return

    # Auto-commit anything the turn changed. Reads the most-recent project
    # row (settings might have flipped mid-turn) and ALWAYS swallows errors
    # so commit problems can't propagate into the turn flow.
    project = db.get_project(project_id) or {}
    if not project.get("auto_commit_enabled", True):
        return
    changed_paths = reporter.consume_changed_paths()
    if not changed_paths:
        return
    try:
        await loop.run_in_executor(
            None,
            lambda: _autocommit_and_maybe_push(
                workspace=project.get("workspace_path", workspace),
                session_id=session_id,
                user_prompt=user_prompt,
                strategy=project.get("branch_strategy", "session"),
                push=bool(project.get("auto_push_enabled", False)),
                reporter=reporter,
            ),
        )
    except Exception as e:   # commit/push must never break the turn
        reporter.commit_skipped(reason=f"unexpected error: {e}")


def _autocommit_and_maybe_push(
    workspace: str,
    session_id: str,
    user_prompt: str,
    strategy: str,
    push: bool,
    reporter: WebReporter,
) -> None:
    """Sync body (runs in the executor). Publishes results back through the
    reporter — caller's responsibility to ensure the reporter is bound to
    the same WebSocket bus this session uses."""
    result = run_autocommit(
        workspace=workspace,
        session_id=session_id,
        user_prompt=user_prompt,
        branch_strategy=strategy,
    )
    if result.committed:
        reporter.commit_made(
            sha=result.sha, branch=result.branch,
            message=result.message, files=result.files,
        )
    else:
        reporter.commit_skipped(
            reason=result.skip_reason or "unknown",
            branch=result.branch,
            hook_output=result.hook_output,
        )
        return   # nothing to push if nothing committed

    if push:
        pr = push_to_remote(workspace=workspace, branch=result.branch)
        reporter.push_done(
            branch=pr.branch or result.branch,
            ok=pr.pushed, remote=pr.remote, error=pr.error,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _final_assistant_text(messages: list) -> str:
    """Return the last assistant message's text, with any inline `<think>…</think>`
    chain-of-thought blocks stripped. Handles string and list content
    (Anthropic returns a list of content blocks)."""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        c = msg.content
        text: str = ""
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            parts = []
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(block, str):
                    parts.append(block)
            text = "\n".join(parts)
        if text.strip():
            return _strip_thinking_tags(text).strip()
    return ""


# Inline-thinking format used by MiniMax M2 / DeepSeek-R1 / Qwen-thinking.
# We already route streamed-chunk thinking to its own UI channel via the
# splitter in agents/nodes.py, but the canonical AIMessage.content still
# contains the raw tags — strip them here so the final flush + the
# persisted-message DB row are tag-free.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking_tags(text: str) -> str:
    return _THINK_TAG_RE.sub("", text)


def _count_tool_uses(messages: list) -> int:
    """Count unique tool invocations across the turn. Each tool call lands as
    one ToolMessage (the tool's result) — counting those is the most reliable
    way to get the "real" tool count, independent of how many model iterations
    the agent loop took."""
    return sum(1 for m in messages if isinstance(m, ToolMessage))


def _load_claude_md(workspace: str) -> str:
    """Pick up CLAUDE.md / CLAUDE.local.md / .agent.md if present in the
    workspace root and concatenate them as extra instructions for the
    agent prompt."""
    candidates = ("CLAUDE.md", "CLAUDE.local.md", ".agent.md")
    parts: list[str] = []
    root = Path(workspace)
    for name in candidates:
        p = root / name
        if p.is_file():
            try:
                content = p.read_text(encoding="utf-8").strip()
                if content:
                    parts.append(f"## {name}\n\n{content}")
            except OSError:
                pass
    return "\n\n".join(parts)


# =============================================================================
# LLM-suggested session rename
# =============================================================================
# After a turn finishes, if the session still has a default name (e.g. the
# "Session 6/5/2026, 10:34:21 PM" placeholder the project view creates), we
# ask the LLM for a 3-5 word descriptive title and rename. Best-effort —
# never raises, never blocks the turn, never overwrites a name the user
# already set. If the LLM call times out or returns empty, we leave the
# placeholder in place and let the next turn retry.

_AUTO_RENAME_DEFAULT_PREFIXES = ("Session ", "Chat ")  # placeholder prefixes
_AUTO_RENAME_HISTORY: set[str] = set()     # session_ids already auto-renamed
# Title-suggestion LLM call budget. The configured model (e.g. MiniMax-M3)
# thinks for a few seconds on a title-suggestion prompt and then emits a
# short answer, so end-to-end is usually a handful of seconds. A tighter
# budget killed the call mid-generation and silently kept the placeholder.
# 60s is a very comfortable ceiling that still feels instant to a human
# — the rename runs in the background after the turn completes.
_AUTO_RENAME_LLM_TIMEOUT_S = 60.0

import asyncio as _asyncio
import re as _re


def _looks_like_default_name(name: str) -> bool:
    """True for the placeholder the project view creates — currently
    "Chat <date>, <time>" (the Workspace sidebar seeds new sessions with
    that exact format). We only auto-rename those — never overwrite a
    name the user explicitly set (even if it's also generic, e.g. "test").

    The heuristic: the name starts with one of the known placeholder
    prefixes AND the rest looks like a timestamp (has a comma, slash,
    or any digit). This avoids over-eagerly renaming things like
    "Session 0" which a user might have actually typed."""
    if not name:
        return True
    n = name.strip()
    prefix = next((p for p in _AUTO_RENAME_DEFAULT_PREFIXES if n.startswith(p)), None)
    if prefix is None:
        return False
    rest = n[len(prefix):].strip()
    return ("," in rest) or ("/" in rest) or bool(_re.search(r"\d", rest))


async def _maybe_auto_rename(session_id: str) -> None:
    """If the session is still on a default name, ask the LLM for a
    descriptive title and rename. Idempotent: runs at most once per
    session (tracked in _AUTO_RENAME_HISTORY so we don't burn tokens
    on every subsequent turn if the rename failed).

    Cost guarantee: the LLM is ONLY called when the session name
    still looks like a default placeholder ("Chat <date>, <time>" or
    "Session <date>, <time>"). If the user has already given the
    session a custom name (e.g. "Fix login bug"), this is a no-op —
    the user's intent is preserved."""
    if session_id in _AUTO_RENAME_HISTORY:
        return
    # NOTE: we add to _AUTO_RENAME_HISTORY only AFTER a successful
    # rename. Otherwise a flaky LLM (empty title, timeout, transient
    # provider error) would poison the cache and prevent retries on
    # subsequent turns.
    try:
        sess = db.get_session(session_id)
        if sess is None:
            print(f"[auto-rename] {session_id[:8]}: no session row", flush=True)
            return
        current_name = sess.get("name", "")
        if not _looks_like_default_name(current_name):
            print(f"[auto-rename] {session_id[:8]}: name {current_name!r} is not a default placeholder — skipping", flush=True)
            return
        # Pull the first user prompt + the first assistant response.
        msgs = db.list_messages(session_id)
        if not msgs:
            print(f"[auto-rename] {session_id[:8]}: no messages", flush=True)
            return
        first_user = next((m for m in msgs if m["role"] == "user"), None)
        if first_user is None:
            print(f"[auto-rename] {session_id[:8]}: no user message", flush=True)
            return
        first_assistant = next(
            (m for m in msgs if m["role"] == "assistant" and m.get("content")),
            None,
        )
        if first_assistant is None:
            print(f"[auto-rename] {session_id[:8]}: no assistant message", flush=True)
            return
        # Compose a compact prompt for the LLM.
        prompt_user = first_user["content"][:1000]
        prompt_asst = first_assistant["content"][:1000]
        suggest_prompt = (
            "Based on the following exchange, suggest a concise, descriptive "
            "title for this chat session. Reply with ONLY the title — no "
            "quotes, no prefix, no punctuation at the end. 3-6 words. Title "
            "Case. Do not use generic words like 'Session' or 'Chat'.\n\n"
            f"USER:\n{prompt_user}\n\n"
            f"ASSISTANT:\n{prompt_asst}\n\n"
            "TITLE:"
        )
        title = await _call_llm_for_title(suggest_prompt)
        if not title:
            # LLM returned empty (timeout, model quirks, or stripped
            # of its think-block). Don't rename — the next turn can
            # retry, and silently overwriting the placeholder with a
            # guess would mask what the LLM could have produced.
            print(f"[auto-rename] {session_id[:8]}: LLM returned empty title, leaving placeholder", flush=True)
            return
        # Sanitize: strip quotes, collapse whitespace, cap length.
        title = title.strip().strip('"').strip("'").strip("`")
        title = _re.sub(r"\s+", " ", title).strip()
        title = title.strip(".,;:!?-—–")
        if not title or len(title) > 80:
            print(f"[auto-rename] {session_id[:8]}: sanitized title {title!r} invalid", flush=True)
            return
        # Reject if the LLM gave us back the default placeholder.
        if _looks_like_default_name(title):
            print(f"[auto-rename] {session_id[:8]}: LLM title {title!r} still looks like a default", flush=True)
            return
        # Apply. Use allocate_unique_session_name for the suffix path so we
        # capture was_suffixed for the UI toast. (allocate_unique_session_name
        # just picks a name; rename_session actually writes it.)
        try:
            final = db.allocate_unique_session_name(
                project_id=sess["project_id"],
                desired=title,
                exclude_id=session_id,
            )
            was_suffixed = (final != title)
            db.rename_session(session_id, final)
            # Notify the WebSocket so the sidebar + chat header update
            # without a manual refresh.
            try:
                WebReporter(session_id).session_renamed(
                    new_name=final,
                    previous_name=current_name,
                    was_suffixed=was_suffixed,
                )
            except Exception:
                pass
            # Only mark this session as done AFTER a successful
            # rename (and WS event) — a flake should be retryable
            # on the next turn, not silently cached as "done".
            _AUTO_RENAME_HISTORY.add(session_id)
            print(f"[auto-rename] {session_id[:8]}: '{current_name}' → '{final}' (suffixed={was_suffixed})", flush=True)
        except Exception as e:
            print(f"[auto-rename] {session_id[:8]}: rename failed: {e}", flush=True)
    except Exception as e:
        # Swallow everything — this is best-effort and must never bleed
        # into the turn flow or the user's UI.
        print(f"[auto-rename] {session_id[:8]}: unexpected error: {e}", flush=True)
        pass


async def _call_llm_for_title(prompt: str) -> str | None:
    """One-shot LLM call to generate a session title. Uses the same
    model the agent is configured with so titles match the agent's
    "voice". Bounded latency: see _AUTO_RENAME_LLM_TIMEOUT_S — the
    model thinks for a few seconds on this prompt and then emits a
    short answer, so the budget has to cover the FULL think + answer
    round-trip, not just the answer.

    IMPORTANT: this model emits extended-thinking blocks even when
    asked not to, so the raw response usually looks like
    "<think>...reasoning...</think>\n\nFix Login Flow". We strip
    everything before the closing </think> tag and take what follows.
    If there's no think block, the response is used as-is.

    The stripper has to be robust against several quirks of the
    MiniMax / Claude / GPT families:
      - think blocks whose content contains `</think>` (nested,
        model slip) — greedy DOTALL match would eat the answer.
      - think blocks followed by nothing (model thought but
        forgot to answer) — log + return None so the caller
        leaves the placeholder in place and the next turn retries.
      - JSON-wrapped answers (`{"title": "..."}`) from models
        that follow the "format as JSON" hint too literally.
      - Markdown code-fence wrapping around the answer.
    """
    t0 = time.monotonic()
    try:
        from agents.nodes import _get_llm
        llm = _get_llm(streaming=False, thinking=False)
        from langchain_core.messages import HumanMessage
        coro = llm.ainvoke([HumanMessage(content=prompt)])
        msg = await _asyncio.wait_for(coro, timeout=_AUTO_RENAME_LLM_TIMEOUT_S)
        # `msg.content` is either a string or a list of content blocks.
        raw = ""
        if isinstance(msg.content, str):
            raw = msg.content
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        raw += block.get("text", "")
                elif hasattr(block, "text"):
                    raw += block.text
        else:
            raw = str(msg.content)
        if not raw:
            print(f"[auto-rename] LLM returned empty content blocks", file=sys.stderr, flush=True)
            return None
        # Strip <think>...</think> blocks. Use a non-greedy match
        # AND require the closing tag to actually appear. If the
        # model emits an unclosed think block (truncation, model
        # bug), fall back to the raw content rather than eating
        # the whole response.
        if "<think>" in raw and "</think>" in raw:
            cleaned = _re.sub(r"<think>.*?</think>", "", raw, flags=_re.DOTALL)
        elif "<think>" in raw:
            # Unclosed think block — everything after <think> is the
            # model's answer (or empty). Strip just the opening tag.
            cleaned = raw.split("<think>", 1)[1]
        else:
            cleaned = raw
        # JSON wrapping: some models follow a "format as JSON" hint
        # too literally and return {"title": "..."} or {"answer": "..."}.
        m = _re.search(r'"(?:title|answer|name)"\s*:\s*"([^"]+)"', cleaned)
        if m:
            cleaned = m.group(1)
        # Markdown code fences: strip ```json\n...\n``` or ```\n...\n```
        cleaned = _re.sub(r"^```[a-zA-Z]*\s*\n?", "", cleaned.strip())
        cleaned = _re.sub(r"\n?```\s*$", "", cleaned.strip())
        cleaned = cleaned.strip()
        if not cleaned:
            print(f"[auto-rename] LLM response stripped to empty after {time.monotonic()-t0:.2f}s (raw was {raw[:200]!r})", file=sys.stderr, flush=True)
            return None
        print(f"[auto-rename] LLM title OK in {time.monotonic()-t0:.2f}s: {cleaned!r}", file=sys.stderr, flush=True)
        return cleaned
    except _asyncio.TimeoutError:
        print(f"[auto-rename] LLM title-suggestion timed out ({_AUTO_RENAME_LLM_TIMEOUT_S}s)", file=sys.stderr, flush=True)
        return None
    except Exception as e:
        print(f"[auto-rename] LLM title-suggestion error after {time.monotonic()-t0:.2f}s: {e}", file=sys.stderr, flush=True)
        return None
