"""
SQLite store for the web backend.

Tables:
  projects — one row per repo the user is editing (name + workspace path)
  sessions — one row per chat session within a project
  messages — chat history (user prompts + assistant responses)
  events   — live activity feed (tool_start, tool_done, agent_spawn, …)
             persisted so the UI can replay them when reconnecting

LangGraph checkpoints (resumable agent state) live SEPARATELY in
~/.agent/checkpoints.db via memory.checkpointer.CompactingCheckpointer.
This DB only stores the user-visible session metadata + activity feed.

Plain sqlite3 (stdlib). No SQLAlchemy — schema is small and stable.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def server_db_path() -> Path:
    p = Path.home() / ".agentic-rag" / "server.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id                    TEXT    PRIMARY KEY,
    name                  TEXT    NOT NULL UNIQUE,
    workspace_path        TEXT    NOT NULL,
    created_at            INTEGER NOT NULL,
    -- Phase 4 settings: auto-commit ON, auto-push OFF, session-branch strategy
    auto_commit_enabled   INTEGER NOT NULL DEFAULT 1,
    auto_push_enabled     INTEGER NOT NULL DEFAULT 0,
    branch_strategy       TEXT    NOT NULL DEFAULT 'session'
);

CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT    PRIMARY KEY,
    project_id      TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    last_active_at  INTEGER NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, last_active_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          TEXT    PRIMARY KEY,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT    NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, created_at);

CREATE TABLE IF NOT EXISTS events (
    id            TEXT    PRIMARY KEY,
    session_id    TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind          TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL,
    created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_session
    ON events(session_id, created_at);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash    TEXT    PRIMARY KEY,
    label         TEXT    NOT NULL,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER
);
"""


def init_db() -> None:
    """Idempotent — safe to call on every backend boot. Also runs forward
    migrations for any settings columns added after the initial release."""
    with _connect() as cx:
        cx.executescript(_SCHEMA)
        _migrate_projects(cx)


def _migrate_projects(cx: sqlite3.Connection) -> None:
    """Add new columns to an EXISTING projects table created before Phase 4.
    `ALTER TABLE ADD COLUMN` is the safe + portable way to do this; we check
    the table's current shape first to keep the call idempotent."""
    cols = {row["name"] for row in cx.execute("PRAGMA table_info(projects)")}
    additions = [
        ("auto_commit_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("auto_push_enabled",   "INTEGER NOT NULL DEFAULT 0"),
        ("branch_strategy",     "TEXT NOT NULL DEFAULT 'session'"),
    ]
    for name, decl in additions:
        if name not in cols:
            cx.execute(f"ALTER TABLE projects ADD COLUMN {name} {decl}")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    cx = sqlite3.connect(
        server_db_path(), isolation_level=None, check_same_thread=False,
    )
    cx.row_factory = sqlite3.Row
    cx.execute("PRAGMA foreign_keys = ON")
    cx.execute("PRAGMA journal_mode = WAL")
    try:
        yield cx
    finally:
        cx.close()


def _now() -> int:
    return int(time.time())


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


# ============================================================================
# Projects
# ============================================================================

_PROJECT_COLS = (
    "id, name, workspace_path, created_at, "
    "auto_commit_enabled, auto_push_enabled, branch_strategy"
)


def _row_project(r: sqlite3.Row | None) -> dict | None:
    """Convert a projects row to a dict, casting the SQLite-INT settings to
    real bools so they JSON-serialize correctly."""
    if r is None:
        return None
    d = dict(r)
    if "auto_commit_enabled" in d:
        d["auto_commit_enabled"] = bool(d["auto_commit_enabled"])
    if "auto_push_enabled" in d:
        d["auto_push_enabled"] = bool(d["auto_push_enabled"])
    return d


def create_project(name: str, workspace_path: str) -> dict:
    """Create a project. Raises ValueError if the name already exists."""
    pid = uuid.uuid4().hex
    now = _now()
    workspace_path = str(Path(workspace_path).expanduser().resolve())
    try:
        with _connect() as cx:
            cx.execute(
                "INSERT INTO projects(id, name, workspace_path, created_at) "
                "VALUES (?, ?, ?, ?)",
                (pid, name, workspace_path, now),
            )
    except sqlite3.IntegrityError as e:
        raise ValueError(f"project name '{name}' already exists") from e
    return get_project(pid)   # round-trip so callers see the defaults filled in


def delete_project(project_id: str) -> bool:
    """Delete a project and CASCADE everything under it: sessions, messages,
    events. Returns True if a row was deleted, False if the project didn't
    exist. FK cascades are wired in the schema, so a single DELETE here
    cleans up the entire subtree without orphan rows."""
    with _connect() as cx:
        cur = cx.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0


def delete_session(session_id: str) -> bool:
    """Delete one session and CASCADE its messages + events. Returns True if
    a row was deleted, False if the session didn't exist."""
    with _connect() as cx:
        cur = cx.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return cur.rowcount > 0


def list_projects() -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            f"SELECT {_PROJECT_COLS} FROM projects ORDER BY created_at DESC"
        ).fetchall()
    return [_row_project(r) for r in rows]


