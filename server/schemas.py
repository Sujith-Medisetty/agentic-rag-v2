"""Pydantic request/response schemas for the FastAPI app."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---- Auth -----------------------------------------------------------------

class AuthStatusResponse(BaseModel):
 # True when no users exist yet AND no root creds are configured. The UI
 # uses this to show a signup screen on first boot.
 needs_setup: bool
 # True when OJAS_ROOT_EMAIL / PASSWORD are configured in env. Lets the
 # UI hint "use your root credentials" on the login page.
 has_root: bool = False
 # True when the server allows new signups via /api/auth/signup.
 signup_allowed: bool = True

class SetupRequest(BaseModel):
 # Legacy passcode-style setup; kept for backward compat but not used.
 passcode: str = Field(min_length=4, description="Legacy field")

class SignupRequest(BaseModel):
 email: str
 password: str = Field(min_length=6, description="At least 6 characters")

class LoginRequest(BaseModel):
 email: str
 password: str
 device_label: str | None = Field(None, description="Friendly label e.g. 'Sujith iPhone'")

class UserResponse(BaseModel):
 id: str
 email: str
 role: str
 created_at: int

class AdminResetPasswordRequest(BaseModel):
 # New password. Same validation as signup: min 6 chars.
 new_password: str = Field(min_length=6, description="At least 6 characters")

class LoginResponse(BaseModel):
 token: str
 user: UserResponse

# ---- Projects -------------------------------------------------------------

class ProjectCreateRequest(BaseModel):
 name: str = Field(min_length=1, max_length=128)
 workspace_path: str = Field(description="Absolute or ~-expanded path to the repo on disk")

class ProjectResponse(BaseModel):
 id: str
 name: str
 workspace_path: str
 created_at: int
 # Phase 4 settings — included on every project payload so the UI doesn't
 # need a separate round-trip to render the toggles.
 auto_commit_enabled: bool = True
 auto_push_enabled: bool = False
 branch_strategy: str = "session"

class ProjectSettingsRequest(BaseModel):
 """PATCH body — every field optional, only the ones present are updated."""
 auto_commit_enabled: bool | None = None
 auto_push_enabled: bool | None = None
 branch_strategy: str | None = Field(
 None, description="'session' | 'current'",
 )

# ---- Sessions -------------------------------------------------------------

class SessionCreateRequest(BaseModel):
 name: str = Field(min_length=1, max_length=128)

class SessionRenameRequest(BaseModel):
 """PATCH body for renaming a session. Same name uniqueness rule as
 create: no two sessions in the same project can share a name
 (case-sensitive)."""
 new_name: str = Field(min_length=1, max_length=128)

class SessionResponse(BaseModel):
 id: str
 project_id: str
 name: str
 last_active_at: int
 created_at: int
 # The per-session subdirectory under the project workspace where the
 # agent writes files for this session. None on legacy rows created
 # before the subdir column.
 workspace_subdir: str | None = None
 # The owner of this session. None on legacy rows; new sessions always
 # have it set.
 user_id: str | None = None

class ProcessResponse(BaseModel):
 pid: int
 session_id: str
 command: str
 port: int | None = None
 started_at: int
 # True if the PID is still a live process on the box. False means the
 # DB row is stale (the process exited but the session wasn't cleaned
 # up). The UI shows a 💀 marker on dead rows so the admin can decide
 # whether to click Kill (which will unregister the row).
 is_alive: bool = True

class OjasServiceResponse(BaseModel):
 """An Ojas-owned service row. Either a live process (pid set) or a
 known port/URL endpoint (pid null — e.g. a deployed app's static URL
 served by caddy). `source` is a short tag the admin UI uses to group
 rows: 'ojas-main', 'ojas-proxy', 'ojas-deployed', 'ojas-mcp'."""
 id: str
 source: str
 pid: int | None = None
 label: str
 command: str | None = None
 port: int | None = None
 # All listening ports the service currently owns. `port` above is the
 # first entry of this list (kept for backward-compat single-port lookups);
 # the full list lives here so the UI can show "caddy: 80, 443, 2019".
 ports: list[int] = []
 bind_addr: str | None = None
 url: str | None = None
 started_at: int
 meta: dict | None = None

# ---- Messages -------------------------------------------------------------

class MessagePostRequest(BaseModel):
 content: str = Field(min_length=1)

class MessageResponse(BaseModel):
 id: str
 session_id: str
 role: str
 content: str
 created_at: int

class EventResponse(BaseModel):
 id: str
 session_id: str
 kind: str
 payload: dict
 created_at: int

# ---- Git -----------------------------------------------------------------

class GitInfoResponse(BaseModel):
 is_git_repo: bool
 branch: str = ""
 last_commit_sha: str = ""
 last_commit_subject: str = ""
 has_remote: bool = False
 ahead: int = 0
 behind: int = 0
 dirty: bool = False

class PushResponse(BaseModel):
 pushed: bool
 branch: str = ""
 remote: str = ""
 error: str = ""

# ---- Deployed apps -------------------------------------------------------

class DeployRequest(BaseModel):
 # Optional user-chosen slug. If omitted, server slugifies session name.
 # If the requested slug already exists, server appends -2, -3, etc.
 slug: str | None = None
 # Optional subfolder under the session's workspace_subdir whose `dist/`
 # should be promoted. Lets a single session host multiple apps (e.g.
 # session contains `calorie-tracker/` AND `weather/` — deploy each as
 # its own URL). Empty/omitted deploys the session root's `dist/`.
 project_dir: str | None = None

class DeployedAppResponse(BaseModel):
 slug: str
 name: str
 source_session_id: str | None = None
 source_project_id: str | None = None
 owner_user_id: str | None = None
 app_dir: str
 deployed_at: int
 last_redeploy_at: int
 # Subfolder of the source session that was promoted (None for the
 # session root). Lets the UI pre-fill the Sub-app field on re-deploy.
 project_dir: str | None = None
 # State machine: "running" | "stopped" | "starting" | "error". Toggled
 # via the settings page. Static apps are always "running" (the toggle
 # just swaps the Caddy route between live and paused).
 state: str = "running"
 last_state_at: int | None = None
 last_health_at: int | None = None
 error_message: str | None = None
 # Fullstack-only fields (NULL for static apps).
 service_name: str | None = None
 port: int | None = None


class DeployStateResponse(BaseModel):
 """Returned by GET /api/deployed-apps/<slug>/state and the start/stop
 endpoints so the UI can refresh the badge without re-listing everything.
 """
 slug: str
 state: str
 last_state_at: int | None = None
 last_health_at: int | None = None
 error_message: str | None = None


class DeployedAppsBySession(BaseModel):
 """One row in the settings page — a session with its deployed apps."""
 session_id: str | None = None
 session_name: str = "(deleted session)"
 deployed_apps: list[DeployedAppResponse]

class DeployResponse(BaseModel):
 # The slug we actually allocated (may differ from request if collision).
 slug: str
 # Relative URL — UI prepends current origin for the share link.
 url: str
 # The full DeployedAppResponse so the UI can immediately add it to its list.
 app: DeployedAppResponse



class DeployStep(BaseModel):
    # Machine-readable step name: "validate", "eager_caddy", "copy_dist",
    # "copy_backend", "venv_create", "pip_install", "write_unit",
    # "enable_service", "db_row", "caddy_regen", "start_service", "finalize".
    name: str
    # Human-readable label for the UI ("Copying build to /opt/ojas-apps").
    label: str
    # "pending" | "running" | "done" | "failed".
    status: str
    # Optional detail (e.g. pip stderr tail, error message on failure).
    message: str | None = None
    started_at: int | None = None
    finished_at: int | None = None


class DeployJobStartResponse(BaseModel):
    # Returned by the async POST /deploy endpoint with 202 Accepted.
    # The client polls GET /deploy-jobs/{job_id} for progress; the
    # `result` field in DeployJobStatusResponse holds the canonical
    # DeployResponse once status="succeeded".
    job_id: str
    slug: str
    # The URL the deployed app will live at once the job finishes. The
    # URL is hijacked by an eager "Deploying..." placeholder the moment
    # the job starts (so the public hostname never 404s), then bound to
    # the real /api/* reverse proxy in the caddy_regen step.
    url: str
    # Optimistic DeployedApp row the chat strip can render immediately
    # (state="starting"). The canonical row overwrites this once the
    # job succeeds; the strip will dedupe by slug.
    placeholder_app: DeployedAppResponse


class DeployJobStatusResponse(BaseModel):
    job_id: str
    session_id: str
    slug: str
    # "pending" | "running" | "succeeded" | "failed" | "cancelled".
    status: str
    # Human-readable current phase name (mirrors the running step's label).
    phase: str
    # Always 11 entries (one per step), in fixed order, so the UI
    # checklist is stable across polls.
    steps: list[DeployStep]
    # Set on failed / cancelled. Truncated to 500 chars server-side.
    error: str | None = None
    # Populated only when status="succeeded". Same shape as the old
    # synchronous 201 DeployResponse.
    result: DeployResponse | None = None
    created_at: int
    updated_at: int
