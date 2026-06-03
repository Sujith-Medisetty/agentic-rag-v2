// TurnCard — Claude-Code-style transcript entry for one user turn.
//
// Visual structure (no card border — separation comes from a hair-thin
// border-top on every turn after the first):
//
//   > <user prompt>                                           #N · 09:42:15
//
//   <assistant text>
//
//   ⏺ read_file(auth.py)
//   ⎿  ✓ 80 lines
//
//   ⏺ grep_search("def authenticate")
//   ⎿  ✓ found in 3 files
//
//   ⏺ Agent[Explore] · find auth callers
//   ⎿  ✓ done · output saved
//
//   ⏺ edit_file(auth.py)
//   ⎿  ✎ +12 -3  ▸ click to expand
//      [expanded diff lines]
//
//   ⏺ commit
//   ⎿  ✓ abc1234 on session/xyz · "refactor auth module"
//
//                  ── 5 tools · 23.4s · 3.4k tok · $0.07 ──
//
// All mono. Tree connectors are real box-drawing chars (⏺ U+23FA, ⎿ U+23BF).
// Activity is rendered in chronological order — tools / files / agents /
// commits merged by timestamp so the tree reads top-to-bottom as it happened.

import { useEffect, useState } from "react";
import type {
  Turn, ToolEvent, FileChange, AgentRecord, CommitRecord,
} from "@/lib/types";
import TurnFooter from "@/components/TurnFooter";
import { formatDuration } from "@/lib/format";

// ============================================================================
// Activity item — a discriminated union covering everything that can land in
// the tree. Sorted by `ts` ascending when rendered.
// ============================================================================

type ActivityItem =
  | { kind: "tool";   ts: number; data: ToolEvent }
  | { kind: "file";   ts: number; data: FileChange }
  | { kind: "agent";  ts: number; data: AgentRecord }
  | { kind: "commit"; ts: number; data: CommitRecord };

function buildActivity(turn: Turn): ActivityItem[] {
  const items: ActivityItem[] = [
    ...turn.tools       .map((d) => ({ kind: "tool"   as const, ts: d.startedAt, data: d })),
    ...turn.fileChanges .map((d) => ({ kind: "file"   as const, ts: d.ts,        data: d })),
    ...Object.values(turn.agents)
                       .map((d) => ({ kind: "agent"  as const, ts: d.spawned_at, data: d })),
    ...turn.commits     .map((d) => ({ kind: "commit" as const, ts: d.ts,        data: d })),
  ];
  return items.sort((a, b) => a.ts - b.ts);
}

// ============================================================================
// TurnCard
// ============================================================================

export default function TurnCard({ turn, index }: { turn: Turn; index: number }) {
  const activity = buildActivity(turn);
  return (
    <article
      className={
        "animate-fade-in-up pb-4 pt-4 " +
        (index > 0 ? "border-t border-border/40" : "")
      }
    >
      {/* User-prompt header — ">" prefix, prompt text, right-aligned meta */}
      <header className="mb-3 flex items-baseline gap-2 border-l-2 border-accent/50 pl-2.5">
        <span className="text-accent/80">&gt;</span>
        <span className="flex-1 whitespace-pre-wrap font-medium text-text">{turn.userPrompt}</span>
        <span className="shrink-0 font-sans text-tx-xs text-subtle">
          #{index + 1} · {new Date(turn.startedAt).toLocaleTimeString()}
        </span>
      </header>

      {/* Assistant text */}
      {turn.assistantText && (
        <div className="mb-2 whitespace-pre-wrap text-text">
          {turn.assistantText}
          {turn.isStreaming && !turn.error && (
            <span className="stream-dot ml-1.5" />
          )}
        </div>
      )}
      {!turn.assistantText && turn.isStreaming && !turn.error && (
        <div className="mb-2 flex items-center gap-2 text-muted">
          <span className="stream-dot" />
          <span>Thinking…</span>
        </div>
      )}

      {/* Error banner (if reporter.error landed during this turn). Replaces
          any "Thinking…" / streaming dot — the turn is OVER. */}
      {turn.error && (
        <div className="my-2 rounded border border-danger/40 bg-danger/10 px-2 py-1 text-tx-sm text-danger">
          ✗ {turn.error}
        </div>
      )}

      {/* Activity tree */}
      {activity.length > 0 && (
        <div className="mt-3 space-y-1.5">
          {activity.map((item) => (
            <ActivityNode key={nodeKey(item)} item={item} />
          ))}
        </div>
      )}

      {/* Footer — only after summary lands */}
      {turn.summary && <TurnFooter summary={turn.summary} />}

      {/* Live elapsed for in-flight turns only — never on errored turns.
          The error itself is the terminal state; no point counting. */}
      {!turn.summary && turn.isStreaming && !turn.error && (
        <div className="mt-3 text-center text-tx-xs text-subtle">
          ── <ElapsedLive startedAt={turn.startedAt} /> ──
        </div>
      )}
    </article>
  );
}

function nodeKey(item: ActivityItem): string {
  switch (item.kind) {
    case "tool":   return `tool-${item.data.id}`;
    case "file":   return `file-${item.data.id}`;
    case "agent":  return `agent-${item.data.agent_id}`;
    case "commit": return `commit-${item.data.id}`;
  }
}

// ============================================================================
// ActivityNode — one row in the tree.
//   ⏺ <head>
//   ⎿  <result>
//      [optional expanded body indented under the result]
// ============================================================================

