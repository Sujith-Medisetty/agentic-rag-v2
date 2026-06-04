// TurnCard — one user turn rendered as a chronological timeline.
//
// Claude-Code-style flow: text / tool / thinking / sub-agent / file / commit
// blocks interleaved IN THE ORDER they actually happened. No more "all tools
// in one section, all text in another" — the reader follows the agent's
// narrative top-to-bottom.
//
//   ─── TURN 3 · 09:42:15 ──────────────────────────────────────────
//
//   ┌─ User prompt ────────────────────────────────────────────┐
//   │ Find every Python file that imports langchain.            │
//   └───────────────────────────────────────────────────────────┘
//
//   ⏺ TodoWrite                                  1.2s ✓
//
//   ▸ Thinking · 142 chars                                    (collapsed)
//
//   ⏺ WebFetch  pypi.org/pypi/langchain/json     2.4s ✓
//
//   ┌─ [SUB-AGENT][Explore] find langchain imports ─ 18s ⏳ ──┐
//   │ model: MiniMax-M2 · tools: 3 · tokens: 1.2k / 480       │
//   │ ⏺ glob_search …                                          │
//   │ ⏺ grep_search …                                          │
//   └──────────────────────────────────────────────────────────┘
//
//   ⏺ AgentStatus                                21ms ✓
//
//   Here's the version comparison and recommendation…           (response prose)
//
//   ───── 23.4s · 5 tools · 1 agent · 3.4k in / 1.2k out ─────

import { useEffect, useState } from "react";
import type {
  Turn, ToolEvent, FileChange, AgentRecord, CommitRecord, TimelineBlock,
} from "@/lib/types";
import TurnFooter from "@/components/TurnFooter";
import Markdown from "@/components/Markdown";
import { formatDuration } from "@/lib/format";

// ============================================================================
// TurnCard
// ============================================================================

export default function TurnCard({ turn, index }: { turn: Turn; index: number }) {
  // Build a lookup of orchestrator-level tools so the timeline's `tool` blocks
  // can resolve their current ToolEvent state (status, duration, preview)
  // without each block having to carry a copy.
  const toolById = new Map(turn.tools.map((t) => [t.id, t]));

  return (
    <article className="animate-fade-in-up py-6">
      {/* Turn divider ----------------------------------------------------- */}
      <TurnDivider index={index} startedAt={turn.startedAt} />

      {/* USER PROMPT ------------------------------------------------------ */}
      <div className="mb-4 rounded-lg border border-accent/25 bg-accent/[0.06] px-4 py-3 font-sans text-[15px] font-medium leading-relaxed text-text">
        {turn.userPrompt}
      </div>

      {/* CHRONOLOGICAL TIMELINE — text / tools / thinking / sub-agents /
          files / commits in the actual order they happened. */}
      <div className="space-y-3">
        {turn.blocks.map((block) => (
          <TimelineBlockRow
            key={block.id}
            block={block}
            toolById={toolById}
            agents={turn.agents}
            isStreaming={turn.isStreaming}
          />
        ))}

        {/* Empty waiting state — shown when the turn is streaming but no
            blocks have arrived yet (first 1-2 seconds, before any event). */}
        {turn.isStreaming && turn.blocks.length === 0 && !turn.error && (
          <div className="flex items-center gap-2 font-sans text-tx-sm text-muted">
            <span className="stream-dot" />
            <span>Working through this…</span>
          </div>
        )}
      </div>

      {/* ERROR ------------------------------------------------------------ */}
      {turn.error && (
        <div className="mt-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 font-sans text-sm text-danger">
          <span className="font-semibold">✗ Error:</span> {turn.error}
        </div>
      )}

      {/* FOOTER ----------------------------------------------------------- */}
      {turn.summary && <TurnFooter summary={turn.summary} />}
      {!turn.summary && turn.isStreaming && !turn.error && (
        <LiveProgress turn={turn} />
      )}
    </article>
  );
}

// ============================================================================
// TimelineBlockRow — dispatch on block.kind to render the right component.
// ============================================================================

