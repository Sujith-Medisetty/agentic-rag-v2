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
    GET  /api/projects/{project_id}/sessions      → paginated list (newest
                                                    first). Query params:
                                                    ?limit=50&offset=0.
                                                    Returns {items, total,
                                                    limit, offset}.
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

  Deploy (async with progress polling):
    POST /api/sessions/{session_id}/deploy               → 202 + {job_id, slug, url,
                                                              placeholder_app}
                                                          (sync 400/409 stay
                                                           synchronous for the
                                                           obvious user errors)
    GET  /api/sessions/{session_id}/deploy-jobs/{job_id} → {status, phase, steps[11],
                                                              error?, result?}
                                                          for the polling UI
    POST /api/sessions/{session_id}/deploy-jobs/{job_id}/cancel
                                                         → cooperative cancel

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
    AuthStatusResponse, DeployRequest, DeployedAppResponse,
    DeployJobStartResponse, DeployJobStatusResponse,
    DeployStateResponse, DeployedAppsBySession,
    DeleteJobStartResponse, DeleteJobStatusResponse,
    EventResponse, GitInfoResponse, LoginRequest,
    LoginResponse, MessagePostRequest, MessageResponse, OjasServiceResponse,
    ProcessResponse, ProjectCreateRequest, ProjectResponse, ProjectSettingsRequest,
    PushResponse, SessionCreateRequest, SessionRenameRequest, SessionResponse,
    SignupRequest, UserResponse, AdminResetPasswordRequest,
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
    _start_deploy_job_sweeper()
    _start_delete_job_sweeper()
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

    # Boot-time orphan cleanup. Reaps state left behind by deploys that
    # were killed mid-flight (e.g. backend restart, SIGKILL during
    # pip install):
    #
    #   1. Orphan Caddy fragments -- a per-slug .caddy file whose slug
    #      has no matching deployed_apps row. This happens when step 1
    #      of _run_deploy_job (eager_caddy) wrote the placeholder but
    #      step 8 (db_row) never ran. Without this reaper the public
    #      URL would stay stuck on the "Deploying..." page forever.
    #
    #   2. Stale .staging-* dirs -- copytree staging dirs that were
    #      started but the rename never ran. Older than 1 hour so we
    #      don't disturb an in-flight deploy that just started.
    #
    #   3. Orphan systemd units -- ojas-app-<slug>.service files for
    #      slugs that no longer have a deployed_apps row. The
    #      deployed-app delete path stops+disables+rm's the unit, but
    #      rows that predate that cleanup (or rows that were hand-
    #      removed from the DB) leak the unit. A leaked unit holds
    #      its TCP port forever, blocking the next deploy that
    #      allocator gives the same port (the first free one, which
    #      is always the lowest free port in 9100-9899). Symptoms:
    #      a freshly deployed app's uvicorn crashes with
    #      "Errno 98 address already in use" and the subdomain
    #      quietly serves whatever stale backend was last holding
    #      that port. We re-create the slug from the unit filename
    #      (ojas-app-<slug>.service) and use the same
    #      _remove_app_service_files() helper the live delete paths
    #      use, so we get exactly the same stop+disable+rm+reload
    #      sequence -- no duplicated logic.
    try:
        with db._connect() as cx:   # noqa: SLF001
            live_rows = cx.execute(
                "SELECT slug, service_name FROM deployed_apps"
            ).fetchall()
        live_slugs = {r["slug"] for r in live_rows}
        live_unit_names = {
            r["service_name"] for r in live_rows if r["service_name"]
        }
        reaped_fragments = 0
        if OJAS_CADDY_ROUTES_DIR.exists():
            for f in OJAS_CADDY_ROUTES_DIR.glob("*.caddy"):
                if f.stem not in live_slugs:
                    try:
                        f.unlink()
                        reaped_fragments += 1
                    except OSError:
                        pass
        if reaped_fragments:
            _reload_caddy()
        reaped_staging = 0
        if OJAS_APPS_ROOT.exists():
            cutoff = time.time() - 3600
            for d in OJAS_APPS_ROOT.glob(".staging-*"):
                try:
                    if d.is_dir() and d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                        reaped_staging += 1
                except OSError:
                    pass
        reaped_units = 0
        if OJAS_APPS_UNIT_DIR.exists():
            for unit_file in OJAS_APPS_UNIT_DIR.glob("ojas-app-*.service"):
                unit_name = unit_file.name
                # If the DB still has a row pointing at this unit, the
                # unit belongs to a live app -- leave it alone. (We
                # check unit_name, not slug, because a row's
                # service_name is the authoritative unit name; the
                # slug might have changed if someone hand-renamed.)
                if unit_name in live_unit_names:
                    continue
                # Otherwise: derive the slug from the unit name and
                # tear it down via the same helper the live delete
                # path uses. The helper swallows per-step errors.
                slug = unit_name[len("ojas-app-"):-len(".service")]
                _remove_app_service_files(slug, None)
                reaped_units += 1
        # 3b. Dangling symlinks in multi-user.target.wants/. The glob
        #     above only catches files directly in /etc/systemd/system/
        #     (the canonical unit location). On boxes that predate the
        #     current cleanup, the symlink in multi-user.target.wants/
        #     survives even after the target unit file is gone. The
        #     symlink is harmless (systemd auto-disables a broken link)
        #     but we reap it for cleanliness, and so `systemctl list-unit-files`
        #     stops showing it as a zombie.
        #
        #     Note: this dir is root-owned 0755, so the ojas user can't
        #     unlink from it. Route through the setuid helper (the same
        #     one used for Caddy reloads + unit writes) which validates
        #     the name pattern and runs as root.
        wants_dir = Path("/etc/systemd/system/multi-user.target.wants")
        if wants_dir.exists():
            for symlink in wants_dir.glob("ojas-app-*.service"):
                try:
                    if not symlink.resolve().exists():
                        # Dangling — ask the helper to unlink it. The
                        # helper re-validates the name + lstat/realpath
                        # so this is safe to call for every symlink in
                        # the dir (a live symlink is a no-op).
                        import subprocess
                        subprocess.run(
                            ["/usr/local/sbin/ojas-systemd-helper",
                             "rm-wants-symlink", symlink.name],
                            check=False, capture_output=True, timeout=5,
                        )
                        reaped_units += 1
                except OSError:
                    pass
        # 4. Orphan app dirs at /opt/ojas-apps/<slug>/. THIS is the
        #    piece that was missing before — Caddy's wildcard block
        #    serves ANY <slug>.<root> from /opt/ojas-apps/<slug>/ with
        #    no DB lookup, so a dir that survives a session/user delete
        #    keeps the URL alive. Reap any dir whose slug is not in
        #    the deployed_apps table. We also reap /opt/ojas-apps/
        #    .stopped/<slug>/ for the same reason (paused apps whose
        #    row was deleted without first un-pausing).
        reaped_orphan_dirs = 0
        if OJAS_APPS_ROOT.exists():
            for d in OJAS_APPS_ROOT.iterdir():
                # Skip the meta dirs we want to keep:
                #   .deploying/  -- in-flight deploy staging
                #   .paused/     -- shared "this app is paused" page
                #   .stopped/    -- paused apps land here (handled below)
                if d.name.startswith("."):
                    continue
                if not d.is_dir():
                    continue
                if d.name in live_slugs:
                    continue  # live app — leave it alone
                # No matching DB row → orphan. rmtree so the Caddy
                # wildcard block 404s the URL on the next request.
                # ignore_errors=True so a single bad dir (chmod 000,
                # NFS hiccup, bind-mount) doesn't block the rest.
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    reaped_orphan_dirs += 1
                except OSError:
                    pass
        stopped_root = OJAS_APPS_STOPPED_DIR
        if stopped_root.exists():
            for d in stopped_root.iterdir():
                if not d.is_dir():
                    continue
                if d.name in live_slugs:
                    continue
                try:
                    shutil.rmtree(d, ignore_errors=True)
                    reaped_orphan_dirs += 1
                except OSError:
                    pass
        # 4b. Reap any ojas_services ghost row whose slug is no longer
        #     live AND whose on-disk app dir is gone. The earlier ghost
        #     reaper (in _register_ojas_services) only checks the DB —
        #     if the deployed_apps row was dropped without dropping the
        #     ojas_services row, the panel shows a "still running" entry
        #     for an app whose dir is gone. Belt + suspenders: this
        #     extra sweep drops the ojas_services row whenever the
        #     dir on disk is missing, which is the truest source of
        #     "this app is gone" (Caddy's wildcard serves from there).
        reaped_ghost_services = 0
        try:
            with db._connect() as cx:
                ghost_svcs = [
                    r[0] for r in cx.execute(
                        "SELECT id FROM ojas_services WHERE source = 'ojas-deployed'"
                    )
                    if r[0].split(":", 1)[1] not in live_slugs
                ]
                for gid in ghost_svcs:
                    slug = gid.split(":", 1)[1]
                    app_dir = OJAS_APPS_ROOT / slug
                    stopped_dir = stopped_root / slug
                    if app_dir.exists() or stopped_dir.exists():
                        # Dir is still on disk — the orphan-dir reaper
                        # above should have reaped it; if it didn't, we
                        # don't either. Let the user notice and we'll
                        # re-investigate.
                        continue
                    cx.execute("DELETE FROM ojas_services WHERE id = ?", (gid,))
                    reaped_ghost_services += 1
        except Exception:
            pass
        if (reaped_fragments or reaped_staging or reaped_units
                or reaped_orphan_dirs or reaped_ghost_services):
            print(
                f"[ojas] boot orphan cleanup: reaped {reaped_fragments} "
                f"Caddy fragments, {reaped_staging} stale staging dirs, "
                f"{reaped_units} orphan systemd units, "
                f"{reaped_orphan_dirs} orphan app dirs, "
                f"{reaped_ghost_services} ghost ojas_services rows"
            )
    except Exception:
        # Best-effort; if the reaper itself fails, the next deploy's
        # Caddy fragment gen will overwrite the orphan, and the user
        # can re-deploy to clear stale staging.
        pass

def _register_ojas_services() -> None:
    """Idempotently register the Ojas-owned runtime services (main backend,
    caddy reverse proxy) in `ojas_services` so the admin panel can show
    them. Called once on backend boot. Port-only rows for deployed apps
    are also created/refreshed here so the panel always reflects the
    truth on disk under /opt/ojas-apps/."""
    # 0) Reap ghost rows FIRST so the rest of registration starts
    #    from a clean table. A ghost is an `ojas_services` row with
    #    source='ojas-deployed' whose slug isn't in `deployed_apps`
    #    anymore -- usually because a session delete cleaned the
    #    deployed_apps row but missed the ojas_services row, or
    #    because of a half-finished delete from a prior boot. The
    #    settings + admin panels both render this table directly,
    #    so a ghost row shows a deleted app as still alive. The
    #    deployment-time + per-app-delete + session-delete paths
    #    ALSO drop the row, but this boot-time sweep is the safety
    #    net for any future drift.
    try:
        with db._connect() as cx:   # noqa: SLF001
            live_slugs = {
                r[0] for r in cx.execute("SELECT slug FROM deployed_apps")
            }
            ghost_ids = [
                r[0] for r in cx.execute(
                    "SELECT id FROM ojas_services "
                    "WHERE source = 'ojas-deployed'"
                )
                if r[0].split(":", 1)[1] not in live_slugs
            ]
            for gid in ghost_ids:
                cx.execute("DELETE FROM ojas_services WHERE id = ?", (gid,))
            if ghost_ids:
                print(f"[ojas-boot] reaped {len(ghost_ids)} ghost ojas_services rows: {ghost_ids}")
    except Exception as e:
        print(f"[ojas-boot] ojas_services reconcile failed (non-fatal): {e}")

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
# No-cache everywhere — user-facing policy.
# The user wants zero caching across the stack: every response must be
# re-fetched from the server on every request. This middleware:
#   1. Strips any Cache-Control / ETag / Last-Modified the app layer set
#      (we want one consistent policy, not "did this route forget?").
#   2. Strips the ETag + Last-Modified from the request side too, so the
#      browser can't use 304 If-None-Match to short-circuit a fetch.
#   3. Sets Cache-Control: no-store, must-revalidate on every response.
#      `no-store` tells the browser "don't keep a copy at all";
#      `must-revalidate` is belt-and-braces for any proxy that ignores it.
# Trade-offs accepted: every navigation re-downloads the JS bundle
# (~500KB), every API call hits the DB, no offline launch from the
# home-screen icon. The user explicitly asked for this.
# ============================================================================
class NoCacheEverywhere:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Strip the conditional-request headers from the incoming request
        # so the server can't return 304 for a cached representation.
        if scope["type"] == "http":
            headers = scope.get("headers") or []
            stripped = []
            for name, value in headers:
                lname = name.decode("latin-1").lower() if isinstance(name, bytes) else name.lower()
                if lname in ("if-none-match", "if-modified-since", "if-match", "if-unmodified-since", "if-range"):
                    continue
                stripped.append((name, value))
            scope["headers"] = stripped

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # Drop any Cache-Control the route set, then add ours.
                new_headers = []
                for name, value in message.get("headers", []):
                    lname = name.decode("latin-1").lower() if isinstance(name, bytes) else name.lower()
                    if lname in ("cache-control", "etag", "last-modified", "expires", "age", "vary"):
                        continue
                    new_headers.append((name, value))
                new_headers.append((b"cache-control", b"no-store, must-revalidate"))
                new_headers.append((b"pragma", b"no-cache"))
                message["headers"] = new_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(NoCacheEverywhere)

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

