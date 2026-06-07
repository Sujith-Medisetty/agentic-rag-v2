"""
FastAPI app -- HTTP + WebSocket surface for the web backend.

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
import logging
import os
import re
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger("ojas.app")

# Load .env at the project root BEFORE any other module reads os.getenv. The
# file is owner-only by convention (`chmod 600 .env`) and is git-ignored, so
# secrets stay out of the repo. python-dotenv silently no-ops if the file is
# missing -- production VMs can still set vars via systemd / docker env if
# they prefer.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass   # dotenv not installed -- fall back to shell env only

from fastapi import (
    Depends, FastAPI, HTTPException, Header, Query, Request, Response,
    WebSocket, WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware

from server import auth, db
from server.git_autocommit import get_git_info, push_to_remote
from server.reporter import WebReporter, get_bus
from pydantic import BaseModel
from server.schemas import (
    AuthStatusResponse, DeployRequest, DeployResponse, DeployedAppResponse,
    DeployStateResponse, DeployedAppsBySession,
    EventResponse, GitInfoResponse, LoginRequest,
    LoginResponse, MessagePostRequest, MessageResponse, OjasServiceResponse,
    ProcessResponse, ProjectCreateRequest, ProjectResponse, ProjectSettingsRequest,
    PushResponse, SessionCreateRequest, SessionRenameRequest, SessionResponse,
    SetupRequest, SignupRequest, UserResponse, AdminResetPasswordRequest,
)
from server.session_runner import run_turn


# ============================================================================
# App lifecycle -- bootstrap DB + safety singletons on startup
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await _configure_runtime_singletons()
    _register_ojas_services()
    _reconcile_deployed_apps_on_boot()
    yield


def _reconcile_deployed_apps_on_boot() -> None:
    """Bring the on-disk state of every deployed app in line with the
    DB's `state` column. Runs once on Ojas startup.

    Why this exists: a previous boot may have been killed mid-toggle,
    leaving a deployed app's dir at the wrong path (e.g. a paused
    app's dir still at the live path because the rename to .stopped
    never completed). Caddy will then serve the wrong content. The
    reconciliation is idempotent -- for each row we just call
    _apply_app_state_to_disk() which is a no-op if the dir is already
    in the right place.
    """
    try:
        with db._connect() as cx:   # noqa: SLF001
            rows = cx.execute("SELECT slug, state FROM deployed_apps").fetchall()
    except Exception:
        return
    reconciled = 0
    for r in rows:
        try:
            _apply_app_state_to_disk(r["slug"], r["state"] or "running")
            reconciled += 1
        except Exception:
            # One bad app shouldn't block the rest. Logged elsewhere
            # (the toggle endpoint will surface the real error to the
            # user when they next try to use the app).
            pass
    if reconciled:
        print(f"[ojas] reconciled {reconciled} deployed app(s) to match DB state on boot")


def _register_ojas_services() -> None:
    """Idempotently register the Ojas-owned runtime services (main backend,
    caddy reverse proxy) in `ojas_services` so the admin panel can show
    them. Called once on backend boot. Port-only rows for deployed apps
    are also created/refreshed here so the panel always reflects the
    truth on disk under /opt/ojas-apps/."""
    # 1) Drop any stale PID rows from a previous boot. Deployed-app port
    #    rows (pid IS NULL) are kept -- they're tied to on-disk files, not
    #    a process. We'll re-add the still-running ones below.
    db.clear_ojas_services_with_pid()

    # 2) Main uvicorn process. PID of this process is the uvicorn worker
    #    (the parent is whatever launched us -- uvicorn / python -m / etc.).
    main_pid = os.getpid()
    bind_addr = "127.0.0.1"
    # Try to read the actual --port from the parent cmdline (works for the
    # common `uvicorn server.app:app --host ... --port N` invocation).
    port = 8765
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as fh:
            parent_cmdline = fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
        m = re.search(r"--port[=\s]+(\d+)", parent_cmdline)
        if m:
            port = int(m.group(1))
    except (OSError, ValueError):
        pass
    db.upsert_ojas_service(
        id="ojas-main",
        source="ojas-main",
        pid=main_pid,
        label="Ojas backend (FastAPI + uvicorn)",
        command=f"uvicorn server.app:app (pid {main_pid})",
        port=port,
        bind_addr=bind_addr,
        url=f"http://{bind_addr}:{port}/api/health",
    )

    # Resolve the public hostname once for URL construction below.
    public_domain = _resolve_public_domain()

    # 3) Caddy reverse proxy (if installed). Caddy terminates 80/443 and
    #    forwards to this backend. Detect via PATH lookup; if missing,
    #    skip -- a developer running `uvicorn` directly won't have it.
    caddy_path = shutil.which("caddy")
    if caddy_path:
        # Find caddy's pid + a list of ports it's actually bound to.
        try:
            out = subprocess.run(
                ["pgrep", "-x", "caddy"], capture_output=True, text=True, timeout=2,
            ).stdout.strip().split()
        except (OSError, subprocess.SubprocessError):
            out = []
        caddy_pids = [int(p) for p in out if p.isdigit()]
        # ss from a non-root user redacts foreign pids, so we can't
        # always attribute caddy's listening ports back to its pid. Fall
        # back to: (a) caddy's well-known default ports (80, 443, 2019),
        # intersected with (b) the set of LISTEN ports we can read from
        # world-readable /proc/net/tcp{,6}. Caddy operators can override
        # via the OJAS_CADDY_PORTS env var (comma-separated).
        caddy_ports = _listening_ports_for_pids(caddy_pids) if caddy_pids else []
        if not caddy_ports:
            override = os.getenv("OJAS_CADDY_PORTS", "").strip()
            if override:
                candidates = [int(x) for x in override.split(",") if x.strip().isdigit()]
            else:
                candidates = [80, 443, 2019]
            listening_now = {p for (p, _, _) in _listening_ports_system_wide()}
            caddy_ports = sorted(p for p in candidates if p in listening_now)
        if caddy_pids:
            scheme = "https" if 443 in caddy_ports else ("http" if 80 in caddy_ports else None)
            caddy_url: str | None = None
            if scheme and public_domain:
                caddy_url = f"{scheme}://{public_domain}/"
            elif scheme:
                caddy_url = f"{scheme}://localhost/"
            db.upsert_ojas_service(
                id="ojas-caddy",
                source="ojas-proxy",
                pid=caddy_pids[0],
                label="Caddy reverse proxy",
                command=f"{caddy_path} (pids {','.join(map(str, caddy_pids))})",
                port=caddy_ports[0] if caddy_ports else None,
                bind_addr="0.0.0.0",
                url=caddy_url,
                meta={
                    "pids": caddy_pids,
                    "ports": caddy_ports,
                    "public_domain": public_domain,
                },
            )
        # Cache the discovered ports in the ojas-external reconcile step
        # too, so the /api/admin/services call returns them.

    # 3b) Ojas web UI -- the React/Vite build at /opt/ojas/web/dist. NOT a
    #     separate process: the static files are served by caddy's
    #     file_server directive (see deploy/Caddyfile). Registered as a
    #     port-only row pointing at the caddy URL so the admin panel
    #     shows it as a first-class Ojas service.
    webui_dist = Path("/opt/ojas/web/dist")
    if webui_dist.exists() and webui_dist.is_dir():
        index_html = webui_dist / "index.html"
        if index_html.exists():
            stat = index_html.stat()
            scheme = "https" if (caddy_path and 443 in (caddy_ports or [])) else (
                "http" if (caddy_path and 80 in (caddy_ports or [])) else None
            )
            webui_url: str | None = None
            if scheme and public_domain:
                webui_url = f"{scheme}://{public_domain}/"
            elif scheme:
                webui_url = f"{scheme}://localhost/"
            db.upsert_ojas_service(
                id="ojas-webui",
                source="ojas-main",
                pid=None,
                label="Ojas web UI (React build, served by Caddy)",
                command=None,
                port=443 if (caddy_path and 443 in (caddy_ports or [])) else (
                    80 if (caddy_path and 80 in (caddy_ports or [])) else None
                ),
                bind_addr="0.0.0.0" if caddy_path else None,
                url=webui_url,
                meta={
                    "build_path": str(webui_dist),
                    "served_by": "ojas-caddy" if caddy_path else None,
                    "build_mtime": int(stat.st_mtime),
                    "build_size_kb": round(stat.st_size / 1024, 1),
                    "public_domain": public_domain,
                },
            )

    # 4) Deployed apps -- port-only rows. Each deployed app is static
    #    files served at /apps/<slug>/ via caddy (or by this backend in
    #    dev mode). Re-register every row on disk so the panel stays
    #    accurate even if the backend was restarted.
    OJAS_APPS_ROOT.mkdir(parents=True, exist_ok=True)
    # Resolve the apps root domain once. Apps live at <slug>.<apps_root>
    # -- separate from the apex (the Ojas main URL) so users can host
    # apps on a different domain if they want.
    apps_root = _resolve_apps_root_domain() or public_domain
    for app_row in db.list_deployed_apps(owner_user_id=None):
        slug = app_row["slug"]
        # Build a clickable public URL using the resolved apps root.
        # Falls back to the legacy /apps/<slug>/ subpath when no
        # public domain is resolved (e.g. local dev). Same backend
        # serves both routes so old links don't break.
        deployed_url: str | None = None
        if apps_root:
            deployed_url = f"https://{slug}.{apps_root}/"
        else:
            deployed_url = f"/apps/{slug}/"
        db.upsert_ojas_service(
            id=f"deployed:{slug}",
            source="ojas-deployed",
            pid=None,
            label=f"Deployed app: {app_row['name']}",
            command=None,
            port=None,
            bind_addr=None,
            url=deployed_url,
            meta={
                "slug": slug,
                "app_dir": app_row.get("app_dir"),
                "owner_user_id": app_row.get("owner_user_id"),
                "source_session_id": app_row.get("source_session_id"),
                "public_domain": public_domain,
            },
        )


def _listening_ports_for_pids(pids: list[int]) -> list[int]:
    """Return the sorted list of ports any process in `pids` is currently
    listening on. Tries, in order:
      1. `ss -ltnp -H` (works fully when the process is owned by us;
         partial -- no pid info -- for foreign-user processes)
      2. /proc/<pid>/fd + /proc/net/tcp inode walk (works fully only for
         same-user pids)
    For a non-root service like the Ojas backend (running as the `ojas`
    user), this only attributes ports to the main uvicorn. Foreign-user
    listeners (caddy, future services) fall through to a known-port
    hint table in _register_ojas_services()."""
    if not pids:
        return []
    pid_set = {str(p) for p in pids}
    ss = shutil.which("ss")
    if ss:
        try:
            res = subprocess.run(
                ["ss", "-ltnp", "-H"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            res = None
        if res is not None and res.returncode == 0:
            ports: set[int] = set()
            for line in res.stdout.splitlines():
                # Lines look like:
                #   LISTEN 0 4096 *:80 *:* users:(("caddy",pid=23816,fd=4))
                if "pid=" not in line:
                    continue
                if not any(f"pid={p}," in line or f"pid={p})" in line for p in pid_set):
                    continue
                try:
                    local = line.split()[3]
                    port = int(local.rsplit(":", 1)[-1])
                    ports.add(port)
                except (IndexError, ValueError):
                    continue
            if ports:
                return sorted(ports)
    # Fallback -- /proc scan. Works for the main backend (same user); fails
    # for foreign-user processes like caddy running as the `caddy` user.
    inodes_for_pid: set[str] = set()
    try:
        for pid in pids:
            try:
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        link = os.readlink(f"/proc/{pid}/fd/{fd}")
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        inodes_for_pid.add(link[len("socket:["):-1])
            except (OSError, FileNotFoundError):
                continue
    except Exception:
        return []
    if not inodes_for_pid:
        return []
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r") as fh:
                fh.readline()  # header
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    local = parts[1]
                    state = parts[3]
                    inode = parts[9]
                    if state != "0A":  # 0A = LISTEN
                        continue
                    if inode not in inodes_for_pid:
                        continue
                    try:
                        port = int(local.split(":", 1)[1], 16)
                    except (ValueError, IndexError):
                        continue
                    ports.add(port)
        except OSError:
            continue
    return sorted(ports)


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
    # Web mode never prompts on stdin -- sensitive ops should be approved via
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

    # MCP tools -- empty `mcp_servers` config returns [] immediately, so this
    # is a no-op for fresh installs. Once the user adds entries to .agent.json
    # and restarts the backend, the loaded tools land here, get bound to the
    # agent's tool list, and are surfaced in the system prompt.
    mcp_tools = await load_mcp_tools(cfg.mcp_servers)
    configure_tools(mcp_tools)


app = FastAPI(title="agentic-rag-v2", lifespan=lifespan)


# CORS -- the Vite dev server runs on a different port than the FastAPI app
# during development. In production the static build is served by FastAPI
# itself so CORS isn't needed there.
_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("OJAS_CORS_ORIGINS", _DEFAULT_ORIGINS).split(","),
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


def require_user(authorization: str | None = Header(default=None)) -> dict:
    """Like `require_token` but resolves the bearer to a full user dict
    (with `id`, `email`, `role`). Used by any handler that needs to know
    WHO is calling -- i.e. anything multi-user (project / session listing,
    creation, deletion) plus the admin endpoints."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(None, 1)[1].strip()
    user = auth.user_from_token(token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked token")
    return user


def require_root(user: dict = Depends(require_user)) -> dict:
    """Gate admin endpoints -- caller must be the root user."""
    if user.get("role") != "root":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "root role required")
    return user


