// Typed fetch wrapper around the FastAPI backend.
// Auto-injects Authorization: Bearer <token>; throws ApiError on non-2xx.

import { getToken, clearToken } from "@/lib/auth";
import type {
  Project, ProjectSettingsUpdate, Session, Message, EventRecord,
  GitInfo, PushResult,
} from "@/lib/types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { skipAuth?: boolean } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init.headers as Record<string, string> | undefined),
  };
  if (!init.skipAuth) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) {
    clearToken();
    throw new ApiError(401, "unauthenticated");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  // 202 endpoints (e.g. messages POST) may return small JSON like {accepted:true}
  return res.json() as Promise<T>;
}

// ---- Auth ----------------------------------------------------------------

export interface AuthStatus {
  needs_setup: boolean;
  has_root?: boolean;
  signup_allowed?: boolean;
}
export interface AuthUser {
  id: string;
  email: string;
  role: "user" | "root";
  created_at: number;
}
export interface AuthToken {
  token: string;
  user: AuthUser;
}

export const authApi = {
  status: () =>
    request<AuthStatus>("/api/auth/status", { skipAuth: true }),
  signup: (email: string, password: string) =>
    request<AuthToken>("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password }),
      skipAuth: true,
    }),
  login: (email: string, password: string, device_label?: string) =>
    request<AuthToken>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password, device_label }),
      skipAuth: true,
    }),
  me: () => request<AuthUser>("/api/auth/me"),
  logout: () =>
    request<{ ok: true }>("/api/auth/logout", { method: "POST" }),
};

// ---- Projects ------------------------------------------------------------

export const projectsApi = {
  list: () =>
    request<Project[]>("/api/projects"),
  create: (name: string, workspace_path: string) =>
    request<Project>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, workspace_path }),
    }),
  getDefault: () =>
    request<Project>("/api/projects/default"),
  get: (id: string) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}`),
  updateSettings: (id: string, patch: ProjectSettingsUpdate) =>
    request<Project>(`/api/projects/${encodeURIComponent(id)}/settings`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  remove: (id: string) =>
    request<{ ok: true }>(`/api/projects/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};

// ---- Admin (root only) --------------------------------------------------

export interface AdminProcess {
  pid: number;
  session_id: string;
  command: string;
  port: number | null;
  started_at: number;
  // True if the PID is still alive on the box. False means the DB row
  // is stale (the process exited but wasn't cleaned up). The UI shows
  // a 💀 marker for dead rows.
  is_alive: boolean;
}

export interface OjasService {
  id: string;
  // "ojas-main"     → FastAPI/uvicorn backend
  // "ojas-proxy"    → caddy / reverse proxy
  // "ojas-deployed" → a deployed app (static files served via caddy)
  // "ojas-mcp"      → MCP server
  // "ojas-external" → discovered on a listening port we didn't register
  source: string;
  pid: number | null;
  label: string;
  command: string | null;
  port: number | null;
  // Full list of listening ports the service owns. `port` above is the
  // first entry; the full list is here so caddy shows 80, 443, 2019.
  ports: number[];
  bind_addr: string | null;
  url: string | null;
  started_at: number;
  meta: Record<string, any> | null;
}

export const adminApi = {
  processes: () => request<AdminProcess[]>("/api/admin/processes"),
  killProcess: (pid: number) =>
    request<{ ok: true }>(`/api/admin/processes/${pid}`, { method: "DELETE" }),
  services: () => request<OjasService[]>("/api/admin/services"),
  users: () => request<AuthUser[]>("/api/admin/users"),
  deleteUser: (userId: string) =>
    request<{ ok: true }>(`/api/admin/users/${encodeURIComponent(userId)}`, { method: "DELETE" }),
  resetUserPassword: (userId: string, newPassword: string) =>
    request<{ ok: true }>(
      `/api/admin/users/${encodeURIComponent(userId)}/password`,
      {
        method: "POST",
        body: JSON.stringify({ new_password: newPassword }),
      },
    ),
};

export const pathsApi = {
  common: () =>
    request<{ locations: { label: string; path: string }[] }>(
      "/api/paths/common",
    ),
  browse: (cwd?: string) =>
    request<{
      cwd: string;
      parent: string | null;
      entries: { name: string; path: string }[];
    }>(
      `/api/paths/browse${cwd ? `?cwd=${encodeURIComponent(cwd)}` : ""}`,
    ),
};

// ---- Deployed apps -------------------------------------------------------
//
// Persistent installable apps living at https://<host>/apps/<slug>/. A
// session's built dist/ is "promoted" via POST /api/sessions/:id/deploy.
// The deployed app survives session-delete + backend restart — it's just
// static files on disk + a DB row, no process.

