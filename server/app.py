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
# missing — production VMs can still set vars via systemd / docker env if
# they prefer.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass   # dotenv not installed — fall back to shell env only

from fastapi import (
    Depends, FastAPI, HTTPException, Header, Query, Request, WebSocket,
    WebSocketDisconnect, status,
)
from fastapi.middleware.cors import CORSMiddleware

from server import auth, db
from server.git_autocommit import get_git_info, push_to_remote
from server.reporter import WebReporter, get_bus
from server.schemas import (
    AuthStatusResponse, DeployRequest, DeployResponse, DeployedAppResponse,
    EventResponse, GitInfoResponse, LoginRequest,
    LoginResponse, MessagePostRequest, MessageResponse, OjasServiceResponse,
    ProcessResponse, ProjectCreateRequest, ProjectResponse, ProjectSettingsRequest,
    PushResponse, SessionCreateRequest, SessionRenameRequest, SessionResponse,
    SetupRequest, SignupRequest, UserResponse, AdminResetPasswordRequest,
)
from server.session_runner import run_turn


# ============================================================================
# App lifecycle — bootstrap DB + safety singletons on startup
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await _configure_runtime_singletons()
    _register_ojas_services()
    yield


def _register_ojas_services() -> None:
    """Idempotently register the Ojas-owned runtime services (main backend,
    caddy reverse proxy) in `ojas_services` so the admin panel can show
    them. Called once on backend boot. Port-only rows for deployed apps
    are also created/refreshed here so the panel always reflects the
    truth on disk under /opt/ojas-apps/."""
    # 1) Drop any stale PID rows from a previous boot. Deployed-app port
    #    rows (pid IS NULL) are kept — they're tied to on-disk files, not
    #    a process. We'll re-add the still-running ones below.
    db.clear_ojas_services_with_pid()

    # 2) Main uvicorn process. PID of this process is the uvicorn worker
    #    (the parent is whatever launched us — uvicorn / python -m / etc.).
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
    #    skip — a developer running `uvicorn` directly won't have it.
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

    # 3b) Ojas web UI — the React/Vite build at /opt/ojas/web/dist. NOT a
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

    # 4) Deployed apps — port-only rows. Each deployed app is static
    #    files served at /apps/<slug>/ via caddy (or by this backend in
    #    dev mode). Re-register every row on disk so the panel stays
    #    accurate even if the backend was restarted.
    OJAS_APPS_ROOT.mkdir(parents=True, exist_ok=True)
    for app_row in db.list_deployed_apps(owner_user_id=None):
        slug = app_row["slug"]
        # Build a clickable public URL using the resolved public domain.
        # Falls back to the bare path if we can't determine the domain.
        deployed_url: str | None = None
        if public_domain:
            deployed_url = f"https://{public_domain}/apps/{slug}/"
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
         partial — no pid info — for foreign-user processes)
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
    # Fallback — /proc scan. Works for the main backend (same user); fails
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
    WHO is calling — i.e. anything multi-user (project / session listing,
    creation, deletion) plus the admin endpoints."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = authorization.split(None, 1)[1].strip()
    user = auth.user_from_token(token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or revoked token")
    return user


def require_root(user: dict = Depends(require_user)) -> dict:
    """Gate admin endpoints — caller must be the root user."""
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
        # Project creation must not break signup itself — the user can
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
    """Idempotent — create the calling user's default project if they don't
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
      1. `OJAS_DEFAULT_WORKSPACE` env var — explicit override, wins always.
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
    # new orphan project per login on the VM — the old one's sessions then
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


def _purge_session_workspace_subdir(session: dict) -> None:
    """Delete the session's private subdirectory under its project workspace.
    This is where the agent actually built files for this session, so on
    session delete we want it gone. Best-effort — silently skips if the
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
            return  # subdir is suspiciously outside the workspace — skip
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


def _purge_session_everything(session: dict) -> None:
    """One-stop cleanup for everything tied to a session OUTSIDE the main
    SQLite (which CASCADE handles). Idempotent + best-effort — none of
    these can fail the API call."""
    sid = session["id"]
    _kill_session_processes(sid)
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

    Deployed apps are NOT touched here — they have ON DELETE SET NULL on
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
    checkpoint + bus. Sibling sessions are untouched."""
    session = _session_or_404(session_id, user)
    task = _active_turns.get(session_id)
    if task is not None and not task.done():
        task.cancel()
    if not db.delete_session(session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    _purge_session_everything(session)
    return {"ok": True}


@app.patch("/api/sessions/{session_id}", response_model=SessionResponse)
def sessions_rename(
    session_id: str,
    req: SessionRenameRequest,
    user: dict = Depends(require_user),
):
    """Rename a session. The new name must be unique within the session's
    project (case-sensitive). On collision, returns 409 with the existing
    session's id so the UI can offer to jump to it instead."""
    session = _session_or_404(session_id, user)
    try:
        updated = db.rename_session(session_id, req.new_name)
    except db.SessionNameConflict as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": str(e),
                "existing_session_id": e.existing_id,
                "existing_session_name": e.existing_name,
            },
        ) from e
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return SessionResponse(**updated)


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
    # the ojas service user) — sessions_create would then 500 with an
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
    with db._connect() as cx:   # noqa: SLF001 — small helper, inline write
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
    # publishing — bind_loop is idempotent.
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


