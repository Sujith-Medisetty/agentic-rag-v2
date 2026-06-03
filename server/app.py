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


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def projects_get(project_id: str, _token: str = Depends(require_token)):
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return ProjectResponse(**p)


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
    asyncio.create_task(run_turn(
        session_id=session_id,
        project_id=project["id"],
        workspace=project["workspace_path"],
        user_prompt=req.content,
    ))
    return {"accepted": True}


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
