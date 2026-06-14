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
  // Last real `input_tokens` from the most recent LLM call. The chat
  // page reads this on mount to seed the context chip synchronously,
  // so the user doesn't see "0% used" flash before the WS event arrives.
  last_context_used?: number | null;
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
  // Nested state — tools fired by THIS sub-agent (events stamped with this
  // agent_id arrive from the backend and get folded in here, not into the
  // parent turn's activity tree). Same applies to live token deltas.
  tools: ToolEvent[];
  liveInputTokens: number;
  liveOutputTokens: number;
  // Per-LLM-call breakdown for THIS sub-agent — one entry per `token_update`
  // event stamped with this agent_id. Lets the UI show "the sub-agent made N
  // model calls, each cost X tokens".
  llmCalls: LlmCall[];
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
  previewTruncated?: boolean;   // true when the backend capped the preview (rare; outputs > ~100KB)
  status: "running" | "done" | "error";
  startedAt: number;
  endedAt?: number;             // set when tool_done lands; used to show duration
}

// Per-turn end-of-turn metrics. Token counts are PER TURN (the frontend
// sums across turns to display session totals).
//
// `cost_*_usd` are the per-component cost sub-totals emitted by the server
// from `CostEstimate` — the same model-priced breakdown that produces the
// total `cost_usd`. The UI uses them to show "cost of in vs cost of out"
// and the cache-savings split without re-pricing on the client. They're
// optional in the type so older session replays (saved before the field
// was added) still typecheck.
export interface TurnSummary {
  tools_used: number;
  duration_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  cost_input_usd?: number;
  cost_output_usd?: number;
  cost_cache_read_usd?: number;
  cost_cache_write_usd?: number;
}

// One complete turn — user prompt + everything that happened in response.
// The transcript on the chat page is a list of these.
//
// We keep BOTH a chronological `blocks` timeline (for rendering — Claude-Code
// style: text / tools / thinking / sub-agents interleaved in the order they
// actually happened) AND the legacy flat collections (kept so the stats
// footer, debug panel, and per-agent lookup remain O(1)). Both views are
// derived from the same event stream.
export interface Turn {
  id: string;
  userPrompt: string;
  startedAt: number;          // ms epoch when the user sent the prompt
  assistantText: string;      // accumulates as chunks stream in
  thinkingText: string;       // model reasoning — kept separate from the visible answer
  isStreaming: boolean;       // true until assistant_text(done=true)
  tools: ToolEvent[];         // ONLY orchestrator-level tools (sub-agent tools live under their AgentRecord)
  fileChanges: FileChange[];
  agents: Record<string, AgentRecord>;
  commits: CommitRecord[];
  blocks: TimelineBlock[];    // chronological view — drives TurnCard rendering
  liveInputTokens: number;
  liveOutputTokens: number;
  summary: TurnSummary | null;
  error: string | null;
}

// One row in the chronological turn timeline. Heterogeneous — each kind
// renders with its own visual treatment (prose / mono / nested card / diff).
// Identity-only refs (agentId, toolId) for `agent` / `tool` because the
// underlying records live in `turn.agents` / `turn.tools` and may update
// after the block is pushed (e.g. a running tool transitions to done).
export type TimelineBlock =
  | { id: string; kind: "text";     text: string; startedAt: number }
  | { id: string; kind: "thinking"; text: string; startedAt: number }
  | { id: string; kind: "tool";     toolId: string;  ts: number }
  | { id: string; kind: "agent";    agentId: string; ts: number }
  | { id: string; kind: "file";     file: FileChange }
  | { id: string; kind: "commit";   commit: CommitRecord }
  | { id: string; kind: "llm_call"; ts: number; inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheCreationTokens: number };

// Per-LLM-call record kept INSIDE an AgentRecord so a sub-agent's individual
// iterations show up nested under it (orchestrator's calls live in
// `turn.blocks` as `llm_call` entries instead).
export interface LlmCall {
  ts: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
}

// Pinned-header session totals computed by summing per-turn metrics.
// `cacheWriteTokens` and the per-component `costCacheReadUsd` /
// `costInputUsd` / `costOutputUsd` are aggregated alongside the existing
// `cacheReadTokens` / `costUsd` so the chip can show "new" tokens and a
// cost-of-in vs cost-of-out split. The server already populates
// `TurnSummary.cache_write_tokens`; the frontend just wasn't summing it.
export interface SessionTotals {
  turns: number;
  tools: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
  costUsd: number;
  costCacheReadUsd: number;
  costInputUsd: number;
  costOutputUsd: number;
  durationMs: number;
}