def _session_or_404(session_id: str, user: dict) -> dict:
    """Return the session row IF the caller can access it. Root sees all,
    non-root only their own. 404 (not 403) on access-denied so we don't
    leak the existence of other users' sessions."""
    s = db.get_session(session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    if user["role"] != "root" and s.get("user_id") not in (None, user["id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return s


def _project_or_404(project_id: str, user: dict) -> dict:
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if user["role"] != "root" and p.get("user_id") not in (None, user["id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return p


# ============================================================================
# Auth routes
# ============================================================================

@app.get("/api/auth/status", response_model=AuthStatusResponse)
def auth_status():
    return AuthStatusResponse(
        needs_setup=auth.needs_setup(),
        has_root=auth.has_root_configured(),
        signup_allowed=auth.signup_allowed(),
    )


@app.post("/api/auth/signup", response_model=LoginResponse)
def auth_signup(req: SignupRequest):
    try:
        user = auth.signup(req.email, req.password)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    # Immediately materialise the user's default project so they aren't
    # stranded with a token but no project if the Workspace page fails to
    # load for any reason (network blip, navigation race, etc.). This was
    # the "I signed up but my sidebar is empty / project_count = 0" bug.
    try:
        _bootstrap_default_project(user)
    except Exception:
        # Project creation must not break signup itself -- the user can
        # still trigger it via the Workspace flow on next visit. We log
        # with full traceback so a recurrence is debuggable instead of
        # silently leaving the user with a half-set-up account that
        # later 500s on session create with a confusing permission
        # error on the missing/wrong-owned workspace dir.
        logger.exception(
            "bootstrap_default_project failed for user_id=%s email=%s",
            user.get("id"), user.get("email"),
        )

    # Auto-log-in after signup so the user lands straight in the app.
    _, token = auth.login(req.email, req.password)
    return LoginResponse(
        token=token,
        user={
            "id": user["id"], "email": user["email"],
            "role": user["role"], "created_at": user["created_at"],
        },
    )


def _bootstrap_default_project(user: dict) -> None:
    """Idempotent -- create the calling user's default project if they don't
    already have one. Shared between auth_signup and projects_default so
    both code paths behave identically."""
    if db.list_projects(user_id=user["id"]):
        return
    base_ws = _default_workspace_path()
    if user["role"] == "root":
        default_ws = base_ws
    else:
        local = user["email"].split("@", 1)[0]
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in local).lower()[:40]
        default_ws = str(Path(base_ws) / slug)
    try:
        os.makedirs(default_ws, exist_ok=True)
    except OSError:
        # Surface why mkdir failed before re-raising. The signup caller
        # swallows the exception but logs it, so this trace tells us
        # whether the parent was unreadable, owned by root, on a RO
        # mount, etc. instead of leaving us guessing later.
        logger.exception(
            "default workspace mkdir failed: path=%s euid=%s",
            default_ws, os.geteuid(),
        )
        raise
    name = "Ojas"
    suffix = 0
    while True:
        try:
            db.create_project(name, default_ws, user_id=user["id"])
            return
        except ValueError:
            suffix += 1
            name = f"Ojas {suffix}"
            if suffix > 50:
                return


@app.post("/api/auth/login", response_model=LoginResponse)
def auth_login(req: LoginRequest):
    try:
        user, token = auth.login(req.email, req.password)
    except PermissionError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(e) or "unauthorized")
    return LoginResponse(
        token=token,
        user={
            "id": user["id"], "email": user["email"],
            "role": user["role"], "created_at": user["created_at"],
        },
    )


@app.get("/api/auth/me", response_model=UserResponse)
def auth_me(user: dict = Depends(require_user)):
    return {
        "id": user["id"], "email": user["email"],
        "role": user["role"], "created_at": user["created_at"],
    }


@app.post("/api/auth/logout")
def auth_logout(token: str = Depends(require_token)):
    auth.revoke_token(token)
    return {"ok": True}


# ============================================================================
# Projects
# ============================================================================

@app.get("/api/projects", response_model=list[ProjectResponse])
def projects_list(user: dict = Depends(require_user)):
    # Root sees every project (across all users). Non-root only their own.
    user_filter = None if user["role"] == "root" else user["id"]
    return [ProjectResponse(**p) for p in db.list_projects(user_id=user_filter)]


@app.post(
    "/api/projects",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def projects_create(
    req: ProjectCreateRequest,
    user: dict = Depends(require_user),
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
        p = db.create_project(req.name, str(ws.resolve()), user_id=user["id"])
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return ProjectResponse(**p)


def _default_workspace_path() -> str:
    """Where should the default project live on disk?

    Resolution order:
      1. `OJAS_DEFAULT_WORKSPACE` env var -- explicit override, wins always.
      2. Platform default:
         - macOS:    ~/Desktop/Ojas
         - Linux:    ~/ojas         (no Desktop folder convention)
         - Windows:  ~/Ojas
    """
    import platform
    override = os.getenv("OJAS_DEFAULT_WORKSPACE")
    if override:
        return os.path.expanduser(override)
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Desktop/Ojas")
    if system == "Windows":
        return os.path.expanduser("~/Ojas")
    return os.path.expanduser("~/ojas")


@app.get("/api/projects/default", response_model=ProjectResponse)
def projects_default(user: dict = Depends(require_user)):
    """Get-or-create THE default project for the calling user. Each user gets
    their own Ojas workspace folder so files don't mix across accounts.

    Layout:
      <OJAS_DEFAULT_WORKSPACE>/                  (shared root)
        ├── <user_email_slug_1>/                  (this user's projects + sessions)
        └── <user_email_slug_2>/                  (another user's)
    Root user uses the unscoped root directly (no email subdir) for simplicity.
    """
    # If the user already owns ANY project, reuse it. We used to require an
    # exact `workspace_path` match here, but path canonicalization differences
    # (symlinks / trailing slash / Path(...).resolve()) were producing one
    # new orphan project per login on the VM -- the old one's sessions then
    # became unreachable. Returning the first owned project is robust to all
    # of that. Users only ever have one "default" in practice.
    existing = db.list_projects(user_id=user["id"])
    if existing:
        return ProjectResponse(**existing[0])

    base_ws = _default_workspace_path()
    if user["role"] == "root":
        default_ws = base_ws
    else:
        # Slugify the email's local part as the user's folder. Idempotent.
        local = user["email"].split("@", 1)[0]
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in local).lower()[:40]
        default_ws = str(Path(base_ws) / slug)

    try:
        os.makedirs(default_ws, exist_ok=True)
    except OSError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not create default workspace folder: {e}",
        )
    # Pick a unique name within this user.
    name = "Ojas"
    suffix = 0
    while True:
        try:
            project = db.create_project(name, default_ws, user_id=user["id"])
            break
        except ValueError:
            suffix += 1
            name = f"Ojas {suffix}"
            if suffix > 50:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "couldn't create default project (too many name collisions)",
                )
    return ProjectResponse(**project)


@app.get("/api/projects/{project_id}", response_model=ProjectResponse)
def projects_get(project_id: str, user: dict = Depends(require_user)):
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    if user["role"] != "root" and p.get("user_id") != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    return ProjectResponse(**p)


def _purge_session_state_dir(session_id: str) -> None:
    """Best-effort removal of a session's private agent-state directory
    (~/.agent/sessions/<id>/). Silently swallows errors -- the DB row is
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
    history persists forever even after the session is deleted -- if the
    same session_id were ever reused (unlikely but possible), the new
    session would resume from the old conversation."""
    try:
        from agents.graph import runner_graph
        cp = runner_graph.checkpointer
        if cp is not None and hasattr(cp, "delete_thread"):
            cp.delete_thread(session_id)
    except Exception:
        # Fallback: raw SQL on the known SqliteSaver tables. Best-effort --
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


def _purge_session_workspace_subdir(session: dict) -> None:
    """Delete the session's private subdirectory under its project workspace.
    This is where the agent actually built files for this session, so on
    session delete we want it gone. Best-effort -- silently skips if the
    project is gone (cascade may have happened first) or the subdir is missing."""
    import shutil
    try:
        subdir_slug = session.get("workspace_subdir")
        if not subdir_slug:
            return  # legacy session created before the subdir column
        project = db.get_project(session["project_id"])
        if project is None:
            return
        target = Path(project["workspace_path"]) / subdir_slug
        # Defence in depth: make sure target is actually under the project
        # workspace, never traversing out.
        try:
            target_resolved = target.resolve()
            ws_resolved = Path(project["workspace_path"]).resolve()
            target_resolved.relative_to(ws_resolved)
        except (ValueError, OSError):
            return  # subdir is suspiciously outside the workspace -- skip
        if target_resolved.exists():
            shutil.rmtree(target_resolved, ignore_errors=True)
    except Exception:
        pass


def _kill_session_processes(session_id: str) -> None:
    """SIGTERM every long-running process registered for this session, then
    remove the DB rows."""
    import os
    import signal
    procs = db.list_processes_for_session(session_id)
    for p in procs:
        pid = p["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        db.unregister_process(pid)


def _purge_deployed_apps_for_session(session_id: str) -> None:
    """SIGTERM-nothing, but rmtree every deployed_apps `app_dir` rooted in
    this session AND delete the DB rows so the public subdomain stops
    resolving. Best-effort. The schema's ON DELETE CASCADE (on new rows
    only) also handles the row drop; we explicitly delete here so the
    semantics are identical for legacy rows too."""
    import shutil
    try:
        # Inline SQL because db.list_deployed_apps doesn't filter by session.
        with db._connect() as cx:   # noqa: SLF001 -- internal helper, fine here
            rows = cx.execute(
                "SELECT slug, app_dir FROM deployed_apps "
                "WHERE source_session_id = ?",
                (session_id,),
            ).fetchall()
        for row in rows:
            slug = row["slug"]
            # Stop the systemd unit first (v1.1 fullstack) so the
            # process releases its files before we rmtree. v1 is
            # static-only so this is a no-op; service_name is NULL.
            row_full = db.get_deployed_app(slug) or {}
            _stop_app_service(row_full)
            # The app dir is at /opt/ojas-apps/<slug>/. If the app was
            # paused, the dir is at /opt/ojas-apps/.stopped/<slug>/
            # instead -- wipe both.
            for d in (row["app_dir"], str(OJAS_APPS_STOPPED_DIR / slug)):
                if d and Path(d).exists():
                    try:
                        shutil.rmtree(d, ignore_errors=True)
                    except Exception:
                        pass
            try:
                db.delete_deployed_app(slug)
            except Exception:
                pass
    except Exception:
        pass


def _purge_session_everything(session: dict) -> None:
    """One-stop cleanup for everything tied to a session OUTSIDE the main
    SQLite (which CASCADE handles). Idempotent + best-effort -- none of
    these can fail the API call."""
    sid = session["id"]
    _kill_session_processes(sid)
    _purge_deployed_apps_for_session(sid)
    _purge_session_workspace_subdir(session)
    _purge_session_state_dir(sid)
    _purge_langgraph_checkpoint(sid)
    _purge_session_bus(sid)


def _purge_user_everything(user_id: str) -> None:
    """One-stop cleanup for everything owned by a user, OUTSIDE what
    SQLite CASCADE will handle. Walks every project the user owns, then
    every session in each project, and reuses _purge_session_everything
    to SIGTERM the agent's spawned processes + rm workspace subdirs +
    drop langgraph checkpoints + clear the per-session event bus.

    Deployed apps are NOT touched here -- they have ON DELETE SET NULL on
    owner_user_id, so the DB row survives with owner_user_id=NULL. The
    on-disk files at /opt/ojas-apps/<slug>/ also stay; the apps remain
    live at https://<host>/apps/<slug>/ but become 'orphan' (visible
    to root only). If you want to wipe them too, call
    deployed_apps_delete for each before deleting the user."""
    for project in db.list_projects(user_id=user_id):
        for session in db.list_sessions(project["id"]):
            _purge_session_everything(session)


@app.delete("/api/projects/{project_id}")
def projects_delete(project_id: str, user: dict = Depends(require_user)):
    """Delete a project AND every cascade target. The workspace files on
    disk under <workspace>/<each session's subdir>/ ARE removed (since
    those were generated for that project). The root workspace path itself
    is untouched if it was a folder you had before Ojas."""
    _project_or_404(project_id, user)
    sessions = db.list_sessions(project_id)
    for s in sessions:
        task = _active_turns.get(s["id"])
        if task is not None and not task.done():
            task.cancel()
    if not db.delete_project(project_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
    for s in sessions:
        _purge_session_everything(s)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
def sessions_delete(session_id: str, user: dict = Depends(require_user)):
    """Delete one session AND its subdir + processes + agent state +
    checkpoint + bus. Sibling sessions are untouched.

    Order matters: we run the filesystem purge FIRST (while deployed_apps
    rows still exist so we can find their on-disk paths and rmtree
    them), THEN delete the session row (which CASCADEs the deployed_apps
    rows away). If we did it the other way, the cascade would wipe the
    rows before we knew which dirs to clean up, and the .stopped/
    fallback dirs would survive on disk forever."""
    session = _session_or_404(session_id, user)
    task = _active_turns.get(session_id)
    if task is not None and not task.done():
        task.cancel()
    # 1. Filesystem cleanup while rows still exist
    _purge_session_everything(session)
    # 2. Session row → cascades to deployed_apps (rows gone after this)
    if not db.delete_session(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return {"ok": True}


@app.get("/api/sessions/{session_id}", response_model=SessionResponse)
def sessions_get(session_id: str, user: dict = Depends(require_user)):
    """Fetch a single session's current state. Used by the chat page
    after turn_summary to re-read the (potentially LLM-renamed) name --
    the WS session_renamed event isn't 100% reliable on flaky mobile
    networks, so the frontend polls as a safety net."""
    session = _session_or_404(session_id, user)
    return SessionResponse(**session)


@app.patch("/api/sessions/{session_id}", response_model=SessionResponse)
def sessions_rename(
    session_id: str,
    response: Response,
    req: SessionRenameRequest,
    user: dict = Depends(require_user),
):
    """Rename a session. On collision, the new name is auto-suffixed with
    "-2", "-3", etc. (so the user never has to manually disambiguate).

    The response includes two non-standard headers so the UI can show a
    toast telling the user what happened:
      X-Actual-Name:  the final name that ended up in the DB (may differ
                      from `new_name` if auto-suffixed)
      X-Was-Suffixed: "true" if auto-suffix kicked in, "false" otherwise

    The body is the same SessionResponse shape as a normal success -- the
    `name` field already contains the actual final name."""
    session = _session_or_404(session_id, user)
    desired = req.new_name
    try:
        # First try the exact name (cheap path: no collision).
        updated = db.rename_session(session_id, desired)
        response.headers["X-Actual-Name"] = updated["name"]
        response.headers["X-Was-Suffixed"] = "false"
        return SessionResponse(**updated)
    except db.SessionNameConflict:
        # Auto-suffix: pick the next free variant ("X" → "X-2" → "X-3" …).
        final = db.allocate_unique_session_name(
            project_id=session["project_id"],
            desired=desired,
            exclude_id=session_id,
        )
        updated = db.rename_session(session_id, final)
        response.headers["X-Actual-Name"] = updated["name"]
        response.headers["X-Was-Suffixed"] = "true"
        return SessionResponse(**updated)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


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
def sessions_list(project_id: str, user: dict = Depends(require_user)):
    _project_or_404(project_id, user)
    return [SessionResponse(**s) for s in db.list_sessions(project_id)]


@app.post(
    "/api/projects/{project_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def sessions_create(
    project_id: str,
    req: SessionCreateRequest,
    user: dict = Depends(require_user),
):
    """Create a session AND its private workspace subdirectory under the
    project's workspace. The subdirectory is what the agent actually edits;
    deleting the session can then safely rmtree it without touching other
    sessions' files."""
    project = _project_or_404(project_id, user)
    # Each session gets its own subdir: <project_workspace>/<session_slug>/
    # Slug = first 8 chars of the new session's uuid. Short, unique, friendly
    # to ls. Real id is in the DB row.
    s = db.create_session(project_id, req.name, user_id=user["id"])
    subdir_slug = s["id"][:8]
    workspace_root = Path(project["workspace_path"])
    subdir = workspace_root / subdir_slug
    # Self-heal the project workspace directory if it went missing or was
    # never materialised. Previously a silent _bootstrap_default_project
    # failure during signup would leave the project row pointing at a path
    # that didn't exist (or got patched in as root and thus unwritable by
    # the ojas service user) -- sessions_create would then 500 with an
    # opaque "Permission denied: <subdir>" error. Now we validate the
    # parent first, attempt to (re)create it with our own ownership, and
    # surface a much clearer error when even that fails.
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
        if not os.access(workspace_root, os.W_OK):
            raise PermissionError(
                f"project workspace '{workspace_root}' exists but isn't "
                f"writable by the backend (uid={os.geteuid()}). "
                f"Check ownership: `sudo chown -R ojas:ojas "
                f"{workspace_root}`."
            )
        subdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Roll back the session row so we don't leave a dangling record.
        db.delete_session(s["id"])
        logger.error(
            "sessions_create mkdir failed: project_id=%s workspace=%s "
            "subdir=%s err=%s",
            project_id, workspace_root, subdir, e,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not create session workspace: {e}",
        )
    # Update the DB row with the subdir we just created.
    with db._connect() as cx:   # noqa: SLF001 -- small helper, inline write
        cx.execute(
            "UPDATE sessions SET workspace_subdir = ? WHERE id = ?",
            (subdir_slug, s["id"]),
        )
    s["workspace_subdir"] = subdir_slug
    return SessionResponse(**s)


# ============================================================================
# Messages + events
# ============================================================================

@app.get(
    "/api/sessions/{session_id}/messages",
    response_model=list[MessageResponse],
)
def messages_list(session_id: str, user: dict = Depends(require_user)):
    _session_or_404(session_id, user)
    return [MessageResponse(**m) for m in db.list_messages(session_id)]


@app.post(
    "/api/sessions/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
)
async def messages_post(
    session_id: str,
    req: MessagePostRequest,
    user: dict = Depends(require_user),
):
    """Submit a new user prompt. Returns 202 immediately; the agent runs as
    a background task and emits events over the WebSocket."""
    session = _session_or_404(session_id, user)
    project = db.get_project(session["project_id"])
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    # The agent operates inside the session's private subdir if one was
    # assigned at session-create time, so deletes can safely rmtree it
    # without trampling other sessions' files. Falls back to the project
    # workspace for legacy sessions created before the subdir column.
    effective_workspace = project["workspace_path"]
    if session.get("workspace_subdir"):
        effective_workspace = str(
            Path(project["workspace_path"]) / session["workspace_subdir"]
        )

    # Make sure the bus is bound to THIS loop before the worker thread starts
    # publishing -- bind_loop is idempotent.
    bus = get_bus(session_id)
    if not bus.is_bound():
        bus.bind_loop(asyncio.get_running_loop())

    # Fire-and-forget. Errors land on the bus via reporter.error().
    task = asyncio.create_task(run_turn(
        session_id=session_id,
        project_id=project["id"],
        workspace=effective_workspace,
        user_prompt=req.content,
    ))
    _active_turns[session_id] = task
    task.add_done_callback(lambda _t, sid=session_id: _active_turns.pop(sid, None))
    return {"accepted": True}


# Per-session in-flight turn registry -- used by the cancel endpoint to abort a
# running turn. Cleared when the task finishes naturally.
_active_turns: dict[str, asyncio.Task] = {}


@app.post("/api/sessions/{session_id}/cancel")
async def cancel_turn(
    session_id: str,
    user: dict = Depends(require_user),
):
    _session_or_404(session_id, user)
    """Cancel the in-flight turn for this session, if any. Idempotent -- if no
    turn is running, returns ok=false rather than failing.

    The end-of-turn events (error + assistant_text(done=True) + turn_summary
    + persisted system message) are emitted by session_runner's
    CancelledError handler, so the turn closes consistently regardless of
    whether it ended by success, exception, or cancel. Publishing them here
    too would double up on the wire and risk drift between the two paths.

    The in-flight worker thread is hard to interrupt (Python limitation), so
    late events from the LangGraph worker may still drip onto the bus for a
    few seconds -- the UI ignores them once turn_summary has arrived."""
    task = _active_turns.get(session_id)
    if task is None or task.done():
        return {"ok": False, "reason": "no active turn"}
    task.cancel()
    return {"ok": True}


@app.post("/api/sessions/{session_id}/compact")
async def compact_session(
    session_id: str,
    user: dict = Depends(require_user),
):
    _session_or_404(session_id, user)
    """Manually compact this session's LangGraph message history NOW, instead
    of waiting for the automatic 100K-token trigger. Useful when responses
    are getting slow and the user wants a fresh context budget without
    starting a new session.

    Replaces the entire `messages` list in the checkpoint with the compacted
    version: a SystemMessage summary + the last few messages verbatim. The
    `add_messages` reducer accepts `RemoveMessage(id=...)` for deletion, so
    we send removes for every existing message followed by the compacted
    list -- no orphans, no duplication.
    """
    if _active_turns.get(session_id) and not _active_turns[session_id].done():
        return {"ok": False, "reason": "a turn is in flight -- cancel it first"}

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
    user: dict = Depends(require_user),
):
    _session_or_404(session_id, user)
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
def sessions_git(session_id: str, user: dict = Depends(require_user)):
    _session_or_404(session_id, user)
    workspace = _resolve_session_workspace(session_id)
    info = get_git_info(workspace)
    return GitInfoResponse(**info.__dict__)


@app.post(
    "/api/sessions/{session_id}/push",
    response_model=PushResponse,
)
async def sessions_push(session_id: str, user: dict = Depends(require_user)):
    _session_or_404(session_id, user)
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
# WebSocket -- live activity stream
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
    token = envelope.get("token") or ""
    if envelope.get("type") != "auth":
        await websocket.close(code=4401, reason="auth failed")
        return
    user = auth.user_from_token(token)
    if user is None:
        await websocket.close(code=4401, reason="auth failed")
        return

    session = db.get_session(session_id)
    if session is None:
        await websocket.close(code=4404, reason="session not found")
        return
    # Ownership check -- non-root users can only stream their own sessions.
    if user["role"] != "root" and session.get("user_id") not in (None, user["id"]):
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
# Preview -- serve the session's built PWA so it can be installed on any device.
# ============================================================================

def _session_preview_dir(
    session_id: str,
    project_dir: str | None = None,
) -> Path | None:
    """Resolve the session's `dist/` folder, or None if the session/project
    can't be found (or the project_dir is unsafe). Used by the build watcher
    that emits preview_ready events AND by the deploy endpoint to find the
    files to copy.

    `project_dir` lets a single session host multiple apps -- the session
    workspace might contain `calorie-tracker/dist/`, `weather/dist/`, etc.,
    and each can be deployed as its own slug. Empty/None → session root.
    """
    session = db.get_session(session_id)
    if session is None:
        return None
    project = db.get_project(session["project_id"])
    if project is None:
        return None
    base = Path(project["workspace_path"])
    if session.get("workspace_subdir"):
        base = base / session["workspace_subdir"]
    if project_dir:
        # Sanitize: no traversal, no absolute paths, no NULs. Empty segments
        # are tolerated (e.g. "calorie-tracker/") but we collapse them so
        # the resolver stays predictable.
        parts = [p for p in project_dir.replace("\x00", "").split("/") if p]
        if any(p == ".." for p in parts):
            return None
        for p in parts:
            base = base / p
    # Resolve dist/ — the layout depends on whether the project has a
    # backend/ folder:
    #   - fullstack (backend/ + frontend/)   → <project>/frontend/dist
    #   - static-only (no backend/)         → <project>/dist OR
    #                                          <project>/frontend/dist
    # For static-only, prefer <project>/dist (the conventional Vite
    # static layout). Fall back to <project>/frontend/dist if that's
    # the only build the agent produced.
    static_idx = base / "dist" / "index.html"
    fullstack_idx = base / "frontend" / "dist" / "index.html"
    if static_idx.exists():
        return base / "dist"
    if fullstack_idx.exists():
        return base / "frontend" / "dist"
    return None  # signal: no build found at all (caller raises 400)


# Directories the agent scaffold script creates that the deploy detector
# must NOT consider as a "sub-app" -- they're build artefacts, not user apps.
_DEPLOY_IGNORE_DIRS = frozenset({
    "node_modules", ".git", ".claude", ".cache", "dist", "build",
    ".next", ".nuxt", ".svelte-kit", ".vite", "coverage", ".turbo",
    ".parcel-cache", ".gradle", "target", "__pycache__", ".venv", "venv",
})


def _session_workspace_root(session_id: str) -> Path | None:
    """Return the session's per-session workspace dir (parent of any
    sub-apps the agent may have scaffolded). The session root contains
    `dist/` only if the agent built AT the root; the typical case is
    that the agent built inside a sub-app folder like `my-app/dist/`."""
    session = db.get_session(session_id)
    if session is None:
        return None
    project = db.get_project(session["project_id"])
    if project is None:
        return None
    base = Path(project["workspace_path"])
    if session.get("workspace_subdir"):
        base = base / session["workspace_subdir"]
    return base


def _detect_dist_candidates(session_id: str) -> list[dict]:
    """Scan the session workspace for built dist/ folders. Returns a
    list of candidates with project_dir, absolute path, mtime, and size,
    sorted by mtime descending (most recent build first). The session
    root's own dist/ is represented as project_dir="". Used by the
    deploy dialog to pre-fill (and lock) the Sub-app folder field, and
    by sessions_deploy as a fallback when the client doesn't specify
    a project_dir.

    Skips noise dirs (node_modules, .git, etc.) so a Vite-installed
    `node_modules/some-pkg/dist/` doesn't pollute the list. The session
    root's own dist/ is always considered (it's not a "subdir")."""
    base = _session_workspace_root(session_id)
    if base is None or not base.exists():
        return []

    cands: list[dict] = []

    # 1) Session root dist/ (project_dir="") -- only if it's a *real*
    #    user build. Two layouts count as a "real" root build:
    #      (a) static Vite at root:  <base>/package.json + <base>/dist/index.html
    #      (b) fullstack at root:    <base>/frontend/dist/index.html
    #                                 (and ideally <base>/backend/ alongside,
    #                                 but we accept the dist alone so the user
    #                                 sees the candidate even if backend/ is
    #                                 mid-scaffold). The deploy-time
    #                                 fullstack check (presence of
    #                                 backend/requirements.txt or
    #                                 backend/main.py) decides the runtime
    #                                 path; the candidate is the dist that
    #                                 needs promoting.
    root_dist = base / "dist" / "index.html"
    root_fullstack_dist = base / "frontend" / "dist" / "index.html"
    if root_dist.exists() and (base / "package.json").exists():
        try:
            st = (base / "dist").stat()
            cands.append({
                "project_dir": "",
                "abs_path": str(base / "dist"),
                "mtime": int(st.st_mtime),
                "index_size": root_dist.stat().st_size,
            })
        except OSError:
            pass
    elif root_fullstack_dist.exists():
        try:
            st = (base / "frontend" / "dist").stat()
            cands.append({
                "project_dir": "",
                "abs_path": str(base / "frontend" / "dist"),
                "mtime": int(st.st_mtime),
                "index_size": root_fullstack_dist.stat().st_size,
            })
        except OSError:
            pass

    # 2) Sub-app folders (any direct child that's a directory, not in
    #    the noise set, and contains dist/index.html OR a fullstack
    #    layout with frontend/dist/index.html).
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        children = []
    for child in children:
        if not child.is_dir():
            continue
        if child.name in _DEPLOY_IGNORE_DIRS or child.name.startswith("."):
            continue
        # Layout priority: a static <child>/dist/ build wins over the
        # fullstack <child>/frontend/dist/ because the Vite static
        # layout is the more common one. The deploy code still
        # detects fullstack at runtime by the presence of backend/
        # inside the same project, so the candidate path doesn't
        # dictate stack.
        static_idx = child / "dist" / "index.html"
        fullstack_idx = child / "frontend" / "dist" / "index.html"
        if static_idx.exists():
            dist_idx = static_idx
            dist_dir = child / "dist"
        elif fullstack_idx.exists():
            dist_idx = fullstack_idx
            dist_dir = child / "frontend" / "dist"
        else:
            continue
        try:
            st = dist_dir.stat()
            cands.append({
                "project_dir": child.name,
                "abs_path": str(dist_dir),
                "mtime": int(st.st_mtime),
                "index_size": dist_idx.stat().st_size,
            })
        except OSError:
            continue

    cands.sort(key=lambda c: c["mtime"], reverse=True)

    # Dedupe by abs_path. A fullstack app has frontend/dist/ at the session
    # root AND inside the `frontend/` subdir — they point to the SAME dist
    # (no, wait — at root it's <base>/frontend/dist/, and as a "subdir" we
    # treat `frontend/` as the sub-app, so it's <base>/frontend/frontend/dist/
    # which doesn't exist). The duplication we just saw is because the
    # fullstack-at-root check matched AND the subdir-iteration also matched
    # the `frontend/` dir. Both produce abs_path <base>/frontend/dist. Keep
    # the root-level match (project_dir="") and drop the subdir-iteration
    # duplicate, since "session root with fullstack layout" is the cleaner
    # user mental model.
    seen: set[str] = set()
    deduped: list[dict] = []
    for c in cands:
        if c["abs_path"] in seen:
            continue
        seen.add(c["abs_path"])
        deduped.append(c)
    return deduped


def _auto_pick_project_dir(session_id: str) -> str | None:
    """Pick the most likely project_dir for a session: the only candidate
    if there's exactly one, else None. Returns "" (empty string) for the
    session root dist/. None means 'ambiguous -- caller must decide'."""
    cands = _detect_dist_candidates(session_id)
    if len(cands) == 1:
        return cands[0]["project_dir"]
    return None


# /preview/{session_id}/* was removed: it generated temporary URLs tied
# to a session ID that the user (rightly) found unreliable -- they were
# half-broken without a service worker scope, and disappeared on session
# delete. The Deploy button → `<slug>.<apps-root>/` flow replaces it with
# a stable, installable URL the user controls. The dist-dir resolver
# (`_session_preview_dir`) is kept because the deploy endpoint still uses
# it to find the built files to copy.


# ============================================================================
# Deployed apps -- promote a sessions built dist/ to a persistent URL.
#
# Flow:
#   1. Agent builds → <session_workspace>/dist/index.html exists
#   2. POST /api/sessions/<sid>/deploy {slug?}
#   3. Server picks slug (slugify name; -2, -3 on collision), copies
#      dist/ → /opt/ojas-apps/<slug>/, inserts deployed_apps row
#   4. App is live at https://<host>/apps/<slug>/ -- survives session delete
#      + backend restart (it's just files on disk + a DB row, no process)
# ============================================================================

OJAS_APPS_ROOT = Path("/opt/ojas-apps")

# Public domain Ojas is served at. Read from the env (OJAS_DOMAIN) if set,
# else parsed from the Caddyfile, else fall back to the request's Host
# header at runtime. Used to build clickable URLs in the admin panel +
# /api/deployed-apps responses.
OJAS_DOMAIN: str | None = None
OJAS_DOMAIN_OVERRIDE: str | None = os.getenv("OJAS_DOMAIN", "").strip() or None

# Root domain for deployed-app subdomains. Each deployed app gets its own
# subdomain at https://<slug>.<OJAS_APPS_ROOT_DOMAIN>/ (e.g.
# https://weather.ojas.karmacode.cloud/). Caddy uses the on-demand TLS
# ask endpoint to validate and provision certs for these subdomains.
#
# IMPORTANT: this is NOT auto-derived from OJAS_DOMAIN. A user might run
# the Ojas apex at `ojas.karmacode.cloud` (the standard) but want apps at
# `ojas.karmacode.cloud` (apps live alongside the apex) OR at
# `karmacode.cloud` (apps live on the bare domain). The right answer
# depends on the user's DNS + Caddyfile, so we make it explicit.
#
# Resolution order:
#   1. OJAS_APPS_ROOT_DOMAIN env var (set by install.sh)
#   2. Auto-derive: if OJAS_DOMAIN is set, assume apps live at the
#      same domain. (Override if your setup differs.)
#   3. None (caller falls back to <slug>.<OJAS_DOMAIN> or request host)
OJAS_APPS_ROOT_DOMAIN: str | None = None
OJAS_APPS_ROOT_DOMAIN_OVERRIDE: str | None = (
    os.getenv("OJAS_APPS_ROOT_DOMAIN", "").strip() or None
)


def _resolve_public_domain() -> str | None:
    """Find the public hostname the user reaches Ojas at. Order:
    1. OJAS_DOMAIN env var (explicit, set by install.sh)
    2. First vhost line in the active Caddyfile (e.g. 'ojas.example.com {')
    3. None (caller falls back to request host / localhost)
    """
    if OJAS_DOMAIN_OVERRIDE:
        return OJAS_DOMAIN_OVERRIDE
    for caddy_path in ("/etc/caddy/Caddyfile", "/opt/ojas/deploy/Caddyfile"):
        try:
            with open(caddy_path) as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    # Vhost lines end with '{' (or are a bare host).
                    if s.endswith("{") or (":" in s and " " not in s and "/" not in s):
                        host = s.rstrip("{").strip()
                        # Skip directives like 'log' or 'import'
                        if host and "." in host and not host.startswith("/"):
                            return host
        except OSError:
            continue
    return None


def _resolve_apps_root_domain() -> str | None:
    """Find the root domain under which deployed apps are served at
    https://<slug>.<root>/. Order:
    1. OJAS_APPS_ROOT_DOMAIN env var (explicit override)
    2. Fall back to OJAS_DOMAIN -- if apps live alongside the apex
       (the most common setup), they share the same root.
    3. None (caller will use a sensible default like <slug>.<OJAS_DOMAIN>).
    """
    if OJAS_APPS_ROOT_DOMAIN_OVERRIDE:
        return OJAS_APPS_ROOT_DOMAIN_OVERRIDE
    apex = _resolve_public_domain()
    if apex:
        return apex
    return None


def _deployed_app_or_404(slug: str, user: dict) -> dict:
    """Lookup + ownership check. Returns the row dict, or raises 404 if the
    app doesn't exist OR the caller isn't allowed to see it. 404 (not 403)
    so we don't leak the existence of other users' deploys."""
    app = db.get_deployed_app(slug)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
    if user["role"] != "root" and app.get("owner_user_id") not in (None, user["id"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "app not found")
    return app


# ---- Systemd service helpers (v1.1 fullstack; stubs for v1) -------------
#
# These are placeholders that succeed for static apps and would shell out
# to `systemctl` once the per-app backend process lands in v1.1. Keeping
# the call sites in place now means toggling starts working as soon as the
# toggle endpoints + Caddy regen land; the systemd paths light up
# automatically when deploys start writing `service_name` and `port`.

# ---- Systemd service helpers (fullstack apps) -----------------------------
#
# Each fullstack app runs as its own systemd unit. We:
#   - Allocate a free TCP port from OJAS_APPS_PORT_RANGE (9100-9899).
#   - Generate a unit file at /etc/systemd/system/ojas-app-<slug>.service
#     with hard resource limits (MemoryMax, CPUQuota), filesystem
#     isolation (ProtectSystem=strict), and bind to 127.0.0.1 (Caddy
#     proxies to it).
#   - Start/stop the unit on toggle. /api/* is proxied via Caddy.

OJAS_APPS_PORT_RANGE_START = 9100
OJAS_APPS_PORT_RANGE_END = 9899
OJAS_APPS_PORT_RANGE_SIZE = OJAS_APPS_PORT_RANGE_END - OJAS_APPS_PORT_RANGE_START + 1
OJAS_APPS_UNIT_DIR = Path("/etc/systemd/system")
OJAS_APPS_VENV_BIN = "bin"  # relative to backend dir

# The Ojas backend runs as `ojas` (uid 997 in the system unit). All
# per-app systemd units also run as `ojas` so they share the same file
# permissions and can write to /opt/ojas-apps/<slug>/data/.
OJAS_APP_USER = "ojas"


def _listening_ports_in_range() -> set[int]:
    """Return the set of TCP ports currently in LISTEN state within
    OJAS_APPS_PORT_RANGE. Used by _allocate_app_port() to find a free
    slot. Reads /proc/net/tcp (world-readable) -- no privileges needed.
    Excludes ports the Ojas backend itself allocated (recorded in DB)
    so a restart of Ojas doesn't accidentally re-grab a port another
    unit is still listening on (after a crash, the LISTEN state
    outlives the DB record briefly)."""
    import struct
    import socket
    in_use: set[int] = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            data = open(f, "rb").read()
        except OSError:
            continue
        # Each line: sl local_address rem_address st ...
        # local_address is "<hex_ip>:<hex_port>". st=0A means LISTEN.
        for line in data.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            if parts[3] != "0A":  # not LISTEN
                continue
            local = parts[1].decode("ascii", "replace")
            if ":" not in local:
                continue
            try:
                port = int(local.split(":", 1)[1], 16)
            except ValueError:
                continue
            if OJAS_APPS_PORT_RANGE_START <= port <= OJAS_APPS_PORT_RANGE_END:
                in_use.add(port)
    return in_use


def _allocate_app_port() -> int:
    """Return a free TCP port in OJAS_APPS_PORT_RANGE that is NOT
    currently in the deployed_apps DB AND NOT in LISTEN state. Raises
    RuntimeError if the range is exhausted (caller should surface a
    clear error to the user -- "all 800 ports in use, pause an app
    first")."""
    used_by_db: set[int] = set()
    with db._connect() as cx:   # noqa: SLF001
        for r in cx.execute("SELECT port FROM deployed_apps WHERE port IS NOT NULL"):
            if r["port"] is not None:
                used_by_db.add(int(r["port"]))
    used_by_listen = _listening_ports_in_range()
    used = used_by_db | used_by_listen
    for port in range(OJAS_APPS_PORT_RANGE_START, OJAS_APPS_PORT_RANGE_END + 1):
        if port not in used:
            return port
    raise RuntimeError(
        f"no free ports in {OJAS_APPS_PORT_RANGE_START}-{OJAS_APPS_PORT_RANGE_END} "
        f"({len(used_by_db)} apps deployed, {len(used_by_listen)} listening) -- "
        f"pause an existing app from /settings and try again"
    )


def _write_systemd_unit(slug: str, port: int, backend_dir: Path) -> Path:
    """Generate /etc/systemd/system/ojas-app-<slug>.service with hard
    resource limits + filesystem isolation. Idempotent: overwrites
    existing unit. Returns the unit file path."""
    unit = OJAS_APPS_UNIT_DIR / f"ojas-app-{slug}.service"
    venv_python = backend_dir / ".venv" / "bin" / "python"
    venv_uvicorn = backend_dir / ".venv" / "bin" / "uvicorn"
    data_dir = backend_dir.parent / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # DATABASE_URL points at the per-app SQLite file. Use a fully
    # qualified path so it works regardless of WorkingDirectory.
    db_path = data_dir / "app.db"
    # The StandardOutput/Error go to the journal so the logs button
    # in the chat strip + Settings page can tail them via
    # `journalctl -u ojas-app-<slug>`. We DO NOT add `--workers N`
    # to uvicorn: one process per app keeps the memory math honest
    # (we promised "toggle off = memory goes to ~0"). v1.2 can add
    # workers behind a per-app CPU quota if needed.
    body = f"""# Auto-generated by Ojas for slug={slug}. Do not edit by hand.
[Unit]
Description=Ojas fullstack app: {slug}
After=network.target

[Service]
Type=simple
User={OJAS_APP_USER}
Group={OJAS_APP_USER}
WorkingDirectory={backend_dir}
Environment="DATABASE_URL=sqlite:////{db_path}"
Environment="PORT={port}"
ExecStart={venv_uvicorn} main:app --host 127.0.0.1 --port {port} --log-level info
Restart=on-failure
RestartSec=2

# Hard resource limits. MemoryMax caps RAM; CPUQuota caps CPU at
# 200% of one core (multi-threaded uvicorn can use 2 cores; the
# rest of the system is unaffected).
MemoryMax=512M
CPUQuota=200%

# Filesystem isolation. ProtectSystem=strict makes /usr, /boot, etc.
# read-only. ProtectHome=true hides /home/* (other users' files).
# PrivateTmp=true gives the app its own /tmp. ReadWritePaths whitelists
# the app's own dir for the SQLite file and any user uploads.
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={backend_dir} {data_dir} /opt/ojas-apps/{slug}

# No new privileges, no core dumps, predictable PID file.
NoNewPrivileges=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=false

[Install]
WantedBy=multi-user.target
"""
    # The Ojas backend runs as the `ojas` user inside a systemd unit
    # with `NoNewPrivileges=true`, which blocks `sudo` from elevating.
    # We use a setuid root helper at /usr/local/sbin/ojas-systemd-helper
    # which validates unit names against the ojas-app-*.service
    # pattern before doing anything. Built from deploy/ojas-systemd-helper.c.
    import tempfile
    unit.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".service", delete=False,
        prefix=f"ojas-app-{slug}-",
    ) as tmp:
        tmp.write(body)
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            ["/usr/local/sbin/ojas-systemd-helper", "write-unit", f"ojas-app-{slug}.service", str(tmp_path)],
            check=False, timeout=5, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"helper write-unit failed: {result.stderr}")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return unit


def _start_app_service(app: dict) -> bool:
    """Start the systemd unit for a fullstack app. Returns True on
    success. No-op for static apps (no service_name in the row).
    For fullstack: starts the unit via `sudo systemctl` (the Ojas
    user can't directly control systemd without a NOPASSWD rule --
    see /etc/sudoers.d/ojas-systemd), then polls /health for up to
    5 seconds.

    Uses `restart` (not `start`) so a re-deploy picks up the rewritten
    unit file (e.g. new PORT, new DATABASE_URL). `start` on an
    already-active unit is a no-op and the running process keeps
    using the OLD EnvironmentFile values from when it first started."""
    svc = app.get("service_name")
    if not svc:
        return True
    try:
        subprocess.run(
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "daemon-reload"],
            check=False, timeout=5, capture_output=True,
        )
        subprocess.run(
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "enable", svc],
            check=False, timeout=10, capture_output=True,
        )
        subprocess.run(
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "restart", svc],
            check=True, timeout=10, capture_output=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"[ojas] failed to start {svc}: {e}")
        return False
    # Health-check poll: up to 5s for the backend to bind + accept requests.
    import urllib.request
    port = app.get("port")
    if not port:
        return True
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return True