function TimelineBlockRow({
  block, toolById, agents, isStreaming,
}: {
  block: TimelineBlock;
  toolById: Map<string, ToolEvent>;
  agents: Record<string, AgentRecord>;
  isStreaming: boolean;
}) {
  switch (block.kind) {
    case "text":
      return <TextBlockView text={block.text} isStreaming={isStreaming} />;
    case "thinking":
      return <ThinkingBlockView text={block.text} />;
    case "tool": {
      const t = toolById.get(block.toolId);
      return t ? <div className="font-mono text-tx-sm"><ToolNode tool={t} /></div> : null;
    }
    case "agent": {
      const a = agents[block.agentId];
      return a ? <AgentNode agent={a} /> : null;
    }
    case "file":
      return <div className="font-mono text-tx-sm"><FileNode file={block.file} /></div>;
    case "commit":
      return <div className="font-mono text-tx-sm"><CommitNode commit={block.commit} /></div>;
  }
}

// ============================================================================
// TextBlockView — response prose, rendered as markdown so headings, bold,
// tables, code blocks, etc. format correctly instead of showing raw syntax.
// ============================================================================

function TextBlockView({ text, isStreaming }: { text: string; isStreaming: boolean }) {
  return (
    <div className="relative">
      <Markdown text={text} />
      {isStreaming && <span className="stream-dot ml-1.5 align-middle" />}
    </div>
  );
}

// ============================================================================
// ThinkingBlockView — collapsible inline. Short label by default; click to
// expand the full chain-of-thought. Italic dim text in a violet-rail card
// so it's unmistakably "internal reasoning, not the answer".
// ============================================================================

function ThinkingBlockView({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-baseline gap-1.5 font-sans text-tx-xs text-accent-2 hover:text-accent"
      >
        <span className="font-semibold uppercase tracking-[0.14em]">Thinking</span>
        <span className="text-subtle">· {text.length} chars</span>
        <span className="text-subtle">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="mt-1 rounded-lg border-l-2 border-accent-2/40 bg-elevated/30 px-4 py-3 whitespace-pre-wrap font-sans text-[13px] italic leading-relaxed text-muted">
          {text}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// LiveProgress — ticking stats card shown while a turn is still streaming.
// Mirrors TurnFooter's layout so the final swap (live → final) is seamless.
// ============================================================================

function LiveProgress({ turn }: { turn: Turn }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, []);

  const totalTok = turn.liveInputTokens + turn.liveOutputTokens;
  const runningTools = turn.tools.filter((t) => t.status === "running").length;
  const runningAgents = Object.values(turn.agents)
    .filter((a) => a.status === "running").length;

  return (
    <div className="mt-5 rounded-lg border border-accent/30 bg-accent/5 px-3 py-2 font-sans backdrop-blur-sm">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-tx-xs">
        <div className="inline-flex items-center gap-1.5">
          <span className="stream-dot" />
          <span className="text-[10px] font-bold uppercase tracking-[0.18em] text-accent">
            Live
          </span>
        </div>
        <Divider />
        <LiveStat label="Elapsed" value={formatDuration(now - turn.startedAt)} />
        <Divider />
        <LiveStat
          label="Tools"
          value={`${turn.tools.length}${runningTools ? ` (${runningTools} running)` : ""}`}
          valueClass={runningTools ? "text-warn" : "text-text"}
        />
        {Object.keys(turn.agents).length > 0 && (
          <>
            <Divider />
            <LiveStat
              label="Agents"
              value={`${Object.keys(turn.agents).length}${runningAgents ? ` (${runningAgents} running)` : ""}`}
              valueClass={runningAgents ? "text-warn" : "text-text"}
            />
          </>
        )}
        {totalTok > 0 && (
          <>
            <Divider />
            <LiveStat
              label="In"
              value={formatTokensCompact(turn.liveInputTokens)}
              valueClass="text-accent"
            />
            <LiveStat
              label="Out"
              value={formatTokensCompact(turn.liveOutputTokens)}
              valueClass="text-accent-2"
            />
          </>
        )}
      </div>
    </div>
  );
}

function LiveStat({
  label, value, valueClass = "text-text",
}: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="inline-flex items-baseline gap-1.5">
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-subtle">
        {label}
      </span>
      <span className={`font-mono text-tx-sm ${valueClass}`}>{value}</span>
    </div>
  );
}