# Per-session in-flight turn registry — used by the cancel endpoint to abort a
# running turn. Cleared when the task finishes naturally.
_active_turns: dict[str, asyncio.Task] = {}


@app.post("/api/sessions/{session_id}/cancel")
async def cancel_turn(
    session_id: str,
    user: dict = Depends(require_user),
):
    _session_or_404(session_id, user)
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
    # Ownership check — non-root users can only stream their own sessions.
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
# Preview — serve the session's built PWA so it can be installed on any device.
# ============================================================================

def _session_preview_dir(session_id: str) -> Path | None:
    """Resolve the session's `dist/` folder, or None if the session/project
    can't be found. Used by both the static-serve route AND the build
    watcher that emits preview_ready events."""
    session = db.get_session(session_id)
    if session is None:
        return None
    project = db.get_project(session["project_id"])
    if project is None:
        return None
    base = Path(project["workspace_path"])
    if session.get("workspace_subdir"):
        base = base / session["workspace_subdir"]
    return base / "dist"


@app.get("/preview/{session_id}")
@app.get("/preview/{session_id}/")
@app.get("/preview/{session_id}/{file_path:path}")
def preview_serve(session_id: str, file_path: str = ""):
    """Static-serve the session's `<workspace>/dist/` at a public URL.
    NO auth — the URL is shareable to your phone so a PWA can install
    itself. session_id is a uuid hex; guessing one is impractical, and the
    preview only exists if the agent built one.

    SPA fallback: missing assets resolve to `index.html` so client-side
    React Router takes over."""
    from fastapi.responses import FileResponse
    dist_dir = _session_preview_dir(session_id)
    if dist_dir is None or not dist_dir.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "preview not built yet — the agent needs to run `npm run build` first",
        )
    dist_resolved = dist_dir.resolve()
    requested = file_path or "index.html"
    target = (dist_dir / requested).resolve()
    # Path traversal defence — target MUST be inside dist_dir.
    try:
        target.relative_to(dist_resolved)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not target.exists() or not target.is_file():
        target = dist_dir / "index.html"
        if not target.exists():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "preview index.html missing",
            )
    return FileResponse(target)


# ============================================================================
# Deployed apps — promote a session's built dist/ to a persistent URL.
#
# Flow:
#   1. Agent builds → <session_workspace>/dist/index.html exists
#   2. POST /api/sessions/<sid>/deploy {slug?}
#   3. Server picks slug (slugify name; -2, -3 on collision), copies
#      dist/ → /opt/ojas-apps/<slug>/, inserts deployed_apps row
#   4. App is live at https://<host>/apps/<slug>/ — survives session delete
#      + backend restart (it's just files on disk + a DB row, no process)
# ============================================================================

OJAS_APPS_ROOT = Path("/opt/ojas-apps")

