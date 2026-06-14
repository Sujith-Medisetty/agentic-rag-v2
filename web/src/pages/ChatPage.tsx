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
import { useParams, useOutletContext } from "react-router-dom";
import type { Project as ProjectType } from "@/lib/types";
import { sessionApi, sessionsApi, deployedAppsApi } from "@/lib/api";
import type { DeployedApp, DeployJobStatus, DetectedDist } from "@/lib/api";
import { openEventStream } from "@/lib/ws";
import { useTheme } from "@/lib/theme";
import { useSessions } from "@/lib/sessionContext";
import type {
  LiveEvent, TodoItem, AgentRecord, FileChange, GitInfo,
  CommitRecord, ToolEvent, TurnSummary, Turn, SessionTotals,
  TimelineBlock,
} from "@/lib/types";

// Chat-visible auto-compact breadcrumb. One per `context_compacted` event
// (server-side `maybe_compact` in memory/checkpointer.py fires this when
// the message list crosses the auto-compact threshold). Renders as a
// collapsible system message in the transcript so the user can SEE that
// older turns got summarised and what the agent now remembers about them.
type ContextCompactedNote = {
  id: string;
  ts: number;
  removed: number;
  kept: number;
  tokensBefore: number;
  tokensAfter: number;
  summaryPreview: string;
  threshold: number;
};
import PlanPanel from "@/components/PlanPanel";
import TurnCard, { ActiveTurnCard } from "@/components/TurnCard";
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
  inputTokens: 0, outputTokens: 0,
  cacheReadTokens: 0, cacheWriteTokens: 0,
  costUsd: 0, costCacheReadUsd: 0, costInputUsd: 0, costOutputUsd: 0,
  durationMs: 0,
};

// ============================================================================
// ChatPage
// ============================================================================

