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
    "agentic_rag_reporter", default=_default_reporter,
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
