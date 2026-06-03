// Chat page — turn-based transcript.
//
// State model: instead of "messages + side activity", every user prompt opens
// a NEW Turn that collects everything the agent does in response (tools,
// file changes, sub-agent spawns, commits, end-of-turn summary). The page
// renders a vertical stack of TurnCards. Each turn is self-contained — you
// can scroll back to any turn and see exactly what happened.
//
// Event routing:
//   user_message              → open a new currentTurn
//   assistant_text(chunk)     → append to currentTurn.assistantText
//   assistant_text(done=true) → mark currentTurn.isStreaming = false
//   tool_start / tool_done    → push / patch currentTurn.tools
//   file_changed              → push to currentTurn.fileChanges
//   agent_spawn / agent_status_update → update currentTurn.agents
//   commit_made / commit_skipped      → push to currentTurn.commits
//   push_done                 → refresh git info (no per-turn card)
//   turn_summary              → freeze currentTurn (push to turns[]),
//                                update session totals
//   error                     → set currentTurn.error
//   todo_update               → updates the sticky PlanPanel (turn-independent)
//
// Reconstructing on mount: fetch /messages + /events, walk the event log,
// fold into turns[]. This gives full restore-on-reload behaviour.

import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { sessionApi } from "@/lib/api";
import { openEventStream } from "@/lib/ws";
import type {
  LiveEvent, TodoItem, AgentRecord, FileChange, GitInfo,
  CommitRecord, ToolEvent, TurnSummary, Turn, SessionTotals,
} from "@/lib/types";
import PlanPanel from "@/components/PlanPanel";
import TurnCard from "@/components/TurnCard";
import RunningTotals from "@/components/RunningTotals";

const EMPTY_TOTALS: SessionTotals = {
  turns: 0, tools: 0,
  inputTokens: 0, outputTokens: 0, cacheReadTokens: 0,
  costUsd: 0, durationMs: 0,
};

// ============================================================================
// ChatPage
// ============================================================================