def _purge_one_deployed_app(slug: str, app_row: dict) -> bool:
    """Single-app cleanup. Reused by:
      • _purge_deployed_apps_for_session (per session delete)
      • admin_user_delete (per user delete, as a safety net for
        deployed apps whose source_session_id doesn't match any of
        the user's live sessions — legacy rows, apps whose session
        was deleted on a previous run, etc.)

    What "cleanup" means: the app leaves NO trace behind. The
    systemd unit is stopped + removed (fullstack only), the per-slug
    Caddy fragment is unlinked, the on-disk app dir is rmtree'd
    (both the live /opt/ojas-apps/<slug>/ AND the
    /opt/ojas-apps/.stopped/<slug>/ fallback if it was paused), the
    deployed_apps row is dropped, and the matching ojas_services
    row is dropped. After this returns, the public URL
    (https://<slug>.<host>/) stops resolving and the port is free.

    Returns True iff at least one Caddy fragment was unlinked — the
    caller is expected to reload Caddy once at the end of a batch
    so we don't pay N reloads for N apps.

    All steps are best-effort: each is wrapped in its own try/except
    so one bad row (e.g. rmtree failing because the dir is on a read-
    only mount) doesn't poison the rest of the batch. Failures are
    logged at WARNING via the module logger so the admin can see
    what went sideways after the fact.
    """
    caddy_changed = False
    try:
        # Tear down the systemd unit (fullstack) so the process
        # releases its files AND its bound port before we rmtree.
        # _remove_app_service_files handles the case where the row
        # predates the service_name column (derives the unit name
        # from the slug), and also disables + removes the unit
        # file so it doesn't come back on the next boot. v1 static
        # apps have no service_name and no unit file, so this is
        # effectively a no-op for them.
        _remove_app_service_files(slug, app_row or {})
    except Exception:
        logger.exception(
            "_purge_one_deployed_app: systemd unit cleanup failed for %s", slug,
        )
    try:
        # Unlink the per-slug Caddy fragment so the subdomain stops
        # resolving. Without this the Caddy block stays live and the
        # URL keeps serving the now-orphaned /opt/ojas-apps/<slug>/
        # dir. (The wildcard Caddy block in the main Caddyfile
        # doesn't include /apps/<slug>/ so without the fragment
        # there's no route at all — the request 404s.)
        caddy_frag = OJAS_CADDY_ROUTES_DIR / f"{slug}.caddy"
        if caddy_frag.exists():
            caddy_frag.unlink()
            caddy_changed = True
    except OSError:
        logger.exception(
            "_purge_one_deployed_app: caddy fragment unlink failed for %s", slug,
        )
    # The app dir is at /opt/ojas-apps/<slug>/. If the app was
    # paused, the dir is at /opt/ojas-apps/.stopped/<slug>/
    # instead — wipe both. Use explicit try/except (NOT
    # ignore_errors=True) so silent rmtree failures get logged.
    # If shutil.rmtree raises (foreign-uid dir, chmod 0, read-only
    # mount, bind-mount), fall back to the setuid helper which
    # runs as root and can chmod/rm -rf anything under
    # /opt/ojas-apps/<slug>/. Without this fallback the dir
    # survives and the Caddy wildcard block keeps serving the URL
    # (the my-portfolio + stock-demo ghost-row bug from 2026-06-11).
    for d in (app_row.get("app_dir"), str(OJAS_APPS_STOPPED_DIR / slug)):
        if d and Path(d).exists():
            try:
                shutil.rmtree(d)
            except Exception as rmt_err:
                logger.warning(
                    "_purge_one_deployed_app: shutil.rmtree(%s) failed (%s); "
                    "falling back to force-rmtree helper",
                    d, rmt_err,
                )
                try:
                    import subprocess
                    subprocess.run(
                        ["/usr/local/sbin/ojas-systemd-helper",
                         "force-rmtree", d],
                        check=True, capture_output=True, timeout=60,
                    )
                except Exception as helper_err:
                    logger.exception(
                        "_purge_one_deployed_app: force-rmtree helper also "
                        "failed for %s (slug=%s)",
                        d, slug,
                    )
                    raise
    try:
        db.delete_deployed_app(slug)
    except Exception:
        logger.exception(
            "_purge_one_deployed_app: delete_deployed_app(%s) failed", slug,
        )
    # Drop the matching ojas_services row too — the Admin panel's
    # "services & ports" view reads from this table, so a stale row
    # here shows a deleted app as still "running". The per-app
    # DELETE handler does this; the session-delete path used to
    # skip it, leaving ghost rows in the admin panel until manual
    # cleanup.
    try:
        db.delete_ojas_service(f"deployed:{slug}")
    except Exception:
        logger.exception(
            "_purge_one_deployed_app: delete_ojas_service(deployed:%s) failed",
            slug,
        )
    return caddy_changed


def _purge_deployed_apps_for_session(session_id: str) -> None:
    """SIGTERM-nothing, but rmtree every deployed_apps `app_dir` rooted in
    this session AND delete the DB rows so the public subdomain stops
    resolving. Best-effort.

    This is the SYNC delete path (DELETE /api/sessions/{id}). The async
    delete job (_run_delete_job step 3) does the same work inline; keep
    the two in sync. Both versions unlink the per-slug Caddy fragment
    and reload Caddy at the end — without the reload, a previously-
    deleted session's URL keeps serving the now-orphaned app dir.
    """
    caddy_changed = False
    try:
        # Inline SQL because db.list_deployed_apps doesn't filter by session.
        with db._connect() as cx:   # noqa: SLF001 -- internal helper, fine here
            rows = cx.execute(
                "SELECT * FROM deployed_apps WHERE source_session_id = ?",
                (session_id,),
            ).fetchall()
        for row in rows:
            row_dict = dict(row)
            if _purge_one_deployed_app(row["slug"], row_dict):
                caddy_changed = True
        # One Caddy reload for the whole session, regardless of how
        # many apps we tore down. Matches the async-delete behaviour.
        if caddy_changed:
            try:
                _reload_caddy()
            except Exception:
                logger.exception(
                    "_purge_deployed_apps_for_session: caddy reload failed for %s",
                    session_id,
                )
    except Exception:
        logger.exception(
            "_purge_deployed_apps_for_session: %s", session_id,
        )

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

@app.delete("/api/projects/{project_id}")
def projects_delete(project_id: str, user: dict = Depends(require_user)):
    """Delete a project AND every cascade target. The workspace files on
    disk under <workspace>/<each session's subdir>/ ARE removed (since
    those were generated for that project). The root workspace path itself
    is untouched if it was a folder you had before Ojas.

    Order matters: cancel agent tasks + run the per-session filesystem /
    DB cleanup BEFORE the cascade delete. That way each session's
    deployed_apps rows + ojas_services rows + caddy fragments are
    removed under the FK still being valid, and a half-finished
    cleanup leaves the project in a recoverable state instead of
    dangling ghost rows."""
    _project_or_404(project_id, user)
    sessions = db.list_sessions(project_id)
    # 1. Cancel any in-flight turn task for these sessions.
    for s in sessions:
        task = _active_turns.get(s["id"])
        if task is not None and not task.done():
            task.cancel()
    # 2. Filesystem + DB cleanup for each session. This drops
    #    deployed_apps rows, ojas_services rows, caddy fragments, and
    #    on-disk dirs -- while the parent session row is still alive
    #    so the FK on source_session_id is happy. _purge_session_everything
    #    does NOT delete the session row itself; the cascade below does.
    for s in sessions:
        _purge_session_everything(s)
    # 3. Project row → cascades to sessions (rows gone after this),
    #    leaves deployed_apps rows already-cleaned (step 2).
    if not db.delete_project(project_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")
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


# ---- Async delete (with progress UI) -------------------------------------
#
# These endpoints are the "new" way to delete a session or project — they
# spawn a background job that walks a fixed 7-step cleanup checklist
# (cancel agent, kill processes, teardown sub-projects, rmtree the
# workspace subdir, drop checkpoint, clear bus, drop rows) and report
# progress via GET /api/{sessions|projects}/{id}/delete-jobs/{job_id}.
#
# The OLD synchronous DELETE handlers above are kept untouched (so
# any custom tooling that POSTs a DELETE continues to work). The UI
# (DeleteProgressModal) uses the new POST endpoints to get the
# progress checklist; the sidebar removes the entry optimistically the
# moment the user confirms, so the UI feels instant even if the
# server-side teardown takes 5+ seconds.

@app.post(
    "/api/sessions/{session_id}/delete",
    response_model=DeleteJobStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sessions_delete_async(
    session_id: str,
    user: dict = Depends(require_user),
):
    """Start an async delete job for a session. Returns 202 with
    {job_id, target_id, steps: [7 default steps]}; the client polls
    /api/sessions/{id}/delete-jobs/{jid} for per-step progress.

    Idempotency: calling this twice for the same session_id while a
    previous job is still running returns 409 (only one delete at a
    time). If the previous job already finished, the new call 404s
    because the session row is gone (which is the right behavior —
    there's nothing left to delete)."""
    import uuid
    # Ownership check first — 404s (not 403) for cross-user access to
    # avoid leaking the existence of other users' sessions.
    _session_or_404(session_id, user)
    with _delete_jobs_lock:
        for existing in _delete_jobs.values():
            if existing.target_kind == "session" and existing.target_id == session_id \
                    and existing.status in ("pending", "running"):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"a delete job for this session is already running "
                    f"({existing.job_id})",
                )
    job_id = uuid.uuid4().hex
    now = int(time.time())
    job = _DeleteJob(
        job_id=job_id, target_kind="session", target_id=session_id,
        user_id=user["id"], created_at=now, updated_at=now,
    )
    _init_delete_job_steps(job, len(_DELETE_STEP_NAMES))
    # Bind a fresh event loop task. Capture the loop first so the
    # task is scheduled on the loop the request is running on (matches
    # the deploy job pattern at app.py:2791-2805).
    bus = get_bus(session_id)
    if not bus.is_bound():
        bus.bind_loop(asyncio.get_running_loop())
    job.task = asyncio.create_task(
        _run_delete_job(
            job=job, target_kind="session", target_id=session_id,
            user_id=user["id"],
        )
    )
    with _delete_jobs_lock:
        _delete_jobs[job_id] = job
    return DeleteJobStartResponse(
        job_id=job_id, target_id=session_id,
        target_kind="session",
        steps=job.snapshot()["steps"],
    )


@app.get(
    "/api/sessions/{session_id}/delete-jobs/{job_id}",
    response_model=DeleteJobStatusResponse,
)
def sessions_delete_job_status(
    session_id: str,
    job_id: str,
    user: dict = Depends(require_user),
):
    """Poll for the per-step status of an in-flight or recently-finished
    session-delete job. Returns 404 if the job_id is unknown OR not
    owned by the caller (to avoid leaking other users' job ids)."""
    job = _delete_job_or_404(job_id, session_id, user)
    snap = job.snapshot()
    return DeleteJobStatusResponse(**snap)


@app.post("/api/sessions/{session_id}/delete-jobs/{job_id}/cancel")
async def sessions_delete_job_cancel(
    session_id: str,
    job_id: str,
    user: dict = Depends(require_user),
):
    """Request cancellation of an in-flight delete job. Idempotent —
    returns {ok: false, reason: ...} if the job is no longer running.
    Best-effort: the worker checks cancel_requested at step boundaries
    and bails out cleanly after the current step finishes."""
    job = _delete_job_or_404(job_id, session_id, user)
    with job._lock:
        if job.status not in ("pending", "running"):
            return {"ok": False, "reason": f"job is {job.status}"}
        job.cancel_requested = True
    return {"ok": True}


@app.post(
    "/api/projects/{project_id}/delete",
    response_model=DeleteJobStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def projects_delete_async(
    project_id: str,
    user: dict = Depends(require_user),
):
    """Start an async delete job for a project (and every session in it).
    The step list is 7 * N where N is the number of sessions in the
    project (or 0 if the project is empty)."""
    import uuid
    _project_or_404(project_id, user)
    with _delete_jobs_lock:
        for existing in _delete_jobs.values():
            if existing.target_kind == "project" and existing.target_id == project_id \
                    and existing.status in ("pending", "running"):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"a delete job for this project is already running "
                    f"({existing.job_id})",
                )
    sessions = db.list_sessions(project_id)
    job_id = uuid.uuid4().hex
    now = int(time.time())
    job = _DeleteJob(
        job_id=job_id, target_kind="project", target_id=project_id,
        user_id=user["id"], created_at=now, updated_at=now,
    )
    # 7 steps per session. For an empty project (no sessions) we
    # still want a single 'finalize' step so the UI shows the user
    # something is happening — the worker will skip straight to the
    # final delete_project call.
    step_count = max(len(sessions) * len(_DELETE_STEP_NAMES), 1)
    _init_delete_job_steps(job, step_count)
    # Cancel any in-flight agent turns across all the project's
    # sessions (mirrors what the sync handler did at app.py:1021-1023).
    for s in sessions:
        task = _active_turns.get(s["id"])
        if task is not None and not task.done():
            task.cancel()
    job.task = asyncio.create_task(
        _run_delete_job(
            job=job, target_kind="project", target_id=project_id,
            user_id=user["id"],
        )
    )
    with _delete_jobs_lock:
        _delete_jobs[job_id] = job
    return DeleteJobStartResponse(
        job_id=job_id, target_id=project_id,
        target_kind="project",
        steps=job.snapshot()["steps"],
    )


@app.get(
    "/api/projects/{project_id}/delete-jobs/{job_id}",
    response_model=DeleteJobStatusResponse,
)
def projects_delete_job_status(
    project_id: str,
    job_id: str,
    user: dict = Depends(require_user),
):
    job = _delete_job_or_404(job_id, project_id, user)
    snap = job.snapshot()
    return DeleteJobStatusResponse(**snap)


@app.post("/api/projects/{project_id}/delete-jobs/{job_id}/cancel")
async def projects_delete_job_cancel(
    project_id: str,
    job_id: str,
    user: dict = Depends(require_user),
):
    job = _delete_job_or_404(job_id, project_id, user)
    with job._lock:
        if job.status not in ("pending", "running"):
            return {"ok": False, "reason": f"job is {job.status}"}
        job.cancel_requested = True
    return {"ok": True}


@app.get("/api/sessions/{session_id}", response_model=SessionResponse)
def sessions_get(session_id: str, user: dict = Depends(require_user)):
    """Fetch a single session's current state. Used by the chat page
    after turn_summary to re-read the (potentially LLM-renamed) name --
    the WS session_renamed event isn't 100% reliable on flaky mobile
    networks, so the frontend polls as a safety net."""
    session = _session_or_404(session_id, user)
    return SessionResponse(**session)


@app.get("/api/sessions/{session_id}/llm-trace")
def sessions_llm_trace(session_id: str, user: dict = Depends(require_root)):
    """Return the recent LLM call trace for this session. Each entry
    is one wire-level request/response pair: full prompt, full
    response, usage_metadata, duration, model name. Capped at the
    50 most-recent calls (memory.llm_trace.MAX_RECORDS).
    The trace is process-local; it does not survive a backend restart.
    The full message history is still in the LangGraph checkpointer —
    this is the wire-level audit log only.

    Admin-only: the trace exposes the full system prompt, tool defs, and
    every message in the conversation. The frontend already hides the
    ⌥ llm button for non-admins, but the server enforces it too so a
    crafted client can't bypass the UI.
    """
    session = _session_or_404(session_id, user)
    from memory.llm_trace import get_store
    store = get_store()
    records = store.list(session_id)
    return {
        "session_id": session_id,
        "count": len(records),
        "calls": [r.to_json() for r in records],
    }


@app.delete("/api/sessions/{session_id}/llm-trace")
def sessions_llm_trace_clear(session_id: str, user: dict = Depends(require_root)):
    """Clear the LLM call trace buffer for this session. Useful for
    'start clean' before a fresh debugging run. Admin-only."""
    session = _session_or_404(session_id, user)
    from memory.llm_trace import get_store
    get_store().clear(session_id)
    return {"ok": True, "session_id": session_id}

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
def sessions_list(
    project_id: str,
    user: dict = Depends(require_user),
):
    _project_or_404(project_id, user)
    rows = db.list_sessions(project_id)
    return [SessionResponse(**s) for s in rows]

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

    # If a turn is already running for this session, cancel it and
    # wait for the cancellation to fully finalise BEFORE scheduling
    # the new turn. This is the Claude-Code-style "stop and answer"
    # behaviour: the user types a new question while a task is in
    # flight, hits Enter, and the agent stops what it was doing
    # and serves the new question — instead of:
    #   (a) ignoring the new question until the old turn finishes
    #       (the bug this fixes), or
    #   (b) spawning a SECOND concurrent run_turn task that races
    #       the first for checkpoint ownership and interleaves its
    #       assistant_text into the old turn's history.
    # The cancel-and-wait serialises them: T1 fully closes (with
    # its [cancelled by user] breadcrumb), then T2 starts on a
    # clean checkpoint. The user sees the old turn's response end
    # with "■ stopped", then the new turn's response to the new
    # question. CancelledError + turn_summary + DB write are all
    # done by session_runner's CancelledError handler — we just
    # await the task to ensure they all land before T2 begins.
    prev = _active_turns.get(session_id)
    if prev is not None and not prev.done():
        prev.cancel()
        try:
            # Bounded wait so a wedged worker thread (the Python
            # thread-cancel limitation noted in cancel_turn's
            # docstring) can't deadlock the new submission. The
            # worker thread can keep dripping events for a few
            # seconds after cancel, but the asyncio task itself
            # resolves as soon as the CancelledError handler in
            # run_turn has written the [cancelled by user] row
            # and emitted turn_summary. 2s is plenty for that.
            await asyncio.wait_for(asyncio.shield(prev), timeout=2.0)
        except asyncio.TimeoutError:
            # CancelledError was raised but the run_turn cleanup
            # didn't finish in time. Move on anyway — the new turn
            # will land in its own asyncio.Task and the bus will
            # still serialise events per-publisher. The old turn's
            # finalization will continue in the background and
            # naturally end the task; the done_callback below
            # still pops _active_turns.
            pass
        except (asyncio.CancelledError, Exception):
            # The task was already cancelled and is now finishing
            # up. We don't care about the outcome — we just needed
            # the cleanup to make progress before T2 starts.
            pass
        # The done_callback below will pop _active_turns once the
        # cancelled task truly ends. We don't pop it here because
        # the task object is still referenced by the callback's
        # closure and may not have run its cleanup yet.

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

# ---------------------------------------------------------------------------
# Deploy jobs (in-memory)
# ---------------------------------------------------------------------------
#
# Each POST /api/sessions/<sid>/deploy spawns a background task that walks
# 13 steps (validate, eager_caddy, copy_dist, inject_pwa, copy_backend,
# service, write DB row, regen Caddy, start service, finalize). The
# frontend polls GET /deploy-jobs/<job_id> for the per-step status and
# the eventual result. This mirrors the _active_turns pattern used by
# the chat runner so cancel + done-callback + per-session registry all
# follow the same shape.
#
# Stays in memory only. If the backend restarts mid-deploy, the in-memory
# entry is lost and any in-flight GET returns 404; the boot-time orphan
# reaper in _reconcile_deployed_apps_on_boot() picks up the half-deployed
# on-disk state and cleans it up.
import threading as _threading
from dataclasses import dataclass, field

# Number of named steps the UI checklist always renders. Must match
# the 13 entries emitted in _run_deploy_job; counted at import time
# so an out-of-sync build fails fast rather than silently rendering
# a 4-step checklist for a fullstack deploy.
_DEPLOY_STEP_COUNT = 13

@dataclass
class _DeployJob:
    job_id: str
    session_id: str
    user_id: str
    slug: str
    created_at: int
    updated_at: int
    status: str = "pending"          # pending|running|succeeded|failed|cancelled
    phase: str = "queued"
    steps: list[dict] = field(default_factory=list)
    error: str | None = None
    result: dict | None = None       # populated only on succeeded
    task: asyncio.Task | None = None
    # Set by _set_terminal to int(time.time()) on succeeded/failed/cancelled.
    # Used by the periodic sweeper to drop completed jobs after a TTL so
    # the in-memory dict doesn't grow unbounded for users who deploy
    # many apps in one session. None while the job is still running.
    completed_at: int | None = None
    _lock: _threading.RLock = field(default_factory=_threading.RLock)

    def snapshot(self) -> dict:
        """Thread-safe read of the job state for the GET endpoint.
        Snapshots the steps list (deep copy) so the caller can render
        it without worrying about mid-poll mutation."""
        with self._lock:
            steps_copy = [dict(s) for s in self.steps]
            return {
                "job_id":       self.job_id,
                "session_id":   self.session_id,
                "slug":         self.slug,
                "status":       self.status,
                "phase":        self.phase,
                "steps":        steps_copy,
                "error":        self.error,
                "result":       self.result,
                "created_at":   self.created_at,
                "updated_at":   self.updated_at,
                "completed_at": self.completed_at,
            }

_deploy_jobs: dict[str, _DeployJob] = {}
_deploy_jobs_lock = _threading.Lock()

def _deploy_job_or_404(job_id: str, session_id: str, user: dict) -> _DeployJob:
    """Lookup + ownership check. 404s if missing OR not owned by caller
    (not 403, to avoid leaking other users' job ids). Mirrors the
    _session_or_404 / _deployed_app_or_404 ownership rules."""
    with _deploy_jobs_lock:
        job = _deploy_jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deploy job not found")
    if user["role"] != "root" and job.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deploy job not found")
    if job.session_id != session_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "deploy job not found")
    return job