function Divider() {
  return <span className="text-subtle">·</span>;
}

function formatTokensCompact(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return (n / 1000).toFixed(n < 10_000 ? 1 : 0) + "k";
  return (n / 1_000_000).toFixed(1) + "M";
}

// ============================================================================
// Turn divider — sans eyebrow "TURN 3 · 09:42:15" with a thin connecting line
// ============================================================================

function TurnDivider({ index, startedAt }: { index: number; startedAt: number }) {
  return (
    <div className="mb-5 flex items-center gap-3">
      <span className="font-sans text-[10px] font-bold uppercase tracking-[0.22em] text-accent">
        Turn {index + 1}
      </span>
      <span className="font-sans text-tx-xs text-subtle">
        {new Date(startedAt).toLocaleTimeString()}
      </span>
      <span className="h-px flex-1 bg-gradient-to-r from-border/60 to-transparent" />
    </div>
  );
}

// ---- Tool ----------------------------------------------------------------

function ToolNode({ tool }: { tool: ToolEvent }) {
  // Preview can be up to ~500 chars from the backend; show only the first
  // line truncated to 110 chars by default with a "show more" toggle so the
  // tree stays compact but the full output is one click away. Double-click
  // anywhere on the result row also toggles, for fast skimming.
  const [expanded, setExpanded] = useState(false);
  const preview = tool.preview ?? (tool.status === "error" ? "failed" : "");
  const firstLine = preview.split("\n")[0];
  const hasMore = preview.length > 110 || preview.includes("\n");

  return (
    <div className="rounded-md px-1 py-0.5 hover:bg-elevated/40">
      <div className="flex items-baseline gap-2 whitespace-pre">
        <span className="text-subtle">⏺</span>
        <span className="font-semibold text-text">{tool.tool}</span>
        {tool.target && (
          <span className="truncate text-muted" title={tool.target}>
            {truncate(tool.target, 70)}
          </span>
        )}
        <span className="ml-auto flex shrink-0 items-baseline gap-2 font-sans">
          <LiveDuration
            startedAt={tool.startedAt}
            endedAt={tool.endedAt}
            running={tool.status === "running"}
          />
          <StatusIcon status={tool.status} />
        </span>
      </div>
      {preview && (
        <>
          <div
            className={`flex items-baseline gap-1.5 pl-5 text-tx-xs ${hasMore ? "cursor-pointer select-none" : ""}`}
            onDoubleClick={hasMore ? () => setExpanded((v) => !v) : undefined}
            title={hasMore ? "Double-click to expand / collapse" : undefined}
          >
            <span className="text-subtle">⎿</span>
            <span className={tool.status === "error" ? "text-danger" : "text-muted"}>
              {expanded ? <em className="not-italic text-subtle">(full output below)</em> : truncate(firstLine, 110)}
            </span>
            {hasMore && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
                className="ml-1 font-sans text-tx-xs text-accent hover:text-text"
              >
                {expanded ? "show less" : "show more"}
              </button>
            )}
          </div>
          {expanded && (
            <pre className="mx-5 mt-1 max-h-72 overflow-auto rounded border border-border bg-bg p-2 whitespace-pre-wrap break-words font-mono text-tx-xs leading-snug text-text">
              {preview}
            </pre>
          )}
        </>
      )}
    </div>
  );
}

// LiveDuration — ticks every 250ms while running, freezes when ended.
// Used on tools AND agents so any in-flight item shows real-time elapsed.
function LiveDuration({
  startedAt, endedAt, running,
}: { startedAt: number; endedAt?: number; running: boolean }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [running]);
  const ms = running ? now - startedAt : (endedAt ?? now) - startedAt;
  return (
    <span className={`text-tx-xs ${running ? "text-warn" : "text-subtle"}`}>
      {formatDuration(ms)}
    </span>
  );
}