export default function ChatPage() {
  const { projectId, sessionId } = useParams<{
    projectId: string; sessionId: string;
  }>();

  const [turns, setTurns] = useState<Turn[]>([]);
  const [currentTurn, setCurrentTurn] = useState<Turn | null>(null);
  const [plan, setPlan] = useState<TodoItem[]>([]);
  const [git, setGit] = useState<GitInfo | null>(null);
  const [pushing, setPushing] = useState(false);
  const [wsStatus, setWsStatus] = useState<"connecting" | "open" | "closed" | "error">("connecting");
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // ---- Initial reconstruction from history + events ---------------------
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const [events, gitInfo] = await Promise.all([
          sessionApi.events(sessionId),
          sessionApi.git(sessionId).catch(() => null),
        ]);
        if (cancelled) return;
        if (gitInfo) setGit(gitInfo);
        // Walk the event log in chronological order, folding into turns.
        const { turns: rebuilt, plan: replayedPlan } = rebuildTranscript(
          events.map((e) => ({
            kind: e.kind, payload: e.payload, ts: e.created_at * 1000,
          })),
        );
        setTurns(rebuilt);
        setPlan(replayedPlan);
      } catch (e: any) {
        if (!cancelled) setLoadErr(e?.message ?? "failed to load history");
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  // ---- WebSocket subscription -------------------------------------------
  useEffect(() => {
    if (!sessionId) return;
    const handle = openEventStream(
      sessionId,
      (ev) => handleEvent(ev),
      (s) => setWsStatus(s),
    );
    return () => handle.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Apply ONE event to current state. Pure function over (turns, currentTurn,
  // plan) — but expressed as setter calls because React state is split.
  function handleEvent(ev: LiveEvent) {
    const p = ev.payload as Record<string, any>;

    switch (ev.kind) {
      // --- Plan (turn-independent) ---
      case "todo_update": {
        const items = Array.isArray(p.items) ? (p.items as TodoItem[]) : [];
        setPlan(items);
        return;
      }

      // --- Turn lifecycle ---
      case "user_message": {
        setCurrentTurn((curr) => {
          if (curr && curr.userPrompt === String(p.text) && curr.isStreaming) {
            return curr;   // dedupe — backend echo + our optimistic add
          }
          return makeTurn(String(p.text ?? ""), ev.ts);
        });
        return;
      }
      case "assistant_text": {
        const text = String(p.text ?? "");
        const done = !!p.done;
        setCurrentTurn((curr) => {
          if (!curr) return curr;
          return {
            ...curr,
            assistantText: curr.assistantText + text,
            isStreaming: !done,
          };
        });
        return;
      }
      case "turn_summary": {
        const summary: TurnSummary = {
          tools_used:         Number(p.tools_used ?? 0),
          duration_ms:        Number(p.duration_ms ?? 0),
          input_tokens:       Number(p.input_tokens ?? 0),
          output_tokens:      Number(p.output_tokens ?? 0),
          cache_read_tokens:  Number(p.cache_read_tokens ?? 0),
          cache_write_tokens: Number(p.cache_write_tokens ?? 0),
          cost_usd:           Number(p.cost_usd ?? 0),
        };
        setCurrentTurn((curr) => {
          if (!curr) return null;
          const finished: Turn = { ...curr, summary, isStreaming: false };
          setTurns((ts) => [...ts, finished]);
          return null;
        });
        return;
      }
      case "error": {
        // Error means the turn is over — clear isStreaming RIGHT NOW so the
        // elapsed timer / "Thinking…" / streaming dot stop instantly. We
        // also expect a turn_summary to follow (backend sends both on the
        // error path now), but don't wait for it to freeze the UI.
        const msg = String(p.message ?? "unknown error");
        setCurrentTurn((curr) => curr ? {
          ...curr, error: msg, isStreaming: false,
        } : curr);
        return;
      }

      // --- Tool stream ---
      case "tool_start": {
        const tool: ToolEvent = {
          id: `${ev.ts}-${p.tool}-${Math.random().toString(36).slice(2, 6)}`,
          tool: String(p.tool ?? "?"),
          target: typeof p.target === "string" ? p.target : undefined,
          status: "running",
          startedAt: ev.ts,
        };
        setCurrentTurn((curr) => curr ? { ...curr, tools: [...curr.tools, tool] } : curr);
        return;
      }
      case "tool_done": {
        const toolName = String(p.tool ?? "?");
        const isError = !!p.error;
        const preview = typeof p.preview === "string" ? p.preview : undefined;
        setCurrentTurn((curr) => {
          if (!curr) return curr;
          const tools = [...curr.tools];
          for (let i = tools.length - 1; i >= 0; i--) {
            if (tools[i].tool === toolName && tools[i].status === "running") {
              tools[i] = { ...tools[i], status: isError ? "error" : "done", preview };
              break;
            }
          }
          return { ...curr, tools };
        });
        return;
      }

      // --- File diffs ---
      case "file_changed": {
        const fc: FileChange = {
          id:    `${p.path}-${ev.ts}`,
          path:  String(p.path ?? ""),
          kind:  p.kind === "create" ? "create" : "edit",
          diff:  String(p.diff ?? ""),
          bytes: Number(p.bytes ?? 0),
          ts:    ev.ts,
        };
        setCurrentTurn((curr) => curr ? {
          ...curr, fileChanges: [...curr.fileChanges, fc],
        } : curr);
        return;
      }

      // --- Sub-agents ---
      case "agent_spawn": {
        const r: AgentRecord = {
          agent_id:      String(p.agent_id ?? ""),
          description:   String(p.description ?? ""),
          subagent_type: String(p.subagent_type ?? "general-purpose"),
          name:          String(p.name ?? ""),
          model:         String(p.model ?? ""),
          status:        "running",
          output_file:   "",
          error:         "",
          spawned_at:    ev.ts,
          updated_at:    ev.ts,
        };
        setCurrentTurn((curr) => curr ? {
          ...curr, agents: { ...curr.agents, [r.agent_id]: r },
        } : curr);
        return;
      }
      case "agent_status_update": {
        const id = String(p.agent_id ?? "");
        setCurrentTurn((curr) => {
          if (!curr) return curr;
          const existing = curr.agents[id];
          if (!existing) return curr;
          return {
            ...curr,
            agents: {
              ...curr.agents,
              [id]: {
                ...existing,
                status:      (String(p.status ?? existing.status) as AgentRecord["status"]),
                output_file: String(p.output_file ?? existing.output_file),
                error:       String(p.error ?? ""),
                updated_at:  ev.ts,
              },
            },
          };
        });
        return;
      }

      // --- Commits / push ---
      case "commit_made": {
        const cr: CommitRecord = {
          id:      `${p.sha}-${ev.ts}`,
          sha:     String(p.sha ?? ""),
          branch:  String(p.branch ?? ""),
          message: String(p.message ?? ""),
          files:   Array.isArray(p.files) ? (p.files as string[]) : [],
          ts:      ev.ts,
        };
        setCurrentTurn((curr) => curr ? { ...curr, commits: [...curr.commits, cr] } : curr);
        if (sessionId) sessionApi.git(sessionId).then(setGit).catch(() => {});
        return;
      }
      case "commit_skipped": {
        const reason = String(p.reason ?? "unknown");
        setCurrentTurn((curr) => curr ? {
          ...curr,
          commits: [...curr.commits, {
            id: `skip-${ev.ts}`, sha: "", branch: String(p.branch ?? ""),
            message: `(skipped — ${reason})`, files: [], ts: ev.ts,
          }],
        } : curr);
        return;
      }
      case "push_done": {
        if (sessionId) sessionApi.git(sessionId).then(setGit).catch(() => {});
        return;
      }
    }
  }

  // ---- Auto-scroll on new content ---------------------------------------
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [turns.length, currentTurn?.assistantText, currentTurn?.tools.length]);

  // ---- Session totals — sum of per-turn metrics -------------------------
  const totals = useMemo<SessionTotals>(() => {
    return turns.reduce<SessionTotals>((acc, t) => {
      if (!t.summary) return acc;
      return {
        turns:           acc.turns + 1,
        tools:           acc.tools + t.summary.tools_used,
        inputTokens:     acc.inputTokens + t.summary.input_tokens,
        outputTokens:    acc.outputTokens + t.summary.output_tokens,
        cacheReadTokens: acc.cacheReadTokens + t.summary.cache_read_tokens,
        costUsd:         acc.costUsd + t.summary.cost_usd,
        durationMs:      acc.durationMs + t.summary.duration_ms,
      };
    }, EMPTY_TOTALS);
  }, [turns]);

  // ---- Manual push -------------------------------------------------------
  const pushNow = async () => {
    if (!sessionId || pushing) return;
    setPushing(true);
    try {
      await sessionApi.push(sessionId);
      const g = await sessionApi.git(sessionId).catch(() => null);
      if (g) setGit(g);
    } finally {
      setPushing(false);
    }
  };

  // ---- Send a new prompt -------------------------------------------------
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || !sessionId || sending) return;
    setSending(true);
    // Optimistic open of a new turn so the user sees their prompt instantly.
    // The backend's user_message event echoes it back; handleEvent dedupes.
    setCurrentTurn(makeTurn(text, Date.now()));
    setInput("");
    try {
      await sessionApi.post(sessionId, text);
    } catch (e: any) {
      setCurrentTurn((curr) => curr ? {
        ...curr, error: e?.message ?? "send failed", isStreaming: false,
      } : curr);
    } finally {
      setSending(false);
    }
  };

  // ============================================================================
  return (
    <div className="flex h-screen flex-col">
      {/* Header — sticky, has back link + git + push + totals + connection */}
      <header className="chrome-bar flex items-center justify-between gap-3 px-4 pt-[max(0.75rem,env(safe-area-inset-top))] pb-3">
        <Link
          to={`/p/${projectId}`}
          className="group flex shrink-0 items-center gap-1.5 text-sm text-muted transition-colors hover:text-accent"
        >
          <span className="transition-transform group-hover:-translate-x-0.5">←</span>
          <span>back</span>
        </Link>
        <div className="flex min-w-0 items-center gap-2">
          <BranchBadge git={git} />
          {git?.has_remote && git.ahead > 0 && (
            <button
              type="button"
              onClick={pushNow}
              disabled={pushing}
              className="pill pill-accent min-h-touch disabled:opacity-50"
              title={`Push ${git.ahead} commit(s) to origin`}
            >
              {pushing ? "Pushing…" : `↑ Push ${git.ahead}`}
            </button>
          )}
          <RunningTotals totals={totals} />
          <StatusPill status={wsStatus} />
        </div>
      </header>

      {/* Sticky plan panel — turn-independent state */}
      <PlanPanel items={plan} />

      {/* Scrollable transcript — mono + 13px + tight leading via .transcript */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4">
        <div className="transcript mx-auto flex max-w-4xl flex-col">
          {loadErr && (
            <div className="mt-4 rounded border border-danger/40 bg-danger/10 p-3 text-danger">
              {loadErr}
            </div>
          )}
          {turns.map((t, i) => (
            <TurnCard key={t.id} turn={t} index={i} />
          ))}
          {currentTurn && (
            <TurnCard turn={currentTurn} index={turns.length} />
          )}
          {turns.length === 0 && !currentTurn && !loadErr && (
            <EmptyState />
          )}
        </div>
      </div>

      {/* Input — pinned bottom, safe-area aware */}
      <form
        onSubmit={send}
        className="chrome-bar-bottom px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
      >
        <div className="mx-auto flex max-w-4xl gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type a message…"
            className="field min-h-touch flex-1 text-base md:text-sm"
            autoCapitalize="sentences"
            autoCorrect="on"
            enterKeyHint="send"
            disabled={sending}
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="btn-primary min-h-touch min-w-touch"
          >
            {sending ? "…" : "Send"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ============================================================================
// Helpers + small components
// ============================================================================

function makeTurn(userPrompt: string, ts: number): Turn {
  return {
    id: `turn-${ts}-${Math.random().toString(36).slice(2, 6)}`,
    userPrompt, startedAt: ts,
    assistantText: "", isStreaming: true,
    tools: [], fileChanges: [], agents: {}, commits: [],
    summary: null, error: null,
  };
}

// Replay an event log into turns + final plan. Pure function, used both on
// mount (to restore state) and is the model the live handler emulates.
function rebuildTranscript(events: LiveEvent[]): {
  turns: Turn[]; plan: TodoItem[];
} {
  const completed: Turn[] = [];
  let curr: Turn | null = null;
  let plan: TodoItem[] = [];

  for (const ev of events) {
    const p = ev.payload as Record<string, any>;
    switch (ev.kind) {
      case "user_message":
        if (curr) completed.push({ ...curr, isStreaming: false });
        curr = makeTurn(String(p.text ?? ""), ev.ts);
        break;
      case "assistant_text":
        if (!curr) break;
        curr.assistantText += String(p.text ?? "");
        if (p.done) curr.isStreaming = false;
        break;
      case "turn_summary":
        if (!curr) break;
        curr.summary = {
          tools_used:         Number(p.tools_used ?? 0),
          duration_ms:        Number(p.duration_ms ?? 0),
          input_tokens:       Number(p.input_tokens ?? 0),
          output_tokens:      Number(p.output_tokens ?? 0),
          cache_read_tokens:  Number(p.cache_read_tokens ?? 0),
          cache_write_tokens: Number(p.cache_write_tokens ?? 0),
          cost_usd:           Number(p.cost_usd ?? 0),
        };
        curr.isStreaming = false;
        completed.push(curr);
        curr = null;
        break;
      case "tool_start":
        if (!curr) break;
        curr.tools.push({
          id: `${ev.ts}-${p.tool}-${curr.tools.length}`,
          tool: String(p.tool ?? "?"),
          target: typeof p.target === "string" ? p.target : undefined,
          status: "running", startedAt: ev.ts,
        });
        break;
      case "tool_done":
        if (!curr) break;
        for (let i = curr.tools.length - 1; i >= 0; i--) {
          if (curr.tools[i].tool === p.tool && curr.tools[i].status === "running") {
            curr.tools[i] = {
              ...curr.tools[i],
              status: p.error ? "error" : "done",
              preview: typeof p.preview === "string" ? p.preview : undefined,
            };
            break;
          }
        }
        break;
      case "file_changed":
        if (!curr) break;
        curr.fileChanges.push({
          id: `${p.path}-${ev.ts}`,
          path: String(p.path ?? ""),
          kind: p.kind === "create" ? "create" : "edit",
          diff: String(p.diff ?? ""),
          bytes: Number(p.bytes ?? 0),
          ts: ev.ts,
        });
        break;
      case "agent_spawn":
        if (!curr) break;
        curr.agents[String(p.agent_id ?? "")] = {
          agent_id: String(p.agent_id ?? ""),
          description: String(p.description ?? ""),
          subagent_type: String(p.subagent_type ?? "general-purpose"),
          name: String(p.name ?? ""), model: String(p.model ?? ""),
          status: "running", output_file: "", error: "",
          spawned_at: ev.ts, updated_at: ev.ts,
        };
        break;
      case "agent_status_update":
        if (!curr) break;
        {
          const id = String(p.agent_id ?? "");
          const existing = curr.agents[id];
          if (existing) {
            curr.agents[id] = {
              ...existing,
              status: (String(p.status ?? existing.status) as AgentRecord["status"]),
              output_file: String(p.output_file ?? existing.output_file),
              error: String(p.error ?? ""),
              updated_at: ev.ts,
            };
          }
        }
        break;
      case "commit_made":
        if (!curr) break;
        curr.commits.push({
          id: `${p.sha}-${ev.ts}`,
          sha: String(p.sha ?? ""), branch: String(p.branch ?? ""),
          message: String(p.message ?? ""),
          files: Array.isArray(p.files) ? (p.files as string[]) : [],
          ts: ev.ts,
        });
        break;
      case "commit_skipped":
        if (!curr) break;
        curr.commits.push({
          id: `skip-${ev.ts}`, sha: "", branch: String(p.branch ?? ""),
          message: `(skipped — ${p.reason ?? "unknown"})`, files: [], ts: ev.ts,
        });
        break;
      case "error":
        if (curr) {
          curr.error = String(p.message ?? "unknown error");
          curr.isStreaming = false;
        }
        break;
      case "todo_update":
        plan = Array.isArray(p.items) ? (p.items as TodoItem[]) : [];
        break;
    }
  }
  // If the log ended mid-turn, keep that turn open in the rebuilt state too —
  // the live WS handler will continue updating it.
  if (curr) completed.push({ ...curr, isStreaming: false });
  return { turns: completed, plan };
}

function StatusPill({
  status,
}: { status: "connecting" | "open" | "closed" | "error" }) {
  const map = {
    connecting: { cls: "pill",         dot: "bg-warn animate-pulse-soft", label: "connecting" },
    open:       { cls: "pill pill-success", dot: "bg-success",            label: "live" },
    closed:     { cls: "pill",         dot: "bg-subtle",                  label: "offline" },
    error:      { cls: "pill pill-danger",  dot: "bg-danger",             label: "error" },
  } as const;
  const m = map[status];
  return (
    <div className={`${m.cls} text-[10px]`}>
      <span className={`h-1.5 w-1.5 rounded-full ${m.dot}`} />
      {m.label}
    </div>
  );
}

function BranchBadge({ git }: { git: GitInfo | null }) {
  if (!git || !git.is_git_repo || !git.branch) return null;
  return (
    <div
      className="pill min-w-0"
      title={git.last_commit_subject
        ? `${git.last_commit_sha} ${git.last_commit_subject}`
        : git.branch}
    >
      <span className="text-subtle">⌥</span>
      <span className="truncate font-mono text-text">{git.branch}</span>
      {git.dirty && <span className="text-warn" title="uncommitted changes">●</span>}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="mt-16 flex flex-col items-center gap-4 text-center">
      <div
        aria-hidden
        className="relative flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-gradient shadow-glow-accent"
      >
        <span className="text-bg text-2xl font-bold">⌘</span>
      </div>
      <div className="text-xl font-semibold tracking-tight text-text">
        Start a conversation
      </div>
      <div className="max-w-md text-sm text-muted">
        Type a request below. Each prompt opens a turn that shows the agent's
        reply, every tool it calls, files it changes, and the tokens it used.
      </div>
    </div>
  );
}
