"""
Utility tools — TodoWrite, Sleep, SendUserMessage (Brief).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Small shared helpers (single source of truth — previously duplicated across
# tools/multi_agent.py and server/db.py)
# ---------------------------------------------------------------------------

def now_secs() -> int:
    """Current Unix time, whole seconds."""
    return int(time.time())


def slugify(text: str, *, max_len: int = 40, allow_underscore: bool = True) -> str:
    """Lowercase, collapse non-slug chars to single hyphens, trim, and cap
    length. Returns '' for empty/garbage input so callers can fall back to a
    default like 'app'.

    allow_underscore=True keeps `_` and `-` (deployed-app slugs); False maps
    everything non-alphanumeric to `-` (agent names). The two branches are
    byte-for-byte equal to the original `_slugify` / `_slugify_agent_name`
    implementations they replaced.
    """
    s = (text or "").strip().lower()
    if allow_underscore:
        s = re.sub(r"[^a-z0-9_-]+", "-", s)
        s = re.sub(r"-{2,}", "-", s).strip("-_")
    else:
        s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:max_len]


# ---------------------------------------------------------------------------
# TodoWrite
# ---------------------------------------------------------------------------

def _todo_store_path() -> Path:
    override = os.environ.get("CLAWD_TODO_STORE")
    if override:
        return Path(override)
    return Path.cwd() / ".clawd-todos.json"


@dataclass
class TodoWriteOutput:
    old_todos: list[dict]
    new_todos: list[dict]
    verification_nudge_needed: bool | None = None


def read_todos() -> list[dict]:
    """Read the current todo list from the store (empty list if none).

    Used by the agent loop to build the stateful todo `<system-reminder>`.
    The store is deleted when every item is completed, so an empty list
    means either "no plan yet" or "plan finished" — both fine for the
    reminder logic, which keys off whether items exist + how stale they are.
    """
    store = _todo_store_path()
    if not store.exists():
        return []
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def todo_write(todos: list[dict]) -> TodoWriteOutput:
    """Write/update the todo list.

    Each todo: {content, status: pending|in_progress|completed, activeForm}
    """
    store = _todo_store_path()

    old_todos: list[dict] = []
    if store.exists():
        try:
            old_todos = json.loads(store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old_todos = []

    validated: list[dict] = []
    for todo in todos:
        content = (todo.get("content") or "").strip()
        status = (todo.get("status") or "").strip()
        active_form = (todo.get("activeForm") or "").strip()
        if not content:
            raise ValueError("todo content must be non-empty")
        if not active_form:
            raise ValueError("todo activeForm must be non-empty")
        if status not in ("pending", "in_progress", "completed"):
            raise ValueError(
                "todo status must be one of: pending, in_progress, completed"
            )
        validated.append(
            {"content": content, "status": status, "activeForm": active_form}
        )

    all_completed = bool(validated) and all(
        t["status"] == "completed" for t in validated
    )

    if all_completed:
        try:
            store.unlink()
        except FileNotFoundError:
            pass
    else:
        store.parent.mkdir(parents=True, exist_ok=True)
        store.write_text(json.dumps(validated, indent=2), encoding="utf-8")

    verification_nudge: bool | None = None
    if (
        all_completed
        and len(validated) >= 3
        and not any("verif" in t["content"].lower() for t in validated)
    ):
        verification_nudge = True

    return TodoWriteOutput(
        old_todos=old_todos,
        new_todos=validated,
        verification_nudge_needed=verification_nudge,
    )


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

SLEEP_MAX_MS = 300_000


def sleep_tool(duration_ms: int) -> dict:
    """Wait for `duration_ms` milliseconds.

    Hard-errors when the cap is exceeded — does not silently truncate.
    """
    if duration_ms > SLEEP_MAX_MS:
        raise ValueError(
            f"duration_ms {duration_ms} exceeds maximum allowed sleep of {SLEEP_MAX_MS}ms"
        )
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    time.sleep(duration_ms / 1000.0)
    return {"slept_ms": duration_ms}


# AskUserQuestion is provided by tools.wrappers via LangGraph's interrupt();
# the terminal-input helper that used to live here was removed when the CLI
# was retired.


# ---------------------------------------------------------------------------
# Brief / SendUserMessage
# ---------------------------------------------------------------------------

@dataclass
class BriefOutput:
    message: str
    attachments: list[str]
    sent_at: str


def brief(
    message: str,
    attachments: list[str] | None = None,
    status: str = "Normal",
) -> BriefOutput:
    """Send a structured user-facing message.

    `status` is one of "Normal" or "Proactive".
    """
    if not message.strip():
        raise ValueError("message must be non-empty")
    if status not in ("Normal", "Proactive"):
        raise ValueError("status must be 'Normal' or 'Proactive'")
    sent_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return BriefOutput(
        message=message,
        attachments=list(attachments or []),
        sent_at=sent_at,
    )