function ActivityNode({ item }: { item: ActivityItem }) {
  switch (item.kind) {
    case "tool":   return <ToolNode   tool={item.data} />;
    case "file":   return <FileNode   file={item.data} />;
    case "agent":  return <AgentNode  agent={item.data} />;
    case "commit": return <CommitNode commit={item.data} />;
  }
}

// ---- Tool ---------------------------------------------------------------

function ToolNode({ tool }: { tool: ToolEvent }) {
  const head = tool.target
    ? `${tool.tool}(${truncate(tool.target, 60)})`
    : tool.tool;
  return (
    <div>
      <div className="tree-line">
        <span className="text-subtle">⏺</span>
        <span className="text-text">{head}</span>
      </div>
      <div className="tree-line pl-2">
        <span className={resultColor(tool.status)}>⎿</span>
        <span className={resultIconColor(tool.status)}>{resultIcon(tool.status)}</span>
        <span className="text-muted">
          {tool.preview ? truncate(tool.preview, 100) : statusLabel(tool.status)}
        </span>
      </div>
    </div>
  );
}

// ---- File change ---------------------------------------------------------

function FileNode({ file }: { file: FileChange }) {
  const [open, setOpen] = useState(false);
  const { add, rem } = countDiffLines(file.diff);
  const isCreate = file.kind === "create";
  const headOp = isCreate ? "write_file" : "edit_file";
  const head = `${headOp}(${truncate(file.path, 60)})`;
  const summary = isCreate
    ? `+ created · ${(file.bytes / 1024).toFixed(1)}KB`
    : `✎ +${add} -${rem}`;

  return (
    <div>
      <div className="tree-line">
        <span className="text-subtle">⏺</span>
        <span className="text-text">{head}</span>
      </div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="tree-line pl-2 text-left hover:text-text"
      >
        <span className="text-success">⎿</span>
        <span className={isCreate ? "text-success" : "text-accent"}>{summary}</span>
        <span className="text-subtle">· {open ? "hide" : "show"} diff</span>
      </button>
      {open && (
        <pre className="mt-1 ml-6 max-h-96 overflow-auto rounded border border-border bg-bg p-2 text-tx-xs leading-tight">
          {(isCreate ? file.diff.split("\n").slice(0, 200).map((l) => "+" + l) : file.diff.split("\n"))
            .map((line, i) => {
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

// ---- Sub-agent (one tree node; we don't have inner sub-agent activity yet) ---

function AgentNode({ agent }: { agent: AgentRecord }) {
  const head = `Agent[${agent.subagent_type}] · ${truncate(agent.description, 60)}`;
  const isDone = agent.status === "completed";
  const isFail = agent.status === "failed";
  const resultText = isDone
    ? "done"   + (agent.output_file ? ` · output ${truncate(agent.output_file.split("/").pop() ?? "", 32)}` : "")
    : isFail
      ? `failed · ${truncate(agent.error || "unknown", 80)}`
      : "running…";
  return (
    <div>
      <div className="tree-line">
        <span className="text-subtle">⏺</span>
        <span className="text-text">{head}</span>
        <span className="ml-2 text-subtle">· {agent.agent_id.slice(-8)}</span>
      </div>
      <div className="tree-line pl-2">
        <span className={resultColor(agent.status === "running" ? "running" : isDone ? "done" : "error")}>⎿</span>
        <span className={resultIconColor(agent.status === "running" ? "running" : isDone ? "done" : "error")}>
          {resultIcon(agent.status === "running" ? "running" : isDone ? "done" : "error")}
        </span>
        <span className="text-muted">{resultText}</span>
      </div>
    </div>
  );
}

// ---- Commit --------------------------------------------------------------

function CommitNode({ commit }: { commit: CommitRecord }) {
  const skipped = !commit.sha;
  const head = skipped ? "commit" : `commit · ${commit.sha.slice(0, 7)}`;
  return (
    <div>
      <div className="tree-line">
        <span className="text-subtle">⏺</span>
        <span className="text-text">{head}</span>
        {!skipped && commit.branch && (
          <span className="text-subtle">on {commit.branch}</span>
        )}
      </div>
      <div className="tree-line pl-2">
        <span className={skipped ? "text-warn" : "text-success"}>⎿</span>
        <span className={skipped ? "text-warn" : "text-success"}>
          {skipped ? "∅" : "✓"}
        </span>
        <span className="text-muted">{truncate(commit.message, 100)}</span>
      </div>
      {commit.files.length > 0 && (
        <div className="tree-line pl-6 text-subtle">
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
// Tiny helpers
// ============================================================================

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function resultIcon(status: "running" | "done" | "error"): string {
  if (status === "running") return "⏳";
  if (status === "error")   return "✗";
  return "✓";
}

function resultIconColor(status: "running" | "done" | "error"): string {
  if (status === "running") return "text-warn animate-pulse-soft";
  if (status === "error")   return "text-danger";
  return "text-success";
}

function resultColor(status: "running" | "done" | "error"): string {
  if (status === "error") return "text-danger";
  return "text-subtle";
}

function statusLabel(status: "running" | "done" | "error"): string {
  if (status === "running") return "running…";
  if (status === "error")   return "failed";
  return "done";
}

// Live elapsed counter — only mounts on streaming turns so we don't spawn a
// timer for every card on the page.
function ElapsedLive({ startedAt }: { startedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, []);
  return <span>{formatDuration(now - startedAt)} elapsed</span>;
}