async def _sweep_completed_deploy_jobs() -> None:
    """Periodic background task: drop completed deploy jobs from the
    in-memory registry after a 5-minute TTL. Necessary so the dict
    doesn't grow unbounded for users who deploy many apps in one
    session. The TTL is generous (5 min) so a re-poll by the user
    (e.g. they navigate back to the chat 30 seconds after the modal
    closed) still sees the canonical result before the entry is swept.

    Without this TTL the registry would only ever lose entries when
    the next deploy overwrote them (since job_id is a fresh UUID
    per-deploy, that's never). Bounded memory in practice."""
    COMPLETED_TTL_SECONDS = 300
    SWEEP_INTERVAL_SECONDS = 60
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        cutoff = int(time.time()) - COMPLETED_TTL_SECONDS
        stale_ids: list[str] = []
        with _deploy_jobs_lock:
            for jid, job in _deploy_jobs.items():
                # Only sweep jobs that are in a terminal state AND have
                # been terminal for at least the TTL. We tolerate the
                # race where job.completed_at is None (task crashed
                # before _set_terminal ran) by also sweeping status
                # in {succeeded, failed, cancelled} with no completion
                # time set, as long as updated_at is old.
                if job.completed_at is not None and job.completed_at < cutoff:
                    stale_ids.append(jid)
                elif job.status in ("succeeded", "failed", "cancelled") and \
                     job.completed_at is None and job.updated_at < cutoff:
                    stale_ids.append(jid)
        for jid in stale_ids:
            with _deploy_jobs_lock:
                _deploy_jobs.pop(jid, None)
        if stale_ids:
            print(f"[ojas] swept {len(stale_ids)} stale deploy job(s) (TTL {COMPLETED_TTL_SECONDS}s)")


def _start_deploy_job_sweeper() -> asyncio.Task:
    """Spawn the periodic sweeper. Idempotent — only one instance runs
    at a time per backend process. Called once from lifespan()."""
    if getattr(_start_deploy_job_sweeper, "_task", None) is not None:
        return _start_deploy_job_sweeper._task  # type: ignore[return-value]
    task = asyncio.create_task(_sweep_completed_deploy_jobs())
    _start_deploy_job_sweeper._task = task  # type: ignore[attr-defined]
    return task


# ============================================================================
# Delete-job pattern — mirrors the deploy-job pattern above. Each session
# (or project) delete kicks off a background task that walks a fixed list
# of cleanup steps (cancel agent, kill processes, tear down sub-projects,
# rmtree the workspace subdir, drop checkpoint, clear bus, drop rows). The
# UI shows the steps in a checklist (DeleteProgressModal) while the sidebar
# removes the entry optimistically the moment the user confirms.
# ============================================================================

# Number of named steps PER SESSION in a delete job. For a project delete
# the steps list is 7 * N where N is the number of sessions in the project.
# Kept here so the UI's fallback label array (DeleteProgressModal) can
# match it without a round-trip.
_DELETE_STEP_NAMES: list[tuple[str, str]] = [
    ("cancel_agent",      "Cancelling agent"),
    ("kill_processes",    "Killing spawned processes"),
    ("teardown_subprojects", "Tearing down sub-projects"),
    ("rmtree_subdir",     "Removing workspace files"),
    ("drop_checkpoint",   "Dropping agent checkpoint"),
    ("clear_bus",         "Clearing event bus"),
    ("drop_rows",         "Removing database rows"),
]


@dataclass
class _DeleteJob:
    job_id: str
    target_kind: str             # "session" | "project"
    target_id: str
    user_id: str
    created_at: int
    updated_at: int
    status: str = "pending"      # pending|running|succeeded|failed|cancelled
    phase: str = "queued"
    steps: list[dict] = field(default_factory=list)
    error: str | None = None
    # If the job failed mid-flight, the original (pre-delete) rows that
    # would let the UI restore the sidebar entry on failure. None for
    # sessions (the optimistic removal has nothing to restore from
    # server-side), or for jobs that haven't been started yet.
    restore_apps: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None
    completed_at: int | None = None
    _lock: _threading.RLock = field(default_factory=_threading.RLock)
    # Set to True when the user (or a caller) requests cancellation.
    # The worker checks this at each step boundary and bails out cleanly
    # after the current step finishes. Unlike deploy jobs (which we
    # don't bother cancelling — a deploy is fast), delete jobs are
    # sometimes long (5+ seconds for a fullstack app's systemd tear-down)
    # so cancel is worth supporting.
    cancel_requested: bool = False

    def snapshot(self) -> dict:
        """Thread-safe read of the job state for the GET endpoint."""
        with self._lock:
            steps_copy = [dict(s) for s in self.steps]
            return {
                "job_id":       self.job_id,
                "target_kind":  self.target_kind,
                "target_id":    self.target_id,
                "status":       self.status,
                "phase":        self.phase,
                "steps":        steps_copy,
                "error":        self.error,
                "created_at":   self.created_at,
                "updated_at":   self.updated_at,
                "completed_at": self.completed_at,
            }

_delete_jobs: dict[str, _DeleteJob] = {}
_delete_jobs_lock = _threading.Lock()


def _delete_job_or_404(job_id: str, target_id: str, user: dict) -> _DeleteJob:
    """Lookup + ownership check. 404s if missing OR not owned by caller
    (not 403, to avoid leaking other users' job ids). Mirrors the
    _deploy_job_or_404 ownership rule."""
    with _delete_jobs_lock:
        job = _delete_jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delete job not found")
    if user["role"] != "root" and job.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delete job not found")
    if job.target_id != target_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delete job not found")
    return job


def _init_delete_job_steps(job: _DeleteJob, step_count: int) -> None:
    """Pre-populate job.steps with `step_count` pending entries. The
    actual names+labels are filled in by the worker as it runs (because
    the teardown_subprojects step's message includes the slug being
    torn down at runtime)."""
    with job._lock:
        for i in range(step_count):
            # Module index for default label fallback.
            mod_i = i % len(_DELETE_STEP_NAMES)
            name, label = _DELETE_STEP_NAMES[mod_i]
            job.steps.append({
                "name": name,
                "label": label,
                "status": "pending",
                "message": None,
                "started_at": None,
                "finished_at": None,
            })


def _set_delete_step(
    job: _DeleteJob, idx: int, status: str, message: str | None = None,
) -> None:
    """Mutate job.steps[idx] in place. Caller is responsible for
    pre-populating the list (use _init_delete_job_steps). status is one
    of 'running' | 'done' | 'failed'. Thread-safe via job._lock."""
    with job._lock:
        s = job.steps[idx]
        if status == "running" and s.get("started_at") is None:
            s["started_at"] = int(time.time())
        if status in ("done", "failed"):
            s["finished_at"] = int(time.time())
        s["status"] = status
        if message is not None:
            s["message"] = message
        job.updated_at = int(time.time())


def _set_delete_terminal(
    job: _DeleteJob, status: str, error: str | None = None,
) -> None:
    """Mark the job as finished. Sets completed_at so the sweeper can
    drop it after the TTL."""
    with job._lock:
        job.status = status
        job.phase = status
        job.error = error
        job.completed_at = int(time.time())
        job.updated_at = job.completed_at


