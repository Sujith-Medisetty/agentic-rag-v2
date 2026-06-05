"""
FastAPI app — HTTP + WebSocket surface for the web backend.

Run:  uvicorn server.app:app --host 127.0.0.1 --port 8765

Routes:
  Auth:
    GET  /api/auth/status              → {needs_setup: bool}
    POST /api/auth/setup               → set initial passcode
    POST /api/auth/login               → {passcode, device_label} → {token}
    POST /api/auth/logout              → revoke the caller's token

  Projects:
    GET    /api/projects                          → list
    POST   /api/projects                          → create
    GET    /api/projects/{project_id}             → fetch one
    PATCH  /api/projects/{project_id}/settings    → update per-project settings

  Sessions:
    GET  /api/projects/{project_id}/sessions      → list (newest first)
    POST /api/projects/{project_id}/sessions      → create

  Messages + events (per session):
    GET  /api/sessions/{session_id}/messages      → full chat history
    POST /api/sessions/{session_id}/messages      → submit a new user prompt
                                                    (returns immediately; the
                                                     agent runs in the background
                                                     and events stream over the
                                                     WebSocket)
    GET  /api/sessions/{session_id}/events        → replay activity feed
                                                    (optionally ?since=<ts>)

  Git:
    GET  /api/sessions/{session_id}/git           → current branch + ahead/behind
    POST /api/sessions/{session_id}/push          → manual push (non-force)

  WebSocket:
    /api/sessions/{session_id}/stream → JSON events (assistant_text,
                                         tool_start, tool_done, agent_spawn,
                                         turn_summary, error, …)
                                         Auth: send {"type":"auth","token":"…"}
                                         as the first text frame after connect.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env at the project root BEFORE any other module reads os.getenv. The
# file is owner-only by convention (`chmod 600 .env`) and is git-ignored, so
# secrets stay out of the repo. python-dotenv silently no-ops if the file is
# missing — production VMs can still set vars via systemd / docker env if
# they prefer.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass   # dotenv not installed — fall back to shell env only

from fastapi import (
    Depends, FastAPI, HTTPException, Header, Query, WebSocket,
    WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware

from server import auth, db
from server.git_autocommit import get_git_info, push_to_remote
from server.reporter import WebReporter, get_bus
from server.schemas import (
    AuthStatusResponse, EventResponse, GitInfoResponse, LoginRequest,
    LoginResponse, MessagePostRequest, MessageResponse, ProjectCreateRequest,
    ProjectResponse, ProjectSettingsRequest, PushResponse, SessionCreateRequest,
    SessionResponse, SetupRequest,
)
from server.session_runner import run_turn


# ============================================================================
# App lifecycle — bootstrap DB + safety singletons on startup
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await _configure_runtime_singletons()
    yield


async def _configure_runtime_singletons() -> None:
    """Wire the agent's process-wide config (safety policy, hook runner,
    sandbox, model, optional MCP tools). Done ONCE at server boot because
    these globals affect every tool call across every concurrent session."""
    # Lazy imports so `server.app` can be imported in tests without langchain.
    from agents.nodes import configure_model, configure_tools
    from config.loader import load_config
    from safety.hooks import HookConfig, HookRunner
    from safety.permissions import PermissionPolicy
    from safety.sandbox import resolve_sandbox
    from server.mcp_loader import load_mcp_tools
    from tools.wrappers import configure_safety

    workspace = str(Path(os.getenv("AGENT_WORKSPACE", ".")).resolve())
    cfg = load_config(workspace=workspace, cli_model=None)

    perm_mode = cfg.permission_mode
    # Web mode never prompts on stdin — sensitive ops should be approved via
    # a UI modal in a later phase; for now we trust whatever the config sets.
    perm = PermissionPolicy(mode=perm_mode, prompter=None)
    sandbox = resolve_sandbox(
        workspace=workspace,
        enabled=cfg.sandbox.enabled,
        network_isolated=cfg.sandbox.network_isolated,
    )
    hooks = HookRunner(config=HookConfig(
        pre_tool_use      = cfg.hooks.pre_tool_use,
        post_tool_use     = cfg.hooks.post_tool_use,
        post_tool_failure = cfg.hooks.post_tool_failure,
    ))
    configure_safety(
        permission_policy = perm,
        hook_runner       = hooks,
        sandbox           = sandbox,
        workspace         = workspace,
        permission_mode   = perm_mode,
    )
    configure_model(
        model=cfg.model,
        thinking=cfg.thinking,
        thinking_budget=getattr(cfg, "thinking_budget", 10000),
        provider=cfg.provider,
    )

    # MCP tools — empty `mcp_servers` config returns [] immediately, so this
    # is a no-op for fresh installs. Once the user adds entries to .agent.json
    # and restarts the backend, the loaded tools land here, get bound to the
    # agent's tool list, and are surfaced in the system prompt.
    mcp_tools = await load_mcp_tools(cfg.mcp_servers)
    configure_tools(mcp_tools)


app = FastAPI(title="agentic-rag-v2", lifespan=lifespan)


# CORS — the Vite dev server runs on a different port than the FastAPI app
# during development. In production the static build is served by FastAPI
# itself so CORS isn't needed there.
_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("AGENTIC_RAG_CORS_ORIGINS", _DEFAULT_ORIGINS).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Auth dependency
# ============================================================================

def require_token(authorization: str | None = Header(default=None)) -> str:
    """Resolve the bearer token, validate it, return it. Used as a Depends()
    on every protected route. Returns the raw token so callers can log out."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(None, 1)[1].strip()
    if not auth.verify_token(token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked token")
    return token


# ============================================================================
# Auth routes
# ============================================================================

@app.get("/api/auth/status", response_model=AuthStatusResponse)
def auth_status():
    return AuthStatusResponse(needs_setup=auth.needs_setup())


@app.post("/api/auth/setup", response_model=LoginResponse)
def auth_setup(req: SetupRequest):
    if not auth.needs_setup():
        raise HTTPException(status.HTTP_409_CONFLICT, "passcode already set")
    try:
        auth.setup_passcode(req.passcode)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    token = auth.issue_token(req.passcode, device_label="initial-setup")
    return LoginResponse(token=token)


@app.post("/api/auth/login", response_model=LoginResponse)
def auth_login(req: LoginRequest):
    try:
        token = auth.issue_token(
            req.passcode, device_label=req.device_label or "unknown",
        )
    except PermissionError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong passcode")
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return LoginResponse(token=token)


@app.post("/api/auth/logout")
def auth_logout(token: str = Depends(require_token)):
    auth.revoke_token(token)
    return {"ok": True}


# ============================================================================
# Projects
# ============================================================================

@app.get("/api/projects", response_model=list[ProjectResponse])
def projects_list(_token: str = Depends(require_token)):
    return [ProjectResponse(**p) for p in db.list_projects()]


@app.post(
    "/api/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def projects_create(
    req: ProjectCreateRequest,
    _token: str = Depends(require_token),
):
    ws = Path(req.workspace_path).expanduser()
    if not ws.exists():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"workspace path does not exist: {ws}",
        )
    if not ws.is_dir():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"workspace path is not a directory: {ws}",
        )
    try:
        p = db.create_project(req.name, str(ws.resolve()))
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return ProjectResponse(**p)