def _stop_app_service(app: dict) -> None:
    """Stop the systemd unit for a fullstack app. Best-effort. No-op
    for static apps. Uses `sudo systemctl stop` (ojas user doesn't
    have direct systemd access)."""
    svc = app.get("service_name")
    if not svc:
        return
    try:
        subprocess.run(
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "stop", svc],
            check=False, timeout=5, capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[ojas] helper stop {svc} failed: {e}")


def _rmtree_with_symlinks(path: Path) -> None:
    """Recursive delete that unlinks symlinks BEFORE recursing into
    them. `shutil.rmtree(..., ignore_errors=True)` is the usual tool,
    but it has a known wart with symlinked directories: when called
    on a dir that contains symlinks-to-dirs, it follows the symlinks
    and DELETES THE TARGET, leaving a dangling symlink that subsequent
    copytree calls trip over with "File exists".

    For a Python venv, many packages (passlib, bcrypt, cryptography,
    numpy, etc.) install as symlinks to a shared site-packages elsewhere
    on the system. Deleting the target would break other venvs. This
    helper walks the tree and unlinks each symlink as a link first
    (not its target), then recurses into real directories.

    Best-effort: errors are swallowed (matches the previous rmtree
    ignore_errors=True behaviour). Caller should treat the post-state
    as "no path" — if a file survives, the next copytree will surface
    a clean error.
    """
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        try:
            path.unlink()
        except OSError:
            pass
        return
    if path.is_dir():
        try:
            for child in path.iterdir():
                _rmtree_with_symlinks(child)
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        # File or non-empty dir we couldn't fully recurse into —
        # fall back to unlink (for files / leftover symlinks) or
        # ignore (for non-empty dirs we'd rather warn about).
        try:
            if not path.is_dir():
                path.unlink()
        except OSError:
            pass