async def _run_delete_job(
    *,
    job: _DeleteJob,
    target_kind: str,
    target_id: str,
    user_id: str,
) -> None:
    """Worker for a single delete job. Walks the steps in order,
    flipping each pending → running → done/failed, and emits progress
    to the job's snapshot (which the GET poll endpoint reads).

    The order of operations is fixed and matches the cleanup the server
    has always done synchronously in _purge_session_everything. The
    per-step granularity is what the UI shows in its checklist. We do
    NOT abort the entire job on a single step's failure — the underlying
    helpers are best-effort + idempotent, so we mark the failed step,
    log the error, and keep going. The job as a whole lands in 'failed'
    if any step failed.
    """
    any_step_failed = False
    first_error: str | None = None

    try:
        if target_kind == "session":
            sessions_to_purge = [_load_session_for_delete(target_id, user_id)]
        else:  # project
            sessions_to_purge = _load_sessions_for_project_delete(target_id, user_id)

        # Capture deployed-app rows before we tear them down so we can
        # restore the sidebar on failure (server-side rows the UI
        # optimistically removed are mirrored in job.restore_apps).
        with job._lock:
            job.restore_apps = [
                a for s in sessions_to_purge
                for a in db.list_deployed_apps_for_session(s["id"])
            ]

        # Walk the sessions one by one, running the 7-step sequence for each.
        for s_idx, session in enumerate(sessions_to_purge):
            base = s_idx * len(_DELETE_STEP_NAMES)
            sid = session["id"]
            slug_for_label = (sessions_to_purge[0].get("name") if s_idx == 0
                              else f"session {s_idx + 1}/{len(sessions_to_purge)}")

            with job._lock:
                job.phase = f"deleting {slug_for_label}"
                job.updated_at = int(time.time())
            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 1. cancel_agent
            _set_delete_step(job, base + 0, "running")
            try:
                task = _active_turns.get(sid)
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        # Give the agent a beat to honor the cancellation.
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=1.5,
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                _set_delete_step(job, base + 0, "done")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"cancel_agent: {e}"
                _set_delete_step(job, base + 0, "failed", str(e)[:200])

            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 2. kill_processes
            _set_delete_step(job, base + 1, "running")
            try:
                _kill_session_processes(sid)
                _set_delete_step(job, base + 1, "done")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"kill_processes: {e}"
                _set_delete_step(job, base + 1, "failed", str(e)[:200])

            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 3. teardown_subprojects — for each deployed app: stop+rm
            #    systemd unit, drop Caddy fragment, rmtree on disk.
            _set_delete_step(job, base + 2, "running")
            try:
                app_rows = db.list_deployed_apps_for_session(sid)
                teardown_msgs: list[str] = []
                caddy_changed = False
                # 3a. The DB may have ALREADY lost the deployed_apps row
                #     (e.g. an FK cascade fired before this step ran, or a
                #     previous run of the same job partially succeeded).
                #     We can't enumerate those slugs from `app_rows`, so we
                #     additionally scan ojas_services for any
                #     `deployed:<slug>` row tagged with this session — that
                #     covers the ghost-row case (the BookWise / my-portfolio
                #     "URL still alive, panel still shows it" bug from
                #     2026-06-11). The union of the two is the real cleanup
                #     surface for this session.
                with db._connect() as cx:   # noqa: SLF001
                    svc_slugs = [
                        r[0] for r in cx.execute(
                            "SELECT id FROM ojas_services "
                            "WHERE source = 'ojas-deployed' "
                            "AND id IN (SELECT 'deployed:' || slug "
                            "           FROM deployed_apps "
                            "           WHERE source_session_id = ?)",
                            (sid,),
                        )
                    ]
                svc_slugs = [s.split(":", 1)[1] for s in svc_slugs]
                db_slugs = [row["slug"] for row in app_rows]
                all_slugs: list[str] = []
                seen: set[str] = set()
                for s in db_slugs + svc_slugs:
                    if s not in seen:
                        seen.add(s)
                        all_slugs.append(s)
                if all_slugs:
                    print(
                        f"[ojas-delete] teardown_subprojects session={sid} "
                        f"slugs={all_slugs} (db_rows={len(db_slugs)}, "
                        f"svc_rows={len(svc_slugs)})"
                    )
                for slug in all_slugs:
                    row = next((r for r in app_rows if r["slug"] == slug), None) or {}
                    try:
                        # 1. Tear down the systemd unit (fullstack apps
                        #    only) — stop+disable+rm+daemon-reload.
                        print(f"[ojas-delete]   slug={slug} step=unit row_app_dir={row.get('app_dir')!r}")
                        _remove_app_service_files(slug, row)
                        # 2. Unlink the per-slug Caddy fragment inline
                        #    so we can batch the Caddy reload to ONCE at
                        #    the end of the loop instead of once per
                        #    app — N reloads of Caddy for N apps is
                        #    wasteful and can race.
                        caddy_frag = OJAS_CADDY_ROUTES_DIR / f"{slug}.caddy"
                        try:
                            if caddy_frag.exists():
                                caddy_frag.unlink()
                                caddy_changed = True
                        except OSError:
                            pass
                        # 3. Rmtree the on-disk dir(s). We always try
                        #    BOTH the row's app_dir AND the convention
                        #    /opt/ojas-apps/<slug>/ (the Caddy wildcard
                        #    block serves from there regardless of what
                        #    app_dir says, so the dir surviving at the
                        #    convention path keeps the URL alive even
                        #    if app_dir is stale or NULL). Same for the
                        #    .stopped fallback. Use explicit try/except
                        #    with a defensive chmod/chown fallback so a
                        #    transient permission/IO issue can't quietly
                        #    leave the dir behind (the stock-demo /
                        #    my-portfolio ghost bug from 2026-06-11).
                        candidate_dirs = [
                            row.get("app_dir"),
                            str(OJAS_APPS_ROOT / slug),
                            str(OJAS_APPS_STOPPED_DIR / slug),
                        ]
                        for d in candidate_dirs:
                            if not d or not Path(d).exists():
                                continue
                            print(f"[ojas-delete]   slug={slug} step=rmtree dir={d}")
                            try:
                                shutil.rmtree(d)
                                print(f"[ojas-delete]   slug={slug} step=rmtree OK dir={d}")
                            except Exception as rmt_err:
                                # shutil.rmtree can fail when the dir
                                # was created by a different uid (early
                                # deploy), or has chmod 0, or is on a
                                # read-only mount. Fall back to the
                                # setuid helper which runs as root and
                                # can chmod/rm -rf anything under
                                # /opt/ojas-apps/<slug>/. Without this
                                # fallback the dir survives the delete
                                # and keeps the public URL alive (the
                                # stock-demo / my-portfolio ghost-row
                                # bug from 2026-06-11).
                                print(
                                    f"[ojas-delete]   slug={slug} step=rmtree "
                                    f"FAILED dir={d} err={rmt_err} — "
                                    f"falling back to force-rmtree helper"
                                )
                                try:
                                    import subprocess
                                    subprocess.run(
                                        ["/usr/local/sbin/ojas-systemd-helper",
                                         "force-rmtree", d],
                                        check=True, capture_output=True, timeout=60,
                                    )
                                    print(f"[ojas-delete]   slug={slug} step=rmtree retry OK dir={d}")
                                except Exception as retry_err:
                                    print(
                                        f"[ojas-delete]   slug={slug} step=rmtree "
                                        f"RETRY FAILED dir={d} err={retry_err}"
                                    )
                                    raise
                        # 4. Drop the matching ojas_services row. The
                        #    Admin panel's services & ports view reads
                        #    from this table; a stale row here shows
                        #    the deleted app as still "running" even
                        #    though the deployed_apps row, caddy fragment,
                        #    dir, and systemd unit are all gone. The
                        #    per-app DELETE handler also calls this —
                        #    keep both paths in sync.
                        try:
                            db.delete_ojas_service(f"deployed:{slug}")
                            print(f"[ojas-delete]   slug={slug} step=drop_service OK")
                        except Exception as svc_err:
                            print(
                                f"[ojas-delete]   slug={slug} step=drop_service "
                                f"FAILED err={svc_err}"
                            )
                        teardown_msgs.append(f"torn down {slug}")
                    except Exception as e:
                        import traceback
                        teardown_msgs.append(f"{slug}: {e}")
                        print(
                            f"[ojas-delete] teardown_subprojects slug={slug} "
                            f"session={sid} error: {e}\n{traceback.format_exc()}"
                        )
                # One Caddy reload for the whole session, regardless of
                # how many apps we tore down.
                if caddy_changed:
                    try:
                        _reload_caddy()
                    except Exception:
                        pass
                _set_delete_step(
                    job, base + 2, "done",
                    "; ".join(teardown_msgs) if teardown_msgs else "no sub-projects",
                )
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"teardown_subprojects: {e}"
                _set_delete_step(job, base + 2, "failed", str(e)[:200])

            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 4. rmtree_subdir — the agent's edited workspace files.
            _set_delete_step(job, base + 3, "running")
            try:
                _purge_session_workspace_subdir(session)
                _set_delete_step(job, base + 3, "done")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"rmtree_subdir: {e}"
                _set_delete_step(job, base + 3, "failed", str(e)[:200])

            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 5. drop_checkpoint — langgraph state + on-disk state dir.
            _set_delete_step(job, base + 4, "running")
            try:
                _purge_session_state_dir(sid)
                _purge_langgraph_checkpoint(sid)
                _set_delete_step(job, base + 4, "done")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"drop_checkpoint: {e}"
                _set_delete_step(job, base + 4, "failed", str(e)[:200])

            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 6. clear_bus — drop the per-session WebSocket bus.
            _set_delete_step(job, base + 5, "running")
            try:
                _purge_session_bus(sid)
                _set_delete_step(job, base + 5, "done")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"clear_bus: {e}"
                _set_delete_step(job, base + 5, "failed", str(e)[:200])

            if job.cancel_requested:
                _set_delete_terminal(job, "cancelled", "cancelled by user")
                return

            # 7. drop_rows — DB cascade. Done LAST so the rows still
            #    exist during the teardown_subprojects step (the rmtree
            #    on row["app_dir"] needs the row to know where to look).
            _set_delete_step(job, base + 6, "running")
            try:
                if target_kind == "session":
                    if not db.delete_session(sid):
                        raise RuntimeError("session row not found")
                # For a project delete, the per-session rows are dropped
                # together by delete_project() AFTER the per-session
                # filesystem work (matches the old sync handler's order).
                _set_delete_step(job, base + 6, "done")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"drop_rows: {e}"
                _set_delete_step(job, base + 6, "failed", str(e)[:200])

        # Final step for project deletes: drop the project row itself.
        # (For session deletes, the per-session delete_session call above
        # already removed the row.)
        if target_kind == "project":
            try:
                if not db.delete_project(target_id):
                    raise RuntimeError("project row not found")
            except Exception as e:
                any_step_failed = True
                first_error = first_error or f"delete_project: {e}"

        if any_step_failed:
            _set_delete_terminal(job, "failed", first_error)
        else:
            _set_delete_terminal(job, "succeeded")
    except asyncio.CancelledError:
        _set_delete_terminal(job, "cancelled", "cancelled")
        raise
    except Exception as e:
        _set_delete_terminal(job, "failed", f"unexpected: {e}")


def _load_session_for_delete(session_id: str, user_id: str) -> dict:
    """Load a session row + ownership check. Mirrors _session_or_404
    but raises a 500-shaped RuntimeError (not HTTPException) because
    this is called from the background worker, not the request path."""
    sess = db.get_session(session_id)
    if sess is None:
        raise RuntimeError(f"session {session_id} not found")
    # Ownership check: regular users can only delete their own sessions.
    # The HTTP request handler that started this job already 404'd for
    # cross-user access, so this is a defense-in-depth check.
    if sess.get("user_id") and sess["user_id"] != user_id:
        raise RuntimeError(f"not authorized to delete session {session_id}")
    return sess


def _load_sessions_for_project_delete(project_id: str, user_id: str) -> list[dict]:
    """Load every session in a project + ownership check. Same
    defense-in-depth pattern as _load_session_for_delete."""
    proj = db.get_project(project_id)
    if proj is None:
        raise RuntimeError(f"project {project_id} not found")
    if proj.get("user_id") and proj["user_id"] != user_id:
        raise RuntimeError(f"not authorized to delete project {project_id}")
    sessions = db.list_sessions(project_id)
    if not sessions:
        # Empty project — nothing to purge. The delete_project call at
        # the end will still drop the row.
        return []
    return sessions


async def _sweep_completed_delete_jobs() -> None:
    """Periodic background task: drop completed delete jobs after a
    5-minute TTL. Mirrors _sweep_completed_deploy_jobs."""
    COMPLETED_TTL_SECONDS = 300
    SWEEP_INTERVAL_SECONDS = 60
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        cutoff = int(time.time()) - COMPLETED_TTL_SECONDS
        stale_ids: list[str] = []
        with _delete_jobs_lock:
            for jid, djob in _delete_jobs.items():
                if djob.completed_at is not None and djob.completed_at < cutoff:
                    stale_ids.append(jid)
                elif djob.status in ("succeeded", "failed", "cancelled") and \
                     djob.completed_at is None and djob.updated_at < cutoff:
                    stale_ids.append(jid)
        for jid in stale_ids:
            with _delete_jobs_lock:
                _delete_jobs.pop(jid, None)
        if stale_ids:
            print(f"[ojas] swept {len(stale_ids)} stale delete job(s) (TTL {COMPLETED_TTL_SECONDS}s)")


def _start_delete_job_sweeper() -> asyncio.Task:
    """Spawn the periodic sweeper. Idempotent."""
    if getattr(_start_delete_job_sweeper, "_task", None) is not None:
        return _start_delete_job_sweeper._task  # type: ignore[return-value]
    task = asyncio.create_task(_sweep_completed_delete_jobs())
    _start_delete_job_sweeper._task = task  # type: ignore[attr-defined]
    return task


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
    # Emit an initial context_update so the header chip shows immediately on
    # connect (not just on the first LLM call of the next turn). The value
    # comes from the last real `input_tokens` we persisted in this session's
    # row — same number the user saw mid-task, so the chip doesn't snap to
    # a different value on page load. A fresh session with no LLM call yet
    # has no persisted value; we publish 0 so the chip still appears with
    # "0% used" rather than flashing in after the first turn.
    try:
        from memory.checkpointer import (
            _auto_compact_threshold, CONTEXT_WINDOW_TOKENS,
        )
        persisted = db.get_session(session_id)
        used = int((persisted or {}).get("last_context_used") or 0)
        bus.publish("context_update", {
            "used_tokens":  used,
            "budget_tokens": CONTEXT_WINDOW_TOKENS,
            "compacting": False,
            "threshold": int(_auto_compact_threshold()),
        })
    except Exception:
        # Don't let an initial-context hiccup break the WS upgrade
        pass
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

def _public_url_for(slug: str) -> str:
    """Compute the public URL for a deployed sub-app, used to populate
    `DeployedAppResponse.public_url` so the UI doesn't need to know the
    apps-root domain. Always absolute so a non-technical user can
    click it from any page (chat strip, settings, deploy modal).

    Order matches the deploy response builder at line 376:
      1. `https://<slug>.<apps-root>/` when an apps-root is resolved
         (production -- TLS + on-demand cert via the Caddy wildcard).
      2. `/apps/<slug>/` relative path (legacy fallback, used when
         Ojas runs without a public domain -- e.g. localhost dev).
    """
    apps_root = _resolve_apps_root_domain() or _resolve_public_domain()
    if apps_root:
        return f"https://{slug}.{apps_root}/"
    return f"/apps/{slug}/"

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