def _default_workspace_path() -> str:
    """Where should the default project live on disk?

    Resolution order:
      1. `FORGE_DEFAULT_WORKSPACE` env var — explicit override, wins always.
      2. Platform default:
         - macOS:    ~/Desktop/Forge
         - Linux:    ~/forge        (no Desktop folder convention)
         - Windows:  ~/Forge
    """
    import platform
    override = os.getenv("FORGE_DEFAULT_WORKSPACE")
    if override:
        return os.path.expanduser(override)
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Desktop/Forge")
    if system == "Windows":
        return os.path.expanduser("~/Forge")
    return os.path.expanduser("~/forge")


@app.get("/api/projects/default", response_model=ProjectResponse)
def projects_default(_token: str = Depends(require_token)):
    """Get-or-create THE default project at the platform's Forge workspace
    (overridable via FORGE_DEFAULT_WORKSPACE env var). Used by the sidebar
    UI so casual users skip explicit project setup — every session lands
    in this single workspace until they choose otherwise.

    The folder is created on disk if missing so the agent's first `bash`
    call in the workspace doesn't fail with ENOENT."""
    default_ws = _default_workspace_path()
    # Reuse if a project already points here (idempotent).
    for p in db.list_projects():
        if p["workspace_path"] == default_ws:
            return ProjectResponse(**p)
    try:
        os.makedirs(default_ws, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not create default workspace folder: {e}",
        )
    # Pick a unique name — "Forge" is the obvious first choice but if the
    # user already named another project that, fall back to a suffix.
    name = "Forge"
    suffix = 0
    while True:
        try:
            project = db.create_project(name, default_ws)
            break
        except ValueError:
            suffix += 1
            name = f"Forge {suffix}"
            if suffix > 50:  # paranoia — never spin forever
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "couldn't create default project (too many name collisions)",
                )
    return ProjectResponse(**project)


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def projects_get(project_id: str, _token: str = Depends(require_token)):
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return ProjectResponse(**p)