def _pip_install_for_app(backend_dir: Path, requirements: Path) -> tuple[bool, str]:
    """Create a venv at backend_dir/.venv and pip install -r requirements.
    Returns (success, error_message). Runs as the `ojas` user so the
    resulting files are writable by the systemd unit."""
    import venv
    venv_dir = backend_dir / ".venv"
    try:
        # Build the venv as the current user (ojas). Use --system-site-packages
        # off so we don't leak host packages; a clean venv is cheap.
        builder = venv.EnvBuilder(
            system_site_packages=False,
            clear=True,
            symlinks=False,
            with_pip=True,
        )
        builder.create(str(venv_dir))
    except Exception as e:
        return False, f"venv creation failed: {e}"
    venv_pip = venv_dir / "bin" / "pip"
    try:
        result = subprocess.run(
            [str(venv_pip), "install", "--quiet", "-r", str(requirements)],
            check=False, timeout=300, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"pip install failed: {result.stderr[-500:]}"
    except subprocess.TimeoutExpired:
        return False, "pip install timed out (>5min)"
    except OSError as e:
        return False, f"pip install OS error: {e}"
    return True, ""


# ---- Caddy route regeneration (shared by toggle + boot + delete) --------
#
# We use a SIMPLER scheme than per-slug Caddy fragments: the wildcard
# Caddy block stays static, and toggling is just a directory rename.
# `*.ojas.karmacode.cloud` already serves from `/opt/ojas-apps/<slug>/`;
# when the user pauses an app, we rename the dir to
# `/opt/ojas-apps/.stopped/<slug>/`. The Caddy `try_files` chain ends
# with a shared `/.paused/index.html` fallback, so any request for a
# paused slug lands on a friendly "this app is paused" page instead of
# a 404. No Caddy reload required -- `file_server` re-stats per request.
#
# Layout while toggling:
#   /opt/ojas-apps/
#     <slug>/              ← live; served by the existing wildcard block
#     .stopped/<slug>/     ← paused; falls through to /.paused/index.html
#     .paused/index.html   ← shared paused page (created on first toggle)

OJAS_APPS_STOPPED_DIR = Path("/opt/ojas-apps/.stopped")
OJAS_APPS_PAUSED_DIR = Path("/opt/ojas-apps/.paused")
OJAS_CADDY_ROUTES_DIR = Path("/etc/caddy/routes.d")


def _apply_app_state_to_disk(slug: str, new_state: str) -> None:
    """Move the app dir between `/opt/ojas-apps/<slug>/` (live) and
    `/opt/ojas-apps/.stopped/<slug>/` (paused). Creates the paused
    HTML on first run. Idempotent -- calling with the already-applied
    state is a no-op."""
    OJAS_APPS_PAUSED_DIR.mkdir(parents=True, exist_ok=True)
    OJAS_APPS_STOPPED_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_paused_page()
    live = OJAS_APPS_ROOT / slug
    stopped = OJAS_APPS_STOPPED_DIR / slug
    # If the row exists but the dir was deleted out from under us,
    # there's nothing to move -- the next deploy will recreate it.
    if new_state == "stopped":
        if live.exists():
            live.rename(stopped)
    else:  # running | starting | error
        if stopped.exists():
            stopped.rename(live)


def _regenerate_caddy_routes_for_user(owner_user_id: str | None) -> None:
    """Re-emit per-slug Caddy fragments for FULLSTACK apps. Static apps
    don't need a fragment (the wildcard block handles them). Each
    fragment declares a more-specific site block that Caddy matches
    BEFORE the `*.ojas.karmacode.cloud` wildcard, declaring the
    `/api/*` reverse_proxy to the per-app backend port.

    This function is idempotent: it writes every fragment fresh, so
    a port change or service_name change propagates without manual
    cleanup. The OS-level install is `/etc/caddy/routes.d/<slug>.caddy`,
    included from the main Caddyfile via `import`."""
    OJAS_CADDY_ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    apps_root = _resolve_apps_root_domain() or _resolve_public_domain() or "ojas.example.com"
    with db._connect() as cx:   # noqa: SLF001
        if owner_user_id is None:
            rows = cx.execute(
                "SELECT slug, port, service_name, state "
                "FROM deployed_apps "
                "WHERE port IS NOT NULL AND service_name IS NOT NULL"
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT slug, port, service_name, state "
                "FROM deployed_apps "
                "WHERE port IS NOT NULL AND service_name IS NOT NULL "
                "AND (owner_user_id = ? OR owner_user_id IS NULL)",
                (owner_user_id,),
            ).fetchall()
    live_slugs = {r["slug"] for r in rows}
    # Wipe stale fragments (deleted apps, no longer fullstack)
    for f in OJAS_CADDY_ROUTES_DIR.glob("*.caddy"):
        if f.stem not in live_slugs:
            try:
                f.unlink()
            except OSError:
                pass
    for r in rows:
        r = dict(r)
        target = OJAS_CADDY_ROUTES_DIR / f"{r['slug']}.caddy"
        if r.get("state") == "stopped":
            # Paused: serve the shared paused page (the wildcard block
            # already does this for /<slug>/, so we don't need a fragment
            # -- the wildcard will handle it via the /.paused/ fallback).
            try:
                target.unlink()
            except OSError:
                pass
            continue
        # Live + fullstack: per-slug site block with /api/* reverse proxy
        port = r["port"]
        # NOTE: in Caddyfile syntax, the opening `{` of a site block
        # MUST be on the SAME LINE as the site address. Putting it on
        # the next line (as is common in Caddy JSON configs) is a
        # syntax error in the Caddyfile parser. Hence the awkward
        # formatting below.
        target.write_text(f"""# Auto-generated for {r['slug']} (fullstack, state=running).
# Do not edit by hand. Regenerated by server/app.py:_regenerate_caddy_routes_for_user.
#
# The per-site `tls {{ on_demand }}` block is REQUIRED for new subdomains.
# The wildcard block in the main Caddyfile has `on_demand` too, but it
# only applies to the *route* that matches the wildcard. Per-slug site
# blocks (imported via `import /etc/caddy/routes.d/*.caddy` at server
# level) are separate site definitions; they need their own `tls
# {{ on_demand }}` so the on-demand ACME flow runs for the specific
# hostname. Note the Caddyfile parser requires directives INSIDE a
# block to be on their own line, so this is multi-line, not the more
# common one-liner.
{r['slug']}.{apps_root} {{
    tls {{
        on_demand
    }}
    encode gzip
    root * /opt/ojas-apps/{r['slug']}/static

    # Caddy handler ordering gotcha: top-level directives after a
    # `handle` block ALL run for every request, regardless of whether
    # the `handle` already handled it. So `file_server` after
    # `handle @api` was running for /api/* requests, finding no
    # file at /api/items, falling back to /index.html, and returning
    # the SPA HTML to API callers (the reverse_proxy never ran).
    #
    # Fix: put BOTH the API and the static handlers inside explicit
    # `handle` blocks. Caddy's `handle` directive is terminal -- once
    # a `handle` block's matcher accepts the request, no other
    # directives (or other `handle` blocks) at the same level run.
    @api path /api/*
    handle @api {{
        reverse_proxy 127.0.0.1:{port}
    }}
    handle {{
        # SPA fallback: requested file → /index.html. The
        # /opt/ojas-apps/.paused/ shared page is served by the
        # wildcard block (which has it in its try_files chain) only
        # for hosts that DON'T have a per-slug fragment. Since this
        # per-slug fragment is only present for running apps (the
        # regen deletes it on state=stopped), a paused app's host
        # naturally falls through to the wildcard + /.paused/index.html
        # path. No paused fallback needed here.
        try_files {{path}} /index.html
        file_server
    }}
    header {{
        X-Content-Type-Options "nosniff"
    }}
    @hashed path /assets/*
    header @hashed Cache-Control "public, max-age=31536000, immutable"
    @sw path /sw.js
    header @sw Cache-Control "no-store"
    @html path_regexp ^\\/$|\\.html$
    header @html Cache-Control "no-cache"
}}
""")
    # Reload Caddy
    try:
        subprocess.run(
            ["systemctl", "reload", "caddy"],
            check=False, timeout=5, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _drop_caddy_fragment(slug: str) -> None:
    """Remove a single per-slug Caddy fragment. Used on app-delete."""
    f = OJAS_CADDY_ROUTES_DIR / f"{slug}.caddy"
    try:
        if f.exists():
            f.unlink()
    except OSError:
        pass
    try:
        subprocess.run(
            ["systemctl", "reload", "caddy"],
            check=False, timeout=5, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _ensure_paused_page() -> None:
    """Write the shared `this app is paused` HTML if it is not there. One file, ~700 bytes. Re-runs are no-ops thanks to the content equality check."""
    target = OJAS_APPS_PAUSED_DIR / "index.html"
    body = "<!doctype html><title>App paused</title><body><h1>This app is paused</h1><p>Open the Ojas settings page and toggle it back on.</p></body>"
    try:
        existing = target.read_text() if target.exists() else None
        if existing != body:
            target.write_text(body)
    except OSError:
        pass


@app.post(
    "/api/sessions/{session_id}/deploy",
    response_model=DeployResponse,
    status_code=status.HTTP_201_CREATED,
)
def sessions_deploy(
    session_id: str,
    req: DeployRequest,
    request: Request,
    user: dict = Depends(require_user),
):
    """Copy the sessions built dist/ to a persistent /opt/ojas-apps/<slug>/
    location and register it as a deployed app. The deployed app is
    DECOUPLED from the session -- deleting the session leaves the live URL
    intact. Re-deploying the same session to the SAME slug is allowed
    (atomic in-place swap); re-deploying without a slug picks a fresh one."""
    import shutil
    session = _session_or_404(session_id, user)
    # Normalize the optional sub-app folder. Empty/None deploys the
    # session's own dist/; a value like "todo-app" deploys <subdir>/todo-app/dist/.
    # The resolver blocks ".." segments and absolute paths.
    project_dir = (req.project_dir or "").strip() or None
    # 1. Need a built dist/. If the client didn't tell us which sub-app
    #    to deploy, auto-detect: pick the only candidate if there's just
    #    one, otherwise return a 400 with a list so the user can choose
    #    (this is the common path -- the dialog sends the pre-filled
    #    value from /detected-dist, so this fallback mostly hits the
    #    API-only case).
    if project_dir is None:
        cands = _detect_dist_candidates(session_id)
        if len(cands) == 1:
            project_dir = cands[0]["project_dir"]
        elif len(cands) == 0:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "no built dist/ yet -- ask the agent to run `npm run build` first",
            )
        else:
            names = [c["project_dir"] or "<session root>" for c in cands]
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"multiple sub-apps found in this session: {', '.join(names)}. "
                "Specify which one to deploy (advanced).",
            )
    dist = _session_preview_dir(session_id, project_dir=project_dir)
    if dist is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "invalid sub-app folder -- paths with '..' or absolute paths are not allowed",
        )
    if not dist.exists():
        hint = f" (looked in {dist})" if project_dir else ""
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"no built dist/ yet -- ask the agent to run `npm run build` first{hint}",
        )

    # 2. Pick a slug. If user supplied one, slugify it; if it collides AND
    # the caller doesn't own the colliding app, append -2/-3. If user
    # supplied no slug, derive from session name.
    desired = req.slug if req.slug else session["name"]
    existing = db.get_deployed_app(db._slugify(desired)) if req.slug else None  # noqa: SLF001
    if existing and existing.get("owner_user_id") == user["id"]:
        # Same owner re-deploying to the same slug → atomic swap
        slug = existing["slug"]
        in_place = True
    else:
        # allocate_deployed_slug raises DeployedSlugTaken on collision —
        # the user must pick a different slug. We do NOT auto-suffix
        # with -2/-3 (the user wants to see the conflict and choose).
        try:
            slug = db.allocate_deployed_slug(desired)
        except db.DeployedSlugTaken as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"slug '{e.slug}' is already taken — pick a different one",
            )
        in_place = False

    # 3. Copy files. Use a sibling temp dir + rename for atomicity (so the
    # public URL never serves a half-copied build). If the app was previously
    # paused, the dir lives at /opt/ojas-apps/.stopped/<slug>/; we move it
    # back to the live path before staging so the swap is a no-op-rename
    # rather than a slow cross-tree copy.
    #
    # For FULLSTACK apps, the dist goes into <slug>/static/ (not <slug>/
    # directly) so the backend/ subdir can sit alongside without
    # colliding. We detect fullstack up-front and choose the target
    # subdir accordingly.
    OJAS_APPS_ROOT.mkdir(parents=True, exist_ok=True)
    target = OJAS_APPS_ROOT / slug
    stopped_target = OJAS_APPS_STOPPED_DIR / slug
    _apply_app_state_to_disk(slug, "running")  # ensure live path is writable

    # Resolve the absolute project path so the fullstack check works
    # regardless of where the request's `project_dir` came from.
    project_abs = _session_workspace_root(session_id)
    if project_abs and project_dir:
        parts = [p for p in project_dir.replace("\x00", "").split("/") if p and p != ".."]
        for p in parts:
            project_abs = project_abs / p
    is_fullstack = bool(project_abs) and (
        (project_abs / "backend" / "requirements.txt").exists() or
        (project_abs / "backend" / "main.py").exists()
    )

    # Choose where the dist lands. Fullstack → <slug>/static/. Static →
    # directly under <slug>/.
    dist_target = target / "static" if is_fullstack else target
    dist_target.mkdir(parents=True, exist_ok=True)
    staging = OJAS_APPS_ROOT / f".staging-{slug}-{int(time.time())}"
    try:
        shutil.copytree(dist, staging)
        # shutil.move has TWO modes:
        #   - dst doesn't exist → renames src to dst (what we want)
        #   - dst is a directory → moves src INSIDE dst as a child
        # We need the first. Old code passed a (possibly-existing)
        # dist_target and got mode #2 — staging ended up nested
        # inside dist_target as `.staging-{slug}-{ts}/`, which Caddy
        # never finds. Fix: remove the existing dist_target first,
        # THEN move staging to that path. We lose the "back up the
        # old build" step (was a safety net for fast in-place
        # rollbacks; we don't have a rollback feature yet anyway).
        if dist_target.is_dir() and not dist_target.is_symlink():
            shutil.rmtree(dist_target, ignore_errors=True)
        elif dist_target.is_symlink():
            dist_target.unlink()
        shutil.move(str(staging), str(dist_target))
    except OSError as e:
        # Best-effort cleanup of any half-copied staging dir
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not deploy app: {e}",
        )

    # 3b. FULLSTACK: if the project has a backend/ with requirements.txt,
    # pip-install into a venv + write+start a systemd unit. The frontend
    # dist is at /opt/ojas-apps/<slug>/static/ now. We also need
    # /opt/ojas-apps/<slug>/backend/ + /opt/ojas-apps/<slug>/data/.

    service_name: str | None = None
    port: int | None = None
    if is_fullstack:
        # The dist was already copied into <slug>/static/ above. Now
        # copy backend/ alongside, create data/, and venv.
        # (The earlier rename in step 3 put the dist into dist_target
        # which IS the static dir for fullstack apps.)
        # Now copy backend/ and create data/
        backend_src = project_abs / "backend"
        backend_dst = target / "backend"
        if backend_src.exists():
            if backend_dst.is_symlink():
                # Symlink (often left over from a prior deploy) — unlink
                # only the link, not the target, so we don't nuke the
                # real venv.
                backend_dst.unlink()
            elif backend_dst.exists():
                # The .venv inside the previous backend contains many
                # symlinks (passlib, others) that shutil.rmtree can
                # leave dangling. Use a symlink-aware recursive delete
                # so copytree below doesn't fail with "File exists" on
                # a half-removed symlinked dir.
                _rmtree_with_symlinks(backend_dst)
            shutil.copytree(backend_src, backend_dst)
        # Create a venv + pip install
        ok, err = _pip_install_for_app(backend_dst, backend_src / "requirements.txt")
        if not ok:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"could not install backend deps: {err}",
            )
        # Reuse the existing port for a re-deploy so the systemd unit
        # stays compatible with the Caddy fragment and the running
        # process. Allocating a fresh port on every re-deploy would
        # orphan the old one and break the live URL until Caddy picked
        # up the new port (and would 502 any in-flight requests).
        existing_row = db.get_deployed_app(slug) or {}
        if existing_row.get("port") and existing_row.get("service_name"):
            port = int(existing_row["port"])
        else:
            # Allocate a port + write the systemd unit
            try:
                port = _allocate_app_port()
            except RuntimeError as e:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
        slug_safe = re.sub(r"[^a-z0-9_-]", "-", slug.lower())[:40]
        service_name = f"ojas-app-{slug_safe}.service"
        _write_systemd_unit(slug_safe, port, backend_dst)
        # daemon-reload + enable + start (via the setuid helper, since
        # the Ojas user can't invoke systemctl directly)
        try:
            subprocess.run(
                ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "daemon-reload"],
                check=True, timeout=10, capture_output=True,
            )
            subprocess.run(
                ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "enable", service_name],
                check=True, timeout=10, capture_output=True,
            )
        except (subprocess.CalledProcessError, OSError) as e:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"systemctl enable failed: {e}",
            )

    # 4. Insert or touch the DB row
    if in_place:
        # Re-deploying the same slug: refresh the recorded subfolder too,
        # so a re-deploy that switches from session-root to a sub-app (or
        # vice versa) actually repoints the live URL.
        db.touch_deployed_app(slug, project_dir=project_dir)
        app_row = db.get_deployed_app(slug) or {}
        # Update service_name + port if this is a fullstack re-deploy
        if is_fullstack and service_name and port:
            with db._connect() as cx:   # noqa: SLF001
                cx.execute(
                    "UPDATE deployed_apps SET service_name = ?, port = ? "
                    "WHERE slug = ?",
                    (service_name, port, slug),
                )
            app_row = db.get_deployed_app(slug) or {}
    else:
        app_row = db.create_deployed_app(
            slug=slug,
            name=session["name"],
            app_dir=str(target),
            source_session_id=session_id,
            source_project_id=session.get("project_id"),
            owner_user_id=user["id"],
            project_dir=project_dir,
            service_name=service_name,
            port=port,
        )

    # 4b. If fullstack, regenerate Caddy fragments so the per-slug
    # /api/* reverse_proxy block is written + Caddy reloaded. Also
    # start the systemd unit now that the row exists.
    if is_fullstack and service_name and port:
        # Read the row back to get owner_user_id for the regen
        app_row_for_regen = db.get_deployed_app(slug) or {}
        _regenerate_caddy_routes_for_user(app_row_for_regen.get("owner_user_id"))
        ok = _start_app_service(app_row_for_regen)
        if not ok:
            # Don't fail the deploy -- the row is in place, the user
            # can retry from the settings page. Mark state=starting
            # so the UI shows a spinner; last_health_at stays null.
            db.set_deployed_app_state(slug, "starting", error_message="health check failed")

    # 5. Build the URL. Prefer the canonical subdomain form
    # `https://<slug>.<apps_root>/` -- that's the user-facing URL the
    # deploy button shows in the chat. The apps_root domain is
    # separately configurable from OJAS_DOMAIN so users can host apps
    # on a different domain than the apex (e.g. apex at
    # ojas.karmacode.cloud but apps at apps.karmacode.cloud). Falls
    # back to the legacy `/apps/<slug>/` subpath when no public
    # domain is resolved (e.g. local dev). Same backend serves both
    # routes so old links don't break.
    scheme = "https"
    apps_root = _resolve_apps_root_domain() or _resolve_public_domain()
    if apps_root:
        subdomain_url = f"{scheme}://{slug}.{apps_root}/"
        deployed_service_url = subdomain_url
    else:
        host = request.headers.get("host", request.url.netloc)
        deployed_service_url = f"{request.url.scheme}://{host}/apps/{slug}/"
        subdomain_url = deployed_service_url
    db.upsert_ojas_service(
        id=f"deployed:{slug}",
        source="ojas-deployed",
        pid=None,
        label=f"Deployed app: {app_row['name']}",
        command=None,
        port=None,
        bind_addr=None,
        url=deployed_service_url,
        meta={
            "slug": slug,
            "app_dir": app_row.get("app_dir"),
            "owner_user_id": app_row.get("owner_user_id"),
            "source_session_id": app_row.get("source_session_id"),
            "public_domain": _resolve_public_domain(),
        },
    )
    return DeployResponse(
        slug=slug,
        url=subdomain_url,
        app=DeployedAppResponse(**app_row),
    )