def get_project(project_id: str) -> dict | None:
    with _connect() as cx:
        r = cx.execute(
            f"SELECT {_PROJECT_COLS} FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
    return _row_project(r)


def update_project_settings(
    project_id: str,
    auto_commit_enabled: bool | None = None,
    auto_push_enabled: bool | None = None,
    branch_strategy: str | None = None,
) -> dict | None:
    """Patch any subset of the per-project settings. Returns the updated
    project row, or None if the project doesn't exist."""
    sets: list[str] = []
    args: list[Any] = []
    if auto_commit_enabled is not None:
        sets.append("auto_commit_enabled = ?")
        args.append(1 if auto_commit_enabled else 0)
    if auto_push_enabled is not None:
        sets.append("auto_push_enabled = ?")
        args.append(1 if auto_push_enabled else 0)
    if branch_strategy is not None:
        if branch_strategy not in ("session", "current"):
            raise ValueError("branch_strategy must be 'session' or 'current'")
        sets.append("branch_strategy = ?")
        args.append(branch_strategy)
    if not sets:
        return get_project(project_id)
    args.append(project_id)
    with _connect() as cx:
        cx.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?",
            args,
        )
    return get_project(project_id)


# ============================================================================
# Sessions
# ============================================================================

def create_session(project_id: str, name: str) -> dict:
    sid = uuid.uuid4().hex
    now = _now()
    with _connect() as cx:
        cx.execute(
            "INSERT INTO sessions(id, project_id, name, last_active_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, project_id, name, now, now),
        )
    return {
        "id": sid, "project_id": project_id, "name": name,
        "last_active_at": now, "created_at": now,
    }


def list_sessions(project_id: str) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT id, project_id, name, last_active_at, created_at "
            "FROM sessions WHERE project_id = ? ORDER BY last_active_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: str) -> dict | None:
    with _connect() as cx:
        r = cx.execute(
            "SELECT id, project_id, name, last_active_at, created_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return _row(r)


def touch_session(session_id: str) -> None:
    """Update last_active_at so the session list sorts naturally."""
    with _connect() as cx:
        cx.execute(
            "UPDATE sessions SET last_active_at = ? WHERE id = ?",
            (_now(), session_id),
        )


# ============================================================================
# Messages (chat history)
# ============================================================================

def append_message(session_id: str, role: str, content: str) -> dict:
    mid = uuid.uuid4().hex
    now = _now()
    with _connect() as cx:
        cx.execute(
            "INSERT INTO messages(id, session_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, session_id, role, content, now),
        )
    touch_session(session_id)
    return {
        "id": mid, "session_id": session_id, "role": role,
        "content": content, "created_at": now,
    }


def list_messages(session_id: str, limit: int | None = None) -> list[dict]:
    """Return chat messages for this session. `limit=None` returns all rows;
    same reasoning as `list_events` — silently truncating long sessions
    produced wrong UI state on refresh."""
    sql = (
        "SELECT id, session_id, role, content, created_at "
        "FROM messages WHERE session_id = ? "
        "ORDER BY created_at ASC"
    )
    args: list[Any] = [session_id]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    with _connect() as cx:
        rows = cx.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Events (live activity feed)
# ============================================================================

def append_event(session_id: str, kind: str, payload: dict) -> dict:
    """Persist an activity event. The same payload is pushed to the WebSocket
    in real time; this row is what new clients replay when they reconnect."""
    eid = uuid.uuid4().hex
    now = _now()
    payload_json = json.dumps(payload, default=str)
    with _connect() as cx:
        cx.execute(
            "INSERT INTO events(id, session_id, kind, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, session_id, kind, payload_json, now),
        )
    return {
        "id": eid, "session_id": session_id, "kind": kind,
        "payload": payload, "created_at": now,
    }


def list_events(
    session_id: str,
    since: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Return events for this session, chronologically.

    `limit=None` (the default) returns ALL events for the session. The
    earlier 1000-row default was silently dropping the most recent events
    on long sessions — a Pulse-class build emits thousands of events
    (every tool_start/done, token_update, todo_update is one row) and the
    LIMIT chopped off the final `turn_summary` + `todo_update(all done)`,
    making the UI's rebuilt state stale on refresh ("plan completed live
    but shows as middle on reload"). Sessions with hundreds of thousands of
    events would benefit from explicit paging, but for current scale a
    single full fetch is simpler and correct.
    """
    sql = (
        "SELECT id, session_id, kind, payload_json, created_at "
        "FROM events WHERE session_id = ?"
    )
    args: list[Any] = [session_id]
    if since is not None:
        sql += " AND created_at > ?"
        args.append(since)
    sql += " ORDER BY created_at ASC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)
    with _connect() as cx:
        rows = cx.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json"))
        out.append(d)
    return out


# ============================================================================
# Auth tokens (used by server/auth.py)
# ============================================================================

def store_token(token_hash: str, label: str) -> None:
    with _connect() as cx:
        cx.execute(
            "INSERT OR REPLACE INTO auth_tokens"
            "(token_hash, label, created_at, last_used_at) "
            "VALUES (?, ?, ?, NULL)",
            (token_hash, label, _now()),
        )


def is_token_valid(token_hash: str) -> bool:
    with _connect() as cx:
        r = cx.execute(
            "SELECT 1 FROM auth_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if r is None:
            return False
        cx.execute(
            "UPDATE auth_tokens SET last_used_at = ? WHERE token_hash = ?",
            (_now(), token_hash),
        )
    return True


def revoke_token(token_hash: str) -> None:
    with _connect() as cx:
        cx.execute(
            "DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,),
        )