def _remove_app_service_files(slug: str, row: dict | None = None) -> None:
    """Stop, disable, and remove the systemd unit for a fullstack app.

    Single source of truth for "this deployed app is going away" -- used
    by the API delete handler, the session-purge path, AND the boot-time
    orphan reaper. Does nothing for static apps (no service_name, no
    unit file).

    Resolves the unit name two ways, in order:
      1. row["service_name"] (the column written at deploy time).
      2. f"ojas-app-{slug}.service" (the format _write_systemd_unit uses).
    The fallback covers legacy rows that predate the service_name column
    migration -- a row may be on disk with the right unit but no
    service_name set, and we still want the unit cleaned up.

    All steps are best-effort. `stop` may legitimately fail if the unit
    isn't running; `disable` may fail if the symlink in
    multi-user.target.wants is already gone; `rm-unit` may fail with
    ENOENT. We log + swallow so one misstep doesn't block the rest of
    the cleanup chain.

    `daemon-reload` runs at the end so systemd forgets the unit name
    the next time someone asks "is this enabled?". Without it, a
    removed unit file can linger in systemd's internal cache and
    re-appear in `systemctl list-unit-files` output.
    """
    svc = (row or {}).get("service_name") or f"ojas-app-{slug}.service"
    helper = "/usr/local/sbin/ojas-systemd-helper"
    unit_file = OJAS_APPS_UNIT_DIR / svc
    # Order matters: stop the process first so the bind on its port is
    # released, THEN remove the autostart symlink, THEN remove the file.
    # Doing it the other way risks systemd respawning the unit on the
    # next boot before we can `rm` the file.
    for cmd in (
        [helper, "systemctl", "stop", svc],
        [helper, "systemctl", "disable", svc],
    ):
        try:
            subprocess.run(cmd, check=False, timeout=5, capture_output=True)
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"[ojas] helper {cmd[1:3]} {svc} failed: {e}")
    try:
        if unit_file.exists():
            subprocess.run(
                [helper, "rm-unit", svc],
                check=False, timeout=5, capture_output=True,
            )
        subprocess.run(
            [helper, "systemctl", "daemon-reload"],
            check=False, timeout=5, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[ojas] helper rm-unit/daemon-reload {svc} failed: {e}")


def _prefetch_tls_cert(subdomain_url: str, slug: str) -> None:
    """Trigger Caddy's on-demand TLS flow for a freshly-deployed
    fullstack app so the per-host Let's Encrypt cert lands BEFORE the
    user visits.

    Why this exists: a fullstack app's per-slug Caddy fragment
    declares `tls { on_demand }` -- Caddy only obtains the cert on
    the FIRST user request, and during the 3-5s ACME round-trip it
    serves a temporary cert that the browser flags as "Your
    connection is not private". The user has to hard-refresh a few
    times to clear the warning.

    Fix: make one HEAD request from the server to the new subdomain
    right after the Caddy reload. Caddy's on-demand flow runs in the
    background, the cert lands in its storage (`/var/lib/caddy/...`),
    and by the time the user actually visits, the cert is already
    there. The HEAD is cheap (no body, returns immediately) and
    idempotent -- if the cert is already issued, the request just
    serves normally.

    Best-effort: we never raise out of this function. The deploy
    succeeds even if the cert prefetch fails (DNS hiccup, ACME
    rate-limit, network blip). The user just gets the legacy
    "refresh a few times" experience for that one bad case, which
    is still strictly better than failing the deploy.
    """
    if not subdomain_url:
        return
    try:
        import urllib.request
        # 8s timeout = generous (the user would have given up by now,
        # but Caddy's on-demand is async so this should return in
        # well under a second -- the slow part is the ACME challenge,
        # which happens in Caddy's background goroutine).
        req = urllib.request.Request(subdomain_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=8) as resp:
            # Discard the body. We're not checking the status code
            # either -- a 200 means the cert is ready, a 5xx means
            # the cert fetch is still in flight and the next
            # request will get the cert. Either way, our work here
            # is done.
            _ = resp.status
    except Exception as e:
        # Don't let any error here fail the deploy. Log + move on.
        print(f"[ojas] cert prefetch for {slug} failed: {e}")

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

def _reload_caddy() -> None:
    """Reload the Caddy server config so it picks up freshly-written
    per-slug fragments from /etc/caddy/routes.d/. MUST go through the
    setuid helper at /usr/local/sbin/ojas-systemd-helper — the ojas
    user (which the backend runs as) cannot call /usr/bin/systemctl
    directly. The helper's `systemctl` passthrough accepts any call
    that doesn't have an ojas-app-* arg, so "reload caddy" is allowed
    and runs as root via setuid."""
    try:
        subprocess.run(
            ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "reload", "caddy"],
            check=False, timeout=5, capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _regenerate_caddy_routes_for_user(owner_user_id: str | None) -> None:
    """Re-emit per-slug Caddy fragments for EVERY running app (static
    OR fullstack). The wildcard `*.ojas.karmacode.cloud` block in the
    main Caddyfile does serve static apps, but only from
    `/opt/ojas-apps/{labels.0}/` — and since fullstack apps keep their
    static assets under `static/` (not the slug root), the wildcard
    is incomplete on its own. Every running app gets a per-slug
    fragment so Caddy can match the most-specific site block.

    Static apps get a fragment rooted at `/opt/ojas-apps/<slug>/` with
    SPA fallback and no `/api/*` reverse_proxy (no backend to talk
    to). Fullstack apps get a fragment rooted at
    `/opt/ojas-apps/<slug>/static` with the `/api/*` reverse_proxy
    pointed at their per-app port.

    This function is idempotent: it writes every fragment fresh, so
    a port change or service_name change propagates without manual
    cleanup. The OS-level install is `/etc/caddy/routes.d/<slug>.caddy`,
    included from the main Caddyfile via `import`."""
    OJAS_CADDY_ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    apps_root = _resolve_apps_root_domain() or _resolve_public_domain() or "ojas.example.com"
    # Fetch ALL deployed apps regardless of port/service_name so static
    # apps (which have NULL for both) get a fragment too. The previous
    # version filtered on `port IS NOT NULL AND service_name IS NOT
    # NULL`, which excluded static apps and left them stuck on the
    # in-flight "Deploying..." placeholder forever.
    with db._connect() as cx:   # noqa: SLF001
        if owner_user_id is None:
            rows = cx.execute(
                "SELECT slug, port, service_name, state "
                "FROM deployed_apps"
            ).fetchall()
        else:
            rows = cx.execute(
                "SELECT slug, port, service_name, state "
                "FROM deployed_apps "
                "WHERE (owner_user_id = ? OR owner_user_id IS NULL)",
                (owner_user_id,),
            ).fetchall()
    # Live slugs are the running ones; stopped ones get their
    # fragment wiped so the user-facing URL falls through to the
    # wildcard block (which serves the .paused/ shared page).
    live_slugs = {r["slug"] for r in rows if r["state"] == "running"}
    # Wipe stale fragments (deleted apps, paused apps)
    for f in OJAS_CADDY_ROUTES_DIR.glob("*.caddy"):
        if f.stem not in live_slugs:
            try:
                f.unlink()
            except OSError:
                pass
    for r in rows:
        r = dict(r)
        if r.get("state") != "running":
            # Paused or starting: no per-slug fragment. The wildcard
            # block + /.paused/ + .paused/ in try_files chain handles
            # paused apps.
            continue
        target = OJAS_CADDY_ROUTES_DIR / f"{r['slug']}.caddy"
        port = r.get("port")
        # Determine the shape: fullstack (has port+service_name) or
        # static (neither). Each shape has its own fragment.
        if port and r.get("service_name"):
            # FULLSTACK: per-slug site block with /api/* reverse proxy
            # NOTE: in Caddyfile syntax, the opening `{` of a site block
            # MUST be on the SAME LINE as the site address. Putting it
            # on the next line (as is common in Caddy JSON configs) is
            # a syntax error in the Caddyfile parser. Hence the
            # awkward formatting below. ALSO: the Caddyfile parser
            # requires directives INSIDE a block to be on their own
            # line, so the inner `tls { on_demand }` is multi-line, not
            # the more common one-liner.
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
        else:
            # STATIC: per-slug site block with SPA fallback only. No
            # `/api/*` reverse_proxy because there's no backend. The
            # wildcard block's `{labels.0}` root would also work for
            # static apps, but a per-slug fragment ensures Caddy
            # matches THIS site block first (most-specific wins) and
            # also gives us a single place to add custom headers /
            # cache rules per app if we want to later.
            target.write_text(f"""# Auto-generated for {r['slug']} (static, state=running).
# Do not edit by hand. Regenerated by server/app.py:_regenerate_caddy_routes_for_user.
{r['slug']}.{apps_root} {{
    tls {{
        on_demand
    }}
    encode gzip
    root * /opt/ojas-apps/{r['slug']}
    try_files {{path}} /index.html
    file_server
    header {{
        X-Content-Type-Options "nosniff"
    }}
    @hashed path /assets/*
    header @hashed Cache-Control "no-store, must-revalidate"
    @sw path /sw.js
    header @sw Cache-Control "no-store, must-revalidate"
    @html path_regexp ^\\/$|\\.html$
    header @html Cache-Control "no-store, must-revalidate"
    header Cache-Control "no-store, must-revalidate"
}}
""")
    # Reload Caddy so it picks up the new fragment on its next request
    _reload_caddy()

# Shared "Deploying..." placeholder directory. Holds a single index.html
# served by the eager-Caddy fragment below. Idempotent: _ensure_deploying_page
# is called every time we need the page, and writes only if the content
# is missing or stale.
OJAS_APPS_DEPLOYING_DIR = Path("/opt/ojas-apps/.deploying")

_DEPLOYING_PAGE_BODY = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<title>Deploying...</title>'
    '<meta http-equiv="refresh" content="2">'
    '<style>body{font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif;'
    'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;'
    'background:#0b0b0b;color:#e5e5e5}'
    '.box{text-align:center;max-width:32rem;padding:2rem}'
    'h1{margin:0 0 .75rem 0;font-size:1.5rem;font-weight:500}'
    'p{margin:0;color:#9ca3af;font-size:.9rem;line-height:1.5}'
    '.dot{display:inline-block;width:.5rem;height:.5rem;border-radius:50%;'
    'background:#6ee7b7;margin-right:.5rem;animation:pulse 1.4s infinite}'
    '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}</style></head>'
    '<body><div class="box"><h1><span class="dot"></span>Deploying...</h1>'
    '<p>This app is being published. This page refreshes every 2 seconds. '
    'You can close this tab and come back later.</p></div></body></html>'
)

def _ensure_deploying_page() -> None:
    """Idempotent write of the shared 'Deploying...' HTML. Mirrors
    _ensure_paused_page's content-equality no-op."""
    try:
        OJAS_APPS_DEPLOYING_DIR.mkdir(parents=True, exist_ok=True)
        target = OJAS_APPS_DEPLOYING_DIR / "index.html"
        if target.exists():
            try:
                if target.read_text(encoding="utf-8") == _DEPLOYING_PAGE_BODY:
                    return
            except OSError:
                pass
        target.write_text(_DEPLOYING_PAGE_BODY, encoding="utf-8")
    except OSError:
        # Best-effort: a missing or unreadable placeholder is not
        # fatal. The Caddy fragment still gets written; Caddy will
        # 404 the request and the user sees a Caddy error page,
        # which is better than nothing.
        pass

def _write_caddy_deploying_fragment(slug: str) -> None:
    """Write a minimal per-slug Caddy fragment that serves the
    .deploying/index.html placeholder. NO /api/* reverse_proxy here
    (the DB row doesn't exist yet so we don't know the port).

    The full fragment with /api/* reverse_proxy is written by step 9 of
    _run_deploy_job via _regenerate_caddy_routes_for_user -- this
    function's output is overwritten.

    If the job is cancelled before step 9, the rollback helper unlinks
    this file so the public URL falls back to the wildcard block.

    Self-contained: no DB lookup, no slug-existence check, safe to
    call before the deployed_apps row is written.
    """
    OJAS_CADDY_ROUTES_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_deploying_page()
    apps_root = (_resolve_apps_root_domain()
                 or _resolve_public_domain()
                 or "ojas.example.com")
    target = OJAS_CADDY_ROUTES_DIR / f"{slug}.caddy"
    try:
        target.write_text(
            "# Auto-generated for " + slug + " (in-flight deploy placeholder).\n"
            "# Do not edit by hand. Overwritten by step 9 of\n"
            "# _run_deploy_job via _regenerate_caddy_routes_for_user.\n"
            + slug + "." + apps_root + " {\n"
            # Caddy's parser requires directives inside a `block` to be
            # on their own line. The post-deploy fragment is multi-line
            # for that reason; the in-flight placeholder MUST match or
            # `caddy validate` rejects the whole import chain (and
            # EVERY per-slug fragment stops loading). The first
            # occurrence of this was the `cal` deploy on 2026-06-14:
            # the in-flight one-liner broke the import, the real
            # fragment never got written, and the user saw 404.
            "    tls {\n"
            "        on_demand\n"
            "    }\n"
            "    encode gzip\n"
            "    root * " + str(OJAS_APPS_DEPLOYING_DIR) + "\n"
            "    file_server\n"
            "    header {\n"
            "        X-Content-Type-Options \"nosniff\"\n"
            "        Cache-Control \"no-store\"\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
    except OSError:
        return
    _reload_caddy()

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
    response_model=DeployJobStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sessions_deploy(
    session_id: str,
    req: DeployRequest,
    request: Request,
    user: dict = Depends(require_user),
):
    """ASYNC deploy. Returns 202 with `{job_id, slug, url, placeholder_app}`
    and runs the actual work in a background task. The client polls
    GET /deploy-jobs/{job_id} for the per-step status.

    The synchronous 4xx errors that don't make sense to defer (no dist,
    bad sub-app folder, slug collision) are returned synchronously as
    400/409 — same shape as the old sync endpoint. Everything else
    (the long-running copytree + pip install + systemd work) runs in
    a background task so the FastAPI worker is never blocked.

    Per-step progress is published to the existing SessionBus as
    `deploy_progress` events (both live WebSocket subscribers and the
    events table for reconnect-replay) AND kept in an in-memory
    _DeployJob registry for the GET /deploy-jobs/{job_id} polling
    endpoint.
    """
    import uuid
    session = _session_or_404(session_id, user)
    # 1. Resolve sub-app folder. Same logic as the old sync endpoint;
    #    stays synchronous because the modal already passed the right
    #    value from /detected-dist, and a missing/wrong value is a
    #    user-facing error.
    project_dir = (req.project_dir or "").strip() or None
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
    # 2. Pick a slug. Same collision semantics as the old endpoint:
    #    same-owner re-deploys to the same slug are allowed (atomic
    #    in-place swap); any other collision returns 409 synchronously.
    desired = req.slug if req.slug else session["name"]
    existing = db.get_deployed_app(db._slugify(desired)) if req.slug else None  # noqa: SLF001
    if existing and existing.get("owner_user_id") == user["id"]:
        slug = existing["slug"]
        in_place = True
    else:
        try:
            slug = db.allocate_deployed_slug(desired)
        except db.DeployedSlugTaken as e:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"slug '{e.slug}' is already taken -- pick a different one",
            )
        in_place = False

    # 2a. Enforce: one slug per (session, project_dir). A session can
    #     host N sub-apps (each with its own project_dir), but each
    #     sub-app is locked to a single slug. If the user tries to
    #     re-deploy the same sub-app under a different slug, refuse
    #     with 409 -- the only path to a new slug is: delete the
    #     existing deploy first, then redeploy.
    #
    #     This check is independent of the in-place branch above: that
    #     branch matches by slug (user typed in the same slug), this
    #     branch matches by (session, project_dir) (this sub-app is
    #     already on a different slug). Both can fire for the same
    #     request, in which case the in-place check is the no-op and
    #     this check is the one that returns 409.
    existing_for_subapp = db.get_deployed_app_for_subapp(
        session_id, project_dir,
    )
    if existing_for_subapp and existing_for_subapp["slug"] != slug:
        existing_url = _public_url_for(existing_for_subapp["slug"])
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"this sub-app is already deployed as "
            f"'{existing_for_subapp['slug']}' at {existing_url} -- "
            f"delete the existing deploy first to use a different slug.",
        )

    # 3. Build the public URL (synthesised before the background task
    #    so the 202 response can include it).
    scheme = "https"
    apps_root = _resolve_apps_root_domain() or _resolve_public_domain()
    if apps_root:
        subdomain_url = f"{scheme}://{slug}.{apps_root}/"
    else:
        host = request.headers.get("host", request.url.netloc)
        subdomain_url = f"{request.url.scheme}://{host}/apps/{slug}/"

    # 4. Optimistic placeholder app row. The chat strip splices this in
    #    immediately so the user sees a "starting" pill before the
    #    first poll tick. The canonical row from db.create_deployed_app
    #    (or db.touch_deployed_app) overwrites it once the job succeeds;
    #    the strip's dedupe-by-slug logic handles that.
    placeholder = DeployedAppResponse(
        slug=slug, name=session["name"],
        source_session_id=session_id, source_project_id=session.get("project_id"),
        owner_user_id=user["id"], app_dir=str(OJAS_APPS_ROOT / slug),
        deployed_at=int(time.time()), last_redeploy_at=int(time.time()),
        project_dir=project_dir, state="starting",
        last_state_at=int(time.time()), last_health_at=None,
        error_message=None, service_name=None, port=None,
        public_url=_public_url_for(slug),
    )

    # 5. Register the job + spawn the background task. Mirror the
    #    _active_turns pattern: the done-callback pops the entry from
    #    the dict so a GET after the job finishes returns 404 (the
    #    canonical row in deployed_apps is the source of truth from
    #    then on).
    job_id = uuid.uuid4().hex
    now = int(time.time())
    job = _DeployJob(
        job_id=job_id, session_id=session_id, user_id=user["id"],
        slug=slug, created_at=now, updated_at=now,
    )
    # Bind the bus to the running loop BEFORE the task starts so the
    # WebReporter events flow. (Same pattern as messages_post.)
    bus = get_bus(session_id)
    if not bus.is_bound():
        bus.bind_loop(asyncio.get_running_loop())
    job.task = asyncio.create_task(
        _run_deploy_job(
            job=job,
            session=session,
            project_dir=project_dir,
            in_place=in_place,
            subdomain_url=subdomain_url,
            user_id=user["id"],
        )
    )
    # Note: we deliberately do NOT pop the job from _deploy_jobs when
    # the task finishes. The frontend's first poll can fire a few
    # seconds after the 202 (or the user may re-open the modal and
    # re-poll), and the in-memory row is the only place the per-step
    # result is stored. We sweep stale completed jobs in a background
    # task below (5-minute TTL — generous enough for the user to
    # re-poll if they navigate back, small enough to bound memory).
    with _deploy_jobs_lock:
        _deploy_jobs[job_id] = job
    return DeployJobStartResponse(
        job_id=job_id, slug=slug, url=subdomain_url, placeholder_app=placeholder,
    )