def _purge_session_state_dir(session_id: str) -> None:
    """Best-effort removal of a session's private agent-state directory
    (~/.agent/sessions/<id>/). Silently swallows errors — the DB row is
    already gone, so we don't want a stuck file to prevent the API call
    from succeeding."""
    import shutil
    from server.session_runner import session_state_dir
    try:
        d = session_state_dir(session_id)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def _purge_langgraph_checkpoint(session_id: str) -> None:
    """Delete this session's LangGraph checkpoint rows from
    ~/.agent/checkpoints.db. Without this, the agent's compacted message
    history persists forever even after the session is deleted — if the
    same session_id were ever reused (unlikely but possible), the new
    session would resume from the old conversation."""
    try:
        from agents.graph import runner_graph
        cp = runner_graph.checkpointer
        if cp is not None and hasattr(cp, "delete_thread"):
            cp.delete_thread(session_id)
    except Exception:
        # Fallback: raw SQL on the known SqliteSaver tables. Best-effort —
        # if the schema changes in a future langgraph version, this just
        # leaves orphan rows (functionally harmless).
        import sqlite3
        from pathlib import Path
        try:
            conn = sqlite3.connect(
                str(Path.home() / ".agent" / "checkpoints.db"),
                check_same_thread=False,
            )
            try:
                for table in ("checkpoints", "writes"):
                    try:
                        conn.execute(
                            f"DELETE FROM {table} WHERE thread_id = ?",
                            (session_id,),
                        )
                    except sqlite3.OperationalError:
                        pass
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass


def _purge_session_bus(session_id: str) -> None:
    """Drop the in-memory SessionBus so subsequent code can't accidentally
    reuse it for a session that no longer exists in the DB."""
    try:
        from server.reporter import discard_bus
        discard_bus(session_id)
    except Exception:
        pass


def _purge_session_everything(session_id: str) -> None:
    """One-stop cleanup for everything tied to a session OUTSIDE the main
    SQLite (which CASCADE handles). Idempotent + best-effort — none of
    these can fail the API call."""
    _purge_session_state_dir(session_id)
    _purge_langgraph_checkpoint(session_id)
    _purge_session_bus(session_id)


