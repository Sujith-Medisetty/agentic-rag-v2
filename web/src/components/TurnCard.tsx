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

import { memo, useEffect, useState } from "react";

import type {
  Turn, ToolEvent, FileChange, AgentRecord, CommitRecord, TimelineBlock, LlmCall,
  ContextCompactedNote,
} from "@/lib/types";
import TurnFooter from "@/components/TurnFooter";
import Markdown from "@/components/Markdown";
import { formatDuration, formatTokens } from "@/lib/format";
import { useAppSettings } from "@/lib/appSettings";

// ============================================================================
// TurnCard
// ============================================================================

// TurnCard is the per-turn renderer used in two places:
//   - the historical list (turns.map) — wrapped in React.memo so non-active
//     turns skip re-render when only the active turn changed
//   - the live tail (currentTurn) — ActiveTurnCard (non-memo) so the active
//     turn always re-renders as events stream in
function TurnCardImpl({ turn, index }: { turn: Turn; index: number }) {
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

        {/* Auto-compact breadcrumb cards. Attached to the LAST
            COMPLETED TURN (the one whose accumulated context just
            crossed the threshold and got summarised), so the 📦
            card appears at the bottom of that turn's body — right
            after the agent's last response / tool call, before
            the user typed the new prompt that triggered the next
            LLM call. The card is collapsible to read the summary
            preview or fold away. See the WS handler in ChatPage
            for the attach logic. */}
        {(turn.compactNotes ?? []).map((n) => (
          <CompactBreadcrumbRow key={n.id} note={n} />
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
      {/* Completed turns keep their own per-turn stats card so history is
          inspectable when scrolling back. The LIVE ticker for the running
          turn is NOT shown inside the card — it lives in the sticky
          ChatStatusBar above the compose divider so the user always sees
          current activity without scrolling. */}
      {turn.summary && <TurnFooter summary={turn.summary} />}
    </article>
  );
}

// Memoized default export: shallow-prop comparison is correct because
// ChatPage builds `turn`/`turn.tools`/`turn.agents` once per event and
// reuses the same references for unchanged historical turns. With memo,
// only the active turn (passed to <ActiveTurnCard>) re-renders per event.
const TurnCard = memo(TurnCardImpl);
export default TurnCard;

// Non-memoized named export for the live-tail card. The active turn
// re-renders on every WS event by design (text streams, tool results
// patch) — memo would never skip it anyway since `turn` is a new ref
// every event, so we use a plain component to make the intent obvious.
export const ActiveTurnCard = TurnCardImpl;

// ============================================================================
// CompactBreadcrumbRow — collapsible "📦 auto-compact" card inline in a
// turn's timeline. Renders at the chronological point where the
// compaction actually fired, so the user sees the breadcrumb next to
// the agent's reasoning that triggered it (not stacked at the bottom
// of the transcript where the user has to scroll to find it).
// ============================================================================

function CompactBreadcrumbRowImpl({ note }: { note: ContextCompactedNote }) {
  const [open, setOpen] = useState(false);
  const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}k` : String(n);
  const freed = Math.max(0, note.tokensBefore - note.tokensAfter);
  return (
    <div className="my-2 flex justify-start">
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
const CompactBreadcrumbRow = memo(CompactBreadcrumbRowImpl);

// ============================================================================
// TimelineBlockRow — dispatch on block.kind to render the right component.
// ============================================================================

function TimelineBlockRowImpl({
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
    case "llm_call":
      return <LlmCallBlock inputTokens={block.inputTokens} outputTokens={block.outputTokens} cacheReadTokens={block.cacheReadTokens ?? 0} cacheCreationTokens={block.cacheCreationTokens ?? 0} />;
  }
}
const TimelineBlockRow = memo(TimelineBlockRowImpl);

// ============================================================================
// LlmCallBlock — chronological marker for one model call inside the turn.
// Shows in / out tokens for THAT iteration so the user can see how each step
// in the agent's reasoning loop spent the token budget, not just the total.
// The cached fraction is shown inline ("(X cached)") so the user can see
// the prompt-cache hit rate per call — a 90% cache rate means we're only
// paying full price on ~10% of the input.
// ============================================================================

function LlmCallBlockImpl({
  inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens,
}: { inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheCreationTokens: number }) {
  // Right-aligned, borderless, subtle. Used to mark every model call inside a
  // turn (so the user can see token cost per iteration), but it's secondary
  // information — visual weight is reduced so it doesn't compete with tool
  // / file / agent blocks. No box, no background, just a line of small
  // labelled text aligned to the right rail.
  //
  // The "(X cached · Y new)" parenthetical on input shows the cache hit
  // fraction and the rest (uncached + cache_creation = new tokens billed
  // at full input price). The "new" count drops when cache_creation is
  // positive (i.e. the first call in a session) — uncached + write tokens
  // both count as "new" because they cost the same per token.
  const newTokens = Math.max(0, inputTokens - cacheReadTokens);
  // Admin "show only new tokens" mode: collapse the in figure to just the
  // new (uncached) tokens and drop the cached/new parenthetical. The full
  // breakdown stays in the hover title so the detail is one hover away.
  const { tokensShowNewOnly } = useAppSettings();
  const titleParts: string[] = [`${inputTokens.toLocaleString()} in`];
  if (cacheReadTokens > 0)     titleParts.push(`${cacheReadTokens.toLocaleString()} cache hits`);
  if (cacheCreationTokens > 0) titleParts.push(`${cacheCreationTokens.toLocaleString()} cache writes`);
  titleParts.push(`${newTokens.toLocaleString()} new (uncached + writes)`);
  titleParts.push(`${outputTokens.toLocaleString()} out`);
  return (
    <div className="flex justify-end pr-1 font-sans text-[10px] leading-tight text-subtle">
      <span
        className="inline-flex items-baseline gap-1.5"
        title={`Model call · ${titleParts.join(" · ")}`}
      >
        <span className="font-semibold uppercase tracking-[0.16em]">llm</span>
        {tokensShowNewOnly ? (
          <span className="font-mono text-tx-xs">
            <span className="text-accent">{formatTokens(newTokens)}</span>
            <span className="text-subtle"> in · </span>
            <span className="text-accent-2">{formatTokens(outputTokens)}</span>
            <span className="text-subtle"> out</span>
          </span>
        ) : (
          <span className="font-mono text-tx-xs">
            <span className="text-accent">{formatTokens(inputTokens)}</span>
            {cacheReadTokens > 0 && (
              <span
                className="text-success/80"
                title={`${cacheReadTokens.toLocaleString()} cache hits`}
              >
                {" "}({formatTokens(cacheReadTokens)} cached
                {newTokens > 0 && <> · <span className="text-text">{formatTokens(newTokens)} new</span></>})
              </span>
            )}
            {cacheReadTokens === 0 && newTokens > 0 && (
              <span className="text-subtle"> ({formatTokens(newTokens)} new)</span>
            )}
            <span className="text-subtle"> in · </span>
            <span className="text-accent-2">{formatTokens(outputTokens)}</span>
            <span className="text-subtle"> out</span>
          </span>
        )}
      </span>
    </div>
  );
}
const LlmCallBlock = memo(LlmCallBlockImpl);

// ============================================================================
// SubAgentCallsBreakdown — collapsible per-call token list shown inside a
// sub-agent card. Each row = one model call the sub-agent made. Collapsed by
// default; the card stays compact unless the user wants the detail.
// ============================================================================

function SubAgentCallsBreakdownImpl({ calls }: { calls: LlmCall[] }) {
  const [open, setOpen] = useState(false);
  const totalIn  = calls.reduce((a, c) => a + c.inputTokens, 0);
  const totalOut = calls.reduce((a, c) => a + c.outputTokens, 0);
  const totalCacheRead = calls.reduce((a, c) => a + (c.cacheReadTokens ?? 0), 0);
  const totalNew = Math.max(0, totalIn - totalCacheRead);
  return (
    <div className="border-t border-accent-2/15 px-3 py-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-baseline gap-2 font-sans text-tx-xs text-subtle hover:text-text"
      >
        <span className="font-semibold uppercase tracking-[0.14em] text-accent-2/80">
          LLM calls
        </span>
        <span className="font-mono text-muted">
          {calls.length} · {formatTokens(totalIn)} in
          {totalCacheRead > 0 ? (
            <span
              className="text-success/80"
              title={`${totalCacheRead.toLocaleString()} cache hits across this sub-agent's calls`}
            >
              {" "}({formatTokens(totalCacheRead)} cached
              {totalNew > 0 && <> · <span className="text-text">{formatTokens(totalNew)} new</span></>})
            </span>
          ) : totalNew > 0 ? (
            <span className="text-subtle"> ({formatTokens(totalNew)} new)</span>
          ) : null}
          <span className="text-subtle"> / </span>
          {formatTokens(totalOut)} out
        </span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul className="mt-1 space-y-0.5">
          {calls.map((c, i) => {
            const callNew = Math.max(0, c.inputTokens - (c.cacheReadTokens ?? 0));
            return (
              <li key={`${c.ts}-${i}`} className="flex items-baseline gap-3 font-mono text-tx-xs">
                <span className="w-10 text-right text-subtle">#{i + 1}</span>
                <span className="text-accent">{formatTokens(c.inputTokens)}</span>
                {c.cacheReadTokens > 0 ? (
                  <span className="text-success/80" title={`${c.cacheReadTokens.toLocaleString()} cache hits`}>
                    ({formatTokens(c.cacheReadTokens)} cached
                    {callNew > 0 && <> · <span className="text-text">{formatTokens(callNew)} new</span></>})
                  </span>
                ) : callNew > 0 ? (
                  <span className="text-subtle">({formatTokens(callNew)} new)</span>
                ) : null}
                <span className="text-subtle">in</span>
                <span className="text-accent-2">{formatTokens(c.outputTokens)}</span>
                <span className="text-subtle">out</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
const SubAgentCallsBreakdown = memo(SubAgentCallsBreakdownImpl);

// ============================================================================
// TextBlockView — response prose, rendered as markdown so headings, bold,
// tables, code blocks, etc. format correctly instead of showing raw syntax.
// ============================================================================

function TextBlockViewImpl({ text, isStreaming }: { text: string; isStreaming: boolean }) {
  return (
    <div className="relative">
      <Markdown text={text} />
      {isStreaming && <span className="stream-dot ml-1.5 align-middle" />}
    </div>
  );
}
const TextBlockView = memo(TextBlockViewImpl);

// ============================================================================
// ThinkingBlockView — collapsible inline. Short label by default; click to
// expand the full chain-of-thought. Italic dim text in a violet-rail card
// so it's unmistakably "internal reasoning, not the answer".
// ============================================================================

function ThinkingBlockViewImpl({ text }: { text: string }) {
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
const ThinkingBlockView = memo(ThinkingBlockViewImpl);

// ============================================================================
// Turn divider — sans eyebrow "TURN 3 · 09:42:15" with a thin connecting line
// ============================================================================

function TurnDividerImpl({ index, startedAt }: { index: number; startedAt: number }) {
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
const TurnDivider = memo(TurnDividerImpl);

// ---- Tool ----------------------------------------------------------------

function ToolNodeImpl({ tool }: { tool: ToolEvent }) {
  // Preview can be up to ~500 chars from the backend; show only the first
  // line truncated to 110 chars by default with a "show more" toggle so the
  // tree stays compact but the full output is one click away. Double-click
  // anywhere on the result row also toggles, for fast skimming.
  const [expanded, setExpanded] = useState(false);
  const preview = tool.preview ?? (tool.status === "error" ? "failed" : "");
  const firstLine = preview.split("\n")[0];
  const hasMore = preview.length > 110 || preview.includes("\n");
  // Copy-spill-path button feedback. We set this to "Copied!" briefly
  // then revert; the timeout is short so it doesn't get in the way
  // when the user clicks multiple times.
  const [copied, setCopied] = useState<"path" | "view" | null>(null);
  const onCopy = (text: string, kind: "path" | "view") => {
    if (typeof navigator !== "undefined" && navigator.clipboard) {
      navigator.clipboard.writeText(text).then(
        () => { setCopied(kind); setTimeout(() => setCopied(null), 1200); },
        () => {},
      );
    }
  };

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
            <>
              <pre className="mx-5 mt-1 max-h-72 overflow-auto rounded border border-border bg-bg p-2 whitespace-pre-wrap break-words font-mono text-tx-xs leading-snug text-text">
                {preview}
              </pre>
              {tool.previewTruncated && (
                <div className="mx-5 mt-1 font-sans text-tx-xs text-muted">
                  output was truncated — full result is in the model's context
                </div>
              )}
            </>
          )}
          {/* Smart bash output size line. Renders for EVERY bash call
              (not just truncated ones) so the user always sees the
              status: total size, the cap that was set, and whether
              the output was sent in full or trimmed. When trimmed,
              the kept / dropped numbers and the spill path are
              included inline. Renders BELOW the preview block,
              visible whether the preview is expanded or not. */}
          {tool.bashOutput && (
            <BashOutputStatusLine
              t={tool.bashOutput}
              copied={copied}
              onCopy={onCopy}
            />
          )}
        </>
      )}
    </div>
  );
}

// One-line status for the bash tool. Renders a compact, scannable
// summary of the output size + truncation state. When the output was
// trimmed, the kept/dropped numbers and a copy-spill-path button are
// included. Goal: the user can read this in <1 second and know
// exactly what was sent to the LLM.
function BashOutputStatusLine({
  t,
  copied,
  onCopy,
}: {
  t: NonNullable<ToolEvent["bashOutput"]>;
  copied: "path" | "view" | null;
  onCopy: (text: string, kind: "path" | "view") => void;
}) {
  const fmt = (n: number) => n.toLocaleString();
  const passed = t.status === "passed_through";
  const isFailure = t.verdict === "FAILURE";
  // Color: success/greenish for passed-through, amber for trimmed,
  // red for trimmed+FAILURE. Keep palette tight to the existing
  // tokens (success / warning / danger).
  const color = passed
    ? "text-success"
    : isFailure
      ? "text-danger"
      : "text-warning";
  const label = passed
    ? "✓ sent in full"
    : isFailure
      ? "✗ trimmed (failure)"
      : "⚠ trimmed";
  return (
    <div className="mx-5 mt-1 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 font-sans text-tx-xs">
      <span className={`font-semibold ${color}`}>{label}</span>
      <span className="text-muted">·</span>
      <code className="rounded bg-bg/60 px-1 font-mono">
        {fmt(t.total)}
      </code>
      <span className="text-muted">
        / {fmt(t.cap)} cap chars
      </span>
      {passed ? null : (
        <>
          <span className="text-muted">·</span>
          <span className="text-muted">sent</span>
          <code className="rounded bg-bg/60 px-1 font-mono">
            first {fmt(t.keptFirst ?? 0)}
          </code>
          <span className="text-muted">+</span>
          <code className="rounded bg-bg/60 px-1 font-mono">
            last {fmt(t.keptLast ?? 0)}
          </code>
          <span className="text-muted">·</span>
          <span className="text-muted">dropped</span>
          <code className="rounded bg-bg/60 px-1 font-mono">
            {fmt(t.dropped ?? 0)}
          </code>
          <span className="text-muted">from middle</span>
        </>
      )}
      {t.spillPath && (
        <>
          <span className="text-muted">·</span>
          <code
            className="cursor-pointer break-all rounded bg-bg/60 px-1 font-mono hover:bg-elevated/80"
            title="Click to copy the spill file path"
            onClick={() => onCopy(t.spillPath!, "path")}
          >
            {t.spillPath}
          </code>
          <button
            type="button"
            onClick={() => onCopy(t.spillPath!, "path")}
            className="font-sans text-tx-xs text-accent hover:text-text"
            title="Copy the spill file path"
          >
            {copied === "path" ? "✓" : "copy"}
          </button>
        </>
      )}
    </div>
  );
}

const ToolNode = memo(ToolNodeImpl);

// LiveDuration — ticks every 250ms while running, freezes when ended.
// Used on tools AND agents so any in-flight item shows real-time elapsed.
function LiveDurationImpl({
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
const LiveDuration = memo(LiveDurationImpl);

function StatusIconImpl({ status }: { status: "running" | "done" | "error" }) {
  if (status === "running") {
    return <span className="text-warn animate-pulse-soft text-tx-xs">⏳</span>;
  }
  if (status === "error") {
    return <span className="text-danger text-tx-xs">✗</span>;
  }
  return <span className="text-success text-tx-xs">✓</span>;
}
const StatusIcon = memo(StatusIconImpl);

// ---- File change ---------------------------------------------------------

function FileNodeImpl({ file }: { file: FileChange }) {
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
const FileNode = memo(FileNodeImpl);

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

function AgentNodeImpl({ agent }: { agent: AgentRecord }) {
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
  // Sub-agent's cache hits — sum across its own llmCalls. (Same logic as
  // the orchestrator's ChatStatusBar, scoped to one sub-agent.)
  const subCacheRead = agent.llmCalls.reduce((a, c) => a + (c.cacheReadTokens ?? 0), 0);
  const subNewTokens = Math.max(0, agent.liveInputTokens - subCacheRead);

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
        {agent.llmCalls.length > 0 && (
          <span className="text-muted">
            <span className="text-subtle">calls:</span>{" "}
            <span className="text-text">{agent.llmCalls.length}</span>
          </span>
        )}
        {totalTok > 0 && (
          <span className="text-muted">
            <span className="text-subtle">tokens:</span>{" "}
            <span className="text-accent">{formatTokens(agent.liveInputTokens)}</span>
            {subCacheRead > 0 ? (
              <span
                className="text-success/80"
                title={`${subCacheRead.toLocaleString()} cache hits across this sub-agent's calls`}
              >
                {" "}({formatTokens(subCacheRead)} cached
                {subNewTokens > 0 && <> · <span className="text-text">{formatTokens(subNewTokens)} new</span></>})
              </span>
            ) : subNewTokens > 0 ? (
              <span className="text-subtle"> ({formatTokens(subNewTokens)} new)</span>
            ) : null}
            <span className="text-subtle"> in / </span>
            <span className="text-accent-2">{formatTokens(agent.liveOutputTokens)}</span>
            <span className="text-subtle"> out</span>
          </span>
        )}
      </div>

      {/* Per-LLM-call breakdown for this sub-agent — collapsed by default to
          keep the card compact. Click to expand and see how each iteration
          spent its token budget. */}
      {agent.llmCalls.length > 0 && (
        <SubAgentCallsBreakdown calls={agent.llmCalls} />
      )}

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
const AgentNode = memo(AgentNodeImpl);


// ---- Commit --------------------------------------------------------------

function CommitNodeImpl({ commit }: { commit: CommitRecord }) {
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
const CommitNode = memo(CommitNodeImpl);

// ============================================================================
// Helpers
// ============================================================================

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