@app.get(
    "/api/sessions/{session_id}/deploy-jobs/{job_id}",
    response_model=DeployJobStatusResponse,
)
def sessions_deploy_job_status(
    session_id: str,
    job_id: str,
    user: dict = Depends(require_user),
):
    """Poll for the per-step status of an in-flight or recently-finished
    deploy. Returns 404 if the job_id is unknown OR not owned by the
    caller (to avoid leaking other users' job ids). The 11-entry
    `steps` list is in a fixed order so the UI checklist is stable
    across polls."""
    job = _deploy_job_or_404(job_id, session_id, user)
    snap = job.snapshot()
    return DeployJobStatusResponse(**snap)

@app.post("/api/sessions/{session_id}/deploy-jobs/{job_id}/cancel")
async def sessions_deploy_job_cancel(
    session_id: str,
    job_id: str,
    user: dict = Depends(require_user),
):
    """Request cancellation of an in-flight deploy. Idempotent — if the
    job is no longer running, returns ok=false with a reason rather
    than failing. Cancellation cooperatively: the task is cancelled at
    its next await, the eager Caddy fragment is dropped, the deploy
    state is set to "cancelled", and the deployed_apps row (if any)
    stays at whatever state it was in. The user can re-deploy with the
    same slug — the row is missing on a fresh app, so
    allocate_deployed_slug will not collide."""
    job = _deploy_job_or_404(job_id, session_id, user)
    task = job.task
    if task is None or task.done():
        return {"ok": False, "reason": "job not running"}
    task.cancel()
    return {"ok": True}

# Step definitions for the deploy job's progress checklist. Kept as a
# module-level constant so _run_deploy_job and the UI both reference
# the same ordered list.
_DEPLOY_STEPS: list[tuple[str, str]] = [
    ("validate",       "Validating build"),
    ("eager_caddy",    "Reserving public URL"),
    ("copy_dist",      "Copying build to /opt/ojas-apps"),
    ("inject_pwa",     "Adding PWA defaults"),
    ("copy_backend",   "Copying backend"),
    ("venv_create",    "Creating virtualenv"),
    ("pip_install",    "Installing Python dependencies"),
    ("write_unit",     "Writing systemd unit"),
    ("enable_service", "Enabling systemd service"),
    ("db_row",         "Recording deployment"),
    ("caddy_regen",    "Configuring reverse proxy"),
    ("prefetch_cert",  "Pre-fetching TLS certificate"),
    ("start_service",  "Starting service + health check"),
]

async def _run_deploy_job(
    *,
    job: _DeployJob,
    session: dict,
    project_dir: str | None,
    in_place: bool,
    subdomain_url: str,
    user_id: str,
) -> None:
    """The actual deploy pipeline, kicked off by sessions_deploy. Walks
    the 13 steps in _DEPLOY_STEPS; each step is marked running, the
    blocking work runs in the default executor (so the event loop is
    responsive + the task is cancellable), then the step is marked
    done/failed. Per-step state is published to the bus via
    WebReporter._pub so live WebSocket subscribers see `deploy_progress`
    events; the in-memory _DeployJob.snapshot() is the GET-poll source.

    The blocking functions called here (shutil.copytree, pip install,
    systemctl via the setuid helper) all have their own timeouts; the
    asyncio.CancelledError surfaces when the task is cancelled at its
    next await, OR when the user clicks Stop deploy in the UI."""
    import shutil, sys
    loop = asyncio.get_running_loop()
    reporter = WebReporter(job.session_id)
    slug = job.slug
    target = OJAS_APPS_ROOT / slug

    def _set_step(idx: int, status: str, message: str | None = None) -> None:
        """Mutate job.steps[idx] in place. The list is padded to
        _DEPLOY_STEPS length on first call so callers can address any
        index from 0..12. Emits a deploy_progress event for every
        transition."""
        import sys
        name, label = _DEPLOY_STEPS[idx]
        with job._lock:
                # Pad the steps list to the canonical length on first touch.
            while len(job.steps) < _DEPLOY_STEP_COUNT:
                job.steps.append({
                    "name": _DEPLOY_STEPS[len(job.steps)][0],
                    "label": _DEPLOY_STEPS[len(job.steps)][1],
                    "status": "pending", "message": None,
                    "started_at": None, "finished_at": None,
                })
            s = job.steps[idx]
            s["name"] = name
            s["label"] = label
            if status == "running" and s.get("started_at") is None:
                s["started_at"] = int(time.time())
            s["status"] = status
            s["message"] = message
            if status in ("done", "failed"):
                s["finished_at"] = int(time.time())
            if status == "running":
                job.phase = label
            job.status = "running" if status == "running" else job.status
            job.updated_at = int(time.time())
            snap = job.snapshot()
        try:
                reporter._pub("deploy_progress", {
                "job_id": snap["job_id"],
                "status": snap["status"],
                "phase":  snap["phase"],
                "steps":  snap["steps"],
                "step":   {"index": idx, "name": name, "label": label,
                           "status": status, "message": message},
            })
        except Exception:
            pass

    def _set_terminal(status: str, error: str | None = None, result: dict | None = None) -> None:
        with job._lock:
            job.status = status
            if error is not None:
                job.error = error[:500]
            if result is not None:
                job.result = result
            now = int(time.time())
            job.updated_at = now
            # Mark the wall-clock time the job reached its terminal
            # state. The sweeper uses this to TTL completed jobs out
            # of the in-memory registry (5 minutes — generous enough
            # for a re-poll, small enough to bound memory).
            job.completed_at = now
        try:
            kind = "deploy_complete" if status == "succeeded" else "deploy_failed"
            reporter._pub(kind, {
                "job_id": job.job_id, "status": status,
                "error": job.error, "result": job.result,
            })
        except Exception:
            pass

    # Roll-back helper: drop the eager Caddy fragment (so the public
    # URL is no longer hijacked) + best-effort cleanup of partial
    # dist/backend. Runs on any failure path that leaves the deploy
    # state inconsistent.
    def _rollback_caddy() -> None:
        f = OJAS_CADDY_ROUTES_DIR / f"{slug}.caddy"
        try:
            if f.exists():
                f.unlink()
            _reload_caddy()
        except Exception:
            pass

    try:
        # 0. validate
        _set_step(0, "running")
        # 0a. dist quality -- reject bundles that contain two copies of
        #     React. Symptom at runtime: "Cannot read properties of null
        #     (reading 'useContext')" and a blank <div id="root">. Cause:
        #     the agent ran `npm install` from outside the project and
        #     npm hoisted a second react + react-dom into a parent
        #     node_modules. Vite happily bundles the result -- the bug
        #     only surfaces in the browser. We catch it here by counting
        #     `.useContext=function` definitions in every emitted JS
        #     file: a clean bundle has exactly 1 per file; a two-React
        #     bundle has 2+. Templates ship `npm run verify:render` for
        #     a stronger end-to-end check; this is a last-line-of-
        #     defence that fires even if the agent skipped the
        #     pre-render check.
        try:
            _validate_dist_quality(_session_preview_dir(
                job.session_id, project_dir=project_dir,
            ))
        except _DistQualityError as dq:
            _set_step(0, "failed", str(dq)[:200])
            raise
        _set_step(0, "done")

        # 1. eager_caddy — write the placeholder Caddy fragment BEFORE
        #    any disk work, so the public URL is hijacked to a
        #    "Deploying..." page immediately. Overwritten by step 9
        #    (caddy_regen) once the DB row + port are known.
        _set_step(1, "running")
        await loop.run_in_executor(
            None, lambda: _write_caddy_deploying_fragment(slug),
        )
        _set_step(1, "done")

        # Resolve absolute project path + fullstack flag. Same logic
        # as the old endpoint.
        OJAS_APPS_ROOT.mkdir(parents=True, exist_ok=True)
        _apply_app_state_to_disk(slug, "running")  # move from .stopped if needed
        project_abs = _session_workspace_root(job.session_id)
        if project_abs and project_dir:
            parts = [p for p in project_dir.replace("\x00", "").split("/") if p and p != ".."]
            for p in parts:
                project_abs = project_abs / p
        is_fullstack = bool(project_abs) and (
            (project_abs / "backend" / "requirements.txt").exists() or
            (project_abs / "backend" / "main.py").exists()
        )
        dist_target = target / "static" if is_fullstack else target
        dist_target.mkdir(parents=True, exist_ok=True)

        # 2. copy_dist
        _set_step(2, "running")
        try:
            await loop.run_in_executor(None, lambda: _do_copy_dist(
                dist=_session_preview_dir(job.session_id, project_dir=project_dir),
                dist_target=dist_target, slug=slug,
            ))
        except Exception as e:
            _set_step(2, "failed", str(e)[:200])
            raise
        _set_step(2, "done")

        # 2.5 inject_pwa — write the manifest + sw + icons + <head>
        #     tags the agent forgot to ship. Cheap (a few KB of text +
        #     two small PNGs from /opt/ojas/assets/). Idempotent: an
        #     agent that DID include custom PWA assets keeps them
        #     untouched. Runs before the fullstack-only copy_backend so
        #     the static/ tree is settled before the backend handler
        #     starts touching sibling paths.
        _set_step(3, "running")
        try:
            await loop.run_in_executor(None, lambda: _inject_default_pwa_assets(
                dist_target, slug=slug, name=(session.get("name") or slug),
            ))
        except Exception as e:
            _set_step(3, "failed", str(e)[:200])
            raise
        _set_step(3, "done")

        service_name: str | None = None
        port: int | None = None
        slug_safe = re.sub(r"[^a-z0-9_-]", "-", slug.lower())[:40]
        if is_fullstack:
            # Fullstack branch: step indices 3-7 are the backend work
            # (copy → venv → pip → unit → enable). Step 2 (copy_dist)
            # and step 3 in the canonical list (inject_pwa) already
            # ran ABOVE this block — they apply to both static and
            # fullstack, so the `if is_fullstack:` starts at the first
            # backend-only step (index 4) and bumps the rest by one
            # to leave the caddy/db/start steps at 9-11.

            # 4. copy_backend (was step 3 before the renumber — the
            #    old code overwrote inject_pwa's "done" status with
            #    copy_backend's "running"/"done" cycle, which made
            #    the UI checklist show "Adding PWA defaults" twice
            #    for fullstack deploys)
            _set_step(4, "running")
            try:
                await loop.run_in_executor(None, lambda: _do_copy_backend(
                    project_abs=project_abs, target=target,
                ))
            except Exception as e:
                _set_step(4, "failed", str(e)[:200])
                raise
            _set_step(4, "done")

            # 5. venv_create
            _set_step(5, "running")
            try:
                await loop.run_in_executor(None, lambda: _do_venv_create(
                    backend_dst=target / "backend",
                ))
            except Exception as e:
                _set_step(5, "failed", str(e)[:200])
                raise
            _set_step(5, "done")

            # 6. pip_install (the long step — 1-5 minutes on a fresh venv)
            _set_step(6, "running")
            def _do_pip():
                return _pip_install_for_app(
                    target / "backend", project_abs / "backend" / "requirements.txt",
                )
            ok, err = await loop.run_in_executor(None, _do_pip)
            if not ok:
                _set_step(6, "failed", err[:200])
                raise RuntimeError(f"pip install failed: {err}")
            _set_step(6, "done")

            # 7. write_unit
            _set_step(7, "running")
            existing_row = db.get_deployed_app(slug) or {}
            if existing_row.get("port") and existing_row.get("service_name"):
                port = int(existing_row["port"])
            else:
                try:
                    port = await loop.run_in_executor(None, _allocate_app_port)
                except RuntimeError as e:
                    _set_step(7, "failed", str(e))
                    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))
            service_name = f"ojas-app-{slug_safe}.service"
            try:
                await loop.run_in_executor(
                    None,
                    lambda: _write_systemd_unit(slug_safe, port, target / "backend"),
                )
            except Exception as e:
                _set_step(7, "failed", str(e)[:200])
                raise
            _set_step(7, "done")

            # 8. enable_service
            _set_step(8, "running")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: _do_enable_service(service_name),
                )
            except Exception as e:
                _set_step(8, "failed", str(e)[:200])
                raise
            _set_step(8, "done")

        # 9. db_row (now step 9 for both static and fullstack —
        #    static apps skip steps 4-8 since they have no backend,
        #    and the "done" state is just left as the canonical label)
        _set_step(9, "running")
        if in_place:
            db.touch_deployed_app(slug, project_dir=project_dir)
            if is_fullstack and service_name and port:
                with db._connect() as cx:   # noqa: SLF001
                    cx.execute(
                        "UPDATE deployed_apps SET service_name = ?, port = ? "
                        "WHERE slug = ?",
                        (service_name, port, slug),
                    )
        else:
            db.create_deployed_app(
                slug=slug, name=session["name"], app_dir=str(target),
                source_session_id=job.session_id,
                source_project_id=session.get("project_id"),
                owner_user_id=user_id, project_dir=project_dir,
                service_name=service_name, port=port,
            )
        app_row = db.get_deployed_app(slug) or {}
        # Always record the ojas_services row for the admin panel
        db.upsert_ojas_service(
            id=f"deployed:{slug}",
            source="ojas-deployed", pid=None,
            label=f"Deployed app: {app_row.get('name', slug)}",
            command=None, port=None, bind_addr=None, url=subdomain_url,
            meta={
                "slug": slug, "app_dir": app_row.get("app_dir"),
                "owner_user_id": app_row.get("owner_user_id"),
                "source_session_id": app_row.get("source_session_id"),
                "public_domain": _resolve_public_domain(),
            },
        )
        _set_step(9, "done")

        # 10. caddy_regen — overwrite the eager placeholder from step 1
        #     with the REAL per-slug fragment. _regenerate_caddy_routes_for_user
        #     writes either a static-shaped fragment (rooted at the
        #     app dir, no /api/* reverse_proxy) or a fullstack-shaped
        #     fragment (rooted at <slug>/static with /api/* → 127.0.0.1:<port>)
        #     based on the row's service_name/port. Crucially this runs
        #     for BOTH static and fullstack apps — the previous version
        #     gated it on `is_fullstack and service_name and port`, which
        #     left static deploys with the "Deploying..." placeholder
        #     serving the public URL forever (every static deploy since
        #     the v1.1 pipeline was added hit this). The row in
        #     deployed_apps looks "running" because step 8 wrote it,
        #     but the URL 200s on the in-flight page.
        _set_step(10, "running")
        try:
            await loop.run_in_executor(
                None,
                lambda: _regenerate_caddy_routes_for_user(
                    app_row.get("owner_user_id"),
                ),
            )
        except Exception as e:
            _set_step(10, "failed", str(e)[:200])
            raise
        _set_step(10, "done")

        if is_fullstack and service_name and port:
            # 11. prefetch_cert — fullstack apps use a per-slug Caddy
            #     site block with `tls { on_demand }`, which means
            #     Caddy only obtains the per-host Let's Encrypt cert
            #     on the FIRST user request -- and during that 3-5s
            #     ACME round-trip, Caddy serves a temporary cert that
            #     the user's browser flags as "Your connection is not
            #     private". Static apps are unaffected because the
            #     wildcard `*.ojas.karmacode.cloud` cert is
            #     pre-issued and re-used. So: make one HEAD request to
            #     the new subdomain right after the reload. Caddy's
            #     on-demand flow runs in the background, the cert
            #     lands in its storage, and by the time the user
            #     visits the URL, the cert is already there. Best-
            #     effort: we don't fail the deploy if the cert fetch
            #     itself errors (DNS, network blip, ACME rate-limit);
            #     the user just gets the legacy "refresh a few times"
            #     experience for that one bad case.
            if subdomain_url:
                _set_step(11, "running")
                try:
                    await loop.run_in_executor(
                        None, lambda: _prefetch_tls_cert(subdomain_url, slug),
                    )
                except Exception as e:
                    print(f"[ojas] cert prefetch for {slug} failed: {e}")
                _set_step(11, "done")

            # 12. start_service + 5s health poll
            _set_step(12, "running")
            ok = await loop.run_in_executor(
                None, lambda: _start_app_service(app_row),
            )
            if not ok:
                # Soft failure — row is in place, user can retry.
                db.set_deployed_app_state(slug, "starting", error_message="health check failed")
            _set_step(12, "done")

        # Finalize
        db.set_deployed_app_state(slug, "running", last_health_at=int(time.time()))
        app_row = db.get_deployed_app(slug) or {}
        result = {
            "slug": slug,
            "url": subdomain_url,
            "app": {**DeployedAppResponse(**app_row).model_dump(mode="json"),
                    "public_url": _public_url_for(slug)},
        }
        _set_terminal("succeeded", result=result)

    except asyncio.CancelledError:
        # User clicked Stop deploy OR a higher-level cancellation
        # cascaded. Drop the eager Caddy fragment, mark the job
        # cancelled, re-raise so the done-callback fires.
        for idx in range(_DEPLOY_STEP_COUNT):
            with job._lock:
                if idx < len(job.steps) and job.steps[idx]["status"] == "running":
                    job.steps[idx]["status"] = "failed"
                    job.steps[idx]["message"] = "cancelled"
                    job.steps[idx]["finished_at"] = int(time.time())
        _rollback_caddy()
        _set_terminal("cancelled", error="cancelled by user")
        raise
    except Exception as e:
        # Any other failure (pip install fail, copytree fail, etc).
        # Drop the eager Caddy fragment so the public URL is no
        # longer hijacked. Mark every still-running step as failed
        # for clarity in the UI.
        for idx in range(_DEPLOY_STEP_COUNT):
            with job._lock:
                if idx < len(job.steps) and job.steps[idx]["status"] == "running":
                    job.steps[idx]["status"] = "failed"
                    job.steps[idx]["message"] = str(e)[:200]
                    job.steps[idx]["finished_at"] = int(time.time())
        _rollback_caddy()
        _set_terminal("failed", error=str(e))
        # Do NOT re-raise: the job is done, the result is set, the
        # done-callback fires. Caller (sessions_deploy) already
        # returned 202, so the user sees status="failed" via the
        # poll endpoint.

