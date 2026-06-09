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
-- Multi-user identity. Root user (OJAS_ROOT_EMAIL from .env) is materialised
-- on first login; other users go through /api/auth/signup.
CREATE TABLE IF NOT EXISTS users (
    id             TEXT    PRIMARY KEY,
    email          TEXT    NOT NULL UNIQUE,
    password_hash  TEXT    NOT NULL,
    password_salt  TEXT    NOT NULL,
    role           TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'root')),
    created_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id                    TEXT    PRIMARY KEY,
    user_id               TEXT             REFERENCES users(id) ON DELETE CASCADE,
    name                  TEXT    NOT NULL,
    workspace_path        TEXT    NOT NULL,
    created_at            INTEGER NOT NULL,
    auto_commit_enabled   INTEGER NOT NULL DEFAULT 1,
    auto_push_enabled     INTEGER NOT NULL DEFAULT 0,
    branch_strategy       TEXT    NOT NULL DEFAULT 'session',
    -- Project name is unique PER USER (so two users can both have a project
    -- named "Ojas" without colliding). Old uniqueness was global.
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS sessions (
    id                TEXT    PRIMARY KEY,
    project_id        TEXT    NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id           TEXT             REFERENCES users(id) ON DELETE CASCADE,
    name              TEXT    NOT NULL,
    -- Subdirectory under the project workspace where THIS session's files
    -- live. Lets session-delete safely rmtree just this session's tree
    -- without touching other sessions' builds.
    workspace_subdir  TEXT,
    last_active_at    INTEGER NOT NULL,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON sessions(project_id, last_active_at DESC);
-- idx_sessions_user is created after the user_id column migration runs.

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

-- Long-running processes spawned by the agent (npm run dev, vite preview,
-- etc.). Tracked so session/project deletion can SIGTERM them, and so the
-- root admin endpoints can list what's currently running on the box.
CREATE TABLE IF NOT EXISTS session_processes (
    pid          INTEGER PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    command      TEXT    NOT NULL,
    port         INTEGER,
    started_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_processes_session
    ON session_processes(session_id);

-- Ojas-owned service processes + port endpoints (main backend, caddy
-- reverse proxy, MCP servers, deployed-app static URLs, future apps).
-- Distinct from session_processes: these are NOT tied to a chat session,
-- they outlive any individual session and persist across restarts (the
-- backend re-registers itself on boot). source tells the admin UI where
-- the row came from: 'ojas-main', 'ojas-proxy', 'ojas-deployed',
-- 'ojas-mcp', 'ojas-external'. pid may be NULL for port-only entries
-- (e.g. a deployed app's static URL has no dedicated process — it's
-- served by caddy on an existing port).
CREATE TABLE IF NOT EXISTS ojas_services (
    id           TEXT    PRIMARY KEY,
    source       TEXT    NOT NULL,
    pid          INTEGER,
    label        TEXT    NOT NULL,
    command      TEXT,
    port         INTEGER,
    bind_addr    TEXT,
    url          TEXT,
    started_at   INTEGER NOT NULL,
    meta_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ojas_services_source
    ON ojas_services(source);
CREATE INDEX IF NOT EXISTS idx_ojas_services_port
    ON ojas_services(port);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash    TEXT    PRIMARY KEY,
    user_id       TEXT             REFERENCES users(id) ON DELETE CASCADE,
    label         TEXT    NOT NULL,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER
);

-- Deployed apps: a session's built dist/ "promoted" to a persistent location
-- under /opt/ojas-apps/<slug>/. Slug is the public URL component AND now
-- the subdomain (https://<slug>.<root-domain>/). Lifecycle is tied to
-- the source session: deleting that session removes the deployed app
-- (DB row + on-disk files) — user explicitly asked for this so an app
-- never outlives the chat that produced it.
CREATE TABLE IF NOT EXISTS deployed_apps (
    slug                 TEXT    PRIMARY KEY,
    name                 TEXT    NOT NULL,
    source_session_id    TEXT             REFERENCES sessions(id) ON DELETE CASCADE,
    source_project_id    TEXT,
    owner_user_id        TEXT             REFERENCES users(id) ON DELETE SET NULL,
    -- Sub-app folder name within the session's workspace_subdir. Lets a
    -- single chat session deploy multiple apps independently.
    project_dir          TEXT,
    app_dir              TEXT    NOT NULL,
    deployed_at          INTEGER NOT NULL,
    last_redeploy_at     INTEGER NOT NULL,
    -- State machine for pause/resume. Static apps are always "running"
    -- (the toggle just swaps the Caddy route between live and paused).
    -- Fullstack apps (v1.1+) flip systemd units on/off. Defaults to
    -- running because v1 deploys have no service to stop.
    state                TEXT    NOT NULL DEFAULT 'running'
                                 CHECK (state IN ('running','stopped','starting','error')),
    last_state_at        INTEGER,
    last_health_at       INTEGER,
    error_message        TEXT,
    -- Per-app systemd unit (set by fullstack deploys; NULL for static).
    -- Naming: "ojas-app-<slug>.service", slug-sanitised.
    service_name         TEXT,
    port                 INTEGER
);
CREATE INDEX IF NOT EXISTS idx_deployed_apps_owner
    ON deployed_apps(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_deployed_apps_session
    ON deployed_apps(source_session_id);
-- idx_deployed_apps_state is created in init_db() AFTER the column
-- migration runs (the column may not exist on legacy DBs).
"""


def init_db() -> None:
    """Idempotent — safe to call on every backend boot. Also runs forward
    migrations for columns added after the initial release."""
    with _connect() as cx:
        cx.executescript(_SCHEMA)
        _migrate_projects(cx)
        _migrate_sessions(cx)
        _migrate_auth_tokens(cx)
        _migrate_deployed_apps(cx)
        # Indexes that reference columns added in migrations must be created
        # AFTER those columns exist on legacy DBs.
        cx.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_user "
            "ON sessions(user_id, last_active_at DESC)"
        )
        cx.execute(
            "CREATE INDEX IF NOT EXISTS idx_deployed_apps_session "
            "ON deployed_apps(source_session_id)"
        )
        # Same as above — depends on the `state` column added by
        # _migrate_deployed_apps(). Idempotent because IF NOT EXISTS.
        cx.execute(
            "CREATE INDEX IF NOT EXISTS idx_deployed_apps_state "
            "ON deployed_apps(state)"
        )


def _migrate_deployed_apps(cx: sqlite3.Connection) -> None:
    """Forward-migrate the deployed_apps table. Each ALTER is idempotent —
    if the column already exists, we skip it. SQLite has no ADD COLUMN IF
    NOT EXISTS, so we read PRAGMA table_info first.
    """
    cols = {row["name"] for row in cx.execute("PRAGMA table_info(deployed_apps)")}
    if "project_dir" not in cols:
        cx.execute("ALTER TABLE deployed_apps ADD COLUMN project_dir TEXT")
    if "state" not in cols:
        # 'running' is the safe default — pre-existing deploys were
        # implicitly running (static files served by Caddy).
        cx.execute(
            "ALTER TABLE deployed_apps ADD COLUMN state TEXT NOT NULL DEFAULT 'running'"
        )
    if "last_state_at" not in cols:
        cx.execute("ALTER TABLE deployed_apps ADD COLUMN last_state_at INTEGER")
    if "last_health_at" not in cols:
        cx.execute("ALTER TABLE deployed_apps ADD COLUMN last_health_at INTEGER")
    if "error_message" not in cols:
        cx.execute("ALTER TABLE deployed_apps ADD COLUMN error_message TEXT")
    if "service_name" not in cols:
        cx.execute("ALTER TABLE deployed_apps ADD COLUMN service_name TEXT")
    if "port" not in cols:
        cx.execute("ALTER TABLE deployed_apps ADD COLUMN port INTEGER")

    # One-time FK rebuild. The legacy deployed_apps table was built by
    # a chain of ALTER TABLEs that added source_session_id, source_project_id,
    # etc. without the REFERENCES clause (SQLite can't add FKs to an
    # existing column). The current _SCHEMA in this file DOES declare
    # source_session_id as REFERENCES sessions(id) ON DELETE CASCADE, but
    # a fresh CREATE TABLE only runs for new DBs — the live DB kept the
    # old, FK-less table.
    #
    # Result: a DELETE on sessions() didn't cascade to deployed_apps,
    # leaving orphan rows. We fix it by rebuilding the table with the
    # FK in place and copying the data over. Idempotent: if a FK on
    # source_session_id already exists, the rebuild is a no-op.
    fks = list(cx.execute("PRAGMA foreign_key_list(deployed_apps)"))
    has_session_fk = any(fk[2] == "sessions" and fk[3] == "source_session_id" for fk in fks)
    if not has_session_fk:
        # The deployed_apps rows whose session was already deleted
        # would VIOLATE the new FK at rebuild time, because they
        # reference a now-missing sessions.id. Set source_session_id
        # to NULL for those rows first (NULLs are allowed by REFERENCES
        # without NOT NULL enforcement). This is the correct semantic
        # anyway: the app is an orphan, the session is gone, NULL
        # is the truth.
        cx.execute(
            "UPDATE deployed_apps SET source_session_id = NULL "
            "WHERE source_session_id IS NOT NULL "
            "AND source_session_id NOT IN (SELECT id FROM sessions)"
        )
        # Rebuild the table. We snapshot every column, drop the old
        # table, recreate it via the current _SCHEMA, and copy the
        # data back. PRAGMA foreign_keys is ON (set in _connect) so
        # the new FK is enforced.
        all_cols = [r["name"] for r in cx.execute("PRAGMA table_info(deployed_apps)")]
        col_csv = ", ".join(f'"{c}"' for c in all_cols)
        cx.execute(f"CREATE TABLE _deployed_apps_rebuild AS SELECT {col_csv} FROM deployed_apps")
        cx.execute("DROP TABLE deployed_apps")
        # Re-run the full schema CREATE for this table only. We can't
        # run the whole _SCHEMA here (CREATE TABLE IF NOT EXISTS is
        # a no-op for the now-missing table but we want to be sure
        # we land in the current shape).
        cx.executescript("""
            CREATE TABLE deployed_apps (
                slug                 TEXT    PRIMARY KEY,
                name                 TEXT    NOT NULL,
                source_session_id    TEXT    REFERENCES sessions(id) ON DELETE CASCADE,
                source_project_id    TEXT,
                owner_user_id        TEXT    REFERENCES users(id) ON DELETE SET NULL,
                project_dir          TEXT,
                app_dir              TEXT    NOT NULL,
                deployed_at          INTEGER NOT NULL,
                last_redeploy_at     INTEGER NOT NULL,
                state                TEXT    NOT NULL DEFAULT 'running'
                                         CHECK (state IN ('running','stopped','starting','error')),
                last_state_at        INTEGER,
                last_health_at       INTEGER,
                error_message        TEXT,
                service_name         TEXT,
                port                 INTEGER
            )
        """)
        cx.execute(f"INSERT INTO deployed_apps ({col_csv}) SELECT {col_csv} FROM _deployed_apps_rebuild")
        cx.execute("DROP TABLE _deployed_apps_rebuild")
        # Re-create the indexes the new table lost in the drop.
        cx.execute("CREATE INDEX IF NOT EXISTS idx_deployed_apps_owner ON deployed_apps(owner_user_id)")
        cx.execute("CREATE INDEX IF NOT EXISTS idx_deployed_apps_session ON deployed_apps(source_session_id)")
        cx.execute("CREATE INDEX IF NOT EXISTS idx_deployed_apps_state ON deployed_apps(state)")


def _migrate_projects(cx: sqlite3.Connection) -> None:
    """Add new columns to an EXISTING projects table from earlier releases."""
    cols = {row["name"] for row in cx.execute("PRAGMA table_info(projects)")}
    additions = [
        ("auto_commit_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("auto_push_enabled",   "INTEGER NOT NULL DEFAULT 0"),
        ("branch_strategy",     "TEXT NOT NULL DEFAULT 'session'"),
        ("user_id",             "TEXT REFERENCES users(id) ON DELETE CASCADE"),
    ]
    for name, decl in additions:
        if name not in cols:
            cx.execute(f"ALTER TABLE projects ADD COLUMN {name} {decl}")


def _migrate_sessions(cx: sqlite3.Connection) -> None:
    """Add per-session `user_id` + `workspace_subdir` columns when upgrading."""
    cols = {row["name"] for row in cx.execute("PRAGMA table_info(sessions)")}
    additions = [
        ("user_id",          "TEXT REFERENCES users(id) ON DELETE CASCADE"),
        ("workspace_subdir", "TEXT"),
    ]
    for name, decl in additions:
        if name not in cols:
            cx.execute(f"ALTER TABLE sessions ADD COLUMN {name} {decl}")


def _migrate_auth_tokens(cx: sqlite3.Connection) -> None:
    """Add `user_id` to auth_tokens so we can scope tokens per-user."""
    cols = {row["name"] for row in cx.execute("PRAGMA table_info(auth_tokens)")}
    if "user_id" not in cols:
        cx.execute(
            "ALTER TABLE auth_tokens ADD COLUMN user_id TEXT "
            "REFERENCES users(id) ON DELETE CASCADE"
        )


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
    "id, user_id, name, workspace_path, created_at, "
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


def create_project(name: str, workspace_path: str, user_id: str | None = None) -> dict:
    """Create a project owned by `user_id`. Raises ValueError if the user
    already has a project with that name."""
    pid = uuid.uuid4().hex
    now = _now()
    workspace_path = str(Path(workspace_path).expanduser().resolve())
    try:
        with _connect() as cx:
            cx.execute(
                "INSERT INTO projects(id, user_id, name, workspace_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (pid, user_id, name, workspace_path, now),
            )
    except sqlite3.IntegrityError as e:
        raise ValueError(f"project name '{name}' already exists") from e
    return get_project(pid)


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


def list_projects(user_id: str | None = None) -> list[dict]:
    """List projects. If `user_id` is given, only that user's projects;
    otherwise (root scope) list everything."""
    with _connect() as cx:
        if user_id is not None:
            rows = cx.execute(
                f"SELECT {_PROJECT_COLS} FROM projects "
                f"WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        else:
            rows = cx.execute(
                f"SELECT {_PROJECT_COLS} FROM projects "
                f"ORDER BY created_at DESC"
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

_SESSION_COLS = (
    "id, project_id, user_id, name, workspace_subdir, "
    "last_active_at, created_at"
)


def create_session(
    project_id: str,
    name: str,
    user_id: str | None = None,
    workspace_subdir: str | None = None,
) -> dict:
    sid = uuid.uuid4().hex
    now = _now()
    with _connect() as cx:
        cx.execute(
            "INSERT INTO sessions"
            "(id, project_id, user_id, name, workspace_subdir, last_active_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, project_id, user_id, name, workspace_subdir, now, now),
        )
    return {
        "id": sid, "project_id": project_id, "user_id": user_id,
        "name": name, "workspace_subdir": workspace_subdir,
        "last_active_at": now, "created_at": now,
    }


def list_sessions(project_id: str) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            f"SELECT {_SESSION_COLS} FROM sessions "
            f"WHERE project_id = ? ORDER BY last_active_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_sessions_for_user(user_id: str) -> list[dict]:
    """All sessions belonging to one user, across all their projects."""
    with _connect() as cx:
        rows = cx.execute(
            f"SELECT {_SESSION_COLS} FROM sessions "
            f"WHERE user_id = ? ORDER BY last_active_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: str) -> dict | None:
    with _connect() as cx:
        r = cx.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return _row(r)


class SessionNameConflict(ValueError):
    """Raised by rename_session when the desired new_name is already taken
    by another session in the same project (case-sensitive). The endpoint
    surfaces this as a 409 with the existing session's id so the UI can
    jump to it. Subclasses ValueError so old `except ValueError` callers
    still catch it; adds .existing_id and .existing_name attributes for
    the new structured-handling path."""

    def __init__(self, message: str, existing_id: str, existing_name: str):
        super().__init__(message)
        self.existing_id = existing_id
        self.existing_name = existing_name


def rename_session(session_id: str, new_name: str) -> dict:
    """Rename a session. Returns the updated row. Raises:
      • SessionNameConflict if another session in the SAME project already
        has `new_name` (case-sensitive, exact match) — caller can call
        allocate_unique_session_name to get a free variant
      • LookupError if no such session_id
    Renaming to the same name as the current name is a no-op (allowed)."""
    with _connect() as cx:
        existing = cx.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if existing is None:
            raise LookupError(f"session {session_id} not found")
        existing_d = dict(existing)
        # No-op if renaming to the same name.
        if existing_d["name"] == new_name:
            return existing_d
        # Check for collision in the same project (case-sensitive).
        collision = cx.execute(
            f"SELECT {_SESSION_COLS} FROM sessions "
            f"WHERE project_id = ? AND name = ? AND id != ?",
            (existing_d["project_id"], new_name, session_id),
        ).fetchone()
        if collision is not None:
            cd = dict(collision)
            raise SessionNameConflict(
                f"a session named {new_name!r} already exists in this project",
                existing_id=cd["id"],
                existing_name=cd["name"],
            )
        cx.execute(
            "UPDATE sessions SET name = ? WHERE id = ?",
            (new_name, session_id),
        )
        # Re-read for the caller.
        r = cx.execute(
            f"SELECT {_SESSION_COLS} FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return dict(r)


def allocate_unique_session_name(project_id: str, desired: str, exclude_id: str | None = None) -> str:
    """Return a session name that doesn't collide with any other session
    in `project_id`. If `desired` is free, returns it unchanged.
    Otherwise appends "-2", "-3", ... until a free name is found.
    Used by the PATCH endpoint to auto-suffix on user/agent renames
    so the user never has to manually disambiguate.

    The numeric suffix is appended with a hyphen to keep the base name
    readable: "Fix login bug" → "Fix login bug-2" rather than the more
    cryptic "Fix login bug 2" or "Fix login bug2"."""
    with _connect() as cx:
        existing_names = {
            r["name"]
            for r in cx.execute(
                f"SELECT name FROM sessions WHERE project_id = ?"
                f"{' AND id != ?' if exclude_id else ''}",
                (project_id, exclude_id) if exclude_id else (project_id,),
            ).fetchall()
        }
    if desired not in existing_names:
        return desired
    n = 2
    while True:
        candidate = f"{desired}-{n}"
        if candidate not in existing_names:
            return candidate
        n += 1


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
    sql += " ORDER BY created_at ASC, rowid ASC"
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

def store_token(token_hash: str, label: str, user_id: str | None = None) -> None:
    with _connect() as cx:
        cx.execute(
            "INSERT OR REPLACE INTO auth_tokens"
            "(token_hash, user_id, label, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, NULL)",
            (token_hash, user_id, label, _now()),
        )


def get_token_user_id(token_hash: str) -> str | None:
    """Return the user_id that owns this token, or None if not found."""
    with _connect() as cx:
        r = cx.execute(
            "SELECT user_id FROM auth_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    return r["user_id"] if r is not None else None


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


# ============================================================================
# Users
# ============================================================================

_USER_COLS = "id, email, password_hash, password_salt, role, created_at"


def create_user(
    email: str, password_hash: str, password_salt: str, role: str = "user",
) -> dict:
    """Create a new user. Raises ValueError if the email is already in use."""
    uid = uuid.uuid4().hex
    now = _now()
    try:
        with _connect() as cx:
            cx.execute(
                "INSERT INTO users(id, email, password_hash, password_salt, "
                "role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, email.lower(), password_hash, password_salt, role, now),
            )
    except sqlite3.IntegrityError as e:
        raise ValueError(f"email '{email}' already registered") from e
    return get_user(uid)


def get_user(user_id: str) -> dict | None:
    with _connect() as cx:
        r = cx.execute(
            f"SELECT {_USER_COLS} FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _row(r)


def get_user_by_email(email: str) -> dict | None:
    with _connect() as cx:
        r = cx.execute(
            f"SELECT {_USER_COLS} FROM users WHERE email = ?",
            (email.lower(),),
        ).fetchone()
    return _row(r)


def list_users() -> list[dict]:
    """Root-only — returns every user. Password hashes included; callers must
    strip them before exposing over HTTP."""
    with _connect() as cx:
        rows = cx.execute(
            f"SELECT {_USER_COLS} FROM users ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_user_password(
    user_id: str, password_hash: str, password_salt: str,
) -> bool:
    """Overwrite the password hash + salt. Returns False if no such user.
    Caller is expected to also invalidate existing auth tokens."""
    with _connect() as cx:
        cur = cx.execute(
            "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?",
            (password_hash, password_salt, user_id),
        )
        return cur.rowcount > 0


def delete_user(user_id: str) -> bool:
    """Hard-delete a user. ON DELETE CASCADE on auth_tokens / projects
    handles those; ON DELETE SET NULL on deployed_apps.owner_user_id
    preserves the deployed app but orphans it. Returns False if no
    such user."""
    with _connect() as cx:
        cur = cx.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def revoke_all_user_tokens(user_id: str) -> int:
    """Drop every auth_token for a user — used after a password reset or
    delete. Returns the number of tokens removed."""
    with _connect() as cx:
        cur = cx.execute(
            "DELETE FROM auth_tokens WHERE user_id = ?", (user_id,),
        )
        return cur.rowcount


def count_root_users() -> int:
    """How many users have role='root'. Used to enforce the
    'never delete the last root' invariant."""
    with _connect() as cx:
        r = cx.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role = 'root'"
        ).fetchone()
        return int(r["c"])


# ============================================================================
# Session processes — long-running PIDs spawned by the agent (npm run dev, etc).
# Tracked so deletes can SIGTERM them and admin endpoints can list them.
# ============================================================================

def register_process(
    session_id: str, pid: int, command: str, port: int | None = None,
) -> None:
    """Idempotent on PID — if a row exists, update it. Different sessions
    can't reuse a PID at the same time anyway."""
    with _connect() as cx:
        cx.execute(
            "INSERT OR REPLACE INTO session_processes"
            "(pid, session_id, command, port, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pid, session_id, command, port, _now()),
        )


def unregister_process(pid: int) -> None:
    with _connect() as cx:
        cx.execute("DELETE FROM session_processes WHERE pid = ?", (pid,))


def list_processes_for_session(session_id: str) -> list[dict]:
    with _connect() as cx:
        rows = cx.execute(
            "SELECT pid, session_id, command, port, started_at "
            "FROM session_processes WHERE session_id = ? "
            "ORDER BY started_at ASC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_all_processes() -> list[dict]:
    """Root-only — every tracked spawned process across every session."""
    with _connect() as cx:
        rows = cx.execute(
            "SELECT pid, session_id, command, port, started_at "
            "FROM session_processes ORDER BY started_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# ============================================================================
# Ojas services — main backend, caddy, deployed apps, MCP servers, etc.
#
# Two flavours of row:
#   - process row:  pid IS NOT NULL  — a live OS process owned by Ojas
#   - port row:     pid IS NULL      — a port/URL the backend knows about
#                                    (e.g. a deployed app served via caddy)
#
# `id` is caller-chosen so re-registration is idempotent (e.g. the main
# backend always uses id='ojas-main', so a restart overwrites in place).
# ============================================================================

def upsert_ojas_service(
    id: str,
    source: str,
    label: str,
    pid: int | None = None,
    command: str | None = None,
    port: int | None = None,
    bind_addr: str | None = None,
    url: str | None = None,
    meta: dict | None = None,
) -> None:
    """Idempotent register-or-replace by primary key. Use a stable `id`
    (e.g. 'ojas-main', 'ojas-caddy', f'deployed:{slug}') so restarts update
    the row in place instead of accumulating stale entries."""
    with _connect() as cx:
        cx.execute(
            "INSERT OR REPLACE INTO ojas_services"
            "(id, source, pid, label, command, port, bind_addr, url, started_at, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id, source, pid, label, command, port, bind_addr, url,
                _now(),
                json.dumps(meta) if meta is not None else None,
            ),
        )


def delete_ojas_service(id: str) -> None:
    with _connect() as cx:
        cx.execute("DELETE FROM ojas_services WHERE id = ?", (id,))


def list_ojas_services() -> list[dict]:
    """All Ojas-owned service rows — both live-process and port-only entries.
    Returned shape is dict with string-parsed meta so the API layer can
    just JSON it."""
    with _connect() as cx:
        rows = cx.execute(
            "SELECT id, source, pid, label, command, port, bind_addr, url, "
            "       started_at, meta_json "
            "FROM ojas_services ORDER BY source ASC, port ASC NULLS LAST, label ASC"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        if d.get("meta_json"):
            try:
                d["meta"] = json.loads(d.pop("meta_json"))
            except (ValueError, TypeError):
                d["meta"] = None
        else:
            d.pop("meta_json", None)
            d["meta"] = None
        out.append(d)
    return out


def clear_ojas_services_with_pid() -> int:
    """Drop every ojas_services row that has a pid (live-process rows).
    Port-only rows (deployed apps, known URLs) are kept. Called at backend
    boot before re-registering the current process tree, so we don't
    leave pointers to PIDs that no longer exist."""
    with _connect() as cx:
        cur = cx.execute("DELETE FROM ojas_services WHERE pid IS NOT NULL")
        return cur.rowcount


# ============================================================================
# Deployed apps — promoted session builds living at /opt/ojas-apps/<slug>/
# ============================================================================

import re as _re


def _slugify(text: str) -> str:
    """Lowercase, drop non-alphanumeric (except - and _), collapse repeated
    hyphens. Returns '' for empty/garbage input so callers can fall back
    to a default like 'app'."""
    s = (text or "").strip().lower()
    s = _re.sub(r"[^a-z0-9_-]+", "-", s)
    s = _re.sub(r"-{2,}", "-", s).strip("-_")
    return s[:40]   # cap length so URLs stay tidy


class DeployedSlugTaken(RuntimeError):
    """Raised when the requested slug is already in use by another
    deployed app. The user (not the server) picks a new slug — we
    don't auto-suffix with `-2`/`-3` because that produces surprising
    URLs the user didn't ask for. The HTTP layer maps this to a 409
    with the conflicting slug in the body so the dialog can show it
    inline next to the input."""

    def __init__(self, slug: str) -> None:
        super().__init__(f"slug already taken: {slug}")
        self.slug = slug


def allocate_deployed_slug(desired: str) -> str:
    """Return `desired` if it's free, otherwise raise
    :class:`DeployedSlugTaken`. We do NOT auto-suffix with `-2`/`-3`
    — the user is shown a 409 and picks a different slug themselves.
    Auto-suffixing was removed because it produced URLs the user
    didn't choose (silent surprise) and made slug-based book-keeping
    (DNS, browser bookmarks, settings) ambiguous.
    """
    base = _slugify(desired) or "app"
    if get_deployed_app(base) is None:
        return base
    raise DeployedSlugTaken(base)


def create_deployed_app(
    slug: str, name: str, app_dir: str,
    source_session_id: str | None = None,
    source_project_id: str | None = None,
    owner_user_id: str | None = None,
    project_dir: str | None = None,
    service_name: str | None = None,
    port: int | None = None,
) -> dict:
    """Insert a new deployed-app row. Caller has already copied files to
    `app_dir` and verified the slug is free. `project_dir` records which
    subfolder of the source session was promoted (for re-deploys and UI
    display); nullable for legacy callers. `service_name` and `port` are
    set by fullstack deploys (v1.1); static deploys leave them NULL.
    New rows default to state='running'."""
    now = int(time.time())
    with _connect() as cx:
        cx.execute(
            "INSERT INTO deployed_apps "
            "(slug, name, source_session_id, source_project_id, owner_user_id, "
            "app_dir, deployed_at, last_redeploy_at, project_dir, "
            "state, last_state_at, service_name, port) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)",
            (slug, name, source_session_id, source_project_id, owner_user_id,
             app_dir, now, now, project_dir,
             now, service_name, port),
        )
    return get_deployed_app(slug) or {}


def touch_deployed_app(slug: str, project_dir: str | None = None) -> None:
    """Bump last_redeploy_at after an in-place re-promote of the same slug.
    When `project_dir` is provided, also update the recorded subfolder so a
    re-deploy can repoint the app at a different build without allocating
    a new slug. Pass `project_dir=None` (default) to leave the existing
    value untouched."""
    with _connect() as cx:
        if project_dir is not None:
            cx.execute(
                "UPDATE deployed_apps "
                "SET last_redeploy_at = ?, project_dir = ? WHERE slug = ?",
                (int(time.time()), project_dir, slug),
            )
        else:
            cx.execute(
                "UPDATE deployed_apps SET last_redeploy_at = ? WHERE slug = ?",
                (int(time.time()), slug),
            )


# ---- Pause / resume ------------------------------------------------------
#
# State machine for deployed apps. Transitions:
#   running  --toggle-off-->  stopped
#   stopped  --toggle-on--->  running
#   starting --health-ok---->  running
#   starting --health-fail--> error
#   error    --toggle-on----> starting  (retry)
#
# Static apps skip the systemd layer: toggling off just makes the
# Caddy route point to a "paused" page. The Caddy route generator
# in server/app.py reads `state` and writes the appropriate route.
# Fullstack apps (v1.1+) additionally start/stop the systemd unit.

_DEPLOYED_STATE_TRANSITIONS = {
    "running":  {"stopped", "starting", "error"},
    "stopped":  {"running", "starting"},
    "starting": {"running", "error", "stopped"},
    "error":    {"stopped", "starting"},
}


def set_deployed_app_state(
    slug: str,
    new_state: str,
    error_message: str | None = None,
    last_health_at: int | None = None,
) -> bool:
    """Update the state of a deployed app. Returns True on success, False
    if the slug doesn't exist or the transition is illegal. `error_message`
    is stored only when state='error' (cleared otherwise). `last_health_at`
    is stored only when provided (callers pass it after a successful /api/health
    check)."""
    if new_state not in _DEPLOYED_STATE_TRANSITIONS:
        return False
    with _connect() as cx:
        row = cx.execute(
            "SELECT state FROM deployed_apps WHERE slug = ?", (slug,),
        ).fetchone()
        if row is None:
            return False
        current = row["state"]
        if new_state != current and new_state not in _DEPLOYED_STATE_TRANSITIONS.get(current, set()):
            return False
        err = error_message if new_state == "error" else None
        cx.execute(
            "UPDATE deployed_apps "
            "SET state = ?, last_state_at = ?, error_message = ?, "
            "    last_health_at = COALESCE(?, last_health_at) "
            "WHERE slug = ?",
            (new_state, int(time.time()), err, last_health_at, slug),
        )
    return True


def get_deployed_app(slug: str) -> dict | None:
    with _connect() as cx:
        row = cx.execute(
            "SELECT * FROM deployed_apps WHERE slug = ?", (slug,),
        ).fetchone()
    return dict(row) if row else None


def list_deployed_apps_for_session(session_id: str) -> list[dict]:
    """List every deployed app whose `source_session_id` matches. Used by
    the deploy banner to compare the latest build mtime against the
    most recent `last_redeploy_at` from this session — so the banner
    only shows "Build ready" when there's actually something newer."""
    with _connect() as cx:
        rows = cx.execute(
            "SELECT slug, last_redeploy_at FROM deployed_apps "
            "WHERE source_session_id = ?",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_deployed_app_for_subapp(
    source_session_id: str, project_dir: str | None,
) -> dict | None:
    """Return the deployed app row whose (source_session_id, project_dir)
    matches -- i.e. the slug this sub-app is currently published under.
    Used by the deploy endpoint to enforce 'one slug per sub-app per
    session': a session can host N sub-apps (N project_dir values), but
    each sub-app is locked to a single slug. Re-deploying the same
    sub-app under a different slug returns 409 -- the user must delete
    the existing deploy first.

    `project_dir` is matched NULL-safe: None <-> NULL and '' <-> ''
    (the session root). The deploy endpoint passes the same project_dir
    it intends to deploy, so a None/empty project_dir in the request
    matches a row whose stored project_dir is also None or ''.

    Returns None if the sub-app has never been deployed.
    """
    with _connect() as cx:
        if project_dir is None or project_dir == "":
            row = cx.execute(
                "SELECT * FROM deployed_apps "
                "WHERE source_session_id = ? AND "
                "      (project_dir IS NULL OR project_dir = '') "
                "LIMIT 1",
                (source_session_id,),
            ).fetchone()
        else:
            row = cx.execute(
                "SELECT * FROM deployed_apps "
                "WHERE source_session_id = ? AND project_dir = ? "
                "LIMIT 1",
                (source_session_id, project_dir),
            ).fetchone()
    return dict(row) if row else None


def list_deployed_apps(owner_user_id: str | None = None) -> list[dict]:
    """List deployed apps. If owner_user_id is given, only that user's apps
    (plus any orphan apps whose owner was deleted — keeps the admin view
    able to clean those up). None = all (root scope)."""
    with _connect() as cx:
        if owner_user_id is None:
            rows = cx.execute(
                "SELECT * FROM deployed_apps ORDER BY last_redeploy_at DESC"
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT * FROM deployed_apps WHERE owner_user_id = ? "
                "ORDER BY last_redeploy_at DESC",
                (owner_user_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_deployed_app(slug: str) -> bool:
    """Remove the DB row. Caller is responsible for rmtree on app_dir.
    Returns True if a row was deleted."""
    with _connect() as cx:
        cur = cx.execute("DELETE FROM deployed_apps WHERE slug = ?", (slug,))
        return cur.rowcount > 0


def list_deployed_apps_grouped(
    owner_user_id: str | None = None,
) -> list[dict]:
    """List deployed apps grouped by source session for the settings page.
    Returns a list of session-shaped dicts (one per session that has at
    least one app), each with a `deployed_apps` sublist. Sessions are
    sorted by the most recent app activity (last_redeploy_at desc).

    owner_user_id=None means root scope (all users). Otherwise, the caller's
    own apps + orphans (NULL owner, left behind when a user was deleted).
    Sessions with no apps are NOT included — use the regular session list
    for that.
    """
    with _connect() as cx:
        if owner_user_id is None:
            rows = cx.execute(
                "SELECT a.*, s.name AS session_name "
                "FROM deployed_apps a "
                "LEFT JOIN sessions s ON s.id = a.source_session_id "
                "ORDER BY a.last_redeploy_at DESC"
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT a.*, s.name AS session_name "
                "FROM deployed_apps a "
                "LEFT JOIN sessions s ON s.id = a.source_session_id "
                "WHERE a.owner_user_id = ? OR a.owner_user_id IS NULL "
                "ORDER BY a.last_redeploy_at DESC",
                (owner_user_id,),
            ).fetchall()
    # Group by session
    sessions: dict[str, dict] = {}
    for r in rows:
        r = dict(r)
        sid = r.get("source_session_id") or "_orphan"
        bucket = sessions.setdefault(sid, {
            "session_id": r.get("source_session_id"),
            "session_name": r.get("session_name") or "(deleted session)",
            "deployed_apps": [],
        })
        bucket["deployed_apps"].append(r)
    return list(sessions.values())