export interface DeployedApp {
  slug: string;
  name: string;
  source_session_id: string | null;
  source_project_id: string | null;
  owner_user_id: string | null;
  app_dir: string;
  deployed_at: number;
  last_redeploy_at: number;
  project_dir: string | null;
  // State machine: running | stopped | starting | error.
  state: string;
  last_state_at: number | null;
  last_health_at: number | null;
  error_message: string | null;
  service_name: string | null;
  port: number | null;
  // Live public URL for this sub-app (e.g. https://<slug>.<host>/).
  // Surfaced in the chat strip pill and Settings so the user can
  // bookmark and re-open the same URL across re-deploys. Empty
  // string is a legacy fallback -- if a future server build drops
  // the field, the UI falls back to a derived URL.
  public_url: string;
}

export interface DeployState {
  slug: string;
  state: string;
  last_state_at: number | null;
  last_health_at: number | null;
  error_message: string | null;
}

export interface DeployedAppsBySession {
  session_id: string | null;
  session_name: string;
  deployed_apps: DeployedApp[];
}

// The dist-auto-detection endpoint. The dialog pre-fills and locks the
// Sub-app folder from this so the user only has to pick a slug.
export interface DistCandidate {
  project_dir: string;   // "" = session root; otherwise sub-app folder name
  abs_path: string;
  mtime: number;          // epoch seconds — used for "built 3m ago"
  index_size: number;     // bytes in dist/index.html
}
export interface DetectedDist {
  candidates: DistCandidate[];
  status: "single" | "multiple" | "none";
  // The server's best guess (== candidates[0] when single). The dialog
  // pre-fills from this when present.
  auto_pick: string | null;
  // True when the freshest dist in this session is newer than the
  // most recent deploy FROM this session. Used by the chat to show
  // a "Build ready" banner under the agent's last reply.
  fresh_build: boolean;
  // mtime (epoch seconds) of the freshest candidate, or 0 if none.
  // Lets the UI show "built 3m ago" without re-fetching candidates.
  fresh_mtime: number;
}

export interface DeployResult {
  slug: string;
  url: string;
  app: DeployedApp;
}

export type DeployStepStatus = "pending" | "running" | "done" | "failed";

export interface DeployStep {
  name: string;
  label: string;
  status: DeployStepStatus;
  message: string | null;
  started_at: number | null;
  finished_at: number | null;
}

export type DeployJobLifecycleStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface DeployJobStatus {
  job_id: string;
  session_id: string;
  slug: string;
  status: DeployJobLifecycleStatus;
  phase: string;
  steps: DeployStep[];
  error: string | null;
  result: DeployResult | null;
  created_at: number;
  updated_at: number;
}

export interface DeployJobStart {
  job_id: string;
  slug: string;
  url: string;
  placeholder_app: DeployedApp;
}

export const deployedAppsApi = {
  list: () => request<DeployedApp[]>("/api/deployed-apps"),
  delete: (slug: string) =>
    request<{ ok: true }>(
      `/api/deployed-apps/${encodeURIComponent(slug)}`,
      { method: "DELETE" },
    ),
  // Pause / resume. Static apps (the only kind in v1) just swap a
  // directory on disk; v1.1 fullstack apps will additionally start/stop
  // a per-app systemd unit (transparent to the UI).
  start: (slug: string) =>
    request<DeployState>(
      `/api/deployed-apps/${encodeURIComponent(slug)}/start`,
      { method: "POST" },
    ),
  stop: (slug: string) =>
    request<DeployState>(
      `/api/deployed-apps/${encodeURIComponent(slug)}/stop`,
      { method: "POST" },
    ),
  state: (slug: string) =>
    request<DeployState>(
      `/api/deployed-apps/${encodeURIComponent(slug)}/state`,
    ),
  // Grouped by source session for the Settings page.
  mine: () =>
    request<DeployedAppsBySession[]>("/api/users/me/deployed-apps"),
  // Deploy is per-session — convenience method lives on sessionApi below.
};

// ---- Sessions ------------------------------------------------------------

