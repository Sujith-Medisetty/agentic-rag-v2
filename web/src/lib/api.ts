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

export interface AuthStatus { needs_setup: boolean }
export interface AuthToken  { token: string }

export const authApi = {
  status: () =>
    request<AuthStatus>("/api/auth/status", { skipAuth: true }),
  setup: (passcode: string) =>
    request<AuthToken>("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({ passcode }),
      skipAuth: true,
    }),
  login: (passcode: string, device_label?: string) =>
    request<AuthToken>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ passcode, device_label }),
      skipAuth: true,
    }),
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
};
