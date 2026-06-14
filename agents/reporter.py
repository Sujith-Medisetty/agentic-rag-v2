"""
Progress reporter — the abstraction the agent loop uses to surface activity.

There's exactly one production implementation: server.reporter.WebReporter
(pushes events to a per-session WebSocket bus). The base class here is a
working no-op so:
  - Any unit test or one-off script that imports tools.wrappers without
    setting up a reporter scope keeps working.
  - The default value of the ContextVar is a real object, not None — every
    reporter method is always callable.

Per-task isolation lives on a contextvars.ContextVar so concurrent web
sessions get their own reporter without stepping on each other. The
ContextVar is automatically copied into asyncio.run_in_executor() workers,
so the agent's worker thread sees the right reporter for its session.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager


class ProgressReporter:
    """Default implementation = no-op. Subclasses override the methods
    they care about. Adding a new event type? Add it here as a `pass`
    method so callers never need to check `hasattr`."""

    # ---- core lifecycle ------------------------------------------------
    def tool_start(self, tool: str, target: str = "") -> None:
        """About to invoke `tool`. `target` is a short preview (path, query, …)."""

    def tool_done(self, tool: str, preview: str = "", error: bool = False) -> None:
        """`tool` finished. `preview` is the first line of output; `error=True`
        marks a failed call (timeout, exception, BLOCKED:…)."""

    def message(self, text: str) -> None:
        """Generic user-facing message (used by SendUserMessage and budget pauses)."""

    # ---- streaming model output ---------------------------------------
    def assistant_text(self, text: str, done: bool = False) -> None:
        """Streamed assistant text chunk. `done=True` marks the end of a turn —
        the WebReporter uses this to flush the live bubble into history."""

    def thinking_text(self, text: str, done: bool = False) -> None:
        """Streamed model-reasoning chunk. Routed to a dedicated UI section so
        the chain-of-thought never blends into the visible answer. Used for
        Anthropic's `thinking` content blocks AND OpenAI-compatible models
        that emit `<think>...</think>` inline (MiniMax M2, DeepSeek-R1, Qwen)."""

    def token_update(
        self,
        input_delta: int = 0,
        output_delta: int = 0,
        cache_read_delta: int = 0,
        cache_creation_delta: int = 0,
    ) -> None:
        """Published after each LLM call within a turn so the UI can show live,
        ticking input/output token counts (rather than waiting for the
        end-of-turn summary). Deltas — frontend accumulates them per turn.
        `cache_read_delta` / `cache_creation_delta` are the per-call
        cache-hit / cache-write token counts from the provider's usage
        metadata; 0 when the provider doesn't surface them."""

    def context_update(
        self,
        *,
        used_tokens: int,
        budget_tokens: int,
        warning: bool = False,
        compacting: bool = False,
        threshold: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        """Published on context-changing events (after each LLM call, around
        auto-compaction) so the UI can show a Claude Code-style "75% used"
        bar. `warning=True` means we're at the warn tier (~50K); `compacting=True`
        means the agent is currently summarising old messages. `threshold` is
        the auto-compact threshold (50K default) so the chip can show
        "X% to compact" against the right denominator.

        `used_tokens` is the NEW (uncached + writes) tokens the model
        had to process this turn — not the total prompt including
        cache hits. Use `cache_read` + `cache_creation` to surface
        the cached fraction in the UI tooltip ("X new, Y cache hits"),
        so the user can see when a long session is being kept cheap
        by the prompt cache. Without this split, the chip would
        inflate by the entire static system prompt on every turn
        (a 5-turn session reading as 76k tokens of "context used"
        just because the prefix keeps hitting cache)."""

    def context_compacted(
        self,
        *,
        removed: int,
        kept: int,
        tokens_before: int,
        tokens_after: int,
        summary_preview: str = "",
        threshold: int = 0,
    ) -> None:
        """Chat-visible notification when auto-compaction fires. The UI
        renders this as a system message in the transcript with a one-line
        summary of what was kept / summarised, so the user can SEE that
        older turns were collapsed and what the agent now remembers about
        them. The full summary is still in the conversation as a
        HumanMessage — this event is the breadcrumb that says "look here
        for what got summarised"."""

    # ---- rich activity (Phase 2) --------------------------------------

    # ---- rich activity (Phase 2) --------------------------------------
    def todo_update(self, items: list[dict]) -> None:
        """Full current todo list after a TodoWrite call. Items are
        {content, status: pending|in_progress|completed, activeForm}."""

    def agent_spawn(
        self,
        agent_id: str,
        description: str,
        subagent_type: str,
        name: str = "",
        model: str = "",
    ) -> None:
        """A sub-agent was just spawned via the Agent tool."""

    def agent_status_update(
        self,
        agent_id: str,
        status: str,
        output_file: str = "",
        error: str = "",
    ) -> None:
        """A sub-agent's manifest just changed (running → completed/failed)."""

    def file_changed(
        self,
        path: str,
        kind: str,
        diff: str,
        bytes_count: int = 0,
    ) -> None:
        """A file was created or edited. `kind` is 'create' or 'edit';
        `diff` is a unified-diff string (or full content for new files)."""

    # ---- commit lifecycle (Phase 4) -----------------------------------
    def commit_made(
        self,
        sha: str,
        branch: str,
        message: str,
        files: list[str],
    ) -> None:
        """Auto-commit succeeded on `branch`."""

    def commit_skipped(
        self,
        reason: str,
        branch: str = "",
        hook_output: str = "",
    ) -> None:
        """Auto-commit skipped (no changes, protected branch, hook failed, …)."""

    def push_done(
        self,
        branch: str,
        ok: bool,
        remote: str = "",
        error: str = "",
    ) -> None:
        """Auto-push or manual push completed (success or failure)."""


# ---------------------------------------------------------------------------
# Per-task reporter via ContextVar.
# ---------------------------------------------------------------------------

_default_reporter: ProgressReporter = ProgressReporter()   # working no-op
_reporter_var: contextvars.ContextVar[ProgressReporter] = contextvars.ContextVar(
    "ojas_reporter", default=_default_reporter,
)


def set_reporter(reporter: ProgressReporter) -> None:
    """Set the process-wide default reporter. Any context that hasn't
    explicitly entered a reporter_scope() will see this one."""
    global _default_reporter
    _default_reporter = reporter
    _reporter_var.set(reporter)


def get_reporter() -> ProgressReporter:
    return _reporter_var.get()


@contextmanager
def reporter_scope(reporter: ProgressReporter):
    """Use a specific reporter for the duration of this context. Safe for
    concurrent tasks; restores the previous reporter on exit."""
    token = _reporter_var.set(reporter)
    try:
        yield
    finally:
        _reporter_var.reset(token)
