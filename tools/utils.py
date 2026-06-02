"""
Utility tools — TodoWrite, Sleep, AskUserQuestion,
SendUserMessage (Brief), ToolSearch.

Ported from Rust: tools/src/lib.rs corresponding tool specs.
"""

import os
import re
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# TodoWrite — Rust execute_todo_write()
# ---------------------------------------------------------------------------

def _todo_store_path() -> Path:
    """Mirrors Rust's `.clawd-todos.json` in cwd, override via CLAWD_TODO_STORE."""
    override = os.environ.get("CLAWD_TODO_STORE")
    if override:
        return Path(override)
    return Path.cwd() / ".clawd-todos.json"


@dataclass
class TodoWriteOutput:
    old_todos: list[dict]
    new_todos: list[dict]
    verification_nudge_needed: bool | None = None


def todo_write(todos: list[dict]) -> TodoWriteOutput:
    """
    Write/update the todo list.
    Each todo: {content, status: pending|in_progress|completed, activeForm}
    Ported from Rust: tools/src/lib.rs execute_todo_write().
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


def todo_read() -> list[dict]:
    """Read current todo list from the same store as todo_write()."""
    store = _todo_store_path()
    if not store.exists():
        return []
    try:
        return json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Sleep — Rust execute_sleep()
# ---------------------------------------------------------------------------

SLEEP_MAX_MS = 300_000


def sleep_tool(duration_ms: int) -> dict:
    """
    Wait for `duration_ms` milliseconds.
    Ported from Rust: tools/src/lib.rs execute_sleep().

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


# ---------------------------------------------------------------------------
# AskUserQuestion — Rust run_ask_user_question()
# ---------------------------------------------------------------------------

def ask_user_question(question: str, options: list[str] | None = None) -> str:
    """
    Ask the user a question and return their answer.
    Ported from Rust: tools/src/lib.rs run_ask_user_question().

    When `options` is provided, prints numbered choices and accepts a 1-based
    index in addition to free-text.
    """
    print(f"[Question] {question}")
    if options:
        for i, opt in enumerate(options, start=1):
            print(f"  {i}. {opt}")
        answer = input(f"Enter choice (1-{len(options)}): ").strip()
        try:
            idx = int(answer)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        except ValueError:
            pass
        return answer
    return input("Your answer: ").strip()


# ---------------------------------------------------------------------------
# Brief / SendUserMessage — Rust execute_brief()
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
    """
    Send a structured user-facing message.
    Ported from Rust: tools/src/lib.rs execute_brief().

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


# Backwards-compatible alias.
send_user_message = brief


# ---------------------------------------------------------------------------
# ToolSearch — Rust execute_tool_search()
# ---------------------------------------------------------------------------

@dataclass
class ToolSearchHit:
    name: str
    description: str
    score: float


@dataclass
class ToolSearchOutput:
    query: str
    matches: list[ToolSearchHit]


def tool_search(query: str, tool_registry, max_results: int = 5) -> ToolSearchOutput:
    """
    Search available tools by query, mirroring Rust execute_tool_search().

    Supports two query forms:
      - "select:Name1,Name2"  — exact-name selection
      - "+req keyword ..."    — `+` prefix marks a required substring; the
        rest are scored. Plain queries are fully scored.
    """
    definitions = list(tool_registry.definitions())
    query = query.strip()

    if query.startswith("select:"):
        names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
        wanted = {n.lower() for n in names}
        matches = [
            ToolSearchHit(
                name=t.name,
                description=getattr(t, "description", "") or "",
                score=1.0,
            )
            for t in definitions
            if t.name.lower() in wanted
        ]
        return ToolSearchOutput(query=query, matches=matches[:max_results])

    tokens = [t for t in re.split(r"\s+", query.lower()) if t]
    required = [t[1:] for t in tokens if t.startswith("+") and len(t) > 1]
    scored_terms = [t for t in tokens if not t.startswith("+")] or required

    hits: list[ToolSearchHit] = []
    for tool_def in definitions:
        name_lower = tool_def.name.lower()
        desc_lower = (getattr(tool_def, "description", "") or "").lower()
        haystack = f"{name_lower} {desc_lower}"

        if any(req not in haystack for req in required):
            continue

        score = 0.0
        for term in scored_terms:
            if not term:
                continue
            if term in name_lower:
                score += 2.0
            if term in desc_lower:
                score += 1.0
        if scored_terms and score == 0.0:
            continue

        hits.append(
            ToolSearchHit(
                name=tool_def.name,
                description=getattr(tool_def, "description", "") or "",
                score=score or 1.0,
            )
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    return ToolSearchOutput(query=query, matches=hits[:max_results])