# --- Sync helpers extracted from the old sync endpoint body. They run
# inside run_in_executor, so they MUST be plain functions that take
# everything they need as arguments (no closures over the loop's
# locals). All three swallow OSError and let _run_deploy_job decide
# whether to set step=failed + raise, since the executor doesn't
# otherwise have access to the step bookkeeping.

class _DistQualityError(Exception):
    """Raised by _validate_dist_quality when a built dist looks like it
    would render as a blank page in the browser -- the most common
    case being two copies of React bundled together (an "Invalid hook
    call" / "Cannot read properties of null" failure that the bundler
    cannot see). Caught by the deploy step which surfaces the message
    to the user instead of shipping a broken app."""


def _validate_dist_quality(dist: Path | None) -> None:
    """Last-line-of-defence check that the built `dist/` will actually
    boot in a browser.

    Cheap static analysis -- runs in <100ms on a typical 500KB bundle.
    It looks for the two-React signature (more than one
    `.useContext=function` definition in any single JS file) and any
    obviously broken dist (missing index.html).

    Does NOT run the code, so it cannot replace the agent's
    `npm run verify:render` smoke test. It only catches the
    specific class of bug that the bundler cannot see: duplicate
    React from a hoisted install.
    """
    if dist is None or not dist.exists():
        # Resolver already raises; if we're here the deploy pipeline
        # was called with no dist. The agent hasn't built yet.
        raise _DistQualityError(
            "no dist/ found for this session/project -- "
            "ask the agent to run `npm run build` first",
        )
    index_html = dist / "index.html"
    if not index_html.exists():
        raise _DistQualityError(
            f"dist/index.html missing under {dist} -- the Vite build "
            "did not produce an output. Run `npm run build` and "
            "check the console for errors.",
        )
    # Static-only apps put assets/ next to index.html. Fullstack apps
    # put them under static/ (because the Ojas per-slug Caddy fragment
    # serves from /opt/ojas-apps/<slug>/static/). Check both.
    assets_dirs = [dist / "assets"]
    if (dist / "static" / "assets").exists():
        assets_dirs.append(dist / "static" / "assets")
    # The check: in any ONE minified JS file, how many
    # `.useContext=function` definitions appear? A clean React bundle
    # has exactly 1 (the hook shim). A two-React bundle has 2+. The
    # `.useState=function` and `.useEffect=function` patterns work
    # the same way; we count all four so we don't get fooled by
    # inlined shims that happen to redefine one of the hooks.
    SIGS = (
        ".useContext=function",
        ".useState=function",
        ".useEffect=function",
        ".useReducer=function",
    )
    for assets in assets_dirs:
        if not assets.exists():
            continue
        for js in assets.glob("*.js"):
            try:
                text = js.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for sig in SIGS:
                count = text.count(sig)
                if count > 1:
                    raise _DistQualityError(
                        f"{js.name} contains {count} copies of "
                        f"`{sig}` -- this is the signature of a "
                        "duplicated React in the bundle (typically "
                        "caused by `npm install` running from a parent "
                        "directory, which hoists a second react + "
                        "react-dom into a parent node_modules). The "
                        "browser will throw `Cannot read properties "
                        "of null (reading 'useContext')` and the app "
                        "will render as a blank page. Fix: `cd` into "
                        "the project, then `rm -rf node_modules "
                        "package-lock.json && npm install` so all "
                        "packages land in the project's own "
                        "node_modules. Then re-run `npm run "
                        "verify:render` (the templates ship a smoke "
                        "test that catches this) and re-deploy.",
                    )


def _do_copy_dist(*, dist, dist_target, slug) -> None:
    """shutil.copytree(staging) + atomic move into dist_target. Same
    symlink-aware dance as the old endpoint body. Raises OSError on
    failure (the caller translates to step=failed).

    Staging-dir safety: the staging name uses
    `time.time_ns()` (nanosecond) plus a `os.getpid()` tail so two
    deploy clicks in the same second — which the UI does every
    time a deploy fails and the user clicks Update again — get
    DIFFERENT staging paths. The old `int(time.time())` suffix
    collided when the user clicked twice in <1s, the second
    `shutil.copytree(dist, staging)` raised `FileExistsError`
    on the still-present first staging dir, the entire deploy
    failed at step 2 ("copy_dist"), and the user saw
    `Deploy failed: [Errno 17] File exists: '.staging-<slug>-…'`.
    nanoseconds + pid makes the collision probability effectively
    zero even under rapid-fire retry.
    """
    import os
    import shutil
    staging = OJAS_APPS_ROOT / (
        f".staging-{slug}-{time.time_ns()}-{os.getpid()}"
    )
    # Defensive: a previous crash might have left a stale staging
    # dir at the (now-unique) path. copytree refuses to overwrite,
    # so nuke any pre-existing target first. Safe because we just
    # minted the name ourselves.
    if staging.exists() or staging.is_symlink():
        _rmtree_with_symlinks(staging)
    try:
        shutil.copytree(str(dist), str(staging))
        if dist_target.is_dir() and not dist_target.is_symlink():
            _rmtree_with_symlinks(dist_target)
        elif dist_target.is_symlink():
            dist_target.unlink()
        shutil.move(str(staging), str(dist_target))
    except OSError:
        _rmtree_with_symlinks(staging)
        raise

# ---- PWA defaults injector ----------------------------------------------
#
# Both Ojas templates ship a working PWA shell (manifest.webmanifest,
# sw.js, icon-192/512.png) under `frontend/public/`, but a non-trivial
# fraction of agent builds either (a) replace the template's `index.html`
# and forget to keep the <link rel="manifest"> / <meta name="apple-mobile-
# web-app-capable"> tags, or (b) ship without a `public/` folder at all.
# The result: the deploy "succeeds" but the served URL is not installable
# on mobile -- the browser never fires `beforeinstallprompt` because the
# manifest check fails. This is invisible until the user tries to install
# the app on their phone and gets nothing.
#
# To make every sub-app installable without trusting the agent, the
# server injects a complete PWA shell at the end of the dist copy. Every
# write is idempotent: existing custom assets are preserved (we only
# write when the file is missing), and we never touch a <link> or <meta>
# tag that's already in the served index.html. Re-deploys are safe and
# won't clobber per-app theming the agent set up.
#
# The defaults use Ojas's bundled icon (a neutral 192/512 PNG that the
# Ojas main app already ships at /opt/ojas/web/public/icons/) so the
# installable icon on the user's home screen is recognizably "an Ojas
# app" even before the agent swaps in a branded asset. Agents can
# replace the icon at any time by shipping their own `public/icon-192.png`
# on the next build.

OJAS_DEFAULT_ICONS_DIR = Path("/opt/ojas/assets")

_DEFAULT_PWA_MANIFEST = (
    '{{\n'
    '  "name": {name_json},\n'
    '  "short_name": {short_name_json},\n'
    '  "description": "An app deployed by Ojas",\n'
    '  "start_url": "./",\n'
    '  "scope": "./",\n'
    '  "display": "standalone",\n'
    '  "background_color": "#020617",\n'
    '  "theme_color": "#4f46e5",\n'
    '  "icons": [\n'
    '    {{ "src": "./icon-192.png", "sizes": "192x192", "type": "image/png" }},\n'
    '    {{ "src": "./icon-512.png", "sizes": "512x512", "type": "image/png" }}\n'
    '  ]\n'
    '}}\n'
)

_DEFAULT_PWA_SW = '''// Minimal service worker installed by Ojas so the browser
// surfaces the install prompt for sub-apps. Network-first for HTML so
// re-deploys are visible immediately; cache-first for content-hashed
// assets (they're immutable by hash, so a new file = a new URL = a
// cache miss). The cache name is keyed to the deploy epoch so a new
// build evicts the old one on activate.
const CACHE = "ojas-static-v" + {version!r};

self.addEventListener("install", (e) => {{
  self.skipWaiting();
}});

self.addEventListener("activate", (e) => {{
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
}});

self.addEventListener("fetch", (e) => {{
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.pathname.endsWith("/sw.js")) return;

  const isHTML =
    e.request.mode === "navigate" ||
    e.request.destination === "document" ||
    url.pathname.endsWith(".html") ||
    url.pathname === "/" ||
    url.pathname.endsWith("/");

  if (isHTML) {{
    e.respondWith(
      fetch(e.request)
        .then((res) => {{
          if (res.ok) {{
            const clone = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, clone));
          }}
          return res;
        }})
        .catch(() => caches.match(e.request).then((c) => c || Response.error()))
    );
    return;
  }}

  e.respondWith(
    caches.match(e.request).then((cached) => {{
      if (cached) return cached;
      return fetch(e.request).then((res) => {{
        if (res.ok && url.origin === self.location.origin) {{
          const clone = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, clone));
        }}
        return res;
      }}).catch(() => cached || Response.error());
    }})
  );
}});
'''

_INDEX_HEAD_PATCHES = [
    ('<link rel="manifest"',
     '<link rel="manifest" href="./manifest.webmanifest" />'),
    ('<meta name="apple-mobile-web-app-capable"',
     '<meta name="apple-mobile-web-app-capable" content="yes" />'),
    ('<meta name="apple-mobile-web-app-title"',
     '<meta name="apple-mobile-web-app-title" content="{short_name}" />'),
    ('<link rel="apple-touch-icon"',
     '<link rel="apple-touch-icon" href="./icon-192.png" />'),
]


def _short_name_from(name: str) -> str:
    """Derive a <=12 char home-screen label from the app's display
    name. The PWA spec caps short_name at 12 chars (or it gets
    truncated on Android). Strips non-printable, collapses spaces,
    falls back to the slug if the result is empty."""
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9 ]+", "", name or "").strip()
    if not s:
        return "Ojas app"
    parts = s.split()
    # Prefer first word if it's <=12, else the first 12 chars.
    if len(parts[0]) <= 12:
        return parts[0]
    return s[:12]