@app.get(
    "/api/deployed-apps",
    response_model=list[DeployedAppResponse],
)
def deployed_apps_list(user: dict = Depends(require_user)):
    """List deployed apps. Root sees all; everyone else sees their own."""
    owner = None if user["role"] == "root" else user["id"]
    return [DeployedAppResponse(**a) for a in db.list_deployed_apps(owner_user_id=owner)]


@app.get(
    "/api/sessions/{session_id}/deployed-apps",
    response_model=list[DeployedAppResponse],
)
def session_deployed_apps_list(
    session_id: str,
    user: dict = Depends(require_user),
):
    """Just the apps deployed FROM this session -- used by the chat UI to
    render the deploy strip with `<slug>.<apps-root>/` URLs inline. Same
    ownership rules as everywhere else (root sees all)."""
    _session_or_404(session_id, user)
    # Filter from the full list so we reuse the existing ownership logic.
    owner = None if user["role"] == "root" else user["id"]
    rows = [
        a for a in db.list_deployed_apps(owner_user_id=owner)
        if a.get("source_session_id") == session_id
    ]
    return [DeployedAppResponse(**a) for a in rows]


# ---- Dist auto-detection --------------------------------------------------
#
# The chat UI's deploy dialog has a "Sub-app folder" field that the user
# should never have to type: the agent knows what it built and the
# server can find it on disk. This endpoint is what the dialog calls
# when it opens, to pre-fill (and lock) the field. It returns ALL
# detected candidates so the UI can show what was found and disable
# Deploy when it's ambiguous (multiple sub-apps) or absent (no build).

