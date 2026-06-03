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
import time
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from agents.reporter import reporter_scope
from server import db
from server.git_autocommit import (
    autocommit as run_autocommit,
    push_to_remote,
)
from server.reporter import WebReporter, get_bus


async def run_turn(
    session_id: str,
    project_id: str,
    workspace: str,
    user_prompt: str,
    max_iterations: int = 50,
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

    # Token snapshot BEFORE the turn (works for both success + error paths).
    # Lazy import so this file can be imported in tests without langchain.
    from agents.nodes import get_token_counter
    tc = get_token_counter()
    before = tc.cumulative if tc else None
    cost_before = tc.cost().total if tc else 0.0

    iters = 0
    turn_failed = False

    def _drive() -> int:
        """Sync body that runs in a worker thread. Returns iteration count.
        Raises whatever the graph raised — we let it propagate so the outer
        try/except in run_turn can synthesize the right end-of-turn events
        from a single place."""
        from agents.graph import runner_graph
        from agents.nodes import reset_run_budget

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
        initial_state = {
            "messages":        [HumanMessage(content=user_prompt)],
            "task":            user_prompt,
            "workspace":       workspace,
            "repo":            "",
            "project_context": _load_claude_md(workspace),
            "mode":            "auto",
            "iterations":      0,
            "max_iterations":  max_iterations,
        }
        for _ in runner_graph.stream(
            initial_state, config=config, stream_mode="updates",
        ):
            pass

        # Happy path — persist final assistant text and close the stream.
        final = runner_graph.get_state(config)
        final_text = _final_assistant_text(final.values.get("messages", []))
        if final_text:
            db.append_message(session_id, "assistant", final_text)
        reporter.assistant_text(final_text or "", done=True)
        return final.values.get("iterations", 0)

    try:
        with reporter_scope(reporter):
            iters = await loop.run_in_executor(None, _drive)
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
    tc_after = get_token_counter()
    after = tc_after.cumulative if tc_after else None
    cost_after = tc_after.cost().total if tc_after else 0.0
    turn_in   = (after.input_tokens          - before.input_tokens)          if before and after else 0
    turn_out  = (after.output_tokens         - before.output_tokens)         if before and after else 0
    turn_cr   = (after.cache_read_tokens     - before.cache_read_tokens)     if before and after else 0
    turn_cw   = (after.cache_creation_tokens - before.cache_creation_tokens) if before and after else 0
    turn_cost = max(0.0, cost_after - cost_before)

    reporter.turn_summary(
        tools_used         = iters,
        duration_ms        = int((time.monotonic() - started) * 1000),
        input_tokens       = max(0, turn_in),
        output_tokens      = max(0, turn_out),
        cache_read_tokens  = max(0, turn_cr),
        cache_write_tokens = max(0, turn_cw),
        cost_usd           = turn_cost,
    )

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
    """Return the last assistant message's text. Handles string and list
    content (Anthropic returns a list of content blocks)."""
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        c = msg.content
        if isinstance(c, str):
            if c.strip():
                return c
        elif isinstance(c, list):
            parts = []
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        parts.append(t)
                elif isinstance(block, str):
                    parts.append(block)
            joined = "\n".join(parts).strip()
            if joined:
                return joined
    return ""


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
