// TypeScript mirrors of the Pydantic models in server/schemas.py.
// Keep these in sync with the backend.

export type BranchStrategy = "session" | "current";

export interface Project {
  id: string;
  name: string;
  workspace_path: string;
  created_at: number;
  // Phase 4 settings — present on every project response.
  auto_commit_enabled: boolean;
  auto_push_enabled: boolean;
  branch_strategy: BranchStrategy;
}

export interface ProjectSettingsUpdate {
  auto_commit_enabled?: boolean;
  auto_push_enabled?: boolean;
  branch_strategy?: BranchStrategy;
}

export interface GitInfo {
  is_git_repo: boolean;
  branch: string;
  last_commit_sha: string;
  last_commit_subject: string;
  has_remote: boolean;
  ahead: number;
  behind: number;
  dirty: boolean;
}

export interface PushResult {
  pushed: boolean;
  branch: string;
  remote: string;
  error: string;
}

export interface Session {
  id: string;
  project_id: string;
  name: string;
  last_active_at: number;
  created_at: number;
}

export interface Message {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: number;
}

export interface EventRecord {
  id: string;
  session_id: string;
  kind: string;
  payload: Record<string, unknown>;
  created_at: number;
}

// Live event envelope pushed over the WebSocket.
// Mirrors server.reporter.SessionBus.publish().
export interface LiveEvent {
  kind: string;
  payload: Record<string, unknown>;
  ts: number;
}

// ---- Phase 2 payload shapes ---------------------------------------------

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface TodoItem {
  content: string;
  status: TodoStatus;
  activeForm: string;
}

export type AgentStatus = "running" | "completed" | "failed" | "unknown";

export interface AgentRecord {
  agent_id: string;
  description: string;
  subagent_type: string;
  name: string;
  model: string;
  status: AgentStatus;
  output_file: string;
  error: string;
  spawned_at: number;       // ms epoch
  updated_at: number;       // ms epoch
}

export type FileChangeKind = "create" | "edit";

export interface FileChange {
  id: string;               // synthetic — `${path}-${ts}` for React keys
  path: string;
  kind: FileChangeKind;
  diff: string;             // unified diff text (or full content for create)
  bytes: number;
  ts: number;
}

// In-memory shape held in ChatPage state for rendering recent commits.
export interface CommitRecord {
  id: string;        // synthetic — `${sha}-${ts}` for React keys
  sha: string;
  branch: string;
  message: string;
  files: string[];
  ts: number;
}

// In-memory tool-call record (one tool invocation inside a turn).
export interface ToolEvent {
  id: string;
  tool: string;
  target?: string;
  preview?: string;
  status: "running" | "done" | "error";
  startedAt: number;
}

// Per-turn end-of-turn metrics. Token counts are PER TURN (the frontend
// sums across turns to display session totals).
export interface TurnSummary {
  tools_used: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
}

// One complete turn — user prompt + everything that happened in response.
// The transcript on the chat page is a list of these.
export interface Turn {
  id: string;
  userPrompt: string;
  startedAt: number;          // ms epoch when the user sent the prompt
  assistantText: string;      // accumulates as chunks stream in
  isStreaming: boolean;       // true until assistant_text(done=true)
  tools: ToolEvent[];
  fileChanges: FileChange[];
  agents: Record<string, AgentRecord>;
  commits: CommitRecord[];
  summary: TurnSummary | null;
  error: string | null;
}

// Pinned-header session totals computed by summing per-turn metrics.
export interface SessionTotals {
  turns: number;
  tools: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  costUsd: number;
  durationMs: number;
}