@app.delete("/api/projects/{project_id}")
def projects_delete(project_id: str, _token: str = Depends(require_token)):
    """Delete a project AND everything that links to it:
      - rows in `sessions`, `messages`, `events` (FK CASCADE)
      - each session's private `~/.agent/sessions/<id>/` directory
      - each session's LangGraph checkpoint rows in `~/.agent/checkpoints.db`
      - each session's in-memory SessionBus
    The workspace folder on disk (the actual code) is NOT touched."""
    sessions = db.list_sessions(project_id)
    # Cancel any in-flight turn in any session of this project so we don't
    # leak a running task or have it write events to a deleted session.
    for s in sessions:
        task = _active_turns.get(s["id"])
        if task is not None and not task.done():
            task.cancel()
    if not db.delete_project(project_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    for s in sessions:
        _purge_session_everything(s["id"])
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
def sessions_delete(session_id: str, _token: str = Depends(require_token)):
    """Delete one session AND everything that links to it:
      - rows in `messages`, `events` (FK CASCADE on session)
      - `~/.agent/sessions/<id>/` (sub-agent records + todo store)
      - LangGraph checkpoint rows in `~/.agent/checkpoints.db`
      - in-memory SessionBus"""
    task = _active_turns.get(session_id)
    if task is not None and not task.done():
        task.cancel()
    if not db.delete_session(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    _purge_session_everything(session_id)
    return {"ok": True}


@app.get("/api/paths/browse")
def paths_browse(
    cwd: str | None = Query(default=None),
    _token: str = Depends(require_token),
):
    """Server-side directory browser. The web platform refuses to give the
    page a real filesystem path (showDirectoryPicker returns an opaque
    handle, <input webkitdirectory> hides absolute paths), so we build a
    custom navigator: backend lists subdirectories of a given path,
    frontend renders them as clickable rows. Same pattern VS Code Web and
    webmail use.

    Returns `{cwd, parent, entries}` where entries are folder rows
    (sorted, name-only-visible, hidden dirs excluded). `parent` is None at
    the filesystem root.

    `cwd=None` → user home directory.
    """
    import os
    if cwd is None or cwd == "":
        target = Path(os.path.expanduser("~"))
    else:
        target = Path(cwd).expanduser()
    try:
        target = target.resolve(strict=False)
    except OSError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"bad path: {e}")
    if not target.exists() or not target.is_dir():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"not a directory: {target}",
        )
    entries: list[dict] = []
    try:
        for child in target.iterdir():
            name = child.name
            if name.startswith("."):
                continue   # hide dotfiles + dotfolders (.git, .DS_Store, etc.)
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            entries.append({"name": name, "path": str(child)})
    except PermissionError:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"permission denied reading {target}",
        )
    entries.sort(key=lambda e: e["name"].lower())
    parent = str(target.parent) if target.parent != target else None
    return {"cwd": str(target), "parent": parent, "entries": entries}