function StatusIcon({ status }: { status: "running" | "done" | "error" }) {
  if (status === "running") {
    return <span className="text-warn animate-pulse-soft text-tx-xs">⏳</span>;
  }
  if (status === "error") {
    return <span className="text-danger text-tx-xs">✗</span>;
  }
  return <span className="text-success text-tx-xs">✓</span>;
}

// ---- File change ---------------------------------------------------------

function FileNode({ file }: { file: FileChange }) {
  const [open, setOpen] = useState(false);
  const { add, rem } = countDiffLines(file.diff);
  const isCreate = file.kind === "create";
  const op = isCreate ? "write_file" : "edit_file";

  return (
    <div className="rounded-md px-1 py-0.5">
      <div className="flex items-baseline gap-2 whitespace-pre">
        <span className="text-subtle">⏺</span>
        <span className="font-semibold text-text">{op}</span>
        <span className="truncate text-muted" title={file.path}>
          {truncate(file.path, 70)}
        </span>
        <span className="ml-auto flex shrink-0 items-baseline gap-2 font-sans">
          {isCreate ? (
            <span className="text-tx-xs text-success">
              +{(file.bytes / 1024).toFixed(1)}KB
            </span>
          ) : (
            <span className="text-tx-xs">
              <span className="text-success">+{add}</span>
              <span className="text-subtle"> / </span>
              <span className="text-danger">-{rem}</span>
            </span>
          )}
        </span>
      </div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline gap-1.5 pl-5 text-left text-tx-xs hover:text-text"
      >
        <span className="text-subtle">⎿</span>
        <span className="text-accent">{open ? "hide diff" : "show diff"}</span>
      </button>
      {open && (
        <pre className="mt-1 ml-6 max-h-96 overflow-auto rounded border border-border bg-bg p-2 text-tx-xs leading-tight">
          {(isCreate
            ? file.diff.split("\n").slice(0, 200).map((l) => "+" + l)
            : file.diff.split("\n")
          ).map((line, i) => {
            let cls = "text-muted";
            if (line.startsWith("+") && !line.startsWith("+++")) cls = "text-success";
            else if (line.startsWith("-") && !line.startsWith("---")) cls = "text-danger";
            else if (line.startsWith("@@")) cls = "text-accent";
            else if (line.startsWith("+++") || line.startsWith("---")) cls = "text-text";
            return <div key={i} className={cls}>{line || " "}</div>;
          })}
        </pre>
      )}
    </div>
  );
}

function countDiffLines(diff: string): { add: number; rem: number } {
  let add = 0, rem = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    if (line.startsWith("+")) add++;
    else if (line.startsWith("-")) rem++;
  }
  return { add, rem };
}

// ---- Sub-agent -----------------------------------------------------------
//
// Sub-agents run in a separate worker — we don't have their per-tool events
// yet, so we show the agent header (type/description/model) clearly and
// surface its output file when it lands. Status drives the right-side icon.

