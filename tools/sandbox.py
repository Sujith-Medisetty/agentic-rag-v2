"""
Workspace jail — keeps a non-root agent's filesystem writes inside the session's
project workspace.

How it works:
  • `set_session_sandbox(workspace, is_root, session_id)` is called by the
    session_runner before each turn so the per-call ContextVars are populated.
    asyncio.copy_context() carries them through the executor thread that
    runs the agent's tools.
  • Tools that WRITE to disk (`write_file`, `edit_file`, the bash background
    spawn, etc.) call `check_write_path(path)` before touching the FS. If
    the resolved path is outside the workspace AND the caller is not root,
    `SandboxViolation` is raised; the tool wrapper turns that into a
    `BLOCKED: ...` response the model sees and can react to.
  • Root bypasses the check entirely — the VM owner can edit any file.
  • Reads aren't gated here; reading system files (which / git config / man
    pages) is legitimate research even for jailed users.

Threading note: ContextVar values set on the asyncio loop ARE visible to the
executor thread that runs the agent ONLY when the executor invocation was
wrapped with `contextvars.copy_context().run(...)`. session_runner already
does that for the reporter scope; the sandbox piggybacks on the same context.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


class SandboxViolation(RuntimeError):
    """Raised when a path resolves outside the active workspace AND the
    caller isn't root. Tool wrappers should catch this and surface it as
    a `BLOCKED:` tool result — the model sees the message and can retry
    with a workspace-relative path."""


@dataclass(frozen=True)
class SandboxConfig:
    workspace: Path        # absolute, resolved
    is_root: bool
    session_id: str        # threaded through so background spawns can register


# `None` means "no active session" — typical in non-agent code (CLI scripts,
# tests, server startup). In that state, every path is allowed: the jail is
# purely a per-turn security layer.
_active: ContextVar[SandboxConfig | None] = ContextVar(
    "forge_sandbox_active", default=None,
)


def set_session_sandbox(workspace: str | Path, is_root: bool, session_id: str) -> None:
    """Bind the per-turn sandbox. Called from session_runner BEFORE running
    the agent graph."""
    cfg = SandboxConfig(
        workspace=Path(workspace).expanduser().resolve(),
        is_root=bool(is_root),
        session_id=session_id,
    )
    _active.set(cfg)


def clear_session_sandbox() -> None:
    """Pair to `set_session_sandbox` — called at the end of a turn so a
    later un-scoped tool call doesn't see stale state."""
    _active.set(None)


def active_sandbox() -> SandboxConfig | None:
    return _active.get()


def active_session_id() -> str | None:
    cfg = _active.get()
    return cfg.session_id if cfg is not None else None


def check_write_path(path: str | Path) -> None:
    """Raise `SandboxViolation` if `path` resolves outside the active
    workspace AND the caller isn't root. No-op when no sandbox is active
    (CLI / tests) or when caller is root."""
    cfg = _active.get()
    if cfg is None or cfg.is_root:
        return
    try:
        # Use realpath so symlinks pointing outside the workspace can't be
        # used as an escape hatch. We don't require the path to exist —
        # writing a NEW file is the common case — so resolve(strict=False).
        target = Path(os.path.expanduser(str(path))).resolve(strict=False)
    except (OSError, ValueError) as e:
        raise SandboxViolation(f"could not resolve path '{path}': {e}") from e
    try:
        target.relative_to(cfg.workspace)
    except ValueError:
        raise SandboxViolation(
            f"path '{target}' is outside the session workspace "
            f"'{cfg.workspace}'. Writes are jailed for non-root users; "
            f"use a workspace-relative path."
        )