@app.get("/api/paths/common")
def paths_common(_token: str = Depends(require_token)):
    """Return a list of common dev-directory locations that actually exist on
    this user's machine. Used by the New Project modal to offer one-tap
    prefill chips instead of forcing the user to type a long path.

    Read-only, single-pass, no recursion. Each entry is `{label, path}`."""
    import os
    home = Path(os.path.expanduser("~"))
    candidates: list[tuple[str, Path]] = [
        ("Home",      home),
        ("Documents", home / "Documents"),
        ("Desktop",   home / "Desktop"),
        ("Downloads", home / "Downloads"),
        ("code",      home / "code"),
        ("Code",      home / "Code"),
        ("dev",       home / "dev"),
        ("Projects",  home / "Projects"),
        ("workspace", home / "workspace"),
        ("github",    home / "github"),
        ("Documents/GitHub", home / "Documents" / "GitHub"),
    ]
    out = []
    seen: set[str] = set()
    for label, p in candidates:
        try:
            resolved = str(p.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        if p.is_dir():
            out.append({"label": label, "path": resolved})
            seen.add(resolved)
    return {"locations": out}


@app.patch("/api/projects/{project_id}/settings", response_model=ProjectResponse)
def projects_update_settings(
    project_id: str,
    req: ProjectSettingsRequest,
    _token: str = Depends(require_token),
):
    if db.get_project(project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    try:
        updated = db.update_project_settings(
            project_id,
            auto_commit_enabled=req.auto_commit_enabled,
            auto_push_enabled=req.auto_push_enabled,
            branch_strategy=req.branch_strategy,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return ProjectResponse(**updated)


# ============================================================================
# Sessions
# ============================================================================

@app.get(
    "/api/projects/{project_id}/sessions",
    response_model=list[SessionResponse],
)
def sessions_list(project_id: str, _token: str = Depends(require_token)):
    if db.get_project(project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return [SessionResponse(**s) for s in db.list_sessions(project_id)]


@app.post(
    "/api/projects/{project_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def sessions_create(
    project_id: str,
    req: SessionCreateRequest,
    _token: str = Depends(require_token),
):
    if db.get_project(project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    s = db.create_session(project_id, req.name)
    return SessionResponse(**s)


# ============================================================================
# Messages + events
# ============================================================================

@app.get(
    "/api/sessions/{session_id}/messages",
    response_model=list[MessageResponse],
)
def messages_list(session_id: str, _token: str = Depends(require_token)):
    if db.get_session(session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return [MessageResponse(**m) for m in db.list_messages(session_id)]


@app.post(
    "/api/sessions/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
)
async def messages_post(
    session_id: str,
    req: MessagePostRequest,
    _token: str = Depends(require_token),
):
    """Submit a new user prompt. Returns 202 immediately; the agent runs as
    a background task and emits events over the WebSocket."""
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    project = db.get_project(session["project_id"])
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    # Make sure the bus is bound to THIS loop before the worker thread starts
    # publishing — bind_loop is idempotent.
    bus = get_bus(session_id)
    if not bus.is_bound():
        bus.bind_loop(asyncio.get_running_loop())

    # Fire-and-forget. Errors land on the bus via reporter.error().
    task = asyncio.create_task(run_turn(
        session_id=session_id,
        project_id=project["id"],
        workspace=project["workspace_path"],
        user_prompt=req.content,
    ))
    _active_turns[session_id] = task
    task.add_done_callback(lambda _t, sid=session_id: _active_turns.pop(sid, None))
    return {"accepted": True}


# Per-session in-flight turn registry — used by the cancel endpoint to abort a
# running turn. Cleared when the task finishes naturally.
_active_turns: dict[str, asyncio.Task] = {}


@app.post("/api/sessions/{session_id}/cancel")
async def cancel_turn(
    session_id: str,
    _token: str = Depends(require_token),
):
    """Cancel the in-flight turn for this session, if any. Idempotent — if no
    turn is running, returns ok=false rather than failing.

    The end-of-turn events (error + assistant_text(done=True) + turn_summary
    + persisted system message) are emitted by session_runner's
    CancelledError handler, so the turn closes consistently regardless of
    whether it ended by success, exception, or cancel. Publishing them here
    too would double up on the wire and risk drift between the two paths.

    The in-flight worker thread is hard to interrupt (Python limitation), so
    late events from the LangGraph worker may still drip onto the bus for a
    few seconds — the UI ignores them once turn_summary has arrived."""
    task = _active_turns.get(session_id)
    if task is None or task.done():
        return {"ok": False, "reason": "no active turn"}
    task.cancel()
    return {"ok": True}


@app.post("/api/sessions/{session_id}/compact")
async def compact_session(
    session_id: str,
    _token: str = Depends(require_token),
):
    """Manually compact this session's LangGraph message history NOW, instead
    of waiting for the automatic 100K-token trigger. Useful when responses
    are getting slow and the user wants a fresh context budget without
    starting a new session.

    Replaces the entire `messages` list in the checkpoint with the compacted
    version: a SystemMessage summary + the last few messages verbatim. The
    `add_messages` reducer accepts `RemoveMessage(id=...)` for deletion, so
    we send removes for every existing message followed by the compacted
    list — no orphans, no duplication.
    """
    if _active_turns.get(session_id) and not _active_turns[session_id].done():
        return {"ok": False, "reason": "a turn is in flight — cancel it first"}

    from agents.graph import runner_graph
    from langchain_core.messages import RemoveMessage
    from memory.checkpointer import _compact_messages

    config = {"configurable": {"thread_id": session_id}}
    snapshot = runner_graph.get_state(config)
    messages = list(snapshot.values.get("messages", []) or [])
    before = len(messages)
    if before == 0:
        return {"ok": False, "reason": "no messages to compact"}

    compacted = _compact_messages(messages)
    if len(compacted) >= before:
        return {"ok": False, "reason": "nothing to compact (history is already small)"}

    removes = [
        RemoveMessage(id=m.id)
        for m in messages
        if getattr(m, "id", None)
    ]
    runner_graph.update_state(config, {"messages": removes + compacted})
    return {"ok": True, "before": before, "after": len(compacted)}


@app.get(
    "/api/sessions/{session_id}/events",
    response_model=list[EventResponse],
)
def events_list(
    session_id: str,
    since: int | None = Query(default=None),
    _token: str = Depends(require_token),
):
    if db.get_session(session_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return [EventResponse(**e) for e in db.list_events(session_id, since=since)]


# ---- Per-session git state -------------------------------------------------

def _resolve_session_workspace(session_id: str) -> str:
    session = db.get_session(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    project = db.get_project(session["project_id"])
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return project["workspace_path"]


@app.get(
    "/api/sessions/{session_id}/git",
    response_model=GitInfoResponse,
)
def sessions_git(session_id: str, _token: str = Depends(require_token)):
    workspace = _resolve_session_workspace(session_id)
    info = get_git_info(workspace)
    return GitInfoResponse(**info.__dict__)


@app.post(
    "/api/sessions/{session_id}/push",
    response_model=PushResponse,
)
async def sessions_push(session_id: str, _token: str = Depends(require_token)):
    """Manual push of the session's current branch. Always non-force.
    Result is also broadcast as a `push_done` event so all connected clients
    see it land."""
    workspace = _resolve_session_workspace(session_id)
    # Run subprocess off the event loop so a slow network push doesn't block.
    pr = await asyncio.get_running_loop().run_in_executor(
        None, lambda: push_to_remote(workspace),
    )
    # Broadcast so any open chat tabs update their badge.
    try:
        WebReporter(session_id).push_done(
            branch=pr.branch, ok=pr.pushed, remote=pr.remote, error=pr.error,
        )
    except Exception:
        pass
    return PushResponse(
        pushed=pr.pushed, branch=pr.branch, remote=pr.remote, error=pr.error,
    )


# ============================================================================
# WebSocket — live activity stream
# ============================================================================

@app.websocket("/api/sessions/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: str):
    """Live activity stream for a session.

    Protocol:
      1. Client connects → server accepts.
      2. Client sends `{"type":"auth","token":"<bearer>"}` as the FIRST text frame.
      3. Server validates; on failure → close with code 4401.
      4. Server then streams `{kind, payload, ts}` JSON frames as the agent runs.
    """
    await websocket.accept()
    try:
        first = await websocket.receive_text()
    except WebSocketDisconnect:
        return

    try:
        envelope = json.loads(first)
    except json.JSONDecodeError:
        await websocket.close(code=4400, reason="bad handshake")
        return
    if envelope.get("type") != "auth" or not auth.verify_token(envelope.get("token") or ""):
        await websocket.close(code=4401, reason="auth failed")
        return

    if db.get_session(session_id) is None:
        await websocket.close(code=4404, reason="session not found")
        return

    bus = get_bus(session_id)
    if not bus.is_bound():
        bus.bind_loop(asyncio.get_running_loop())
    bus.subscribe(websocket)
    try:
        # Keep the socket alive while the bus pushes events. We don't need to
        # read anything else from the client; ignore inbound frames (used as
        # ping / keepalive by some clients).
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        bus.unsubscribe(websocket)


# ============================================================================
# Health
# ============================================================================

@app.get("/api/health")
def health():
    return {"ok": True, "needs_setup": auth.needs_setup()}