export const sessionsApi = {
  list: (projectId: string) =>
    request<Session[]>(
      `/api/projects/${encodeURIComponent(projectId)}/sessions`,
    ),
  create: (projectId: string, name: string) =>
    request<Session>(
      `/api/projects/${encodeURIComponent(projectId)}/sessions`,
      { method: "POST", body: JSON.stringify({ name }) },
    ),
  get: (sessionId: string) =>
    request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}`),
  rename: (sessionId: string, newName: string) =>
    request<Session>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ new_name: newName }),
    }),
  /** Like `rename` but also returns the X-Was-Suffixed / X-Actual-Name
   *  headers so the UI can show a toast when the server auto-suffixed
   *  the name to avoid a collision. */
  renameWithSufStatus: async (
    sessionId: string,
    newName: string,
  ): Promise<{
    session: Session;
    wasSuffixed: boolean;
    actualName: string;
  }> => {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "PATCH",
        headers,
        body: JSON.stringify({ new_name: newName }),
      },
    );
    if (res.status === 401) {
      clearToken();
      throw new ApiError(401, "unauthenticated");
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail ?? detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, detail);
    }
    const session = (await res.json()) as Session;
    const wasSuffixed = res.headers.get("X-Was-Suffixed") === "true";
    const actualName = res.headers.get("X-Actual-Name") ?? session.name;
    return { session, wasSuffixed, actualName };
  },
  remove: (sessionId: string) =>
    request<{ ok: true }>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),
};

// ---- Messages + events ---------------------------------------------------

export const sessionApi = {
  messages: (sessionId: string) =>
    request<Message[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
    ),
  post: (sessionId: string, content: string) =>
    request<{ accepted: true }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/messages`,
      { method: "POST", body: JSON.stringify({ content }) },
    ),
  events: (sessionId: string, since?: number) => {
    const q = since != null ? `?since=${since}` : "";
    return request<EventRecord[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/events${q}`,
    );
  },
  git: (sessionId: string) =>
    request<GitInfo>(`/api/sessions/${encodeURIComponent(sessionId)}/git`),
  push: (sessionId: string) =>
    request<PushResult>(
      `/api/sessions/${encodeURIComponent(sessionId)}/push`,
      { method: "POST" },
    ),
  cancel: (sessionId: string) =>
    request<{ ok: boolean; reason?: string }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/cancel`,
      { method: "POST" },
    ),
  compact: (sessionId: string) =>
    request<{ ok: boolean; reason?: string; before?: number; after?: number }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/compact`,
      { method: "POST" },
    ),
  // Promote a session's built dist/ to a permanent subdomain URL.
  // Returns 202 Accepted with a {job_id, slug, url, placeholder_app}
  // envelope; the actual work runs in a background task and the
  // client polls deployJobStatus() for per-step progress. Sync 4xx
  // errors (no built dist, bad sub-app folder, slug collision) still
  // come back as the corresponding status code from this same call
  // (no job is created for those).
  //   slug         — leftmost label of the public URL. Server slugifies.
  //   project_dir  — usually set automatically from `detectedDist()`.
  deploy: (sessionId: string, opts: { slug?: string; project_dir?: string } = {}, init?: { signal?: AbortSignal }) =>
    request<DeployJobStart>(
      `/api/sessions/${encodeURIComponent(sessionId)}/deploy`,
      {
        method: "POST",
        body: JSON.stringify({
          slug: opts.slug ?? null,
          project_dir: opts.project_dir ?? null,
        }),
        ...(init?.signal ? { signal: init.signal } : {}),
      },
    ),
  // Poll for the per-step status of an in-flight or recently-finished
  // deploy. 404 if the job_id is unknown OR not owned by the caller
  // (the server intentionally doesn't differentiate). 11 entries in
  // `steps` in a fixed order so the UI checklist is stable.
  deployJobStatus: (sessionId: string, jobId: string, init?: { signal?: AbortSignal }) =>
    request<DeployJobStatus>(
      `/api/sessions/${encodeURIComponent(sessionId)}/deploy-jobs/${encodeURIComponent(jobId)}`,
      init?.signal ? { signal: init.signal } : {},
    ),
  // Cooperative cancel of an in-flight deploy. Idempotent — returns
  // {ok: false, reason: "job not running"} if the job is already done.
  cancelDeployJob: (sessionId: string, jobId: string) =>
    request<{ ok: boolean; reason?: string }>(
      `/api/sessions/${encodeURIComponent(sessionId)}/deploy-jobs/${encodeURIComponent(jobId)}/cancel`,
      { method: "POST" },
    ),
  // Scan the session workspace for built dist/ folders. The deploy
  // dialog calls this on open to pre-fill (and lock) the Sub-app
  // folder field. Returns all candidates sorted newest-first; the
  // `auto_pick` is what the deploy endpoint would use by default.
  detectedDist: (sessionId: string) =>
    request<DetectedDist>(
      `/api/sessions/${encodeURIComponent(sessionId)}/detected-dist`,
    ),
  // Just the deploys made from THIS session — chat strip renders these
  // as pills with Open / Delete controls.
  deployedApps: (sessionId: string) =>
    request<DeployedApp[]>(
      `/api/sessions/${encodeURIComponent(sessionId)}/deployed-apps`,
    ),
};