class DistCandidate(BaseModel):
    project_dir: str          # "" = session root; otherwise sub-app folder name
    abs_path: str
    mtime: int                # epoch seconds; useful for "built 3m ago"
    index_size: int           # bytes in dist/index.html


class DetectedDistResponse(BaseModel):
    candidates: list[DistCandidate]
    # "single"     → exactly one dist, deploy can proceed with this value
    # "multiple"   → 2+ candidates; UI must ask the user which one
    # "none"       → no build found; UI should tell the user to build
    status: str
    # The pick the deploy endpoint would use by default (== candidates[0]
    # if status=="single", else None). UI can also pre-fill this.
    auto_pick: str | None = None
    # True when there's a built dist newer than the most recent deploy
    # IN THIS SESSION (or when no deploys exist yet). The chat uses this
    # to show the "Build ready. Click Deploy" banner under the agent's
    # last reply. False when the latest dist is older than the latest
    # deploy (nothing to publish).
    fresh_build: bool = False
    # The mtime of the freshest candidate (or 0 if none). Same epoch
    # seconds as DistCandidate.mtime. Lets the UI show "built 3m ago"
    # without an extra round-trip.
    fresh_mtime: int = 0


@app.get(
    "/api/sessions/{session_id}/detected-dist",
    response_model=DetectedDistResponse,
)
def sessions_detected_dist(
    session_id: str,
    user: dict = Depends(require_user),
):
    """Scan the session workspace for built `dist/` folders. Used by the
    deploy dialog to pre-fill the Project field, and by the chat
    banner to detect "fresh build" (newer than the latest deploy
    from this session)."""
    _session_or_404(session_id, user)
    cands = _detect_dist_candidates(session_id)
    # Compare against the most recent deploy FROM THIS SESSION. A
    # build is "fresh" if its mtime is newer than the latest
    # last_redeploy_at for any of this session's apps — or if
    # there are no deploys yet.
    last_redeploy = 0
    try:
        for row in db.list_deployed_apps_for_session(session_id):
            ts = int(row.get("last_redeploy_at") or 0)
            if ts > last_redeploy:
                last_redeploy = ts
    except Exception:
        # If the helper isn't available, default to "fresh" so we
        # never hide a build that's actually there.
        last_redeploy = 0
    fresh_build = False
    fresh_mtime = 0
    if cands:
        fresh_mtime = max(c["mtime"] for c in cands)
        fresh_build = fresh_mtime > last_redeploy
    if len(cands) == 0:
        return DetectedDistResponse(
            candidates=[], status="none", auto_pick=None,
            fresh_build=False, fresh_mtime=0,
        )
    if len(cands) == 1:
        return DetectedDistResponse(
            candidates=[DistCandidate(**c) for c in cands],
            status="single",
            auto_pick=cands[0]["project_dir"],
            fresh_build=fresh_build, fresh_mtime=fresh_mtime,
        )
    return DetectedDistResponse(
        candidates=[DistCandidate(**c) for c in cands],
        status="multiple",
        auto_pick=cands[0]["project_dir"],  # newest as best guess
        fresh_build=fresh_build, fresh_mtime=fresh_mtime,
    )


