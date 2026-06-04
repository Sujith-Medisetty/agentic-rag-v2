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
import { formatDuration } from "@/lib/format";

// Single source of truth for slash commands. Used by both the inline
// autocomplete picker (live, as the user types) and the /help overlay (full
// reference). Keep `cmd` lowercase and starting with "/".
const SLASH_COMMANDS: { cmd: string; desc: string }[] = [
  { cmd: "/help",     desc: "Show all commands and keyboard shortcuts" },
  { cmd: "/clear",    desc: "Clear the current view (server history is kept)" },
  { cmd: "/stop",     desc: "Cancel the in-flight turn (same as the Stop button)" },
  { cmd: "/cancel",   desc: "Alias of /stop" },
  { cmd: "/history",  desc: "Show the last 50 prompts you've sent in this session" },
  { cmd: "/compact",  desc: "Compact the agent's context now (summarise old turns, keep recent)" },
  { cmd: "/debug",    desc: "Toggle the raw WebSocket event panel (for troubleshooting)" },
];

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

  // ---- Debug stream — last N raw WS events so we can SEE what's flowing.
  // Toggle via the bug icon in the header; persists across reloads so it stays
  // on while we're triaging the live-streaming issue.
  const [debugOpen, setDebugOpen] = useState<boolean>(() => {
    try { return localStorage.getItem("debug_stream") === "1"; }
    catch { return false; }
  });
  const [debugEvents, setDebugEvents] = useState<
    { kind: string; payload: any; ts: number }[]
  >([]);
  const toggleDebug = () => {
    setDebugOpen((v) => {
      const next = !v;
      try { localStorage.setItem("debug_stream", next ? "1" : "0"); } catch {}
      return next;
    });
  };

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
      (ev) => {
        // Tap every event into the debug stream FIRST so we capture it even
        // if handleEvent throws / drops it (which is what we're trying to
        // debug). Keep last 200 entries.
        setDebugEvents((prev) => {
          const next = [...prev, { kind: ev.kind, payload: ev.payload, ts: ev.ts }];
          return next.length > 200 ? next.slice(-200) : next;
        });
        handleEvent(ev);
      },
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
          if (done) {
            // Final flush — canonical tag-stripped text. Replace the most-recent
            // text block (so we don't double-render the streamed answer) AND
            // replace the legacy assistantText field for stats / debug parity.
            return {
              ...curr,
              assistantText: text || curr.assistantText,
              blocks: text
                ? _replaceLastTextBlock(curr.blocks, text, ev.ts)
                : curr.blocks,
              isStreaming: false,
            };
          }
          return {
            ...curr,
            assistantText: curr.assistantText + text,
            blocks: _appendTextChunk(curr.blocks, "text", text, ev.ts),
            isStreaming: true,
          };
        });
        return;
      }
      case "token_update": {
        // Live in/out token deltas — every model call inside a turn emits
        // ONE token_update after it completes (see _stream_model_call in
        // agents/nodes.py). We use that fact to surface PER-CALL token usage
        // in the UI, not just the running total: each event either pushes a
        // chronological `llm_call` block (orchestrator) or appends an
        // `LlmCall` entry inside the sub-agent that issued it.
        const inDelta  = Number(p.input_delta  ?? 0);
        const outDelta = Number(p.output_delta ?? 0);
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        if (inDelta === 0 && outDelta === 0) return;  // skip zero-deltas
        setCurrentTurn((curr) => {
          if (!curr) return curr;
          if (aid && curr.agents[aid]) {
            const a = curr.agents[aid];
            return {
              ...curr,
              agents: {
                ...curr.agents,
                [aid]: {
                  ...a,
                  liveInputTokens:  a.liveInputTokens  + inDelta,
                  liveOutputTokens: a.liveOutputTokens + outDelta,
                  llmCalls: [...a.llmCalls, {
                    ts: ev.ts, inputTokens: inDelta, outputTokens: outDelta,
                  }],
                },
              },
            };
          }
          return {
            ...curr,
            liveInputTokens:  curr.liveInputTokens  + inDelta,
            liveOutputTokens: curr.liveOutputTokens + outDelta,
            blocks: [...curr.blocks, {
              id: _newBlockId("llm_call", ev.ts, curr.blocks.length),
              kind: "llm_call", ts: ev.ts,
              inputTokens: inDelta, outputTokens: outDelta,
            }],
          };
        });
        return;
      }
      case "thinking_text": {
        // Model reasoning chunk — appended to the chronological timeline as
        // its own block kind so it renders distinct from the visible answer.
        const text = String(p.text ?? "");
        if (!text) return;
        setCurrentTurn((curr) => curr ? {
          ...curr,
          thinkingText: curr.thinkingText + text,
          blocks: _appendTextChunk(curr.blocks, "thinking", text, ev.ts),
        } : curr);
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
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        setCurrentTurn((curr) => {
          if (!curr) return curr;
          if (aid && curr.agents[aid]) {
            // Sub-agent's tool — nest under that agent (no top-level block).
            return {
              ...curr,
              agents: {
                ...curr.agents,
                [aid]: {
                  ...curr.agents[aid],
                  tools: [...curr.agents[aid].tools, tool],
                },
              },
            };
          }
          // Orchestrator-level tool: append to the flat list AND push a block.
          return {
            ...curr,
            tools: [...curr.tools, tool],
            blocks: [...curr.blocks, {
              id: _newBlockId("tool", ev.ts, curr.blocks.length),
              kind: "tool", toolId: tool.id, ts: ev.ts,
            }],
          };
        });
        return;
      }
      case "tool_done": {
        const toolName = String(p.tool ?? "?");
        const isError = !!p.error;
        const preview = typeof p.preview === "string" ? p.preview : undefined;
        const previewTruncated = !!p.preview_truncated;
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        setCurrentTurn((curr) => {
          if (!curr) return curr;
          const patchToolList = (list: ToolEvent[]) => {
            const next = [...list];
            for (let i = next.length - 1; i >= 0; i--) {
              if (next[i].tool === toolName && next[i].status === "running") {
                next[i] = {
                  ...next[i],
                  status: isError ? "error" : "done",
                  preview,
                  previewTruncated,
                  endedAt: ev.ts,
                };
                break;
              }
            }
            return next;
          };
          if (aid && curr.agents[aid]) {
            return {
              ...curr,
              agents: {
                ...curr.agents,
                [aid]: {
                  ...curr.agents[aid],
                  tools: patchToolList(curr.agents[aid].tools),
                },
              },
            };
          }
          return { ...curr, tools: patchToolList(curr.tools) };
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
          ...curr,
          fileChanges: [...curr.fileChanges, fc],
          blocks: [...curr.blocks, {
            id: _newBlockId("file", ev.ts, curr.blocks.length),
            kind: "file", file: fc,
          }],
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
          tools:         [],
          liveInputTokens:  0,
          liveOutputTokens: 0,
          llmCalls:        [],
        };
        setCurrentTurn((curr) => curr ? {
          ...curr,
          agents: { ...curr.agents, [r.agent_id]: r },
          blocks: [...curr.blocks, {
            id: _newBlockId("agent", ev.ts, curr.blocks.length),
            kind: "agent", agentId: r.agent_id, ts: ev.ts,
          }],
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
        setCurrentTurn((curr) => curr ? {
          ...curr,
          commits: [...curr.commits, cr],
          blocks: [...curr.blocks, {
            id: _newBlockId("commit", ev.ts, curr.blocks.length),
            kind: "commit", commit: cr,
          }],
        } : curr);
        if (sessionId) sessionApi.git(sessionId).then(setGit).catch(() => {});
        return;
      }
      case "commit_skipped": {
        const reason = String(p.reason ?? "unknown");
        const cr: CommitRecord = {
          id: `skip-${ev.ts}`, sha: "", branch: String(p.branch ?? ""),
          message: `(skipped — ${reason})`, files: [], ts: ev.ts,
        };
        setCurrentTurn((curr) => curr ? {
          ...curr,
          commits: [...curr.commits, cr],
          blocks: [...curr.blocks, {
            id: _newBlockId("commit", ev.ts, curr.blocks.length),
            kind: "commit", commit: cr,
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
  }, [
    turns.length,
    currentTurn?.assistantText,
    currentTurn?.thinkingText,
    currentTurn?.tools.length,
  ]);

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
  // --- Prompt history (↑/↓ recalls past user messages, like a shell) ----
  // Stored in localStorage per session so refreshing doesn't lose it. Holds
  // up to the last 50 prompts; navigation index of -1 means "live input
  // (not browsing history)".
  const HISTORY_KEY = sessionId ? `chat_history:${sessionId}` : "";
  const [historyIdx, setHistoryIdx] = useState(-1);
  const [draftBeforeHistory, setDraftBeforeHistory] = useState("");
  const getHistory = (): string[] => {
    if (!HISTORY_KEY) return [];
    try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); }
    catch { return []; }
  };
  const pushHistory = (text: string) => {
    if (!HISTORY_KEY || !text) return;
    const h = getHistory();
    // Dedupe consecutive identical entries.
    if (h[h.length - 1] === text) return;
    h.push(text);
    while (h.length > 50) h.shift();
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(h)); } catch {}
  };

  // --- Help overlay toggle (/help) --------------------------------------
  const [helpOpen, setHelpOpen] = useState(false);

  // --- Slash-command autocomplete --------------------------------------
  // Picker is open whenever the input starts with "/" and there are matches.
  // `slashIdx` is the highlighted row inside the visible matches list — the
  // arrow-key handler moves it, Tab/Enter accept.
  const slashMatches = (() => {
    const t = input;
    if (!t.startsWith("/")) return [];
    const lower = t.toLowerCase();
    const prefixMatches = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(lower));
    return prefixMatches.length
      ? prefixMatches
      // Fallback: substring match in case the user types "/sto" and "/stop"
      // is the only sensible completion (here it's equivalent to prefix, but
      // future commands like "/run-tests" benefit from this).
      : SLASH_COMMANDS.filter((c) => c.cmd.includes(lower.slice(1)));
  })();
  const showSlashPicker = input.startsWith("/") && slashMatches.length > 0;
  const [slashIdx, setSlashIdx] = useState(0);
  // Reset highlight to the top whenever the visible match set changes.
  useEffect(() => { setSlashIdx(0); }, [input]);

  const acceptSlashCompletion = (i = slashIdx) => {
    const m = slashMatches[i];
    if (!m) return;
    // Trailing space makes it feel finished and lets the user keep typing
    // arguments if a future command takes them.
    setInput(m.cmd + " ");
  };

  // --- Slash-command dispatcher -----------------------------------------
  // Recognizes commands typed at the start of the input box. Anything else
  // falls through to the agent. Mirrors the feel of terminal REPLs / Claude
  // Code's slash commands — kept local to the UI so we don't have to route
  // through the agent for trivial actions.
  const runSlashCommand = async (raw: string): Promise<boolean> => {
    const cmd = raw.trim().toLowerCase();
    if (!cmd.startsWith("/")) return false;
    const [head] = cmd.slice(1).split(/\s+/, 1);
    switch (head) {
      case "help":
      case "?":
        setHelpOpen(true);
        return true;
      case "clear":
        // Local-only clear: hide past turns from view. Backend history is
        // preserved — refreshing the page restores everything.
        setTurns([]);
        setCurrentTurn(null);
        setPlan([]);
        return true;
      case "stop":
      case "cancel": {
        if (!sessionId) return true;
        try { await sessionApi.cancel(sessionId); } catch {}
        return true;
      }
      case "compact": {
        if (!sessionId) return true;
        try {
          const res = await sessionApi.compact(sessionId);
          if (res.ok) {
            alert(`Context compacted: ${res.before} → ${res.after} messages.\n\nThe agent's working memory is now smaller. The chat history above is unaffected — only the agent's internal context was trimmed.`);
          } else {
            alert(`Couldn't compact: ${res.reason ?? "unknown reason"}`);
          }
        } catch (e: any) {
          alert(`Compact failed: ${e?.message ?? "request error"}`);
        }
        return true;
      }
      case "history": {
        // Drop the last 50 prompts into a transient "turn" so the user can
        // see them and re-run by clicking. Lightweight — just visual.
        const h = getHistory();
        if (h.length === 0) {
          alert("No prompt history yet.");
        } else {
          alert("Recent prompts (newest first):\n\n" +
            h.slice().reverse().map((t, i) => `${i + 1}. ${t}`).join("\n"));
        }
        return true;
      }
      case "debug":
        toggleDebug();
        return true;
      default:
        // Unknown slash: just send it through to the agent verbatim — the
        // model may know what to do (e.g. /undo, /retry might be wired
        // later). No silent swallow.
        return false;
    }
  };

  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || !sessionId || sending) return;

    // History bookkeeping — record every submitted line, reset nav cursor.
    pushHistory(text);
    setHistoryIdx(-1);
    setDraftBeforeHistory("");

    // Handle slash commands locally — don't hit the agent for /help, /clear, etc.
    if (await runSlashCommand(text)) {
      setInput("");
      return;
    }

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

  // Keyboard routing for the input box. Priority: slash picker > history nav.
  // The picker is contextual — only active when the input starts with "/" and
  // there's at least one match — so plain typing is unaffected.
  const onInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // ----- Slash picker active -----
    if (showSlashPicker) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashIdx((i) => (i + 1) % slashMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashIdx((i) => (i - 1 + slashMatches.length) % slashMatches.length);
        return;
      }
      if (e.key === "Tab") {
        // Tab = accept highlighted completion, stay in input (don't submit).
        e.preventDefault();
        acceptSlashCompletion();
        return;
      }
      if (e.key === "Enter") {
        // If the user is hovering over a suggestion and the input doesn't
        // already exactly match a command, accept the suggestion first and
        // let the form submit handler (`send`) run the resulting command.
        const exact = SLASH_COMMANDS.find((c) => c.cmd === input.trim().toLowerCase());
        if (!exact) {
          e.preventDefault();
          acceptSlashCompletion();
          // Submit immediately if the user pressed Enter to accept — feels
          // more direct than requiring a second Enter for /clear, /stop, etc.
          setTimeout(() => {
            (e.currentTarget?.form as HTMLFormElement | undefined)?.requestSubmit();
          }, 0);
          return;
        }
      }
      if (e.key === "Escape") {
        // Close the picker by clearing the slash (cheapest cancel).
        e.preventDefault();
        setInput("");
        return;
      }
      // Other keys fall through to default text editing.
      return;
    }

    // ----- History navigation (only when picker is closed) -----
    const h = getHistory();
    if (e.key === "ArrowUp" && h.length > 0) {
      e.preventDefault();
      if (historyIdx === -1) setDraftBeforeHistory(input);
      const next = historyIdx === -1 ? h.length - 1 : Math.max(0, historyIdx - 1);
      setHistoryIdx(next);
      setInput(h[next]);
    } else if (e.key === "ArrowDown" && historyIdx !== -1) {
      e.preventDefault();
      const next = historyIdx + 1;
      if (next >= h.length) {
        setHistoryIdx(-1);
        setInput(draftBeforeHistory);
      } else {
        setHistoryIdx(next);
        setInput(h[next]);
      }
    } else if (e.key === "Escape" && historyIdx !== -1) {
      e.preventDefault();
      setHistoryIdx(-1);
      setInput(draftBeforeHistory);
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
          {currentTurn && currentTurn.isStreaming && !currentTurn.error && (
            <NowChip turn={currentTurn} />
          )}
          <RunningTotals totals={totals} />
          <StatusPill status={wsStatus} />
          <button
            type="button"
            onClick={toggleDebug}
            title="Toggle raw WebSocket event stream"
            className={`pill min-h-touch ${debugOpen ? "pill-accent" : ""}`}
          >
            ⌘ debug
          </button>
        </div>
      </header>

      {/* Sticky plan panel — turn-independent state */}
      <PlanPanel items={plan} />

      {/* Debug stream — floating raw WS event log. Use to diagnose live-event
          delivery: if events appear here in real time but the transcript
          doesn't reflect them, it's a render bug; if they only appear after
          the turn ends, it's a backend buffering bug. */}
      {debugOpen && (
        <DebugStream events={debugEvents} onClear={() => setDebugEvents([])} />
      )}

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

      {/* Input — pinned bottom, safe-area aware. While a turn is in flight the
          Send button morphs into Stop so cancelling is a single click. */}
      <form
        onSubmit={send}
        className="chrome-bar-bottom relative px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
      >
        {/* Slash-command autocomplete picker — pops up from above the input
            whenever the input starts with "/" and there are matches. ↑/↓
            move the highlight, Tab accepts, Enter accepts + sends. */}
        {showSlashPicker && (
          <div className="pointer-events-none absolute bottom-full left-0 right-0 px-3 pb-2">
            <div className="pointer-events-auto mx-auto max-w-4xl overflow-hidden rounded-lg border border-border bg-surface/95 shadow-lift backdrop-blur-md">
              <div className="flex items-center justify-between border-b border-border/60 bg-elevated/60 px-3 py-1.5">
                <span className="text-[10px] font-bold uppercase tracking-[0.16em] text-accent">
                  Commands
                </span>
                <span className="font-sans text-[10px] text-subtle">
                  <kbd className="rounded border border-border bg-bg/60 px-1 font-mono text-[10px]">↑↓</kbd> move ·{" "}
                  <kbd className="rounded border border-border bg-bg/60 px-1 font-mono text-[10px]">Tab</kbd> accept ·{" "}
                  <kbd className="rounded border border-border bg-bg/60 px-1 font-mono text-[10px]">↵</kbd> run ·{" "}
                  <kbd className="rounded border border-border bg-bg/60 px-1 font-mono text-[10px]">Esc</kbd> cancel
                </span>
              </div>
              <ul className="max-h-72 overflow-y-auto py-1">
                {slashMatches.map((m, i) => (
                  <li key={m.cmd}>
                    <button
                      type="button"
                      onMouseDown={(e) => {
                        // mouseDown (not click) so we accept before the input
                        // loses focus and the picker unmounts.
                        e.preventDefault();
                        acceptSlashCompletion(i);
                      }}
                      onMouseEnter={() => setSlashIdx(i)}
                      className={
                        "flex w-full items-baseline gap-3 px-3 py-2 text-left " +
                        (i === slashIdx ? "bg-accent/10" : "hover:bg-elevated/50")
                      }
                    >
                      <span className={
                        "shrink-0 rounded border px-1.5 py-0.5 font-mono text-tx-xs " +
                        (i === slashIdx
                          ? "border-accent/60 bg-accent/15 text-accent"
                          : "border-border bg-bg/60 text-text")
                      }>
                        {m.cmd}
                      </span>
                      <span className="truncate text-tx-sm text-muted">{m.desc}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}

        <div className="mx-auto flex max-w-4xl flex-col gap-1">
          <div className="flex items-center gap-1.5 px-1 font-sans text-[10px] text-subtle">
            <kbd className="rounded border border-border bg-elevated/60 px-1 py-px font-mono text-[10px]">↑</kbd>
            <span>prev</span>
            <kbd className="rounded border border-border bg-elevated/60 px-1 py-px font-mono text-[10px]">↓</kbd>
            <span>next</span>
            <span className="text-border">·</span>
            <button
              type="button"
              onClick={() => setHelpOpen(true)}
              className="hover:text-accent"
              title="Show all slash commands"
            >
              /help
            </button>
            <span className="text-border">·</span>
            <button type="button" onClick={() => setInput("/clear")} className="hover:text-accent">/clear</button>
            <span className="text-border">·</span>
            <button type="button" onClick={() => setInput("/stop")} className="hover:text-accent">/stop</button>
          </div>
          <div className="flex gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onInputKeyDown}
              placeholder="Type a message, or / for commands…"
              className="field min-h-touch flex-1 text-base md:text-sm"
              autoCapitalize="sentences"
              autoCorrect="on"
              enterKeyHint="send"
              disabled={sending && !currentTurn?.isStreaming}
            />
            {currentTurn?.isStreaming && !currentTurn.error ? (
              <button
                type="button"
                onClick={async () => {
                  if (!sessionId) return;
                  try { await sessionApi.cancel(sessionId); } catch {}
                }}
                className="min-h-touch min-w-touch rounded-lg border border-danger/40 bg-danger/10 px-4 py-2 text-sm font-semibold text-danger hover:bg-danger/15"
                title="Cancel the in-flight turn (/stop)"
              >
                ■ Stop
              </button>
            ) : (
              <button
                type="submit"
                disabled={sending || !input.trim()}
                className="btn-primary min-h-touch min-w-touch"
              >
                {sending ? "…" : "Send"}
              </button>
            )}
          </div>
        </div>
      </form>

      {/* Help overlay — opened by /help, the link in the input hints, or ? */}
      {helpOpen && <HelpOverlay onClose={() => setHelpOpen(false)} />}
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
    assistantText: "", thinkingText: "", isStreaming: true,
    tools: [], fileChanges: [], agents: {}, commits: [],
    blocks: [],
    liveInputTokens: 0, liveOutputTokens: 0,
    summary: null, error: null,
  };
}

// Helpers for maintaining the chronological block timeline. Consecutive text
// (or consecutive thinking) chunks merge into the same block — anything else
// closes the streak.

function _newBlockId(kind: string, ts: number, n: number): string {
  return `${kind}-${ts}-${n}-${Math.random().toString(36).slice(2, 6)}`;
}

function _appendTextChunk(
  blocks: TimelineBlock[], kind: "text" | "thinking", text: string, ts: number,
): TimelineBlock[] {
  if (!text) return blocks;
  const last = blocks[blocks.length - 1];
  if (last && last.kind === kind) {
    const next = [...blocks];
    next[next.length - 1] = { ...last, text: last.text + text } as TimelineBlock;
    return next;
  }
  return [...blocks, {
    id: _newBlockId(kind, ts, blocks.length),
    kind, text, startedAt: ts,
  } as TimelineBlock];
}

function _replaceLastTextBlock(
  blocks: TimelineBlock[], canonical: string, ts: number,
): TimelineBlock[] {
  // Replace the most-recent text block with the canonical tag-stripped text
  // (the assistant_text(done=true) flush). If no text block exists yet (rare
  // edge case where the model only emitted a tool call), push one.
  for (let i = blocks.length - 1; i >= 0; i--) {
    if (blocks[i].kind === "text") {
      const next = [...blocks];
      next[i] = { ...next[i], text: canonical } as TimelineBlock;
      return next;
    }
  }
  return [...blocks, {
    id: _newBlockId("text", ts, blocks.length),
    kind: "text", text: canonical, startedAt: ts,
  } as TimelineBlock];
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
        if (p.done) {
          const t = String(p.text ?? "");
          if (t) {
            curr.assistantText = t;
            curr.blocks = _replaceLastTextBlock(curr.blocks, t, ev.ts);
          }
          curr.isStreaming = false;
        } else {
          const t = String(p.text ?? "");
          curr.assistantText += t;
          curr.blocks = _appendTextChunk(curr.blocks, "text", t, ev.ts);
        }
        break;
      case "thinking_text": {
        if (!curr) break;
        const t = String(p.text ?? "");
        curr.thinkingText += t;
        curr.blocks = _appendTextChunk(curr.blocks, "thinking", t, ev.ts);
        break;
      }
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
      case "tool_start": {
        if (!curr) break;
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        const tool: ToolEvent = {
          id: `${ev.ts}-${p.tool}-${curr.tools.length}-${curr.blocks.length}`,
          tool: String(p.tool ?? "?"),
          target: typeof p.target === "string" ? p.target : undefined,
          status: "running", startedAt: ev.ts,
        };
        if (aid && curr.agents[aid]) {
          curr.agents[aid].tools.push(tool);
        } else {
          curr.tools.push(tool);
          curr.blocks.push({
            id: _newBlockId("tool", ev.ts, curr.blocks.length),
            kind: "tool", toolId: tool.id, ts: ev.ts,
          });
        }
        break;
      }
      case "tool_done": {
        if (!curr) break;
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        const list = (aid && curr.agents[aid]) ? curr.agents[aid].tools : curr.tools;
        for (let i = list.length - 1; i >= 0; i--) {
          if (list[i].tool === p.tool && list[i].status === "running") {
            list[i] = {
              ...list[i],
              status: p.error ? "error" : "done",
              preview: typeof p.preview === "string" ? p.preview : undefined,
              previewTruncated: !!p.preview_truncated,
              endedAt: ev.ts,
            };
            break;
          }
        }
        break;
      }
      case "file_changed": {
        if (!curr) break;
        const fc: FileChange = {
          id: `${p.path}-${ev.ts}`,
          path: String(p.path ?? ""),
          kind: p.kind === "create" ? "create" : "edit",
          diff: String(p.diff ?? ""),
          bytes: Number(p.bytes ?? 0),
          ts: ev.ts,
        };
        curr.fileChanges.push(fc);
        curr.blocks.push({
          id: _newBlockId("file", ev.ts, curr.blocks.length),
          kind: "file", file: fc,
        });
        break;
      }
      case "agent_spawn": {
        if (!curr) break;
        const aid = String(p.agent_id ?? "");
        curr.agents[aid] = {
          agent_id: aid,
          description: String(p.description ?? ""),
          subagent_type: String(p.subagent_type ?? "general-purpose"),
          name: String(p.name ?? ""), model: String(p.model ?? ""),
          status: "running", output_file: "", error: "",
          spawned_at: ev.ts, updated_at: ev.ts,
          tools: [], liveInputTokens: 0, liveOutputTokens: 0, llmCalls: [],
        };
        curr.blocks.push({
          id: _newBlockId("agent", ev.ts, curr.blocks.length),
          kind: "agent", agentId: aid, ts: ev.ts,
        });
        break;
      }
      case "token_update": {
        if (!curr) break;
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        const inD  = Number(p.input_delta  ?? 0);
        const outD = Number(p.output_delta ?? 0);
        if (inD === 0 && outD === 0) break;
        if (aid && curr.agents[aid]) {
          curr.agents[aid].liveInputTokens  += inD;
          curr.agents[aid].liveOutputTokens += outD;
          curr.agents[aid].llmCalls.push({
            ts: ev.ts, inputTokens: inD, outputTokens: outD,
          });
        } else {
          curr.liveInputTokens  += inD;
          curr.liveOutputTokens += outD;
          curr.blocks.push({
            id: _newBlockId("llm_call", ev.ts, curr.blocks.length),
            kind: "llm_call", ts: ev.ts,
            inputTokens: inD, outputTokens: outD,
          });
        }
        break;
      }
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
      case "commit_made": {
        if (!curr) break;
        const cr: CommitRecord = {
          id: `${p.sha}-${ev.ts}`,
          sha: String(p.sha ?? ""), branch: String(p.branch ?? ""),
          message: String(p.message ?? ""),
          files: Array.isArray(p.files) ? (p.files as string[]) : [],
          ts: ev.ts,
        };
        curr.commits.push(cr);
        curr.blocks.push({
          id: _newBlockId("commit", ev.ts, curr.blocks.length),
          kind: "commit", commit: cr,
        });
        break;
      }
      case "commit_skipped": {
        if (!curr) break;
        const cr: CommitRecord = {
          id: `skip-${ev.ts}`, sha: "", branch: String(p.branch ?? ""),
          message: `(skipped — ${p.reason ?? "unknown"})`, files: [], ts: ev.ts,
        };
        curr.commits.push(cr);
        curr.blocks.push({
          id: _newBlockId("commit", ev.ts, curr.blocks.length),
          kind: "commit", commit: cr,
        });
        break;
      }
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

// ============================================================================
// NowChip — compact session-level "what's running right now" indicator pinned
// in the chat header. Replaces the old per-turn full-width banner; the user
// always sees the latest activity regardless of scroll position, and turns
// stay clean below.
// ============================================================================

function NowChip({ turn }: { turn: Turn }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, []);

  // Most recent running tool / agent — orchestrator OR any sub-agent's tools.
  const allTools = [
    ...turn.tools,
    ...Object.values(turn.agents).flatMap((a) => a.tools),
  ];
  const runningTool = [...allTools].reverse().find((t) => t.status === "running");
  const runningAgent = Object.values(turn.agents)
    .filter((a) => a.status === "running")
    .sort((a, b) => b.spawned_at - a.spawned_at)[0];

  let icon = "✦";
  let label = "Thinking";
  let startedAt = turn.startedAt;
  let toneText = "text-accent";
  let toneBorder = "border-accent/40 bg-accent/[0.06]";

  if (runningTool) {
    icon = "▸";
    label = runningTool.tool;
    startedAt = runningTool.startedAt;
    toneText = "text-warn";
    toneBorder = "border-warn/40 bg-warn/[0.05]";
  } else if (runningAgent) {
    icon = "▸";
    label = `Agent[${runningAgent.subagent_type}]`;
    startedAt = runningAgent.spawned_at;
    toneText = "text-accent-2";
    toneBorder = "border-accent-2/40 bg-accent-2/[0.05]";
  } else if (turn.assistantText) {
    icon = "✎";
    label = "Writing";
    toneText = "text-success";
    toneBorder = "border-success/40 bg-success/[0.05]";
  }

  return (
    <div
      className={`inline-flex max-w-[220px] items-center gap-1.5 rounded-md border ${toneBorder} px-2 py-0.5 font-sans backdrop-blur-sm`}
      title={label}
    >
      <span className="stream-dot shrink-0" />
      <span className={`shrink-0 text-tx-xs ${toneText}`}>{icon}</span>
      <span className="truncate text-tx-xs font-medium text-text">{label}</span>
      <span className="shrink-0 font-mono text-[10px] text-subtle">
        {formatDuration(now - startedAt)}
      </span>
    </div>
  );
}

// ============================================================================
// HelpOverlay — modal listing the available slash commands + keyboard shortcuts.
// Same look as the ProjectList "new project" modal so the chrome is consistent.
// ============================================================================


const SHORTCUTS: { keys: string; desc: string }[] = [
  { keys: "↑ / ↓",   desc: "Recall previous / next prompt from history" },
  { keys: "Esc",     desc: "Cancel history navigation and restore your draft" },
  { keys: "Enter",   desc: "Send the message" },
  { keys: "Double-click on a tool result", desc: "Expand / collapse the full preview" },
];

function HelpOverlay({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/65 p-4 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="glass-card w-full max-w-lg space-y-5 p-6 animate-fade-in-up"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold tracking-tight">Keyboard shortcuts &amp; commands</h2>
            <p className="mt-0.5 text-xs text-muted">
              Slash commands run locally — they don't cost a model call.
            </p>
          </div>
          <button onClick={onClose} className="text-xl leading-none text-muted hover:text-text" title="Close (Esc)">×</button>
        </div>

        <div>
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-subtle">
            Slash commands
          </div>
          <div className="divide-y divide-border/60 rounded-lg border border-border/60 bg-elevated/40">
            {SLASH_COMMANDS.map((c) => (
              <div key={c.cmd} className="flex items-baseline gap-3 px-3 py-2 text-sm">
                <kbd className="shrink-0 rounded border border-border bg-bg/60 px-1.5 py-0.5 font-mono text-tx-xs text-accent">
                  {c.cmd}
                </kbd>
                <span className="text-muted">{c.desc}</span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.16em] text-subtle">
            Keyboard
          </div>
          <div className="divide-y divide-border/60 rounded-lg border border-border/60 bg-elevated/40">
            {SHORTCUTS.map((s) => (
              <div key={s.keys} className="flex items-baseline gap-3 px-3 py-2 text-sm">
                <kbd className="shrink-0 rounded border border-border bg-bg/60 px-1.5 py-0.5 font-mono text-tx-xs text-text">
                  {s.keys}
                </kbd>
                <span className="text-muted">{s.desc}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="text-right">
          <button onClick={onClose} className="btn-ghost">Close</button>
        </div>
      </div>
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

// ============================================================================
// DebugStream — floating panel showing the last N raw WebSocket events.
// Auto-scrolls; coloured by event kind so the human eye can spot bursts and
// gaps. The relative timestamp on each row is delta-from-previous so we can
// see whether events arrive smoothly or in a single end-of-turn batch.
// ============================================================================

const DEBUG_KIND_COLOR: Record<string, string> = {
  user_message:        "text-text",
  assistant_text:      "text-success",
  thinking_text:       "text-accent-2",
  tool_start:          "text-warn",
  tool_done:           "text-success",
  agent_spawn:         "text-accent-2",
  agent_status_update: "text-accent-2",
  file_changed:        "text-accent",
  commit_made:         "text-success",
  commit_skipped:      "text-warn",
  push_done:           "text-accent",
  token_update:        "text-accent",
  todo_update:         "text-muted",
  turn_summary:        "text-text",
  error:               "text-danger",
};

function DebugStream({
  events, onClear,
}: { events: { kind: string; payload: any; ts: number }[]; onClear: () => void }) {
  const boxRef = useRef<HTMLDivElement | null>(null);
  // Auto-stick to bottom when new events arrive.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [events.length]);

  const rows = events.map((e, i) => {
    const prev = i > 0 ? events[i - 1].ts : e.ts;
    const dt = e.ts - prev;
    const color = DEBUG_KIND_COLOR[e.kind] ?? "text-muted";
    let summary = "";
    if (typeof e.payload === "object" && e.payload) {
      const p = e.payload as Record<string, any>;
      if (e.kind === "tool_start")   summary = `${p.tool}(${truncForDebug(p.target, 40)})`;
      else if (e.kind === "tool_done")  summary = `${p.tool}${p.error ? " ✗" : " ✓"}`;
      else if (e.kind === "assistant_text") summary = `+${String(p.text ?? "").length} chars${p.done ? " · DONE" : ""}`;
      else if (e.kind === "thinking_text")  summary = `+${String(p.text ?? "").length} chars`;
      else if (e.kind === "token_update")   summary = `+${p.input_delta ?? 0} in / +${p.output_delta ?? 0} out`;
      else if (e.kind === "agent_spawn")    summary = `${p.subagent_type} · ${truncForDebug(p.description, 40)}`;
      else if (e.kind === "agent_status_update") summary = `${p.agent_id?.slice?.(-6)} → ${p.status}`;
      else if (e.kind === "file_changed")   summary = `${p.kind} ${p.path}`;
      else if (e.kind === "turn_summary")   summary = `tools=${p.tools_used} in=${p.input_tokens} out=${p.output_tokens}`;
      else if (e.kind === "error")          summary = String(p.message ?? "?");
      else summary = JSON.stringify(p).slice(0, 80);
    }
    return (
      <div key={i} className="flex items-baseline gap-2 whitespace-pre font-mono text-[11px] leading-tight">
        <span className="w-12 shrink-0 text-right text-subtle">+{dt}ms</span>
        <span className={`w-32 shrink-0 ${color}`}>{e.kind}</span>
        <span className="truncate text-muted" title={JSON.stringify(e.payload)}>{summary}</span>
      </div>
    );
  });

  return (
    <div className="fixed bottom-20 right-4 z-30 w-[420px] max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg border border-border bg-bg/95 shadow-lift backdrop-blur-md">
      <div className="flex items-center justify-between border-b border-border bg-elevated/60 px-3 py-1.5">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-accent">
            Debug stream
          </span>
          <span className="font-mono text-[10px] text-subtle">
            {events.length} events
          </span>
        </div>
        <button
          type="button"
          onClick={onClear}
          className="text-[10px] text-muted hover:text-danger"
        >
          clear
        </button>
      </div>
      <div ref={boxRef} className="max-h-72 overflow-y-auto px-3 py-2">
        {events.length === 0 ? (
          <div className="font-sans text-[11px] text-subtle">
            Waiting for events… send a message to see the stream.
          </div>
        ) : (
          rows
        )}
      </div>
    </div>
  );
}

function truncForDebug(s: any, n: number): string {
  const str = String(s ?? "");
  return str.length <= n ? str : str.slice(0, n - 1) + "…";
}
