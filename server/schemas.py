"""Pydantic request/response schemas for the FastAPI app."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---- Auth -----------------------------------------------------------------

class AuthStatusResponse(BaseModel):
 # True when no users exist yet AND no root creds are configured. The UI
 # uses this to show a signup screen on first boot.
 needs_setup: bool
 # True when FORGE_ROOT_EMAIL / PASSWORD are configured in env. Lets the
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