def _inject_default_pwa_assets(dist_dir: Path, *, slug: str, name: str) -> None:
    """Idempotently fill in the four PWA assets + <head> tags the
    agent forgot to ship. Never overwrites existing custom assets --
    the rule is "only write if the file/tag is missing", so an
    agent that DID set up a proper PWA (or that customised the
    theme_color / icon) keeps their work intact on the next deploy.

    Args:
        dist_dir: the on-disk target that was just populated by
            `_do_copy_dist` -- either `/opt/ojas-apps/<slug>/` for
            static apps or `/opt/ojas-apps/<slug>/static/` for
            fullstack apps. Both layouts work because the assets
            land in the same root that Caddy serves from.
        slug: the deployed slug, baked into the SW cache name.
        name: the human-readable app name (from the session at
            deploy time). Used for `name` and `short_name` in the
            manifest and the apple-mobile-web-app-title <meta>.

    Raises nothing on missing-icons-dir (logged + skipped) so a
    half-installed Ojas doesn't block deploys; the rest of the
    PWA still gets written."""
    import re as _re
    if not dist_dir.exists():
        return
    try:
        dist_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    short_name = _short_name_from(name)

    # 1. manifest.webmanifest
    manifest_path = dist_dir / "manifest.webmanifest"
    if not manifest_path.exists():
        import json as _json
        try:
            manifest_path.write_text(
                _DEFAULT_PWA_MANIFEST.format(
                    name_json=_json.dumps(name or "Ojas app"),
                    short_name_json=_json.dumps(short_name),
                ),
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[ojas] inject_pwa: manifest write failed: {e}")

    # 2. sw.js
    sw_path = dist_dir / "sw.js"
    if not sw_path.exists():
        try:
            # Cache name keyed to deploy epoch so every re-deploy
            # evicts the old cache. The slug is included so two
            # apps on the same VM never collide.
            version = f"{slug}-{int(time.time())}"
            sw_path.write_text(
                _DEFAULT_PWA_SW.format(version=version),
                encoding="utf-8",
            )
        except OSError as e:
            print(f"[ojas] inject_pwa: sw.js write failed: {e}")

    # 3. icons (192 + 512) -- copied from the Ojas-bundled defaults.
    #    Best-effort: if /opt/ojas/assets/ is missing for some reason
    #    we log + skip rather than fail the deploy. The manifest
    #    will still load; the browser will just fall back to its
    #    default "no icon" rendering until the agent ships one.
    for size, fname in ((192, "icon-192.png"), (512, "icon-512.png")):
        target = dist_dir / fname
        if target.exists():
            continue
        src = OJAS_DEFAULT_ICONS_DIR / f"ojas-icon-{size}.png"
        if not src.exists():
            print(f"[ojas] inject_pwa: default icon missing at {src}, "
                  f"skipping {fname}")
            continue
        try:
            target.write_bytes(src.read_bytes())
        except OSError as e:
            print(f"[ojas] inject_pwa: {fname} copy failed: {e}")

    # 4. <head> patches in index.html -- idempotent: only inject
    #    each tag if the same opening token isn't already present.
    #    We use a simple substring check on the opening tag (not the
    #    whole tag) because the agent may use a different attribute
    #    order -- e.g. apple-touch-icon with sizes= or before/after
    #    rel=apple-touch-icon-precomposed. Either way, the presence
    #    of `<link rel="apple-touch-icon"` means we don't need to
    #    inject one.
    index_path = dist_dir / "index.html"
    if not index_path.exists():
        return
    try:
        html = index_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    changed = False
    for marker, tag in _INDEX_HEAD_PATCHES:
        if marker in html:
            continue
        # Use a marker-substituted copy of the tag so the short_name
        # flows into apple-mobile-web-app-title.
        try:
            injected = tag.format(short_name=short_name)
        except (KeyError, IndexError):
            injected = tag
        html = html.replace("</head>", f"  {injected}\n  </head>", 1)
        changed = True
    if not changed:
        return
    try:
        index_path.write_text(html, encoding="utf-8")
    except OSError as e:
        print(f"[ojas] inject_pwa: index.html write failed: {e}")

def _do_copy_backend(*, project_abs, target) -> None:
    """Symlink-aware copytree of the backend/ subdir. Raises OSError."""
    import shutil
    backend_src = project_abs / "backend"
    backend_dst = target / "backend"
    if not backend_src.exists():
        return
    if backend_dst.is_symlink():
        backend_dst.unlink()
    elif backend_dst.exists():
        _rmtree_with_symlinks(backend_dst)
    shutil.copytree(str(backend_src), str(backend_dst))
    # Belt-and-braces: the systemd unit runs
    # `uvicorn main:app --host 127.0.0.1 --port <port>` from
    # `backend_dir` as WorkingDirectory. That works for a flat
    # `backend/main.py`, but breaks the moment `main.py` does a
    # relative import (`from .routes import router` or even
    # `from routes import router` with a sibling `routes/`
    # subpackage) — Python's import machinery then walks UP one
    # level looking for a parent package and finds no
    # `__init__.py`, so uvicorn fails at startup with
    #   [Errno 2] No such file or directory: '__init__.py'
    # even though the file the error names is a sibling of the
    # file we DO have. The fix is a no-op for a healthy flat
    # `main.py` (Python doesn't need __init__.py for a top-level
    # module) and a guarantee for every other layout: drop a
    # one-line `__init__.py` at the backend root if the agent
    # forgot to ship one. Idempotent — never overwrites an
    # existing file (the agent's intentional blank or comment
    # header wins).
    _ensure_backend_init_py(backend_dst)


def _ensure_backend_init_py(backend_dst: Path) -> None:
    """Write a minimal `__init__.py` into the deployed backend dir
    if the agent's build doesn't ship one. See the comment in
    `_do_copy_backend` for the full rationale. Never overwrites."""
    init_py = backend_dst / "__init__.py"
    if init_py.exists() or init_py.is_symlink():
        return
    try:
        init_py.write_text(
            "# Auto-generated by Ojas on deploy. Safe to delete if your\n"
            "# backend intentionally has no parent package — uvicorn is\n"
            "# started from this dir as `main:app` and Python does not\n"
            "# require __init__.py for that to work. This file exists\n"
            "# only to satisfy relative imports (`from .routes import …`)\n"
            "# the agent's main.py might do.\n",
            encoding="utf-8",
        )
    except OSError:
        # Worst case: the backend is read-only and we can't write
        # the init file. Let the deploy continue and the user's
        # app will fail at uvicorn startup if it actually needs
        # the parent package — which is the same behaviour as
        # before this fix. We don't want a deploy to be blocked
        # by an init-file write that the majority of builds
        # don't even need.
        pass

def _do_venv_create(*, backend_dst) -> None:
    """Create the .venv using the same venv.EnvBuilder params as the
    old endpoint. Raises on failure."""
    import venv
    venv_dir = backend_dst / ".venv"
    builder = venv.EnvBuilder(
        system_site_packages=False, clear=True,
        symlinks=False, with_pip=True,
    )
    builder.create(str(venv_dir))

def _do_enable_service(service_name: str) -> None:
    """daemon-reload + enable via the setuid helper. Raises
    subprocess.CalledProcessError / TimeoutExpired on failure."""
    subprocess.run(
        ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "daemon-reload"],
        check=True, timeout=10, capture_output=True,
    )
    subprocess.run(
        ["/usr/local/sbin/ojas-systemd-helper", "systemctl", "enable", service_name],
        check=True, timeout=10, capture_output=True,
    )

# (Old sync sessions_deploy removed -- replaced by the async version
# above + the GET/cancel status endpoints. The 4xx errors that
# don't make sense to defer stay synchronous; everything else runs
# in _run_deploy_job.)

@app.get(
    "/api/deployed-apps",
    response_model=list[DeployedAppResponse],
)
def deployed_apps_list(user: dict = Depends(require_user)):
    """List deployed apps. Root sees all; everyone else sees their own."""
    owner = None if user["role"] == "root" else user["id"]
    rows = db.list_deployed_apps(owner_user_id=owner)
    return [
        {**DeployedAppResponse(**a).model_dump(mode="json"),
         "public_url": _public_url_for(a["slug"])}
        for a in rows
    ]

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
    return [
        {**DeployedAppResponse(**a).model_dump(mode="json"),
         "public_url": _public_url_for(a["slug"])}
        for a in rows
    ]

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
    # True when a deployed_apps row already exists for THIS sub-app
    # (matched by source_session_id + project_dir). Drives the
    # "+ Deploy new" enabled state: a candidate is deployable-new only
    # when !is_deployed. Lets the modal drop the already-deployed
    # candidates from its dropdown so the user is never asked to
    # "deploy" something that's already live (which would 409).
    is_deployed: bool = False
    # The slug this sub-app is currently published under, if any. None
    # for unbuilt sub-apps. Lets the per-pill "🔄 Update" state do an
    # O(1) candidates-by-slug lookup instead of comparing mtime against
    # the global freshest — important for multi-app sessions where
    # only one of N deployed apps has been rebuilt.
    deployed_slug: str | None = None
    # True when this candidate's mtime is newer than the matching
    # deployed app's last_redeploy_at. Drives the per-pill "🔄 Update"
    # badge: only the genuinely stale pills show Update, even when the
    # session as a whole has a fresh build waiting for a different
    # sub-app. False for unbuilt candidates (no last_redeploy_at to
    # compare against; they're a separate deploy-new flow).
    is_fresh: bool = False

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
    # True when at least one candidate is BUILT (mtime > 0) AND
    # NOT YET DEPLOYED. Drives the "+ Deploy new" button enabled state
    # in the nav strip. Distinguishes "any fresh build" (fresh_build)
    # from "an unbuilt sub-app is ready to publish" — the latter is
    # the only case where "Deploy new" makes sense. When false, the
    # user is steered toward the per-pill "🔄 Update" for any rebuild
    # work instead.
    has_unbuilt_build: bool = False

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
    from this session).

    Per-candidate enrichment: for each detected dist we look up the
    matching deployed_apps row (by source_session_id + project_dir) and
    stamp is_deployed / deployed_slug / is_fresh. This is what lets the
    UI split the deploy surface into two clean intents:
      - "+ Deploy new" button → enabled only when SOME candidate has a
        build and is NOT yet deployed (has_unbuilt_build).
      - Per-pill "🔄 Update" badge → driven by THIS pill's candidate's
        is_fresh, not the global session-freshest mtime. So in a
        3-app session where only one was rebuilt, only that one pill
        shows Update; the others stay "✓ Up to date".
    """
    _session_or_404(session_id, user)
    raw_cands = _detect_dist_candidates(session_id)
    # Enrich each candidate with its matching deployed_apps row.
    # get_deployed_app_for_subapp is NULL-safe on project_dir ("" /
    # None both match the session-root row).
    enriched: list[DistCandidate] = []
    last_redeploy = 0
    for c in raw_cands:
        deployed_row = None
        try:
            deployed_row = db.get_deployed_app_for_subapp(
                session_id,
                c["project_dir"] if c["project_dir"] else None,
            )
        except Exception:
            # DB hiccup → treat as unbuilt rather than crash the
            # whole endpoint. The "Deploy new" button will then show
            # as enabled, which is the safer default.
            deployed_row = None
        is_deployed = deployed_row is not None
        deployed_slug = deployed_row["slug"] if deployed_row else None
        if is_deployed:
            ts = int(deployed_row.get("last_redeploy_at") or 0)
            if ts > last_redeploy:
                last_redeploy = ts
        is_fresh = (
            is_deployed
            and c["mtime"] > 0
            and c["mtime"] > int(deployed_row.get("last_redeploy_at") or 0)
        )
        enriched.append(DistCandidate(
            project_dir=c["project_dir"],
            abs_path=c["abs_path"],
            mtime=c["mtime"],
            index_size=c["index_size"],
            is_deployed=is_deployed,
            deployed_slug=deployed_slug,
            is_fresh=is_fresh,
        ))

    fresh_build = False
    fresh_mtime = 0
    if enriched:
        fresh_mtime = max(c.mtime for c in enriched)
        fresh_build = fresh_mtime > last_redeploy
    has_unbuilt_build = any(
        c.mtime > 0 and not c.is_deployed for c in enriched
    )

    if not enriched:
        return DetectedDistResponse(
            candidates=[], status="none", auto_pick=None,
            fresh_build=False, fresh_mtime=0, has_unbuilt_build=False,
        )
    if len(enriched) == 1:
        return DetectedDistResponse(
            candidates=enriched, status="single",
            auto_pick=enriched[0].project_dir,
            fresh_build=fresh_build, fresh_mtime=fresh_mtime,
            has_unbuilt_build=has_unbuilt_build,
        )
    return DetectedDistResponse(
        candidates=enriched, status="multiple",
        auto_pick=enriched[0].project_dir,  # newest as best guess
        fresh_build=fresh_build, fresh_mtime=fresh_mtime,
        has_unbuilt_build=has_unbuilt_build,
    )

@app.delete("/api/deployed-apps/{slug}")
def deployed_apps_delete(slug: str, user: dict = Depends(require_user)):
    """Take down a deployed app -- rmtree the on-disk files AND remove the
    DB row. Idempotent on missing files (so a botched-half-state app can
    still be cleaned up from the UI)."""
    import shutil
    app = _deployed_app_or_404(slug, user)
    target = Path(app["app_dir"])
    # Stop + disable + remove the systemd unit (fullstack) BEFORE rmtree
    # so the process releases the files AND releases its bound port.
    # _remove_app_service_files handles the legacy case where the row
    # predates the service_name column (derives the name from the slug).
    _remove_app_service_files(slug, app)
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
    # Stamp each app with its live public_url so the Settings page can
    # render a clickable link without re-deriving the apps-root domain.
    for r in rows:
        r["deployed_apps"] = [
            {**DeployedAppResponse(**a).model_dump(mode="json"),
             "public_url": _public_url_for(a["slug"])}
            for a in r.get("deployed_apps", [])
        ]
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
    #
    # The strategy mirrors the session delete path exactly: for each
    # user-owned session, run _purge_session_everything — which kills
    # the agent's spawned processes, SIGTERMs the systemd unit for
    # every fullstack app deployed from that session, unlinks the
    # per-slug Caddy fragment, rmtree's the on-disk app dir + the
    # .stopped/ fallback, drops the deployed_apps row, drops the
    # matching ojas_services row, rmtree's the workspace subdir +
    # agent state dir, deletes the langgraph checkpoint, and clears
    # the in-memory event bus. Caddy is reloaded once per session.
    #
    # This way, a user delete is exactly "delete every session the
    # user owns" — and every side effect of session delete carries
    # over, including the URL going down. Previously the safety-net
    # loop below was rmtree'ing the app dir but never unlinking the
    # Caddy fragment or stopping the systemd unit, so the URL kept
    # serving until the next Caddy reload (which the next deploy
    # would eventually trigger, but until then the orphan was
    # reachable). The safety-net loop is still here, calling the
    # same _purge_one_deployed_app helper, to catch deployed apps
    # whose source_session_id doesn't match any of the user's live
    # sessions (legacy rows, or apps whose session was already
    # deleted on a previous run).
    try:
        # 1. Cancel any in-flight turn task for the user's sessions.
        #    Same step the session-delete path does first; without
        #    this the agent keeps producing events that try to write
        #    to a soon-to-be-deleted workspace.
        for s in db.list_sessions_for_user(user_id):
            task = _active_turns.get(s["id"])
            if task is not None and not task.done():
                task.cancel()
        # 2. Run the per-session cleanup for each user-owned session.
        #    This handles ~all the user delete side effects (processes,
        #    deployed apps, workspace subdir, state dir, checkpoint,
        #    bus, Caddy routes, systemd units, ojas_services rows).
        for s in db.list_sessions_for_user(user_id):
            _purge_session_everything(s)
        # 3. Safety net: any deployed apps still owned by the user
        #    but not linked to a surviving session (legacy rows whose
        #    source_session_id was already NULLed by an earlier
        #    session delete, or rows that predate the session FK).
        #    Use the same per-app cleanup so they leave no Caddy
        #    fragment / systemd unit / working URL behind.
        caddy_changed = False
        for app_row in db.list_deployed_apps(owner_user_id=user_id):
            if _purge_one_deployed_app(app_row["slug"], app_row):
                caddy_changed = True
        if caddy_changed:
            try:
                _reload_caddy()
            except Exception:
                logger.exception(
                    "admin_user_delete: caddy reload failed for user %s", user_id,
                )
    except Exception:
        # Don't fail the delete on cleanup errors — the DB cascade
        # below is the source of truth, and the admin can re-run
        # the orphan reaper via the Admin panel if anything was
        # left behind. Logged for the admin to see in journald.
        logger.exception("admin_user_delete: cleanup error for %s", user_id)
    try:
        auth.delete_user(user_id)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return {"ok": True}


@app.post("/api/admin/reap-orphans")
def admin_reap_orphans(_root: dict = Depends(require_root)):
    """Re-run the boot-time orphan reaper on demand. Idempotent.

    Useful when:
      • the backend has been up for a while and a partial crash left
        orphan app dirs / Caddy fragments / systemd units behind
      • a manual DB edit (e.g. dropping a `deployed_apps` row by hand)
        left on-disk residue
      • the user is asking "why is <slug>.<root> still serving?" and
        the answer is "orphan dir" — clicking this kills the URL

    Safe to call repeatedly. Returns 200 with a summary of what was
    reaped (caddy fragments / staging dirs / systemd units / app dirs).

    Why a separate endpoint instead of just restarting the backend:
    a restart is heavy (drops in-flight agent turns), and the orphan
    reaper is the ONLY thing in `_reconcile_deployed_apps_on_boot()`
    that depends on the disk being dirty. Letting the admin trigger
    it on demand means the fix is one click, not a service restart.
    """
    # _reconcile_deployed_apps_on_boot() prints its own summary to
    # stdout. Capture the reaper output for the response so the admin
    # sees what was actually cleaned up.
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        _reconcile_deployed_apps_on_boot()
    return {"ok": True, "log": buf.getvalue()}

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
