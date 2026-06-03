"""Pydantic request/response schemas for the FastAPI app."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ---- Auth -----------------------------------------------------------------

class AuthStatusResponse(BaseModel):
 needs_setup: bool

class SetupRequest(BaseModel):
 passcode: str = Field(min_length=4, description="At least 4 characters")

class LoginRequest(BaseModel):
 passcode: str
 device_label: str | None = Field(None, description="Friendly label e.g. 'Sujith iPhone'")

class LoginResponse(BaseModel):
 token: str

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