function AgentNode({ agent }: { agent: AgentRecord }) {
  const isDone = agent.status === "completed";
  const isFail = agent.status === "failed";
  const isRunning = agent.status === "running";

  const resultText = isDone
    ? "done" + (agent.output_file
        ? ` · output ${truncate(agent.output_file.split("/").pop() ?? "", 32)}`
        : "")
    : isFail
      ? `failed · ${truncate(agent.error || "unknown", 80)}`
      : "running…";

  // Nested state for THIS sub-agent. Tools/tokens here are events that the
  // backend stamped with this agent_id — they belong inside this block, not
  // in the parent turn's activity list.
  const subToolCount = agent.tools.length;
  const subRunning = agent.tools.filter((t) => t.status === "running").length;
  const totalTok = agent.liveInputTokens + agent.liveOutputTokens;

  return (
    <div className="rounded-lg border border-accent-2/30 bg-accent-2/[0.04]">
      {/* Sub-agent header bar — clearly distinct from a regular tool row */}
      <div className="flex items-center gap-2 border-b border-accent-2/20 px-3 py-2">
        <span className="rounded bg-accent-2/20 px-2 py-px font-sans text-[10px] font-bold uppercase tracking-[0.12em] text-accent-2">
          Sub-agent
        </span>
        <span className="rounded bg-accent-2/15 px-1.5 py-px font-sans text-[10px] font-medium text-accent-2">
          {agent.subagent_type}
        </span>
        <span className="truncate font-sans text-tx-sm font-medium text-text" title={agent.description}>
          {truncate(agent.description, 70)}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-2 font-sans">
          <LiveDuration
            startedAt={agent.spawned_at}
            endedAt={isRunning ? undefined : agent.updated_at}
            running={isRunning}
          />
          <StatusIcon status={
            isRunning ? "running" : isDone ? "done" : "error"
          } />
        </span>
      </div>

      {/* Sub-agent metadata + token chip */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 font-sans text-tx-xs">
        {agent.model && (
          <span className="text-muted">
            <span className="text-subtle">model:</span> {agent.model}
          </span>
        )}
        <span className="text-muted">
          <span className="text-subtle">id:</span>{" "}
          <span className="font-mono">{agent.agent_id.slice(-10)}</span>
        </span>
        <span className="text-muted">
          <span className="text-subtle">tools:</span>{" "}
          <span className="text-text">{subToolCount}</span>
          {subRunning > 0 && (
            <span className="text-warn"> ({subRunning} running)</span>
          )}
        </span>
        {totalTok > 0 && (
          <span className="text-muted">
            <span className="text-subtle">tokens:</span>{" "}
            <span className="text-accent">{formatTokensCompact(agent.liveInputTokens)}</span>
            <span className="text-subtle"> in / </span>
            <span className="text-accent-2">{formatTokensCompact(agent.liveOutputTokens)}</span>
            <span className="text-subtle"> out</span>
          </span>
        )}
      </div>

      {/* Nested tool tree — what THIS sub-agent ran. Same ToolNode component
          as the main tree, just inside the sub-agent card. */}
      {agent.tools.length > 0 && (
        <div className="space-y-1 border-t border-accent-2/15 px-3 py-2 font-mono text-tx-sm">
          {agent.tools.map((t) => (
            <ToolNode key={t.id} tool={t} />
          ))}
        </div>
      )}

      {/* Result row — done/failed/running */}
      <div className="flex items-baseline gap-1.5 border-t border-accent-2/15 px-3 py-1.5 font-mono text-tx-xs">
        <span className="text-subtle">⎿</span>
        <span className={isFail ? "text-danger" : isDone ? "text-success" : "text-muted"}>
          {resultText}
        </span>
      </div>
    </div>
  );
}


// ---- Commit --------------------------------------------------------------

function CommitNode({ commit }: { commit: CommitRecord }) {
  const skipped = !commit.sha;
  return (
    <div className="rounded-md px-1 py-0.5">
      <div className="flex items-baseline gap-2 whitespace-pre">
        <span className="text-subtle">⏺</span>
        <span className="font-semibold text-text">commit</span>
        {!skipped && (
          <span className="font-mono text-tx-xs text-muted">{commit.sha.slice(0, 7)}</span>
        )}
        {!skipped && commit.branch && (
          <span className="font-sans text-tx-xs text-subtle">on {commit.branch}</span>
        )}
        <span className="ml-auto shrink-0">
          {skipped
            ? <span className="text-warn text-tx-xs">∅</span>
            : <span className="text-success text-tx-xs">✓</span>}
        </span>
      </div>
      <div className="flex items-baseline gap-1.5 pl-5 text-tx-xs">
        <span className="text-subtle">⎿</span>
        <span className={skipped ? "text-warn" : "text-muted"}>
          {truncate(commit.message, 100)}
        </span>
      </div>
      {commit.files.length > 0 && (
        <div className="flex items-baseline gap-1.5 pl-5 text-tx-xs text-subtle">
          <span>—</span>
          <span className="truncate">
            {commit.files.slice(0, 5).join(", ")}
            {commit.files.length > 5 && ` +${commit.files.length - 5} more`}
          </span>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Helpers
// ============================================================================

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