@app.delete("/api/deployed-apps/{slug}")
def deployed_apps_delete(slug: str, user: dict = Depends(require_user)):
    """Take down a deployed app -- rmtree the on-disk files AND remove the
    DB row. Idempotent on missing files (so a botched-half-state app can
    still be cleaned up from the UI)."""
    import shutil
    app = _deployed_app_or_404(slug, user)
    target = Path(app["app_dir"])
    # Stop the systemd unit (fullstack) before rmtree so we don't
    # leave a zombie process holding files open.
    _stop_app_service(app)
    # Disable + remove the systemd unit file (fullstack)
    svc = app.get("service_name")
    if svc:
        for cmd in (
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "disable", svc],
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "stop", svc],
        ):
            try:
                subprocess.run(cmd, check=False, timeout=5, capture_output=True)
            except (subprocess.TimeoutExpired, OSError):
                pass
        unit_file = Path("/etc/systemd/system") / svc
        try:
            if unit_file.exists():
                subprocess.run(
                    ["/usr/local/sbin/ojas-systemd-helper", "rm-unit", svc],
                    check=False, timeout=5, capture_output=True,
                )
            subprocess.run(
                ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "daemon-reload"],
                check=False, timeout=5, capture_output=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if target.exists() and target.is_dir():
        try:
            shutil.rmtree(target)
        except OSError as e:
            # Don't leave the DB row dangling on a filesystem hiccup --
            # surface the error but still drop the row so the user can
            # retry the deploy without slug collision.
            db.delete_deployed_app(slug)
            db.delete_ojas_service(f"deployed:{slug}")
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"deleted DB row but file removal failed: {e}",
            )
    db.delete_deployed_app(slug)
    db.delete_ojas_service(f"deployed:{slug}")
    # Drop the per-slug Caddy route so the subdomain stops resolving.
    _regenerate_caddy_routes_for_user(app.get("owner_user_id"))
    return {"ok": True}


# ---- Pause / resume (toggle) --------------------------------------------
#
# Each deployed app has a state in DB: running | stopped | starting | error.
# For static apps, the toggle just swaps the Caddy route between live and
# a shared paused page. For fullstack apps (v1.1), the toggle also starts/
# stops the per-app systemd unit. Both paths are routed through these
# endpoints; the static-only path is what v1 ships with.

@app.post("/api/deployed-apps/{slug}/start")
def deployed_app_start(slug: str, user: dict = Depends(require_user)):
    """Bring a paused app back up. Idempotent. Marks the row 'starting'
    then 'running' once any backend (v1.1) is healthy, or straight to
    'running' for static apps (the dir rename is the only work)."""
    app = _deployed_app_or_404(slug, user)
    if app["state"] == "running":
        return DeployStateResponse(
            slug=slug, state="running", last_state_at=app.get("last_state_at"),
            last_health_at=app.get("last_health_at"),
            error_message=app.get("error_message"),
        )
    db.set_deployed_app_state(slug, "starting")
    # Fullstack: start the systemd unit. v1 is static-only so this is
    # a no-op for now; v1.1 will shell out to systemctl + healthcheck.
    if app.get("service_name"):
        ok = _start_app_service(app)
        if not ok:
            db.set_deployed_app_state(slug, "error", error_message="failed to start service")
            raise HTTPException(500, "failed to start service")
    # Move the app dir back to the live path. Caddy's `file_server`
    # re-stats per request, so the new config takes effect on the
    # very next request -- no Caddy reload needed.
    _apply_app_state_to_disk(slug, "running")
    now = int(time.time())
    db.set_deployed_app_state(slug, "running", last_health_at=now)
    # Re-emit the per-slug Caddy fragment (was deleted on stop) so
    # the /api/* reverse_proxy block comes back and Caddy picks up
    # the new host block.
    _regenerate_caddy_routes_for_user(app.get("owner_user_id"))
    return DeployStateResponse(slug=slug, state="running", last_state_at=now, last_health_at=now)


@app.post("/api/deployed-apps/{slug}/stop")
def deployed_app_stop(slug: str, user: dict = Depends(require_user)):
    """Take an app down without deleting it. State becomes 'stopped',
    the app dir moves to /opt/ojas-apps/.stopped/<slug>/, and (v1.1)
    the systemd unit is stopped. Re-deploy / re-toggle to bring back."""
    app = _deployed_app_or_404(slug, user)
    if app["state"] == "stopped":
        return DeployStateResponse(
            slug=slug, state="stopped", last_state_at=app.get("last_state_at"),
            last_health_at=app.get("last_health_at"),
            error_message=app.get("error_message"),
        )
    # Fullstack: stop the unit FIRST so the process releases its files
    # before we rename the dir. v1 is static-only so this no-ops.
    if app.get("service_name"):
        _stop_app_service(app)
    # Move the app dir aside. Caddy falls through to the shared
    # /.paused/index.html page for any subsequent request.
    _apply_app_state_to_disk(slug, "stopped")
    db.set_deployed_app_state(slug, "stopped")
    # Drop the per-slug Caddy fragment so the wildcard block takes
    # over (its try_files chain has /.paused/index.html as a
    # fallback). Re-created on the next start.
    _regenerate_caddy_routes_for_user(app.get("owner_user_id"))
    return DeployStateResponse(
        slug=slug, state="stopped",
        last_state_at=int(time.time()),
        last_health_at=app.get("last_health_at"),
        error_message=app.get("error_message"),
    )


@app.get("/api/deployed-apps/{slug}/state", response_model=DeployStateResponse)
def deployed_app_state(slug: str, user: dict = Depends(require_user)):
    """Return just the state for one app. Cheaper than a full listing --
    used by the chat-strip pill to refresh the badge after a toggle."""
    app = _deployed_app_or_404(slug, user)
    return DeployStateResponse(
        slug=slug,
        state=app.get("state", "running"),
        last_state_at=app.get("last_state_at"),
        last_health_at=app.get("last_health_at"),
        error_message=app.get("error_message"),
    )


@app.get("/api/users/me/deployed-apps", response_model=list[DeployedAppsBySession])
def users_deployed_apps(user: dict = Depends(require_user)):
    """All deployed apps the caller can see, grouped by source session.
    Used by the Settings page. Root sees everything; non-root sees
    their own + orphans (apps whose owner was deleted)."""
    owner = None if user["role"] == "root" else user["id"]
    rows = db.list_deployed_apps_grouped(owner_user_id=owner)
    return [DeployedAppsBySession(**r) for r in rows]


# Serve a deployed app's static files. Mirrors the /preview/* handler
# (same SPA fallback + traversal defence + same Caddy reverse-proxy path),
# but reads from the persistent /opt/ojas-apps/<slug>/ location.
@app.get("/apps/{slug}")
@app.get("/apps/{slug}/")
@app.get("/apps/{slug}/{file_path:path}")
def apps_serve(slug: str, file_path: str = ""):
    """Static-serve a deployed app at /apps/<slug>/. NO auth -- the URL is
    intentionally shareable so users can install the PWA on any device.
    Slugs are unguessable enough in practice (the alphanumeric space + the
    fact that an app only exists if the owner explicitly deployed it).

    Cache-Control: we send `no-cache, must-revalidate` so the BROWSER
    revalidates on every request (sends If-Modified-Since / If-None-Match
    and gets a 304 if the file hasn't changed), but still returns the
    latest file when the user re-deploys. Without this, browsers will
    happily keep serving a stale index.html for 5-10 minutes after a
    re-deploy, which is the "I deployed but the app looks the same"
    symptom users hit. The ETags/Last-Modified headers from FileResponse
    give us cheap 304s when the file hasn't changed."""
    from fastapi.responses import FileResponse
    app = db.get_deployed_app(slug)
    if app is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "app not found",
        )
    app_dir = Path(app["app_dir"])
    if not app_dir.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "app files missing on disk (was it deleted?)",
        )
    app_resolved = app_dir.resolve()
    requested = file_path or "index.html"
    target = (app_dir / requested).resolve()
    # Path traversal defence -- target MUST be inside app_dir.
    try:
        target.relative_to(app_resolved)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not target.exists() or not target.is_file():
        # SPA fallback -- every non-asset path returns index.html so the
        # built React Router can take over client-side.
        target = app_dir / "index.html"
        if not target.exists():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "app index.html missing",
            )
    # Hash-bundled assets under /assets/ are content-addressed so they're
    # safe to cache forever (the file name changes when the content does).
    # For everything else (index.html, favicon, source maps, user assets)
    # we force revalidation.
    cache_control = (
        "public, max-age=31536000, immutable"
        if "/assets/" in str(target)
        else "no-cache, must-revalidate"
    )
    return FileResponse(target, headers={"Cache-Control": cache_control})