# Public domain Ojas is served at. Read from the env (OJAS_DOMAIN) if set,
# else parsed from the Caddyfile, else fall back to the request's Host
# header at runtime. Used to build clickable URLs in the admin panel +
# /api/deployed-apps responses.
OJAS_DOMAIN: str | None = None
OJAS_DOMAIN_OVERRIDE: str | None = os.getenv("OJAS_DOMAIN", "").strip() or None


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
    """Copy the session's built dist/ to a persistent /opt/ojas-apps/<slug>/
    location and register it as a deployed app. The deployed app is
    DECOUPLED from the session — deleting the session leaves the live URL
    intact. Re-deploying the same session to the SAME slug is allowed
    (atomic in-place swap); re-deploying without a slug picks a fresh one."""
    import shutil
    session = _session_or_404(session_id, user)
    # 1. Need a built dist/
    dist = _session_preview_dir(session_id)
    if dist is None or not dist.exists():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "no built dist/ yet — ask the agent to run `npm run build` first",
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
        slug = db.allocate_deployed_slug(desired)
        in_place = False

    # 3. Copy files. Use a sibling temp dir + rename for atomicity (so the
    # public URL never serves a half-copied build).
    OJAS_APPS_ROOT.mkdir(parents=True, exist_ok=True)
    target = OJAS_APPS_ROOT / slug
    staging = OJAS_APPS_ROOT / f".staging-{slug}-{int(time.time())}"
    try:
        shutil.copytree(dist, staging)
        if target.exists():
            backup = OJAS_APPS_ROOT / f".old-{slug}-{int(time.time())}"
            target.rename(backup)
            staging.rename(target)
            shutil.rmtree(backup, ignore_errors=True)
        else:
            staging.rename(target)
    except OSError as e:
        # Best-effort cleanup of any half-copied staging dir
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not deploy app: {e}",
        )

    # 4. Insert or touch the DB row
    if in_place:
        db.touch_deployed_app(slug)
        app_row = db.get_deployed_app(slug) or {}
    else:
        app_row = db.create_deployed_app(
            slug=slug,
            name=session["name"],
            app_dir=str(target),
            source_session_id=session_id,
            source_project_id=session.get("project_id"),
            owner_user_id=user["id"],
        )

    # 5. Build the URL. Use the request's scheme + host so it works behind
    # Caddy (forwarded headers handled by the uvicorn --proxy-headers flag).
    scheme = request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    # 6. Register the new app in ojas_services so the admin panel sees it
    #    immediately (no need to wait for a backend restart). Prefer the
    #    resolved public domain for the clickable URL so the admin panel
    #    always shows the canonical link, not a localhost.
    resolved = _resolve_public_domain() or host
    if resolved and not resolved.startswith("http"):
        deployed_service_url = f"{scheme}://{resolved}/apps/{slug}/"
    else:
        deployed_service_url = f"/apps/{slug}/"
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
        url=f"{scheme}://{host}/apps/{slug}/",
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


@app.delete("/api/deployed-apps/{slug}")
def deployed_apps_delete(slug: str, user: dict = Depends(require_user)):
    """Take down a deployed app — rmtree the on-disk files AND remove the
    DB row. Idempotent on missing files (so a botched-half-state app can
    still be cleaned up from the UI)."""
    import shutil
    app = _deployed_app_or_404(slug, user)
    target = Path(app["app_dir"])
    if target.exists() and target.is_dir():
        try:
            shutil.rmtree(target)
        except OSError as e:
            # Don't leave the DB row dangling on a filesystem hiccup —
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
    return {"ok": True}


# Serve a deployed app's static files. Mirrors the /preview/* handler
# (same SPA fallback + traversal defence + same Caddy reverse-proxy path),
# but reads from the persistent /opt/ojas-apps/<slug>/ location.
@app.get("/apps/{slug}")
@app.get("/apps/{slug}/")
@app.get("/apps/{slug}/{file_path:path}")
def apps_serve(slug: str, file_path: str = ""):
    """Static-serve a deployed app at /apps/<slug>/. NO auth — the URL is
    intentionally shareable so users can install the PWA on any device.
    Slugs are unguessable enough in practice (the alphanumeric space + the
    fact that an app only exists if the owner explicitly deployed it)."""
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
    # Path traversal defence — target MUST be inside app_dir.
    try:
        target.relative_to(app_resolved)
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not target.exists() or not target.is_file():
        # SPA fallback — every non-asset path returns index.html so the
        # built React Router can take over client-side.
        target = app_dir / "index.html"
        if not target.exists():
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "app index.html missing",
            )
    return FileResponse(target)


# ============================================================================
# Admin (root only) — running processes, all users, etc.
# ============================================================================

@app.get("/api/admin/processes", response_model=list[ProcessResponse])
def admin_processes_list(_root: dict = Depends(require_root)):
    """Every tracked spawned process across every session on this VM. Each
    row carries the session_id so the admin can navigate to it from the UI.
    Includes ports — useful for spotting "which port is that preview app
    using right now?". Each row also carries `is_alive` so the UI can
    distinguish live processes from stale DB rows (the process exited but
    the row wasn't cleaned up)."""
    rows = []
    for p in db.list_all_processes():
        # `os.kill(pid, 0)` is the cheapest "is this PID alive?" probe —
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
    """SIGTERM the process and unregister the row. Idempotent — if the
    process is already gone, just drops the row. Used to manually clean
    up zombie dev servers / hung builds. Refuses to kill the Ojas main
    backend or the caddy reverse proxy — those are protected services,
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
    """Every Ojas-owned service on this VM — main backend, caddy proxy,
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
    + DB row + ojas_services row) — the public /apps/<slug>/ endpoint
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
            "refusing to delete the last root user — promote another user "
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
    # on deployed_apps.owner_user_id is SET NULL — once the user is
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


@app.get("/api/debug/whoami")
def debug_whoami(user: dict = Depends(require_user)):
    """Diagnostic endpoint — answers "who does the server think I am, and
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