export default function ChatPage() {
  // The new sidebar layout only puts the sessionId in the URL; the active
  // project comes from the Workspace's outlet context. Legacy /p/:projectId
  // routes still expose both via useParams as a fallback.
  const params = useParams<{ projectId?: string; sessionId?: string }>();
  const sessionId = params.sessionId;
  const ctx = useOutletContext<{ project?: ProjectType; sidebarOpen?: boolean } | null>();
  const projectId = params.projectId ?? ctx?.project?.id ?? "";
  // The Workspace sidebar puts a hamburger icon at the top-left when it's
  // collapsed. The chat header needs to reserve space for it whenever the
  // sidebar is NOT visible (so its content doesn't get hidden behind the
  // icon). Default to "reserve space" if context isn't available (legacy
  // standalone routes use this path).
  const needsHamburgerSpace = ctx ? !ctx.sidebarOpen : true;
  // Shared session store — lets the WS handler update the sidebar's
  // session name (and any other consumers) via a normal React state
  // write. No custom events, no polling.
  const sessions = useSessions();

  const [turns, setTurns] = useState<Turn[]>([]);
  const [currentTurn, setCurrentTurn] = useState<Turn | null>(null);
  const [plan, setPlan] = useState<TodoItem[]>([]);
  const [git, setGit] = useState<GitInfo | null>(null);
  const [pushing, setPushing] = useState(false);
  const [wsStatus, setWsStatus] = useState<"connecting" | "open" | "closed" | "error">("connecting");
  // App-wide theme. Same hook used by Layout's header; the chat page renders
  // its own header (so it can show the WS status / debug pill) and therefore
  // needs to surface the toggle here too.
  const { effective: themeEffective, toggle: toggleTheme } = useTheme();
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Deployed apps from THIS session — populated on mount from
  // /api/sessions/<id>/deployed-apps, refreshed when the user deploys a
  // new sub-app via the Deploy button or deletes one from the strip.
  const [deployedApps, setDeployedApps] = useState<DeployedApp[]>([]);
  // Live deploy progress per slug. Keyed by slug; set by the
  // deploy_progress WS event so the matching pill can show a
  // "Step 3/12 — Building…" chip instead of just "Deploying…".
  // Cleared by deploy_complete / deploy_failed.
  const [deployProgress, setDeployProgress] = useState<Record<string, {
    phase: string; steps_done: number; steps_total: number;
  }>>({});
  // The slug the user just clicked Update on for THIS session.
  // deploy_progress events don't carry a slug (the backend runs
  // one deploy at a time per session), so we attribute progress
  // events to this ref. Cleared when the session changes.
  const lastClickedUpdateSlugRef = useRef<string | null>(null);
  // Debounce handle for the live "/detected-dist" re-fetch. The
  // build banner + per-pill "Update" button read from
  // `lastDetected`, but the original code only re-fetched it on
  // turn_summary + deploy_complete — so a 10-step `npm run build`
  // could finish, the turn end, and the Update button wouldn't
  // appear until the next refresh. Triggering a re-fetch on every
  // file_changed (for files that look like a dist build artifact) +
  // every bash tool_done is correct but chatty, so we debounce
  // 500ms after the last trigger — N events collapse into ONE
  // /detected-dist GET, so a long build costs exactly one server
  // scan at the end. Cleared on session switch.
  const detectedDistRefreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // "Build ready" detection — set when the session has a built dist
  // newer than the most recent deploy FROM this session. The chat
  // shows a banner under the agent's last turn when true, and the
  // banner's Deploy button opens the same dialog as the strip's.
  const [lastDetected, setLastDetected] = useState<DetectedDist | null>(null);
  // The Deploy modal lives in the DeployStrip; this flag is what the
  // build-ready banner flips on to open the same modal.
  const [showDeployModal, setShowDeployModal] = useState(false);
  // Context-used progress for the bottom-of-chat bar. Updated by `context_update`
  // WS events. 0 means "no data yet" (don't render the bar).
  const [contextUsed, setContextUsed] = useState(0);
  const [contextCompacting, setContextCompacting] = useState(false);
  // Auto-compact threshold (50K default) — the chip shows "X% used"
  // against this denominator.
  const [contextThreshold, setContextThreshold] = useState(50_000);
  // Auto-compact breadcrumb list. Each entry corresponds to a `context_compacted`
  // WS event and renders as a collapsible system message in the transcript
  // so the user can see what was summarised when. Cleared on session switch.
  const [contextCompactedNotes, setContextCompactedNotes] = useState<ContextCompactedNote[]>([]);
  // Follow-mode for the chat scroll: stick to bottom while new events
  // arrive UNLESS the user scrolls up to read. If they do, leave them where
  // they are and pop a floating "↓" button so they can catch back up on a
  // single tap. The button hides automatically when they reach the bottom
  // again (either by scrolling or by tapping it).
  const [chatAtBottom, setChatAtBottom] = useState(true);
  const onChatScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const threshold = 60;   // px from bottom counted as "still at bottom"
    setChatAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < threshold);
  };
  const jumpChatToBottom = () => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setChatAtBottom(true);
  };

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
  // Track the last-seen event timestamp (in ms) so we can refetch any
  // events the WebSocket missed during the load gap. Without this, an
  // event emitted by the backend AFTER /events returns but BEFORE the
  // WS subscription opens would silently disappear from the UI.
  // Tracked across BOTH the initial events load AND every incoming
  // live event. The WS-open handler uses it to ask for "anything since
  // this timestamp" and replays the gap through handleEvent.
  const lastEventTsRef = useRef<number>(0);
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
    // Ref the pill setTimeout writes so deploy_progress events
    // (which don't carry a slug) can be attributed to the slug the
    // user just clicked Update on. Resets when the session changes.
    lastClickedUpdateSlugRef.current = null;
    // Cancel any pending debounced /detected-dist re-fetch from a
    // previous session — we don't want a stale timer from session A
    // firing setLastDetected for session B.
    if (detectedDistRefreshTimerRef.current) {
      clearTimeout(detectedDistRefreshTimerRef.current);
      detectedDistRefreshTimerRef.current = null;
    }
    setDeployProgress({});
    // HARD RESET all per-session state synchronously BEFORE any async load.
    // Without this, switching from session A to session B mid-turn left A's
    // currentTurn (with isStreaming=true) in state — the Stop button kept
    // showing on B, but clicking it cancelled B (the URL's sessionId), not
    // A. Same leak hit previewUrl (A's preview banner showed on B), plan,
    // git, debugEvents, sending, and loadErr. Resetting all of them up
    // front guarantees the new session starts from a clean slate; the
    // load below then populates fresh data from B's event history.
    setTurns([]);
    setCurrentTurn(null);
    setPlan([]);
    setGit(null);
    setDeployedApps([]);   // wipe previous session's deploy strip
    setSending(false);
    setLoadErr(null);
    setDebugEvents([]);
    setWsStatus("connecting");
    lastEventTsRef.current = 0;
    // Reset the chip's per-session state. The new session's WS connect
    // event will re-populate these from the server's persisted value.
    setContextUsed(0);
    setContextCompacting(false);
    setContextCompactedNotes([]);

    let cancelled = false;
    (async () => {
      try {
        const [events, gitInfo, sessionInfo] = await Promise.all([
          sessionApi.events(sessionId),
          sessionApi.git(sessionId).catch(() => null),
          sessionsApi.get(sessionId).catch(() => null),
        ]);
        if (cancelled) return;
        if (gitInfo) setGit(gitInfo);
        // Seed the context chip from the session's persisted
        // `last_context_used` so the user doesn't see "0% used" flash
        // for a frame on refresh / session switch. The WS
        // `context_update` event (or the persisted value on WS connect)
        // will overwrite this if the LLM fires again.
        if (sessionInfo?.last_context_used) {
          setContextUsed(Number(sessionInfo.last_context_used));
        }
        // Walk the event log in chronological order, folding into turns +
        // (if the log ends mid-turn) an in-progress currentTurn. Setting
        // currentTurn from the rebuild is what makes streaming RESUME on
        // refresh / session-switch — live WS events arriving after this
        // point have a non-null target to update.
        const { turns: rebuilt, currentTurn: rebuiltCurrent, plan: replayedPlan } = rebuildTranscript(
          events.map((e) => ({
            kind: e.kind, payload: e.payload, ts: e.created_at * 1000,
          })),
        );
        setTurns(rebuilt);
        setCurrentTurn(rebuiltCurrent);
        setPlan(replayedPlan);
        // Load this session's deployed apps so the strip renders correctly
        // on every mount (refresh / session switch). Failure is non-fatal —
        // strip just shows empty.
        sessionApi.deployedApps(sessionId!).then(setDeployedApps).catch(() => {});
        // Also load detected-dist so the build-ready banner knows whether
        // to render. Same non-fatal failure mode.
        sessionApi.detectedDist(sessionId!).then(setLastDetected).catch(() => {});
        // Record the watermark for the WS catchup. Use the latest event's
        // created_at in SECONDS (backend's column unit) so the next
        // ?since=<ts> query returns ONLY events newer than what we already
        // have. Falls back to 0 (full replay) on a fresh session.
        if (events.length > 0) {
          lastEventTsRef.current = events[events.length - 1].created_at;
        }
        // Session rename catch-up. If the live WS missed the
        // session_renamed event (e.g. user came back to this tab after
        // the rename fired), the latest one is in the event history —
        // replay it through the shared context so the sidebar stays in
        // sync. The rename() call is a no-op if the name hasn't
        // changed since the last write.
        const lastRename = [...events]
          .reverse()
          .find((e) => e.kind === "session_renamed");
        if (lastRename) {
          const newName = String(lastRename.payload.new_name ?? "");
          if (newName) sessions.rename(sessionId, newName);
        }
      } catch (e: any) {
        if (!cancelled) setLoadErr(e?.message ?? "failed to load history");
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  // ---- WebSocket subscription -------------------------------------------
  useEffect(() => {
    if (!sessionId) return;
    // Capture the sessionId this subscription was bound to in a local
    // const. If the user switches sessions, the cleanup below closes the
    // socket — but a message already in-flight (between WebSocket close()
    // and the next event-loop tick) could still fire its callback after
    // sessionId has changed. The boundSid guard makes those late events a
    // no-op so they can't bleed into the NEW session's state.
    const boundSid = sessionId;
    const handle = openEventStream(
      sessionId,
      (ev) => {
        // Reject any event delivered after we navigated away from this
        // session. The dependency-array invariant tells React this effect
        // belongs to `boundSid`; if the URL's sessionId has drifted, ignore.
        if (boundSid !== sessionId) return;
        // Advance the watermark so the next WS-reconnect catchup query
        // only refetches truly-newer events. Live event ts is in MS;
        // convert to SECONDS to match the backend column unit. Math.floor
        // ensures we don't accidentally request the same event again on
        // a sub-second precision mismatch.
        const evSecs = Math.floor(ev.ts / 1000);
        if (evSecs > lastEventTsRef.current) {
          lastEventTsRef.current = evSecs;
        }
        // Tap every event into the debug stream FIRST so we capture it even
        // if handleEvent throws / drops it (which is what we're trying to
        // debug). Keep last 200 entries.
        setDebugEvents((prev) => {
          const next = [...prev, { kind: ev.kind, payload: ev.payload, ts: ev.ts }];
          return next.length > 200 ? next.slice(-200) : next;
        });
        handleEvent(ev);
      },
      (s) => {
        if (boundSid !== sessionId) return;
        setWsStatus(s);
        // ---- Race-window catchup ------------------------------------------
        // Every time the WS transitions to "open" (initial connect AND any
        // reconnect after network blip), fetch any events the backend
        // emitted while we weren't subscribed. The watermark `lastEventTsRef`
        // is updated by the initial events load + every incoming WS event,
        // so this query returns only the gap. Replay each through
        // handleEvent so they update currentTurn / turns just like a live
        // event would. Without this, an assistant_text emitted during the
        // ~50–200ms gap between the events HTTP fetch and the WS handshake
        // would be silently lost — the user would see a frozen turn and
        // think streaming broke.
        if (s === "open" && lastEventTsRef.current > 0) {
          sessionApi.events(sessionId, lastEventTsRef.current)
            .then((missed) => {
              if (boundSid !== sessionId) return;   // navigated away mid-fetch
              for (const ev of missed) {
                const live: LiveEvent = {
                  kind: ev.kind, payload: ev.payload, ts: ev.created_at * 1000,
                };
                const evSecs = ev.created_at;
                if (evSecs > lastEventTsRef.current) {
                  lastEventTsRef.current = evSecs;
                }
                handleEvent(live);
              }
            })
            .catch(() => { /* best-effort — next reconnect will retry */ });
        }
      },
    );
    return () => handle.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // ---- Live /detected-dist refresh --------------------------------------
  // The build banner + per-pill "Update" button read from
  // `lastDetected`, but the original code only re-fetched it on
  // turn_summary + deploy_complete. So a 10-step `npm run build`
  // could finish, the turn end, and the Update button wouldn't
  // appear until the next refresh. Triggered on:
  //   - file_changed whose path looks like a dist build artifact
  //     (rare — agents usually edit source, but a deliberate edit
  //     of dist/index.html or dist/assets/* should still update live)
  //   - tool_done for bash commands (covers `npm run build` and
  //     every other build tool that writes through the shell)
  // Debounced 500ms after the last trigger so a 10-event build
  // collapses into ONE server scan. The endpoint is cheap (O(workspace
  // scan)) so this is the lowest-risk place to be "always live".
  function scheduleDetectedDistRefresh(reason: string) {
    if (!sessionId) return;
    if (detectedDistRefreshTimerRef.current) {
      clearTimeout(detectedDistRefreshTimerRef.current);
    }
    detectedDistRefreshTimerRef.current = setTimeout(() => {
      detectedDistRefreshTimerRef.current = null;
      const sid = sessionId;
      sessionApi
        .detectedDist(sid)
        .then((d) => {
          // Race guard: sessionId may have changed while the
          // fetch was in flight (user navigated to a different
          // session). The next setLastDetected would clobber the
          // NEW session's value, so check first.
          if (sid !== sessionId) return;
          setLastDetected(d);
        })
        .catch(() => { /* best-effort; turn_summary still re-fetches */ });
    }, 500);
  }

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
      case "context_update": {
        // Server publishes this after every LLM call. `used_tokens` is the
        // real `input + cache_creation + cache_read` from the provider, so
        // the chip matches the actual context window. `threshold` (50K
        // default) is what the chip's "% used" denominator uses.
        const used = Number(p.used_tokens ?? 0);
        const threshold = Number(p.threshold ?? 0);
        if (used <= 0) return;
        if (threshold > 0) setContextThreshold(threshold);
        setContextUsed(used);
        setContextCompacting(Boolean(p.compacting));
        return;
      }
      case "context_compacted": {
        // Chat-visible breadcrumb when auto-compaction fires. Renders as
        // a system message in the transcript so the user can SEE that older
        // turns got summarised (and what the agent now remembers about them),
        // rather than compaction being a silent background process.
        const removed = Number(p.removed ?? 0);
        if (removed <= 0) return;
        const note: ContextCompactedNote = {
          id: `compact-${ev.ts}-${removed}-${Number(p.kept ?? 0)}`,
          ts: ev.ts,
          removed,
          kept: Number(p.kept ?? 0),
          tokensBefore: Number(p.tokens_before ?? 0),
          tokensAfter:  Number(p.tokens_after  ?? 0),
          summaryPreview: String(p.summary_preview ?? ""),
          threshold: Number(p.threshold ?? 0),
        };
        setContextCompactedNotes((prev) => [...prev, note]);
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
        const cacheR  = Number(p.cache_read_delta  ?? 0);
        const cacheW  = Number(p.cache_creation_delta ?? 0);
        const aid = typeof p.agent_id === "string" ? p.agent_id : "";
        if (inDelta === 0 && outDelta === 0 && cacheR === 0 && cacheW === 0) return;  // skip zero-deltas
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
                    cacheReadTokens: cacheR, cacheCreationTokens: cacheW,
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
              cacheReadTokens: cacheR, cacheCreationTokens: cacheW,
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
        // Re-poll detected-dist right after the turn ends so the
        // build-ready banner appears under the new turn (the agent
        // just finished writing files, the dist is fresh). Cheap
        // call; the WS has no equivalent.
        if (sessionId) {
          sessionApi.detectedDist(sessionId).then(setLastDetected).catch(() => {});
        }
        // Background auto-rename happens 0–6s after turn_summary. Refetch
        // the session once after a short delay so the sidebar picks up
        // the LLM-generated name without needing the WS event. Single
        // poll — the WS event is the primary path; this is just a
        // cheap belt-and-suspenders for mobile/PWA cases.
        if (sessionId) {
          const sid = sessionId;
          setTimeout(() => {
            sessionsApi.get(sid).then((s) => sessions.rename(sid, s.name)).catch(() => {});
          }, 2500);
        }
        return;
      }
      case "session_renamed": {
        // The server (background LLM-suggested rename or our own PATCH
        // path) changed this session's display name. Update the shared
        // SessionContext — the sidebar (Workspace) and session list
        // (SessionList page) re-render automatically because they read
        // from the same context.
        const newName = String(p.new_name ?? "");
        if (newName && sessionId) {
          sessions.rename(sessionId, newName);
          if (p.was_suffixed) {
            const prev = String(p.previous_name ?? "");
            if (prev && prev !== newName) {
              sessions.setToast({
                message: `Renamed to "${newName}" — "${prev}" was already taken.`,
              });
            }
          }
        }
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
        // Parse the smart-truncation marker the bash tool embeds in its
        // preview text. Format (set in tools/wrappers.py):
        //   …truncated N chars; this was a SUCCESS/FAILURE.
        //   # truncation: kept_first=X, kept_last=Y, dropped=Z, total=T
        //   Full output saved to `/path`.
        //   …
        // We keep the parsing tolerant — the marker may evolve. If any
        // field is missing we still surface "truncated" but with zeros.
        const bashOutput = parseBashOutputMarker(preview);
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
                  // Only set bashOutput on bash calls (parser returns
                  // null for non-bash previews), so the UI's status
                  // line only appears for bash tool results.
                  bashOutput: bashOutput ?? next[i].bashOutput,
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
        // Live Update-button refresh: bash tool_done covers the common
        // case of `npm run build` / `vite build` / `next build` / etc.
        // rewriting dist/. Source-file edits route through
        // file_changed below instead, so this only fires for shell
        // invocations — which is exactly where the dist mtime actually
        // changes. Debounced 500ms via scheduleDetectedDistRefresh.
        if (toolName === "bash" && !aid) {
          scheduleDetectedDistRefresh("tool_done:bash");
        }
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
        // Live Update-button refresh: a file_changed that lands inside
        // a dist build output (e.g. a deliberate edit of
        // dist/index.html, or a tool the agent uses to write built
        // assets) should bump the candidate's mtime the same way
        // `npm run build` would. The check is intentionally narrow
        // — /dist/ or /build/ segments in the path — to avoid
        // triggering a scan for every source-file edit (those
        // typically don't change dist mtime, and turn_summary's
        // re-fetch covers them). Debounced 500ms.
        const fcPath = fc.path;
        if (
          fcPath &&
          (fcPath.includes("/dist/") || fcPath.includes("/build/") ||
           fcPath.endsWith("/dist") || fcPath.endsWith("/build"))
        ) {
          scheduleDetectedDistRefresh("file_changed:dist");
        }
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
      // --- Deploy lifecycle (in-place update OR new deploy) ---
      // The backend publishes deploy_complete / deploy_failed with the
      // canonical DeployedApp row in payload.result.app. Refresh the
      // pill list + the fresh_build flag so the UI re-renders without
      // a page reload. Without this handler, the deploy pill shows the
      // pre-deploy state until the user manually refreshes.
      case "deploy_complete": {
        if (sessionId) {
          // Re-fetch both: deployedApps (so the pill shows the new
          // public_url / last_redeploy_at / state) and detectedDist
          // (so the fresh_build flag flips back to false and the
          // "🔄 Update" button becomes "✓ Up to date").
          sessionApi.deployedApps(sessionId).then(setDeployedApps).catch(() => {});
          sessionApi.detectedDist(sessionId).then(setLastDetected).catch(() => {});
        }
        // Clear the per-slug progress chip — the deploy is done.
        const okApp = (p?.result?.app ?? {}) as Partial<DeployedApp>;
        if (okApp.slug) {
          setDeployProgress((prev) => {
            const { [okApp.slug!]: _gone, ...rest } = prev;
            return rest;
          });
          lastClickedUpdateSlugRef.current = null;
        }
        // Toast so the user knows it landed. payload.result.app.slug
        // is the canonical fresh row.
        if (okApp.slug) {
          sessions.setToast?.({
            message: `Deployed ${okApp.slug} — live at ${okApp.public_url || "(see pill)"}`,
          });
        }
        return;
      }
      case "deploy_failed": {
        if (sessionId) {
          // Refresh anyway: a failed in-place update may have left the
          // row in state="error" that's worth surfacing in the pill.
          sessionApi.deployedApps(sessionId).then(setDeployedApps).catch(() => {});
        }
        // Clear the in-flight progress chip and reset the slug ref.
        const failSlug = String(p?.result?.app?.slug ?? p?.slug ?? "");
        if (failSlug) {
          setDeployProgress((prev) => {
            const { [failSlug]: _gone, ...rest } = prev;
            return rest;
          });
        }
        lastClickedUpdateSlugRef.current = null;
        sessions.setToast?.({
          message: `Deploy failed: ${String(p?.error ?? "unknown")}`,
        });
        return;
      }
      case "deploy_progress": {
        // Live in-flight step progress — drives the per-pill
        // "Step N/12 — Building…" chip so the user sees the
        // 10-30s fullstack deploy making real progress instead of
        // a frozen "Deploying…" pill. Backend publishes the
        // current phase string + step list at the TOP LEVEL of
        // the payload (see server/app.py deploy_progress publish
        // at the _set_step function). NOT under `result`.
        //
        // We need to know WHICH slug this progress is for. The
        // backend doesn't include slug in deploy_progress
        // (deploys are one-at-a-time per session), so we attribute
        // it to the slug the user most recently clicked Update on
        // for this session (the `lastClickedUpdateSlugRef` the
        // pill writes on click). If the backend ever runs parallel
        // deploys in one session, this will need a slug field on
        // the event itself.
        const phase: string = String(p?.phase ?? p?.step?.label ?? "Deploying");
        const steps: any[] = Array.isArray(p?.steps) ? p.steps : [];
        const steps_done = steps.filter((s: any) => s?.status === "done").length;
        const steps_total = steps.length || 0;
        // Resolve the slug: prefer the payload's slug if present,
        // else the last one the user clicked Update on for this
        // session.
        const slug = String(p?.slug ?? lastClickedUpdateSlugRef.current ?? "");
        if (slug) {
          setDeployProgress((prev) => ({
            ...prev,
            [slug]: { phase, steps_done, steps_total },
          }));
        }
        return;
      }
    }
  }

  // ---- Auto-scroll on new content (only while following) ----------------
  useEffect(() => {
    if (!chatAtBottom) return;   // user has scrolled up; respect that
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [
    chatAtBottom,
    turns.length,
    currentTurn?.assistantText,
    currentTurn?.thinkingText,
    currentTurn?.tools.length,
  ]);

  // ---- Session totals — sum of per-turn metrics -------------------------
  const totals = useMemo<SessionTotals>(() => {
    return turns.reduce<SessionTotals>((acc, t) => {
      if (!t.summary) return acc;
      const s = t.summary;
      return {
        turns:           acc.turns + 1,
        tools:           acc.tools + s.tools_used,
        inputTokens:     acc.inputTokens + s.input_tokens,
        outputTokens:    acc.outputTokens + s.output_tokens,
        cacheReadTokens: acc.cacheReadTokens + s.cache_read_tokens,
        cacheWriteTokens: acc.cacheWriteTokens + s.cache_write_tokens,
        costUsd:         acc.costUsd + s.cost_usd,
        // Per-component cost sub-totals. These default to 0 in the
        // accumulator when older turns pre-date the field — `??` handles
        // the optional `cost_*_usd` shape on TurnSummary.
        costCacheReadUsd: acc.costCacheReadUsd + (s.cost_cache_read_usd ?? 0),
        costInputUsd:     acc.costInputUsd     + (s.cost_input_usd     ?? 0),
        costOutputUsd:    acc.costOutputUsd    + (s.cost_output_usd    ?? 0),
        durationMs:      acc.durationMs + s.duration_ms,
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

  // True when this is a brand-new session with no exchanges yet. Drives the
  // Claude.ai-style centered "Start a conversation" layout: the compose form
  // floats in the middle of the screen instead of being pinned at the bottom,
  // and the scroll/status chrome stay hidden until there's something to show.
  const isEmpty = turns.length === 0 && !currentTurn && !loadErr;

  // ============================================================================
  return (
    <div className="flex h-screen flex-col">
      {/* Header — three explicit zones (left | center | right) so session
          totals can sit truly centered on desktop instead of drifting to the
          right edge. On phone, the center zone is empty (totals live in the
          sticky banner above the compose) and left + right close in tight. */}
      <header className="chrome-bar grid grid-cols-[auto_1fr_auto] items-center gap-2 px-3 pt-[max(0.5rem,env(safe-area-inset-top))] pb-2 sm:gap-3 sm:px-4 sm:pt-3 sm:pb-3">
        {/* LEFT — branch + push. The old "back to sessions" link is gone:
            the Workspace sidebar handles switching between chats now. When
            the sidebar is collapsed, the floating hamburger icon lives at
            top-left, so we reserve pl-12; when it's open, no padding
            needed (the sidebar's own column already pushes us right). */}
        <div className={`flex items-center gap-2 ${needsHamburgerSpace ? "pl-12" : ""}`}>
          <BranchBadge git={git} />
          {git?.has_remote && git.ahead > 0 && (
            <button
              type="button"
              onClick={pushNow}
              disabled={pushing}
              className="pill pill-accent min-h-touch disabled:opacity-50"
              title={`Push ${git.ahead} commit(s) to origin`}
            >
              {pushing ? "Pushing…" : `↑ ${git.ahead}`}
            </button>
          )}
        </div>

        {/* CENTER — session totals + live chip on desktop only. Empty on
            mobile so the left + right zones determine the layout. */}
        <div className="hidden min-w-0 items-center justify-center gap-2 sm:flex">
          {currentTurn && currentTurn.isStreaming && !currentTurn.error && (
            <NowChip turn={currentTurn} />
          )}
          <RunningTotals totals={totals} />
        </div>

        {/* RIGHT — context chip + theme toggle + debug toggle. The chip is
            always rendered (when we have data) so the user can see context
            fill at any moment — fresh session, mid-turn, idle between turns. */}
        <div className="flex items-center gap-2 justify-self-end">
          <ContextChip
            used={contextUsed}
            threshold={contextThreshold}
            compacting={contextCompacting}
          />
          <button
            type="button"
            onClick={toggleTheme}
            className="btn-icon shrink-0"
            title={`Switch to ${themeEffective === "dark" ? "light" : "dark"} mode`}
            aria-label={`Switch to ${themeEffective === "dark" ? "light" : "dark"} mode`}
          >
            {themeEffective === "dark" ? <SunGlyph /> : <MoonGlyph />}
          </button>
          <button
            type="button"
            onClick={toggleDebug}
            title={debugOpen ? "Close debug stream" : "Open debug stream"}
            aria-label="Toggle debug stream"
            className={`shrink-0 ${debugOpen ? "pill-accent" : ""} btn-icon sm:hidden`}
          >
            ⌘
          </button>
          <button
            type="button"
            onClick={toggleDebug}
            title="Toggle raw WebSocket event stream"
            className={`pill min-h-touch hidden sm:inline-flex ${debugOpen ? "pill-accent" : ""}`}
          >
            ⌘ debug
          </button>
        </div>
      </header>

      {/* Sticky plan panel — turn-independent state */}
      <PlanPanel items={plan} />

      {/* Deploy strip — sits between PlanPanel and chat scroll, shows
          deploys made from this session + a "Deploy" button to add more.
          Renders nothing when the session has produced no buildable
          dist/ yet AND has no prior deploys. */}
      <DeployStrip
        sessionId={sessionId ?? ""}
        sessionName={turns[0]?.userPrompt ?? "app"}
        apps={deployedApps}
        // "+ Deploy new" is enabled only when there's an unbuilt
        // sub-app with a build (not a rebuild of an already-deployed
        // one — those go through the pill's "🔄 Update" instead).
        hasUnbuiltBuild={!!lastDetected?.has_unbuilt_build}
        // Per-pill fresh-build lookup: each pill finds its own
        // candidate by deployed_slug and uses THAT candidate's
        // is_fresh (mtime > last_redeploy_at) — not the session-
        // wide freshest mtime — so only the genuinely rebuilt pill
        // shows Update in a multi-app session.
        lastDetected={lastDetected}
        // Per-slug live deploy progress (set by deploy_progress WS
        // event). Pills use this to render a "Step 3/12 — Building…"
        // chip so the user sees the 10-30s fullstack deploy making
        // real progress instead of a frozen "Deploying…".
        deployProgress={deployProgress}
        onUpdateClicked={(slug) => {
          // Mark which slug the user just clicked Update on so the
          // next deploy_progress event (which doesn't carry a slug)
          // can be attributed to it. Cleared on deploy_complete /
          // deploy_failed.
          lastClickedUpdateSlugRef.current = slug;
        }}
        // Hoisted state so the build-ready banner can also open the
        // modal (single source of truth — both the strip button and
        // the banner button call setShowDeployModal(true)).
        showModal={showDeployModal}
        onShowModalChange={setShowDeployModal}
        onDeployed={(app) => {
          setDeployedApps((prev) => [
            app, ...prev.filter((a) => a.slug !== app.slug),
          ]);
          // After a successful deploy, the dist is no longer "fresh" —
          // re-fetch detected-dist so the banner disappears and
          // fresh_mtime is fresh again for the next build.
          sessionApi.detectedDist(sessionId!).then(setLastDetected).catch(() => {});
        }}
        onDeleted={(slug) => setDeployedApps((prev) => prev.filter((a) => a.slug !== slug))}
        onToggled={(slug, state) => setDeployedApps((prev) =>
          prev.map((a) => (a.slug === slug ? { ...a, state } : a))
        )}
      />

      {/* Debug stream — floating raw WS event log. Use to diagnose live-event
          delivery: if events appear here in real time but the transcript
          doesn't reflect them, it's a render bug; if they only appear after
          the turn ends, it's a backend buffering bug. */}
      {debugOpen && (
        <DebugStream
          events={debugEvents}
          onClear={() => setDebugEvents([])}
          onClose={toggleDebug}
        />
      )}

      {/* Scrollable transcript — mono + 13px + tight leading via .transcript.
          The bottom padding gives the last line room to breathe before the
          sticky Live banner / compose divider starts; the gradient fade at
          the bottom of the scroll wrapper softens the visual transition so
          chat content never appears to be cut off mid-line. Hidden when the
          session is empty so the compose form can center vertically. */}
      <div className={`relative min-h-0 ${isEmpty ? "hidden" : "flex-1"}`}>
        <div
          ref={scrollRef}
          onScroll={onChatScroll}
          className="h-full overflow-y-auto px-4"
        >
        <div className="transcript mx-auto flex max-w-4xl flex-col pb-16">
          {loadErr && (
            <div className="mt-4 rounded border border-danger/40 bg-danger/10 p-3 text-danger">
              {loadErr}
            </div>
          )}
          {turns.map((t, i) => (
            <TurnCard key={t.id} turn={t} index={i} />
          ))}
          {/* Auto-compact breadcrumbs — one per `context_compacted` event
              the server has emitted this session. Rendered BELOW the
              turns (and the active turn) so they appear in chronological
              order, with the most recent compact at the bottom. Each
              card is collapsible so the user can read the summary
              preview or fold it away. */}
          {contextCompactedNotes.map((n) => (
            <ContextCompactedNoteCard key={n.id} note={n} />
          ))}
          {currentTurn && (
            <ActiveTurnCard turn={currentTurn} index={turns.length} />
          )}
          {turns.length === 0 && !currentTurn && !loadErr && (
            <EmptyState />
          )}
        </div>
        </div>
        {!chatAtBottom && (
          <button
            type="button"
            onClick={jumpChatToBottom}
            className="absolute bottom-3 right-3 z-20 flex h-10 w-10 items-center justify-center rounded-full border border-border bg-elevated/95 text-base text-text shadow-lift backdrop-blur-md hover:border-accent/60 hover:bg-accent/15 hover:text-accent"
            title="Jump to latest"
            aria-label="Jump to latest activity"
          >
            ↓
          </button>
        )}
      </div>

      {/* Centered welcome — only shown for brand-new sessions, sits just
          above the centered compose form. Mirrors the Claude.ai-style first-
          turn UX: hero copy + a single prominent input. */}
      {isEmpty && (
        <div className="mt-auto px-4 pt-8 text-center">
          <div
            aria-hidden
            className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent-gradient shadow-lift"
          >
            <span className="text-2xl font-bold text-white">⌘</span>
          </div>
          <h1 className="font-serif text-3xl font-semibold tracking-tight text-text sm:text-4xl">
            Start a conversation
          </h1>
          <p className="mx-auto mt-2.5 max-w-md text-sm text-muted">
            Type a request below. The agent will reply, call tools, edit files,
            and track tokens — everything visible as it happens.
          </p>
        </div>
      )}

      {/* Sticky LIVE activity strip — only shown when a turn is actually
          running (and there's chat above to anchor it). */}
      {!isEmpty && <ChatStatusBar currentTurn={currentTurn} />}

      {/* Context-fill indicator now lives in the header chip (see ContextChip
          in the right zone). No bottom progress bar — the chip is enough. */}

      {/* Input — pinned bottom, safe-area aware. While a turn is in flight the
          Send button morphs into Stop so cancelling is a single click.
          When the session is empty (no turns yet), the form is restyled to
          center itself in the chat area instead of sitting at the very
          bottom — Claude.ai-style "start a conversation" feel. */}
      <form
        onSubmit={send}
        className={
          isEmpty
            ? "relative mb-auto w-full px-3 pb-8 pt-4"
            : "chrome-bar-bottom relative px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]"
        }
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
  turns: Turn[]; currentTurn: Turn | null; plan: TodoItem[];
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
        const _bashOutput = parseBashOutputMarker(
          typeof p.preview === "string" ? p.preview : undefined,
        );
        for (let i = list.length - 1; i >= 0; i--) {
          if (list[i].tool === p.tool && list[i].status === "running") {
            list[i] = {
              ...list[i],
              status: p.error ? "error" : "done",
              preview: typeof p.preview === "string" ? p.preview : undefined,
              previewTruncated: !!p.preview_truncated,
              bashOutput: _bashOutput ?? list[i].bashOutput,
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
        const cR  = Number(p.cache_read_delta  ?? 0);
        const cW  = Number(p.cache_creation_delta ?? 0);
        if (inD === 0 && outD === 0 && cR === 0 && cW === 0) break;
        if (aid && curr.agents[aid]) {
          curr.agents[aid].liveInputTokens  += inD;
          curr.agents[aid].liveOutputTokens += outD;
          curr.agents[aid].llmCalls.push({
            ts: ev.ts, inputTokens: inD, outputTokens: outD,
            cacheReadTokens: cR, cacheCreationTokens: cW,
          });
        } else {
          curr.liveInputTokens  += inD;
          curr.liveOutputTokens += outD;
          curr.blocks.push({
            id: _newBlockId("llm_call", ev.ts, curr.blocks.length),
            kind: "llm_call", ts: ev.ts,
            inputTokens: inD, outputTokens: outD,
            cacheReadTokens: cR, cacheCreationTokens: cW,
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
  // If the log ended mid-turn, return the in-progress turn as `currentTurn`
  // (NOT shoved into the completed turns array). This is the critical fix
  // for joining a session that's actively streaming: without it, refreshing
  // the page or switching to a session mid-turn left curr=null, so every
  // subsequent live WS event handler (`setCurrentTurn(c => c ? {...} : c)`)
  // silently no-op'd — the Stop button stayed hidden and streaming text
  // never appeared. Keeping isStreaming=true here is what unlocks the Stop
  // button rendering condition and the live spinner.
  return { turns: completed, currentTurn: curr, plan };
}

// ============================================================================
// ContextChip — compact "X% used" pill in the chat header. Shows the % of
// the auto-compact threshold (50K default) that's currently filled. The
// number ticks UP as the context grows, and 100% is the moment
// auto-compaction fires. Color tiers at a glance:
//   < 60%       → calm
//   60-89%      → warn (orange)
//   90%+        → danger (red)
//   compacting  → pulsing accent dot, label reads "Compacting…"
// Always visible so the user has a stable reference for "how full is
// this session". On a fresh session with no LLM call yet it shows
// "0% used" against the threshold.
// ============================================================================

function ContextChip({
  used, threshold, compacting,
}: { used: number; threshold: number; compacting: boolean }) {
  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k` : String(n);

  const pctUsed = threshold > 0
    ? Math.max(0, Math.min(999, Math.round((used / threshold) * 100)))
    : 0;

  let dotCls = "bg-accent/40";
  let textCls = "text-text";
  let borderCls = "border-border/60 bg-elevated/60";
  if (compacting) {
    dotCls = "bg-accent animate-pulse-soft";
    textCls = "text-accent";
    borderCls = "border-accent/40 bg-accent/[0.06]";
  } else if (pctUsed >= 90) {
    dotCls = "bg-danger/80";
    textCls = "text-danger";
    borderCls = "border-danger/40 bg-danger/[0.06]";
  } else if (pctUsed >= 60) {
    dotCls = "bg-warn/80";
    textCls = "text-warn";
    borderCls = "border-warn/40 bg-warn/[0.05]";
  }

  const label = compacting ? "Compacting…" : `${pctUsed}% used`;

  return (
    <div
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-md border ${borderCls} px-2 py-0.5 font-sans backdrop-blur-sm`}
      title={
        compacting
          ? "Compacting context — summarising older turns to keep the session running"
          : `Context: ${fmt(used)} used. Auto-compact fires at ${fmt(threshold)}.`
      }
    >
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${dotCls}`} />
      <span className={`text-tx-xs font-medium ${textCls}`}>{label}</span>
    </div>
  );
}

// ============================================================================
// ContextCompactedNoteCard — collapsible system message rendered in the
// chat transcript each time auto-compaction fires (one per
// `context_compacted` WS event). Default state is folded — the user
// expands to read the summary preview. Keeps the chat clean while still
// giving a visible breadcrumb of WHAT got summarised WHEN.
// ============================================================================

function ContextCompactedNoteCard({ note }: { note: ContextCompactedNote }) {
  const [open, setOpen] = useState(false);
  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k` : String(n);
  const freed = Math.max(0, note.tokensBefore - note.tokensAfter);

  return (
    <div className="mt-2 flex justify-start">
      <div className="max-w-[92%] rounded-md border border-accent/30 bg-accent/[0.04] text-text">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-tx-xs"
          title={open ? "Hide auto-compact details" : "Show what was summarised"}
        >
          <span className="text-accent">📦</span>
          <span className="font-medium text-text/90">
            Auto-compacted context
          </span>
          <span className="text-text/60">
            · summarised {note.removed} older message{note.removed === 1 ? "" : "s"} ({fmt(freed)} tokens freed)
            · kept {note.kept} recent
          </span>
          <span className="ml-auto text-text/50">
            {open ? "▾" : "▸"}
          </span>
        </button>
        {open && (
          <div className="border-t border-accent/20 px-3 py-2 text-tx-xs text-text/80">
            <p className="mb-1.5 text-text/60">
              Threshold: {fmt(note.threshold || 50_000)} tokens. Before: {fmt(note.tokensBefore)}. After: {fmt(note.tokensAfter)}.
            </p>
            {note.summaryPreview ? (
              <pre className="whitespace-pre-wrap rounded bg-elevated/60 p-2 font-sans text-text/80">
                {note.summaryPreview}{note.summaryPreview.length >= 280 ? "…" : ""}
              </pre>
            ) : (
              <p className="text-text/50">No preview captured for this compaction.</p>
            )}
            <p className="mt-1.5 text-text/50">
              The full summary is in the conversation as a hidden HumanMessage — the agent can re-read it any time.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// ChatStatusBar — sticky LIVE activity strip pinned above the compose divider.
//
// Only renders while a turn is actually running. Completed turns keep their
// own per-turn stats card inside the TurnCard (so history is inspectable on
// scroll). Centered to the same max-width as the chat content so it visually
// lines up with the conversation above instead of running edge-to-edge.
// ============================================================================

function ChatStatusBar({
  currentTurn,
}: { currentTurn: Turn | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!currentTurn?.isStreaming) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [currentTurn?.isStreaming]);

  if (!currentTurn || !currentTurn.isStreaming || currentTurn.error) return null;

  const totalIn = currentTurn.liveInputTokens;
  const totalOut = currentTurn.liveOutputTokens;
  const runningTools = currentTurn.tools.filter((t) => t.status === "running").length;
  const runningAgents = Object.values(currentTurn.agents).filter((a) => a.status === "running").length;

  // Live cache hits — sum cacheReadTokens across every orchestrator-level
  // llm_call block this turn has produced. The blocks carry per-call cache
  // deltas; the running `liveInputTokens` does not (it includes both
  // cache_read and uncached alike).
  const liveCacheRead = currentTurn.blocks.reduce((acc, b) => {
    return b.kind === "llm_call" ? acc + (b.cacheReadTokens ?? 0) : acc;
  }, 0);
  // Live cost is intentionally NOT shown here — the server's authoritative
  // per-component cost lands in turn_summary, and pricing client-side risks
  // drift from the server's MODEL_PRICING table. The per-turn footer and
  // session chip show the real number.

  return (
    <div className="bg-bg">
      <div className="mx-auto max-w-4xl px-4 py-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-accent/25 bg-accent/10 px-3 py-1.5 font-sans text-tx-xs">
          <span className="inline-flex items-center gap-1.5">
            <span className="stream-dot" />
            <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-accent">
              Live
            </span>
          </span>
          <Sep />
          <StatItem label="Elapsed" value={formatDurationCompact(now - currentTurn.startedAt)} />
          <Sep />
          <StatItem
            label="Tools"
            value={`${currentTurn.tools.length}${runningTools ? ` (${runningTools} run)` : ""}`}
            valueClass={runningTools ? "text-warn" : "text-text"}
          />
          {Object.keys(currentTurn.agents).length > 0 && (
            <>
              <Sep />
              <StatItem
                label="Agents"
                value={`${Object.keys(currentTurn.agents).length}${runningAgents ? ` (${runningAgents} run)` : ""}`}
                valueClass={runningAgents ? "text-warn" : "text-text"}
              />
            </>
          )}
          {(totalIn > 0 || totalOut > 0) && (
            <>
              <Sep />
              <StatItem
                label="In"
                value={
                  liveCacheRead > 0
                    ? `${formatTokensTiny(totalIn)} (${formatTokensTiny(liveCacheRead)} cached)`
                    : formatTokensTiny(totalIn)
                }
                valueClass="text-accent"
                title={liveCacheRead > 0
                  ? `${totalIn.toLocaleString()} in · ${liveCacheRead.toLocaleString()} cache hits · ${(totalIn - liveCacheRead).toLocaleString()} new`
                  : `${totalIn.toLocaleString()} in`}
              />
              <StatItem label="Out" value={formatTokensTiny(totalOut)} valueClass="text-accent-2" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function StatItem({
  label, value, valueClass = "text-text", title,
}: { label: string; value: string; valueClass?: string; title?: string }) {
  return (
    <span className="inline-flex items-baseline gap-1" title={title}>
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-subtle">
        {label}
      </span>
      <span className={`font-mono text-tx-sm ${valueClass}`}>{value}</span>
    </span>
  );
}

function Sep() {
  return <span className="text-subtle">·</span>;
}

function formatDurationCompact(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return `${m}m ${r}s`;
}

function formatTokensTiny(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + "k";
  return (n / 1_000_000).toFixed(1) + "M";
}

function formatCostTiny(c: number): string {
  return c < 0.01 ? `$${c.toFixed(4)}` : `$${c.toFixed(c < 1 ? 3 : 2)}`;
}

// Parse the bash-output status marker the bash tool always appends to
// its preview. Two formats, both set in tools/wrappers.py: see
// _smart_truncate_bash_output. The marker is always present on bash
// calls so the chat UI can show a single status line for every
// invocation, not just the truncated ones.
//
//   passed_through:  [bash-output: total=5234, cap=10000, status=passed_through]
//   truncated:       [bash-output: total=25939, cap=10000, status=truncated,
//                                 kept_first=2800, kept_last=6200, dropped=16939,
//                                 verdict=FAILURE, spill=/tmp/ojas-bash/...log]
//
// Returns null when the preview doesn't contain a marker (the tool
// isn't bash, or the preview hasn't arrived yet).
function parseBashOutputMarker(
  preview: string | undefined,
): ToolEvent["bashOutput"] | null {
  if (!preview) return null;
  const m = preview.match(
    /\[bash-output:\s+total=(\d+),\s+cap=(\d+),\s+status=(passed_through|truncated)(?:,\s+kept_first=(\d+))?(?:,\s+kept_last=(\d+))?(?:,\s+dropped=(\d+))?(?:,\s+verdict=(SUCCESS|FAILURE))?(?:,\s+spill=([^\]\s]+))?\s*\]/,
  );
  if (!m) return null;
  const total = parseInt(m[1], 10);
  const cap = parseInt(m[2], 10);
  const status = m[3] as "passed_through" | "truncated";
  if (status === "passed_through") {
    return { total, cap, status };
  }
  // truncated — optional fields default to 0 / null
  const keptFirst = m[4] ? parseInt(m[4], 10) : 0;
  const keptLast = m[5] ? parseInt(m[5], 10) : 0;
  const dropped = m[6] ? parseInt(m[6], 10) : 0;
  const verdict = (m[7] === "FAILURE" ? "FAILURE" : "SUCCESS") as "SUCCESS" | "FAILURE";
  const spillPath = m[8] && m[8] !== "null" ? m[8] : null;
  return { total, cap, status, keptFirst, keptLast, dropped, verdict, spillPath };
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
        <span className="text-white text-2xl font-bold">⌘</span>
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
  events, onClear, onClose,
}: {
  events: { kind: string; payload: any; ts: number }[];
  onClear: () => void;
  onClose: () => void;
}) {
  const boxRef = useRef<HTMLDivElement | null>(null);

  // Drag state — desktop only. The header acts as the drag handle; click +
  // drag moves the panel via a transform offset from its CSS anchor
  // (sm:bottom-20 sm:right-4). Mobile uses the inline layout (under the
  // header) so dragging is disabled there.
  const [dragOff, setDragOff] = useState<{ x: number; y: number }>({ x: 0, y: 0 });
  const dragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null);

  const onHeaderPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    // Only drag on desktop. matchMedia is the cheap way to gate.
    if (window.matchMedia("(max-width: 639px)").matches) return;
    // Don't start a drag if the click landed on a button (Clear / Close).
    if ((e.target as HTMLElement).closest("button")) return;
    dragRef.current = {
      startX: e.clientX, startY: e.clientY,
      baseX: dragOff.x, baseY: dragOff.y,
    };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onHeaderPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    setDragOff({
      x: dragRef.current.baseX + (e.clientX - dragRef.current.startX),
      y: dragRef.current.baseY + (e.clientY - dragRef.current.startY),
    });
  };
  const onHeaderPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    try { (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId); } catch {}
  };
  // Follow-mode: stick to bottom while events arrive, unless the user has
  // scrolled up to inspect. Same UX as the main chat scroll.
  const [atBottom, setAtBottom] = useState(true);
  useEffect(() => {
    if (!atBottom) return;
    const el = boxRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [atBottom, events.length]);
  const onScroll = () => {
    const el = boxRef.current;
    if (!el) return;
    const threshold = 30;
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < threshold);
  };
  const jumpToBottom = () => {
    const el = boxRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setAtBottom(true);
  };

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
    <div
      style={
        // Apply drag offset only when there IS one (and only on desktop, where
        // the panel is fixed-positioned). On mobile the panel is inline so
        // dragOff stays at {0,0} naturally and transform is a no-op.
        dragOff.x !== 0 || dragOff.y !== 0
          ? { transform: `translate(${dragOff.x}px, ${dragOff.y}px)` }
          : undefined
      }
      className="
        z-30 w-full overflow-hidden border-b border-border bg-bg/95
        sm:fixed sm:bottom-20 sm:right-4 sm:w-[420px] sm:max-w-[calc(100vw-2rem)]
        sm:rounded-lg sm:border sm:shadow-lift sm:backdrop-blur-md
      "
    >
      <div
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={onHeaderPointerUp}
        onPointerCancel={onHeaderPointerUp}
        className="flex select-none items-center justify-between border-b border-border bg-elevated/60 px-3 py-1.5 sm:cursor-move"
      >
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-accent">
            Debug stream
          </span>
          <span className="font-mono text-[10px] text-subtle">
            {events.length} events
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onClear}
            className="text-[10px] text-muted hover:text-danger"
          >
            clear
          </button>
          <button
            type="button"
            onClick={onClose}
            className="flex h-5 w-5 items-center justify-center rounded text-muted hover:bg-elevated hover:text-text"
            title="Close debug panel"
            aria-label="Close debug panel"
          >
            ✕
          </button>
        </div>
      </div>
      <div className="relative">
      <div
        ref={boxRef}
        onScroll={onScroll}
        className="max-h-72 overflow-y-auto px-3 py-2"
      >
        {events.length === 0 ? (
          <div className="font-sans text-[11px] text-subtle">
            Waiting for events… send a message to see the stream.
          </div>
        ) : (
          rows
        )}
      </div>
      {!atBottom && events.length > 0 && (
        <button
          type="button"
          onClick={jumpToBottom}
          className="absolute bottom-1.5 right-1.5 flex h-7 w-7 items-center justify-center rounded-full border border-border bg-elevated/95 text-xs text-text shadow-lift backdrop-blur-md hover:border-accent/60 hover:bg-accent/15 hover:text-accent"
          title="Jump to latest event"
          aria-label="Jump to latest event"
        >
          ↓
        </button>
      )}
      </div>
    </div>
  );
}



// ============================================================================
// DeployStrip — sticky horizontal strip between the plan panel and the chat
// scroll area. Shows every app deployed FROM this session as a state-aware
// pill (slug + public URL + 🔄 Update when a fresh build is detected, ✓ Up
// to date when not). A single "+ Deploy new" button on the right opens the
// modal for a *new* app (first-time deploy or a sibling project in a
// multi-app session) — it is only enabled when the agent has produced a
// build since the last re-deploy of the default project, so the user never
// sees a "Deploy" button that would fail with "no dist/ found".
// ============================================================================

// "3m ago" / "2h ago" — short relative-time formatter for the
// per-pill "last deployed" badge. Lives at module scope so the
// pill doesn't re-allocate it on every render.
function timeAgoShort(epochSeconds: number): string {
  if (!epochSeconds) return "";
  const sec = Math.max(1, Math.floor(Date.now() / 1000 - epochSeconds));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

// ============================================================================

function DeployStrip({
  sessionId, sessionName, apps, hasUnbuiltBuild, lastDetected, deployProgress, onUpdateClicked,
  onDeployed, onDeleted, onToggled,
  showModal: showModalProp, onShowModalChange,
}: {
  sessionId: string;
  sessionName: string;
  apps: DeployedApp[];
  // True when at least one detected candidate is BUILT (mtime > 0)
  // AND NOT YET DEPLOYED. Drives the "+ Deploy new" button — that
  // button is the only deploy-new affordance, and it's only
  // meaningful when there's something genuinely new to publish.
  // When all sub-apps are already deployed, the user is steered to
  // the per-pill "🔄 Update" buttons instead.
  hasUnbuiltBuild: boolean;
  // Full detected-dist payload — pills look themselves up in
  // lastDetected.candidates by deployed_slug to get THIS app's
  // is_fresh (mtime > last_redeploy_at for THIS app's dist), not
  // the global session-freshest mtime. Lets a 3-app session where
  // only one was rebuilt show "🔄 Update" on just that one pill.
  lastDetected: DetectedDist | null;
  // Per-slug live progress. Empty when nothing is deploying. Pills
  // look themselves up by slug.
  deployProgress: Record<string, { phase: string; steps_done: number; steps_total: number }>;
  // Called when the user clicks Update on a pill — ChatPage uses
  // this to remember WHICH slug to attribute the next
  // deploy_progress event to (events don't carry a slug).
  onUpdateClicked: (slug: string) => void;
  onDeployed: (app: DeployedApp) => void;
  onDeleted: (slug: string) => void;
  onToggled: (slug: string, state: string) => void;
  // The build-ready banner also opens this dialog. State is hoisted
  // to ChatPage so the banner and the strip share one source of
  // truth. If the prop is omitted (e.g. in tests), we fall back to
  // local state.
  showModal?: boolean;
  onShowModalChange?: (v: boolean) => void;
}) {
  const [localShow, setLocalShow] = useState(false);
  const showModal = showModalProp ?? localShow;
  const setShowModal = (v: boolean) => {
    if (onShowModalChange) onShowModalChange(v);
    else setLocalShow(v);
  };

  return (
    <>
      <div className="border-b border-border bg-elevated/40">
        <div className="mx-auto flex max-w-4xl flex-wrap items-center gap-2 px-4 py-2">
          {apps.length === 0 ? (
            <span className="flex-1 text-tx-xs text-muted">
              {hasUnbuiltBuild
                ? "Build ready — click “Deploy new” to publish."
                : "Build the app first (`npm run build`), then come back and click “Deploy new”."}
            </span>
          ) : (
            apps.map((a) => (
              <DeployedAppPill
                key={a.slug}
                app={a}
                lastDetected={lastDetected}
                // Pass the live progress slice for this slug (if
                // any). Pills fall back to local `busy` state when
                // the progress event hasn't arrived yet.
                progress={deployProgress[a.slug]}
                onUpdateClicked={onUpdateClicked}
                onDeployed={onDeployed}
                onDeleted={onDeleted}
                onToggled={onToggled}
              />
            ))
          )}
          {/* "+ Deploy new" — for a *new* sub-app from this session
              (first-time deploy, or a sibling project in a multi-app
              session). Disabled when every detected sub-app is
              already deployed — in that case the user wants
              "🔄 Update" on a pill, not a brand-new slug. */}
          <button
            type="button"
            onClick={() => setShowModal(true)}
            disabled={!sessionId || !hasUnbuiltBuild}
            title={
              !hasUnbuiltBuild
                ? apps.length === 0
                  ? "Build the app first (`npm run build`) to enable"
                  : "All built sub-apps are already deployed — use a pill's 🔄 Update instead"
                : "Publish a new sub-app from this session"
            }
            className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-md border border-accent/40 bg-accent/15 px-3 py-1.5 text-tx-sm font-medium text-accent hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <UploadIcon /> Deploy new
          </button>
        </div>
      </div>
      {showModal && (
        <DeployModal
          sessionId={sessionId}
          defaultName={sessionName}
          onClose={() => setShowModal(false)}
          onDeployed={(result) => {
            onDeployed(result.app);
            setShowModal(false);
          }}
        />
      )}
    </>
  );
}

function hostApps(): string {
  // Best-effort hint for the URL placeholder — we assume the current host
  // IS the apps root (most common setup). Real URL comes from the deploy
  // response which knows the server-side resolved root.
  if (typeof window === "undefined") return "your-domain";
  return window.location.host;
}

function DeployedAppPill({
  app, lastDetected, progress, onUpdateClicked,
  onDeployed, onDeleted, onToggled,
}: {
  app: DeployedApp;
  // Per-candidate build state. We look ourselves up in
  // lastDetected.candidates by deployed_slug and use THAT candidate's
  // is_fresh — server-computed as `c.mtime > c.last_redeploy_at for
  // this app`. This is the key fix: a 3-app session where only one
  // was rebuilt now shows "🔄 Update" on JUST that one pill, not on
  // all three (which is what the previous session-freshest mtime
  // comparison did). A pill without a matching candidate simply
  // shows "✓ Up to date".
  lastDetected: DetectedDist | null;
  // Live progress for THIS slug (undefined if not currently
  // deploying). Shows up in the pill's "Step N/12 — Building…" chip
  // so the user sees the 10-30s fullstack deploy making progress
  // instead of staring at a frozen "Deploying…" pill.
  progress?: { phase: string; steps_done: number; steps_total: number };
  // Notifies the parent when the user clicks Update, so the parent
  // can remember WHICH slug to attribute the next deploy_progress
  // event to (events don't carry a slug).
  onUpdateClicked: (slug: string) => void;
  onDeployed: (app: DeployedApp) => void;
  onDeleted: (slug: string) => void;
  onToggled: (slug: string, state: string) => void;
}) {
  // Server-computed URL is authoritative (handles apps-root domain
  // resolution + future routing). Fall back to a derived URL only
  // if the server didn't send one -- legacy rows pre-dating the
  // public_url field.
  const url = app.public_url
    || `${window.location.protocol}//${app.slug}.${window.location.host}/`;
  const [busy, setBusy] = useState(false);
  const state = app.state ?? "running";
  const isOff = state === "stopped" || state === "error";
  // Per-app "this build is newer than my last deploy" — server
  // stamps this per-candidate as `c.mtime > c.last_redeploy_at`,
  // so a rebuild of app B doesn't make app A's pill claim it's
  // stale. Pills without a matching candidate fall back to false
  // (e.g., when the user just deleted the dist).
  const appFreshBuild = lastDetected?.candidates.find(
    (c) => c.deployed_slug === app.slug,
  )?.is_fresh ?? false;
  // Reset busy when the deploy settles. The parent clears
  // `deployProgress[slug]` for this slug in BOTH the
  // `deploy_complete` and `deploy_failed` WS handlers, so flipping
  // from { phase, … } to undefined is the canonical "deploy is
  // settled, refresh the row" signal. Without this, the pill would
  // stay amber + "Deploying…" until the 60 s safety timeout fired,
  // which made a successful 12 s fullstack deploy look frozen.
  useEffect(() => {
    if (busy && !progress) setBusy(false);
  }, [busy, progress]);
  const remove = async () => {
    if (!confirm(`Take down ${app.slug}? The public URL will stop working.`)) return;
    try {
      await deployedAppsApi.delete(app.slug);
      onDeleted(app.slug);
    } catch (e: any) {
      alert(`Delete failed: ${e?.message ?? "unknown"}`);
    }
  };
  const toggle = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const next = isOff
        ? await deployedAppsApi.start(app.slug)
        : await deployedAppsApi.stop(app.slug);
      onToggled(app.slug, next.state);
    } catch (e: any) {
      alert(`Toggle failed: ${e?.message ?? "unknown"}`);
    } finally {
      setBusy(false);
    }
  };
  // Update = in-place re-deploy of the same slug. The server detects
  // slug+owner match and swaps the dist atomically (no new port, no
  // new systemd unit, no new URL). The deploy modal would also work
  // but is overkill for a re-deploy -- it asks for a slug, project,
  // and 12-step progress that the user has already seen once.
  //
  // We DON'T await the job here (it can take 10-30s for a fullstack).
  // Instead, the WS event handler in ChatPage listens for
  // deploy_complete / deploy_failed and refreshes deployedApps +
  // detectedDist. The local `busy` flips the pill to a "Deploying…"
  // chip so the user sees the click took effect; the WS event then
  // replaces the pill with the fresh row (which has the new
  // last_redeploy_at) and clears busy via the natural re-render.
  const update = async () => {
    if (busy) return;
    setBusy(true);
    // Tell the parent which slug was clicked, so the next
    // deploy_progress WS event (which doesn't carry a slug) can
    // be attributed to this pill.
    onUpdateClicked(app.slug);
    // Immediate user feedback: a toast on the side panel so the
    // user knows the click took effect even if they miss the
    // pill change. The deploy_complete WS event also fires a
    // success toast, which the user will see in sequence.
    try {
      // Use the global session toast (defined in lib/sessionContext).
      // The component may not have direct access to it; fall back
      // to a console log if the import isn't reachable from here.
      // The pill's amber pulse + the WS-driven success toast is
      // the primary UX, this is the belt-and-braces backup.
      // (Defer to the WS handler for the success toast — the in-flight
      // toast would be redundant and would race the success one.)
      await sessionApi.deploy(app.source_session_id ?? "", {
        slug: app.slug,
        project_dir: app.project_dir ?? undefined,
      });
      // Don't setBusy(false) here — leave the pill in the "Deploying…"
      // state until the deploy_complete / deploy_failed WS event
      // arrives and re-fetches the row. Fall back after 60s in case
      // the WS is wedged so the user isn't stuck on "Deploying…"
      // forever.
      setTimeout(() => setBusy(false), 60_000);
    } catch (e: any) {
      alert(`Update failed: ${e?.message ?? "unknown"}`);
      setBusy(false);
    }
  };
  // State dot colour mirrors the Settings page badge so the user
  // gets the same vocabulary across both surfaces.
  const dotClass =
    state === "running"  ? "bg-emerald-500" :
    state === "starting" ? "bg-amber-500" :
    state === "error"    ? "bg-rose-500" :
                           "bg-zinc-500";
  const pillClass =
    state === "running"  ? "border-success/30 bg-success/10 text-success" :
    state === "stopped"  ? "border-border bg-elevated text-muted" :
                           "border-border bg-elevated text-muted";
  // When busy, override the pillClass to a clearly different colour
  // (amber, with a pulse animation) so the user gets an instant
  // visual signal that the click registered. The default pillClass
  // is reused when not busy.
  const busyPillClass = "border-amber-400/50 bg-amber-400/10 text-amber-700 dark:text-amber-300 animate-pulse";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-tx-xs ${busy ? busyPillClass : pillClass}`}
      aria-busy={busy}
      aria-live={busy ? "polite" : undefined}
    >
      <button
        type="button"
        onClick={toggle}
        disabled={busy || state === "starting"}
        aria-label={isOff ? "Bring online" : "Pause"}
        title={isOff ? "Paused — click to bring online" : "Running — click to pause"}
        className="inline-flex items-center"
      >
        <span className={`inline-block size-2 rounded-full transition-opacity ${dotClass} ${busy ? "opacity-50" : ""}`} />
      </button>
      <span className="font-mono" title={url}>{app.slug}</span>
      {/* Public URL link, always present. The href is the
          server-computed public_url so the user can bookmark one URL
          and have it survive every re-deploy. The "↗" icon is
          intentionally small and the URL is in the title for
          hover-preview without eating pill real estate. */}
      <a
        href={url} target="_blank" rel="noreferrer"
        className="hover:text-accent" title={url}
      >↗</a>
      {/* Last-deployed timestamp. Visible BOTH when idle and when
          busy (the busy state shows " · 12s ago  ↻ Deploying…"
          side by side, so the user knows the previous deploy was
          12s ago AND a new one is in flight). The WS
          deploy_complete handler refreshes the row with the new
          last_redeploy_at so this updates without a page reload. */}
      {app.last_redeploy_at > 0 && (
        <span
          className="text-tx-xs text-muted"
          title={new Date(app.last_redeploy_at * 1000).toISOString()}
        >
          · {timeAgoShort(app.last_redeploy_at)}
        </span>
      )}
      {/* Update / Up-to-date / Deploying indicator. Driven by the
          session-wide fresh_build flag, not per-app: if the agent
          rebuilt any sub-app in this session, the user is probably
          about to want to push it everywhere. We could compute this
          per-app (compare dist mtime to this app's last_redeploy_at)
          but that adds a server round-trip per pill; the session-level
          flag is correct in 99% of cases (the agent rebuilds the
          whole workspace) and the user can always click "Deploy new"
          for a sibling project.

          The "Deploying…" state shows while busy=true (set when the
          user clicks Update; cleared when the deploy_complete WS
          event re-fetches the row). The pill BG also pulses amber
          via the busyPillClass override so the click is impossible
          to miss even for a second. */}
      {busy ? (
        // When the live progress event has arrived, show the
        // current step + step count so the user sees the
        // 10-30s fullstack deploy making real progress. Falls
        // back to a generic "Deploying…" pulse before the first
        // event lands (and after the 60s safety timeout).
        <span
          className="inline-flex items-center gap-1 rounded border border-amber-500/50 bg-amber-500/20 px-1.5 py-0.5 font-medium text-amber-700 dark:text-amber-300"
          title={
            progress
              ? `Step ${progress.steps_done}/${progress.steps_total || "?"} — ${progress.phase}`
              : "Deploy in progress… the deploy_complete WebSocket event will refresh this pill when it lands"
          }
        >
          <span className="inline-block size-1.5 animate-pulse rounded-full bg-amber-500" />
          {progress && progress.steps_total > 0
            ? `${progress.phase} (${progress.steps_done}/${progress.steps_total})`
            : "Deploying…"}
        </span>
      ) : appFreshBuild ? (
        <button
          type="button"
          onClick={update}
          title={`Push the new build to ${app.slug} (same URL, no new port)`}
          className="inline-flex items-center gap-1 rounded border border-accent/40 bg-accent/15 px-1.5 py-0.5 font-medium text-accent hover:bg-accent/25"
        >
          🔄 Update
        </button>
      ) : (
        <span
          className="inline-flex items-center gap-1 rounded border border-success/30 bg-success/10 px-1.5 py-0.5 font-medium text-success"
          title="Live and up to date — no new build since the last deploy"
        >
          ✓ Up to date
        </span>
      )}
      <button
        type="button" onClick={remove}
        className="text-muted hover:text-danger"
        title="Delete" aria-label="Delete app"
      >✕</button>
    </span>
  );
}

function DeployModal({
  sessionId, defaultName, onClose, onDeployed,
}: {
  sessionId: string;
  defaultName: string;
  onClose: () => void;
  // The DeployResult from the OLD sync endpoint shape — ChatPage uses
  // .app to splice the new app into the deployedApps array. We pass
  // the canonical row from the job's `result` (or the placeholder
  // app from the 202 if the user dismisses before the job finishes).
  onDeployed: (result: { slug: string; url: string; app: DeployedApp }) => void;
}) {
  // Phase machine. "config" is the initial slug+projectDir picker;
  // "running" shows the step checklist and HARD-GATES every close
  // affordance (backdrop click, ESC, Cancel button); "done" shows
  // the URL + Open in new tab; "failed" shows the error + Dismiss.
  type Phase = "config" | "running" | "done" | "failed";
  const [phase, setPhase] = useState<Phase>("config");
  const [slug, setSlug] = useState(slugifyName(defaultName));
  const [detected, setDetected] = useState<DetectedDist | null>(null);
  const [detecting, setDetecting] = useState(true);
  const [projectDir, setProjectDir] = useState("");
  const [slugError, setSlugError] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // Job-state refs held across the running-phase polling loop.
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<DeployJobStatus | null>(null);
  const [finalResult, setFinalResult] = useState<{ slug: string; url: string; app: DeployedApp } | null>(null);
  const pollAbortRef = useRef<AbortController | null>(null);

  // ---- Detect built dist on mount (same as the old modal).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const d = await sessionApi.detectedDist(sessionId);
        if (cancelled) return;
        setDetected(d);
        // Pick the freshest UNBUILT candidate as the default
        // projectDir. The server's auto_pick is the freshest of
        // ALL candidates (including already-deployed ones), which
        // would point at a deployed row and fall outside the
        // unbuilt filter — leaving the picker showing nothing.
        // Fall back to d.auto_pick if no unbuilt candidate exists
        // (the picker handles that case via `noUnbuilt`).
        const unbuilt = d.candidates.filter((c) => !c.is_deployed);
        const pick = unbuilt[0]?.project_dir ?? d.auto_pick ?? "";
        if (pick) setProjectDir(pick);
      } catch (e: any) {
        if (cancelled) return;
        setErr(e?.message ?? "could not detect build");
      } finally {
        if (!cancelled) setDetecting(false);
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  // Filter candidates to UNBUILT sub-apps only. The modal is only
  // opened from "+ Deploy new" (which is disabled when every
  // sub-app is already deployed), so an already-deployed candidate
  // in the dropdown would 409 on submit. Drop them up front and
  // let the rest of the modal operate on the filtered list as if
  // it were the only set of candidates.
  const unbuiltCandidates = useMemo(
    () => (detected?.candidates ?? []).filter((c) => !c.is_deployed),
    [detected],
  );
  // Defensive guard: the strip disables "+ Deploy new" when
  // !has_unbuilt_build, so reaching this state from the UI is rare.
  // But if the modal is opened some other way and ALL detected
  // candidates are already deployed, show a clear message instead
  // of a dead empty picker.
  const noUnbuilt =
    !detecting
    && (detected?.candidates?.length ?? 0) > 0
    && unbuiltCandidates.length === 0;
  const multiple = unbuiltCandidates.length > 1;
  const noBuild = !detecting && (detected?.candidates?.length ?? 0) === 0;

  // ---- AbortController for the GET /deploy-jobs/{id} poll. We do
  // NOT abort the POST itself on unmount — the deploy should keep
  // running server-side so the user can come back and see the result.
  // Only the polling is aborted (so a dead-tab doesn't keep pinging
  // a finished job).
  useEffect(() => {
    return () => {
      if (pollAbortRef.current) {
        pollAbortRef.current.abort();
        pollAbortRef.current = null;
      }
    };
  }, []);

  // ---- Polling loop. Starts on phase="running", stops on phase
  // transition or unmount. 1.5s interval (matches the Admin page
  // cadence for similar live-data surfaces).
  useEffect(() => {
    if (phase !== "running" || !jobId) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      pollAbortRef.current = new AbortController();
      try {
        const s = await sessionApi.deployJobStatus(sessionId, jobId, {
          signal: pollAbortRef.current.signal,
        });
        if (cancelled) return;
        setJobStatus(s);
        if (s.status === "succeeded") {
          if (s.result) setFinalResult(s.result);
          setPhase("done");
        } else if (s.status === "failed" || s.status === "cancelled") {
          setErr(s.error || (s.status === "cancelled" ? "Cancelled by you." : "Deploy failed."));
          setPhase("failed");
        }
      } catch (e: any) {
        if (cancelled) return;
        // AbortError on unmount is fine — ignore it. Any other network
        // error: keep polling (transient), don't flip the modal to failed.
        if (e?.name === "AbortError") return;
        // 404 means the job is gone (server restart, very old job) — show failed.
        if (e?.status === 404) {
          setErr("Deploy job not found on server. The backend may have restarted.");
          setPhase("failed");
        }
      }
    };
    // Immediate first tick + 1.5s interval.
    void tick();
    const id = setInterval(tick, 1500);
    return () => {
      cancelled = true;
      clearInterval(id);
      if (pollAbortRef.current) pollAbortRef.current.abort();
    };
  }, [phase, jobId, sessionId]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    setSlugError(null);
    // Submit the deploy. The POST returns 202 + a job_id immediately.
    // We do NOT pass our own AbortSignal here — we want the deploy
    // to keep running server-side even if the user closes the modal
    // (they can come back and see the result via the deployed-apps
    // strip, which re-fetches from /api/sessions/{id}/deployed-apps).
    try {
      const start = await sessionApi.deploy(sessionId, {
        slug: slug || undefined,
        project_dir: projectDir || undefined,
      });
      setJobId(start.job_id);
      setJobStatus({
        job_id: start.job_id, session_id: sessionId, slug: start.slug,
        status: "pending", phase: "queued", steps: [], error: null,
        result: null, created_at: Date.now() / 1000, updated_at: Date.now() / 1000,
      });
      setPhase("running");
    } catch (e: any) {
      const msg = e?.message ?? "deploy failed";
      if (e?.status === 409 || /already taken|already deployed as/i.test(msg)) {
        // 409: slug collision OR sub-app already deployed under a
        // different slug. The server's error already names the
        // existing slug + URL, so just surface it as a field-level
        // message and stay in the config phase.
        setSlugError(msg.replace(/^409:\s*/, ""));
      } else {
        setErr(msg);
      }
    }
  };

  const cancel = async () => {
    if (!jobId) return;
    try {
      await sessionApi.cancelDeployJob(sessionId, jobId);
      // The polling tick will pick up the "cancelled" status on the
      // next iteration and flip to the failed view.
    } catch {
      // Best-effort. If the cancel request fails, the deploy will
      // still complete on its own and the poll will surface it.
    }
  };

  // `noBuild`, `multiple`, `noUnbuilt` are derived up top (right
  // after the detectedDist fetch) so the picker can filter to
  // unbuilt-only before computing these. `noUnbuilt` means "we
  // see builds, but they're all already deployed" — distinct from
  // `noBuild` ("we see no builds at all").
  const readyInConfig = !detecting && !noBuild && !noUnbuilt && !!slug;
  const isRunning = phase === "running";
  const isDone = phase === "done";
  const isFailed = phase === "failed";

  // Backdrop click: hard no-op while running. Same for ESC.
  const onBackdropClick = () => {
    if (!isRunning) onClose();
  };
  useEffect(() => {
    if (!isRunning) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") e.stopPropagation();
    };
    // Capture phase so we intercept ESC before any other handler.
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, [isRunning]);

  // ---- Body content for each phase.
  let body: JSX.Element;
  if (isRunning || isDone || isFailed) {
    body = (
      <>
        <h3 className="font-serif text-xl font-semibold tracking-tight">
          {isDone ? "Deployment complete" : isFailed ? "Deployment failed" : "Deploying…"}
        </h3>
        {isRunning && (
          <p className="text-tx-xs text-muted">
            {jobStatus?.phase || "Starting…"} — keep this window open until it finishes.
          </p>
        )}
        {/* Step checklist. 13 entries in a fixed order (matches the
            server's _DEPLOY_STEPS). We render them by index so the UI
            is stable even if the server emits them out of order (it
            shouldn't, but defensive). The fallback labels are used
            only until the first poll lands; once jobStatus.steps is
            populated we use the server's authoritative label. */}
        <ol className="mt-1 space-y-1.5" data-testid="deploy-steps">
          {Array.from({ length: 13 }).map((_, idx) => {
            // Fallback labels — only used before the first poll.
            // Must stay in lockstep with server/app.py _DEPLOY_STEPS.
            const s = jobStatus?.steps?.[idx];
            const label = s?.label ?? ["", "Validating build", "Reserving public URL",
              "Copying build to /opt/ojas-apps", "Adding PWA defaults", "Copying backend",
              "Creating virtualenv", "Installing Python dependencies",
              "Writing systemd unit", "Enabling systemd service",
              "Recording deployment", "Configuring reverse proxy",
              "Pre-fetching TLS certificate", "Starting service + health check"][idx];
            const status = s?.status ?? (isRunning ? "pending" : "pending");
            const message = s?.message ?? null;
            const isCurrent = status === "running";
            const isDone_ = status === "done";
            const isFailed_ = status === "failed";
            return (
              <li key={idx} className="flex items-start gap-2 text-tx-sm">
                <span
                  className={
                    "mt-0.5 inline-flex size-4 shrink-0 items-center justify-center font-mono " +
                    (isDone_ ? "text-success" :
                     isFailed_ ? "text-danger" :
                     isCurrent ? "text-accent" :
                     "text-subtle")
                  }
                  aria-hidden
                >
                  {isDone_ ? "✓" :
                   isFailed_ ? "✗" :
                   isCurrent ? <span className="inline-block size-3 animate-spin rounded-full border-2 border-accent border-t-transparent" /> :
                   "○"}
                </span>
                <div className="flex-1">
                  <div className={
                    isCurrent ? "font-medium text-text" :
                    isDone_ ? "text-muted line-through opacity-70" :
                    isFailed_ ? "text-danger" :
                    "text-subtle"
                  }>
                    {label}
                  </div>
                  {message && (
                    <div className={
                      "mt-0.5 text-tx-xs " + (isFailed_ ? "text-danger" : "text-muted")
                    }>
                      {message}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
        {isDone && finalResult && (
          <div className="rounded-lg border border-success/30 bg-success/10 px-3 py-2 text-tx-sm">
            <div className="text-success font-medium">Live now</div>
            <a
              href={finalResult.url}
              target="_blank" rel="noreferrer"
              className="mt-1 block break-all font-mono text-tx-xs text-accent hover:underline"
            >
              {finalResult.url}
            </a>
          </div>
        )}
        {isFailed && err && (
          <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-tx-xs text-danger">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          {isRunning && (
            <button type="button" onClick={cancel} className="btn-ghost" data-testid="deploy-cancel">
              Stop deploy
            </button>
          )}
          {isDone && (
            <>
              <a href={finalResult?.url ?? "#"} target="_blank" rel="noreferrer"
                 className="btn-ghost">Open in new tab</a>
              <button type="button" onClick={() => {
                if (finalResult) onDeployed(finalResult);
                onClose();
              }} className="btn-primary">Done</button>
            </>
          )}
          {isFailed && (
            <button type="button" onClick={onClose} className="btn-primary">Dismiss</button>
          )}
        </div>
      </>
    );
  } else {
    // Config phase — the original UI.
    body = (
      <>
        <h3 className="font-serif text-xl font-semibold tracking-tight">Deploy this build</h3>
        <p className="text-tx-xs text-muted">
          The build's <code className="font-mono">dist/</code> will be copied
          to a permanent location and served at the URL below.
        </p>
        <label className="block">
          <span className="text-tx-xs font-medium text-muted">Slug</span>
          <input
            type="text" value={slug}
            // Don't auto-slugify on every keystroke -- the user might
            // be typing "My Cool App" and we shouldn't mangle the
            // input mid-type (cursor jumps, "hyphens disappear"
            // because spaces are silently converted to dashes).
            // We only normalize: lowercase + drop leading/trailing
            // whitespace, so the user sees exactly what they typed
            // + the URL preview updates live with their input.
            // Full slug sanitization (replacing non-allowed chars,
            // collapsing multiple hyphens) happens on submit via
            // the server's _slugify, AND we show a hint below the
            // input explaining what the final slug will look like.
            onChange={(e) => {
              const v = e.target.value.toLowerCase();
              setSlug(v);
              setSlugError(null);
            }}
            onBlur={(e) => {
              // On blur, normalize further: collapse whitespace +
              // run the full slugify so the user sees the final form
              // before they hit Deploy.
              setSlug(slugifyName(e.target.value));
            }}
            aria-invalid={!!slugError}
            className={`field mt-1 font-mono ${slugError ? "border-danger/60" : ""}`}
            placeholder="weather-app" required autoFocus
          />
          <span className="mt-1 block text-tx-xs text-subtle">
            URL: <span className="font-mono">https://{slugifyName(slug) || "<slug>"}.{hostApps()}/</span>
            <span className="ml-2 text-muted">
              · only <span className="font-mono">a-z 0-9 - _</span> allowed, auto-normalized on submit
            </span>
          </span>
          {slugError && (
            <span className="mt-1 block text-tx-xs text-danger" role="alert">
              {slugError}
            </span>
          )}
        </label>
        <div className="block">
          <span className="text-tx-xs font-medium text-muted">
            Project{" "}
            <span className="text-subtle">
              ({multiple ? "pick which app to publish" : "auto-detected from the agent's build"})
            </span>
          </span>
          {detecting ? (
            <div className="field mt-1 flex items-center gap-2 text-tx-xs text-muted">
              <span className="inline-block size-3 animate-spin rounded-full border-2 border-muted border-t-transparent" />
              Detecting build…
            </div>
          ) : noUnbuilt ? (
            <div className="field mt-1 flex items-center gap-2 font-mono text-tx-sm text-muted">
              <span aria-hidden>🔒</span>
              <span>All built sub-apps are already deployed — nothing new to publish.</span>
            </div>
          ) : multiple ? (
            <>
              <select
                value={projectDir}
                onChange={(e) => setProjectDir(e.target.value)}
                className="field mt-1 font-mono"
                data-testid="detected-subdir"
              >
                {unbuiltCandidates.map((c) => (
                  <option key={c.project_dir} value={c.project_dir}>
                    {c.project_dir === "" ? "<session root>" : c.project_dir}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-tx-xs text-muted">
                Multiple unbuilt sub-apps — pick the one to publish.
              </p>
            </>
          ) : (
            <div className="field mt-1 flex items-center gap-2 font-mono text-tx-sm">
              <span aria-hidden className="text-muted">🔒</span>
              <span data-testid="detected-subdir" className="flex-1">
                {projectDir === "" ? "<session root>" : (projectDir || "—")}
              </span>
              {projectDir === "" && (
                <span className="rounded bg-surface-2 px-1.5 py-0.5 text-tx-xs text-muted">root</span>
              )}
            </div>
          )}
        </div>
        {noBuild && (
          <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-tx-xs text-danger">
            No built <code className="font-mono">dist/</code> found in this session. Ask the agent to run{" "}
            <code className="font-mono">npm run build</code> and try again.
          </div>
        )}
        {noUnbuilt && (
          <div className="rounded border border-warn/30 bg-warn/10 px-3 py-2 text-tx-xs text-warn">
            All built sub-apps are already deployed. To update an existing app,
            use its <span className="font-mono">🔄 Update</span> pill in the nav strip.
          </div>
        )}
        {err && !noBuild && (
          <div className="rounded border border-danger/30 bg-danger/10 px-3 py-2 text-tx-xs text-danger">
            {err}
          </div>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="btn-ghost">Cancel</button>
          <button type="submit" disabled={!readyInConfig} className="btn-primary">
            Deploy
          </button>
        </div>
      </>
    );
  }

  return (
    <div
      role="dialog" aria-modal
      onClick={onBackdropClick}
      className="fixed inset-0 z-40 flex items-end justify-center bg-black/45 backdrop-blur-sm sm:items-center"
    >
      <form
        onSubmit={isRunning ? (e) => e.preventDefault() : submit}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md space-y-3 rounded-t-2xl border border-border bg-surface p-5 shadow-lift sm:rounded-2xl"
      >
        {body}
      </form>
    </div>
  );
}

function slugifyName(input: string): string {
  return input
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    // DNS label max is 63 chars per RFC 1035. Caddy's wildcard
    // block rewrites `<slug>.<root>` and the root itself is
    // already ~20 chars, so 40 leaves ~3 chars of headroom on
    // the longest root domains. 60 is the safe ceiling; we
    // round to 60 to leave 3 chars for the root and the
    // trailing dot/slash.
    .slice(0, 60);
}

function UploadIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M17 8l-5-5-5 5M12 3v12" />
    </svg>
  );
}


function truncForDebug(s: any, n: number): string {
  const str = String(s ?? "");
  return str.length <= n ? str : str.slice(0, n - 1) + "…";
}

// Inline sun/moon glyphs — shared shape with Layout's header icons but kept
// local so the chat page doesn't add an import cycle.
function SunGlyph() {
  return (
    <svg
      width="16" height="16" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden
    >
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonGlyph() {
  return (
    <svg
      width="16" height="16" viewBox="0 0 24 24"
      fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden
    >
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}