# ============================================================================
# Admin (root only) -- running processes, all users, etc.
# ============================================================================

@app.get("/api/admin/processes", response_model=list[ProcessResponse])
def admin_processes_list(_root: dict = Depends(require_root)):
    """Every tracked spawned process across every session on this VM. Each
    row carries the session_id so the admin can navigate to it from the UI.
    Includes ports -- useful for spotting "which port is that preview app
    using right now?". Each row also carries `is_alive` so the UI can
    distinguish live processes from stale DB rows (the process exited but
    the row wasn't cleaned up)."""
    rows = []
    for p in db.list_all_processes():
        # `os.kill(pid, 0)` is the cheapest "is this PID alive?" probe --
        # sends no signal, just checks for ESRCH vs success. We tolerate
        # permission errors (rare; only happens for setuid pids).
        try:
            os.kill(int(p["pid"]), 0)
            alive = True
        except (OSError, ProcessLookupError):
            alive = False
        except PermissionError:
            # Process exists but we can't signal it (foreign user). Count as alive.
            alive = True
        p["is_alive"] = alive
        rows.append(ProcessResponse(**p))
    return rows


@app.delete("/api/admin/processes/{pid}")
def admin_processes_kill(pid: int, _root: dict = Depends(require_root)):
    """SIGTERM the process and unregister the row. Idempotent -- if the
    process is already gone, just drops the row. Used to manually clean
    up zombie dev servers / hung builds. Refuses to kill the Ojas main
    backend or the caddy reverse proxy -- those are protected services,
    not session-spawned work."""
    import signal
    # Refuse to kill protected Ojas services
    for svc in db.list_ojas_services():
        if svc.get("pid") == pid and svc.get("source") in ("ojas-main", "ojas-proxy"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"refusing to kill protected service '{svc['label']}' (pid {pid}); "
                f"stop the backend / caddy via systemd instead",
            )
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    db.unregister_process(pid)
    return {"ok": True}


@app.get("/api/admin/services", response_model=list[OjasServiceResponse])
def admin_services_list(_root: dict = Depends(require_root)):
    """Every Ojas-owned service on this VM -- main backend, caddy proxy,
    deployed apps (port-only rows), MCP servers, future external services.
    Each row is tagged with `source` so the admin UI can group them.

    Also opportunistically reconciles against the live /proc/net/tcp view
    so that any ports we've forgotten to register (e.g. an MCP server
    started outside the DB) are picked up here."""
    rows = db.list_ojas_services()

    # For each row, derive a `ports` list. The boot-time registration
    # stashes the full list in meta['ports'] (since the `port` column is
    # scalar). For rows that don't have that, fall back to the scalar
    # `port` field so the field is always populated.
    for r in rows:
        meta_ports = (r.get("meta") or {}).get("ports") or []
        if meta_ports:
            r["ports"] = sorted(set(int(x) for x in meta_ports))
        elif r.get("port") is not None:
            r["ports"] = [int(r["port"])]
        else:
            r["ports"] = []

    # Reconcile: enumerate every listening port on the box and make sure
    # each one is either (a) a registered ojas_services row, or (b) a
    # system service we'd expect (sshd, systemd-resolve, caddy). Anything
    # else is flagged as `ojas-external` so the admin sees it.
    listening = _listening_ports_system_wide()
    known_ports: set[int] = set()
    for r in rows:
        known_ports.update(r.get("ports") or [])
        if r.get("port") is not None:
            known_ports.add(int(r["port"]))
    for port, proc_name, pid in listening:
        if port in known_ports:
            continue
        # Skip well-known system ports we'd expect on a Linux box.
        if port in (22, 53, 80, 443, 2019, 65529):
            continue
        if "systemd" in proc_name or "sshd" in proc_name:
            continue
        rows.append({
            "id": f"external:{port}",
            "source": "ojas-external",
            "pid": pid,
            "label": f"External listener on :{port}",
            "command": proc_name,
            "port": port,
            "ports": [port],
            "bind_addr": None,
            "url": None,
            "started_at": int(time.time()),
            "meta": {"discovered_via": "proc_net_tcp"},
        })

    return [OjasServiceResponse(**r) for r in rows]


@app.get("/api/admin/buses")
def admin_buses_list(_root: dict = Depends(require_root)):
    """Snapshot of every active session bus -- queue depth, subscriber
    count, dropped-event count. Helps operators spot clients too slow
    to keep up (dropped_count > 0) or sessions with wedged
    subscribers (queue full, no one draining it)."""
    from server.reporter import bus_stats
    return {"buses": bus_stats()}


@app.get("/api/admin/users", response_model=list[UserResponse])
def admin_users_list(_root: dict = Depends(require_root)):
    """List every account on this VM. Strips password hashes."""
    return [
        UserResponse(
            id=u["id"], email=u["email"], role=u["role"],
            created_at=u["created_at"],
        )
        for u in db.list_users()
    ]


@app.delete("/api/admin/users/{user_id}")
def admin_user_delete(
    user_id: str,
    root: dict = Depends(require_root),
):
    """Hard-delete a user. Refuses to delete the last root account
    (would leave the box un-administrable). Cascades to their auth
    tokens + projects. FULLY removes their deployed apps (files + URL
    + DB row + ojas_services row) -- the public /apps/<slug>/ endpoint
    will return 404 once this returns.

    Side effects (all best-effort, never block the delete):
      • SIGTERMs every agent-spawned process owned by the user (closes
        their dev servers / http.servers / venv-spawned uvicorns so the
        ports are released)
      • removes per-session workspace subdirs under each project
      • removes per-session langgraph checkpoint files
      • clears per-session event bus subscribers
      • rmtree's /opt/ojas-apps/<slug>/ for every deployed app the
        user owns; the public URL stops serving immediately"""
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if target.get("role") == "root" and db.count_root_users() <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "refusing to delete the last root user -- promote another user "
            "to root first, or set OJAS_ROOT_EMAIL/PASSWORD in .env to "
            "bootstrap a new one",
        )
    # Best-effort OS-level cleanup BEFORE the DB cascade. We catch
    # everything so a single bad row can't lock the admin out.
    try:
        _purge_user_everything(user_id)
    except Exception:
        pass
    # Fully tear down the user's deployed apps (files + URL + DB row +
    # ojas_services row). Runs BEFORE auth.delete_user() because the FK
    # on deployed_apps.owner_user_id is SET NULL -- once the user is
    # gone, the apps become orphan and we'd have to filter by name
    # instead of owner_user_id.
    import shutil
    for app_row in db.list_deployed_apps(owner_user_id=user_id):
        slug = app_row["slug"]
        target_dir = Path(app_row["app_dir"])
        try:
            if target_dir.exists() and target_dir.is_dir():
                shutil.rmtree(target_dir)
        except OSError:
            pass
        try:
            db.delete_deployed_app(slug)
        except Exception:
            pass
        try:
            db.delete_ojas_service(f"deployed:{slug}")
        except Exception:
            pass
    try:
        auth.delete_user(user_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/password")
def admin_user_reset_password(
    user_id: str,
    req: AdminResetPasswordRequest,
    _root: dict = Depends(require_root),
):
    """Reset a user's password. Invalidates all of their existing
    auth tokens so they have to log in again with the new password."""
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    try:
        auth.reset_password(user_id, req.new_password)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return {"ok": True}


def _listening_ports_system_wide() -> list[tuple[int, str, int | None]]:
    """Enumerate every TCP port currently in LISTEN on this host, with
    the owning pid + comm name when available. Prefers `ss` (netlink,
    works regardless of process ownership); falls back to a /proc scan
    that can only attribute ports to same-user pids."""
    ss = shutil.which("ss")
    if ss:
        out: list[tuple[int, str, int | None]] = []
        try:
            res = subprocess.run(
                ["ss", "-ltnp", "-H"],
                capture_output=True, text=True, timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            res = None
        if res is not None and res.returncode == 0:
            for line in res.stdout.splitlines():
                # Example: LISTEN 0 4096 *:80 *:* users:(("caddy",pid=23816,fd=4))
                try:
                    local = line.split()[3]
                    port = int(local.rsplit(":", 1)[-1])
                except (IndexError, ValueError):
                    continue
                pid: int | None = None
                if "pid=" in line:
                    try:
                        pid = int(line.split("pid=", 1)[1].split(",", 1)[0].split(")", 1)[0])
                    except (IndexError, ValueError):
                        pid = None
                # Process name = the quoted token right before `pid=`
                proc_name = ""
                if "users:((\"" in line:
                    try:
                        proc_name = line.split("users:((\"", 1)[1].split("\"", 1)[0]
                    except IndexError:
                        proc_name = ""
                out.append((port, proc_name, pid))
            out.sort(key=lambda t: t[0])
            return out

    # Fallback: /proc scan. Only attributes ports to same-user pids.
    try:
        inode_to_pid: dict[str, int] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            fd_dir = f"/proc/{pid}/fd"
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(f"{fd_dir}/{fd}")
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        inode = link[len("socket:["):-1]
                        inode_to_pid.setdefault(inode, pid)
            except (OSError, PermissionError):
                continue
    except OSError:
        return []

    out = []
    seen_ports: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, "r") as fh:
                fh.readline()
                for line in fh:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":
                        continue
                    local = parts[1]
                    inode = parts[9]
                    try:
                        port = int(local.split(":", 1)[1], 16)
                    except (ValueError, IndexError):
                        continue
                    if port in seen_ports:
                        continue
                    seen_ports.add(port)
                    pid = inode_to_pid.get(inode)
                    proc_name = ""
                    if pid is not None:
                        try:
                            with open(f"/proc/{pid}/comm", "r") as pcf:
                                proc_name = pcf.read().strip()
                        except OSError:
                            proc_name = f"pid:{pid}"
                    out.append((port, proc_name, pid))
        except OSError:
            continue
    out.sort(key=lambda t: t[0])
    return out


# ============================================================================
# Health
# ============================================================================

@app.get("/api/health")
def health():
    return {"ok": True, "needs_setup": auth.needs_setup()}


# ============================================================================
# Caddy on-demand TLS ask endpoint.
# Caddy calls this for every NEW hostname before fetching a Let's Encrypt
# cert. We say "yes" only for slugs we've actually deployed, so typo'd /
# malicious subdomains can't burn through Let's Encrypt rate limits.
# Bound to 127.0.0.1 in the Caddyfile, no auth needed.
# ============================================================================

@app.get("/internal/caddy-ask")
def caddy_ask(domain: str = Query(...)):
    """Caddy hits us with ?domain=<host>. We accept only registered slugs
    on the configured apps root domain. 200 = OK to mint cert, 404 = decline."""
    if not domain:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no domain")
    domain = domain.strip().lower()
    # Apps-root domain. Apps live at <slug>.<apps_root>. The apex
    # (OJAS_DOMAIN) is a separate thing -- apps may share it OR live
    # on a different domain entirely.
    root = (_resolve_apps_root_domain() or _resolve_public_domain() or "").lower()
    if root and not domain.endswith("." + root):
        # Not under our apps root -- reject.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not our domain")
    # Extract the slug (the leftmost label).
    slug = domain.split(".", 1)[0]
    if not slug or not db.get_deployed_app(slug):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such app")
    return {"ok": True}


@app.get("/api/debug/whoami")
def debug_whoami(user: dict = Depends(require_user)):
    """Diagnostic endpoint -- answers "who does the server think I am, and
    what data does it see for me?" Used to pin down session/project
    persistence bugs. Safe to expose: only returns the caller's own data
    (root sees everything, but that's already true everywhere)."""
    projects = db.list_projects(user_id=user["id"])
    sessions_by_project: list[dict] = []
    total_sessions = 0
    for p in projects:
        ss = db.list_sessions(p["id"])
        total_sessions += len(ss)
        sessions_by_project.append({
            "project_id":   p["id"],
            "project_name": p["name"],
            "workspace":    p["workspace_path"],
            "user_id":      p.get("user_id"),
            "session_count": len(ss),
            "sessions": [
                {
                    "id":              s["id"],
                    "name":            s["name"],
                    "user_id":         s.get("user_id"),
                    "workspace_subdir": s.get("workspace_subdir"),
                    "last_active_at":  s["last_active_at"],
                }
                for s in ss[:10]   # cap so the response stays compact
            ],
        })
    return {
        "user": {
            "id": user["id"], "email": user["email"], "role": user["role"],
        },
        "project_count": len(projects),
        "session_count": total_sessions,
        "projects": sessions_by_project,
        "default_workspace_path": _default_workspace_path(),
    }
