// LLMTracePanel — wire-level LLM call trace for the active session.
//
// What this shows:
//   For every LLM call the agent made in this session (capped at the
//   server's MAX_RECORDS=50 ring buffer), show the EXACT request
//   payload (system prompt + message history + tool definitions) and
//   the EXACT response that came back. Plus usage_metadata so you
//   can see the input/output/cache split.
//
// Why:
//   The per-turn stats the chat shows ("In 97k · 114 cached · 97k
//   new") are aggregated summaries. To debug why the prompt is
//   unexpectedly large, why the cache isn't hitting, or which tool
//   call the model produced, you need the raw bytes. This is that
//   view.
//
// UX:
//   - Full-screen overlay (similar to DebugStream)
//   - List of LLM calls on the left, newest first
//   - Each call shows: timestamp, iteration #, model, duration,
//     in/out tokens, cache split, finish_reason
//   - Click → expands to show full request_messages + response
//   - Request messages: role, content, tool_calls, thinking blocks
//   - Response: content + tool_calls + additional_kwargs
//   - "Refresh" button to re-fetch (since the buffer grows during
//     the session and stale panels miss the latest call)
//   - "Clear" button wipes the server-side buffer

import { memo, useEffect, useState, useCallback } from "react";
import { sessionApi } from "@/lib/api";

type LLMMessage = {
  role: string;
  type: string;
  content: unknown;
  tool_calls?: unknown;
  tool_call_id?: string | null;
  name?: string | null;
  additional_kwargs?: Record<string, unknown>;
  response_metadata?: Record<string, unknown>;
  id?: string | null;
};

type LLMCall = {
  ts: number;
  iteration: number;
  model: string;
  duration_ms: number;
  finish_reason: string;
  request_messages: LLMMessage[];
  response: LLMMessage;
  usage: Record<string, unknown>;
};

type LLMTraceResponse = {
  session_id: string;
  count: number;
  calls: LLMCall[];
};

function formatTs(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString();
  } catch {
    return String(ts);
  }
}

function bytesOf(x: unknown): number {
  try {
    return JSON.stringify(x).length;
  } catch {
    return 0;
  }
}

function approxTokensOf(x: unknown): number {
  // Rough — 1 token ≈ 4 chars. Same heuristic the server uses
  // locally for the compact estimate; the actual LLM-reported
  // `input_tokens` is the truth, this is for the per-message
  // visual only.
  return Math.round(bytesOf(x) / 4);
}

function renderContent(content: unknown): string {
  if (content == null) return "(empty)";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block: any) => {
        if (typeof block === "string") return block;
        if (block && typeof block === "object") {
          if (block.type === "text") return block.text || "";
          if (block.type === "thinking") return `[thinking] ${block.thinking || block.text || ""}`;
          if (block.type === "tool_use")
            return `[tool_use ${block.name || ""}] ${JSON.stringify(block.input || {})}`;
          if (block.type === "tool_result")
            return `[tool_result ${block.name || ""}] ${
              typeof block.content === "string"
                ? block.content
                : JSON.stringify(block.content)
            }`;
          return JSON.stringify(block, null, 2);
        }
        return String(block);
      })
      .join("\n");
  }
  try {
    return JSON.stringify(content, null, 2);
  } catch {
    return String(content);
  }
}

function summarizeUsage(usage: Record<string, unknown>): {
  inTok: number;
  outTok: number;
  cacheRead: number;
  cacheCreation: number;
  total: number;
} {
  const inTok = Number(usage.input_tokens ?? 0);
  const outTok = Number(usage.output_tokens ?? 0);
  let cacheRead = 0;
  let cacheCreation = 0;
  const details =
    (usage.prompt_tokens_details as Record<string, unknown> | undefined) ||
    (usage.input_token_details as Record<string, unknown> | undefined) ||
    {};
  cacheRead = Number(
    (details as any).cached_tokens ?? (details as any).cache_read ?? 0,
  );
  cacheCreation = Number(
    (details as any).cache_creation_tokens ?? (details as any).cache_creation ?? 0,
  );
  // OpenAI/Anthropic native shape: cache fields at top level
  if (!cacheRead)
    cacheRead = Number(
      (usage as any).cache_read_input_tokens ?? 0,
    );
  if (!cacheCreation)
    cacheCreation = Number(
      (usage as any).cache_creation_input_tokens ?? 0,
    );
  return {
    inTok,
    outTok,
    cacheRead,
    cacheCreation,
    total: inTok + outTok,
  };
}

function UsageBadge({ usage }: { usage: Record<string, unknown> }) {
  const { inTok, outTok, cacheRead, cacheCreation } = summarizeUsage(usage);
  if (!inTok && !outTok) return <span className="text-muted">no usage</span>;
  return (
    <span className="font-mono text-[11px]">
      <span className="text-text">in {inTok.toLocaleString()}</span>
      {cacheRead > 0 && (
        <span className="ml-2 text-success">
          cache {cacheRead.toLocaleString()}
        </span>
      )}
      {cacheCreation > 0 && (
        <span className="ml-2 text-warn">
          write {cacheCreation.toLocaleString()}
        </span>
      )}
      <span className="ml-2 text-text">out {outTok.toLocaleString()}</span>
    </span>
  );
}

function MessageBlock({ msg, idx }: { msg: LLMMessage; idx: number }) {
  const [expanded, setExpanded] = useState(false);
  const preview = renderContent(msg.content);
  const previewShort =
    preview.length > 200 ? preview.slice(0, 200) + "…" : preview;
  const tok = approxTokensOf(msg.content);
  const toolCalls = Array.isArray(msg.tool_calls) ? msg.tool_calls : [];
  return (
    <div className="rounded border border-border/50 bg-bg/40 p-2">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-baseline justify-between gap-2 text-left"
      >
        <span className="flex items-baseline gap-2">
          <span className="font-mono text-[10px] text-muted">#{idx}</span>
          <span className="font-mono text-[11px] font-semibold text-accent">
            {msg.role}
          </span>
          <span className="font-mono text-[10px] text-muted">{msg.type}</span>
          {msg.name && (
            <span className="font-mono text-[10px] text-muted">
              → {msg.name}
            </span>
          )}
          {toolCalls.length > 0 && (
            <span className="font-mono text-[10px] text-warn">
              · {toolCalls.length} tool_call{toolCalls.length === 1 ? "" : "s"}
            </span>
          )}
          <span className="font-mono text-[10px] text-muted">~{tok} tok</span>
        </span>
        <span className="font-mono text-[10px] text-muted">
          {expanded ? "▾" : "▸"}
        </span>
      </button>
      <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-snug text-text/90">
        {expanded ? preview : previewShort}
      </pre>
      {toolCalls.length > 0 && expanded && (
        <div className="mt-2 space-y-2">
          {toolCalls.map((tc: any, i: number) => (
            <div
              key={i}
              className="rounded border border-warn/30 bg-warn/5 p-2"
            >
              <div className="font-mono text-[10px] font-semibold text-warn">
                {tc.name || "tool"}(
                {tc.id && (
                  <span className="ml-1 text-muted">id={tc.id}</span>
                )}
                )
              </div>
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[10px] leading-snug text-text/80">
                {JSON.stringify(tc.args ?? {}, null, 2)}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

type Props = {
  sessionId: string;
  onClose: () => void;
};

function LLMTracePanelImpl({ sessionId, onClose }: Props) {
  const [data, setData] = useState<LLMTraceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>("");
  const [expandedCall, setExpandedCall] = useState<number | null>(null);

  const load = useCallback(async () => {
    if (!sessionId) return;
    setLoading(true);
    setError("");
    try {
      const res = await sessionApi.llmTrace(sessionId);
      setData(res);
      // Auto-expand the most recent call so the user lands on
      // something useful rather than a closed list.
      if (res.calls.length > 0 && expandedCall == null) {
        setExpandedCall(res.calls.length - 1);
      }
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [sessionId, expandedCall]);

  useEffect(() => {
    load();
  }, [load]);

  const clear = useCallback(async () => {
    if (!sessionId) return;
    if (!confirm("Clear all LLM call records for this session?")) return;
    try {
      await sessionApi.clearLlmTrace(sessionId);
      await load();
    } catch (e: any) {
      setError(e?.message || String(e));
    }
  }, [sessionId, load]);

  // Esc to close
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const calls = data?.calls ?? [];
  const selected = expandedCall != null ? calls[expandedCall] : null;

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-bg/95 backdrop-blur-sm"
      role="dialog"
      aria-label="LLM call trace"
    >
      <header className="flex items-center justify-between border-b border-border bg-bg/90 px-4 py-3">
        <div className="flex items-baseline gap-3">
          <h2 className="font-semibold text-text">LLM trace</h2>
          <span className="font-mono text-[11px] text-muted">
            {calls.length} call{calls.length === 1 ? "" : "s"} captured
            (server keeps up to 50)
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={load}
            disabled={loading}
            className="pill min-h-touch"
            title="Re-fetch from server"
          >
            {loading ? "loading…" : "↻ refresh"}
          </button>
          <button
            type="button"
            onClick={clear}
            disabled={loading || calls.length === 0}
            className="pill min-h-touch"
            title="Clear the server-side trace buffer"
          >
            clear
          </button>
          <button
            type="button"
            onClick={onClose}
            className="pill min-h-touch"
            title="Close (Esc)"
            aria-label="Close LLM trace"
          >
            ✕
          </button>
        </div>
      </header>

      {error && (
        <div className="border-b border-danger/40 bg-danger/10 px-4 py-2 font-mono text-[12px] text-danger">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        {/* Left: list of calls, newest first */}
        <aside className="w-80 shrink-0 overflow-y-auto border-r border-border bg-bg/60">
          {calls.length === 0 ? (
            <div className="p-4 font-mono text-[12px] text-muted">
              {loading ? "loading…" : "no calls captured yet"}
            </div>
          ) : (
            <ul className="divide-y divide-border/40">
              {[...calls].reverse().map((c, i) => {
                const realIdx = calls.length - 1 - i;
                const isSel = realIdx === expandedCall;
                const usage = summarizeUsage(c.usage);
                return (
                  <li key={c.ts + "-" + realIdx}>
                    <button
                      type="button"
                      onClick={() => setExpandedCall(realIdx)}
                      className={`flex w-full flex-col items-start gap-1 px-3 py-2 text-left hover:bg-accent/10 ${
                        isSel ? "bg-accent/15" : ""
                      }`}
                    >
                      <div className="flex w-full items-baseline justify-between">
                        <span className="font-mono text-[11px] font-semibold text-text">
                          #{realIdx + 1} · iter {c.iteration}
                        </span>
                        <span className="font-mono text-[10px] text-muted">
                          {formatTs(c.ts)}
                        </span>
                      </div>
                      <div className="flex w-full items-baseline justify-between">
                        <span className="font-mono text-[10px] text-muted">
                          {c.model} · {c.duration_ms}ms · {c.finish_reason || "?"}
                        </span>
                      </div>
                      <UsageBadge usage={c.usage} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* Right: detail of the selected call */}
        <main className="min-w-0 flex-1 overflow-y-auto p-4">
          {!selected ? (
            <div className="font-mono text-[12px] text-muted">
              select a call on the left to inspect
            </div>
          ) : (
            <div className="space-y-4">
              <div className="rounded border border-border bg-bg/50 p-3">
                <div className="mb-1 font-mono text-[10px] uppercase text-muted">
                  request · {selected.request_messages.length} message
                  {selected.request_messages.length === 1 ? "" : "s"}
                </div>
                <div className="space-y-1.5">
                  {selected.request_messages.map((m, i) => (
                    <MessageBlock key={i} msg={m} idx={i} />
                  ))}
                </div>
              </div>
              <div className="rounded border border-accent/30 bg-accent/5 p-3">
                <div className="mb-1 font-mono text-[10px] uppercase text-accent">
                  response · {selected.response.type}
                </div>
                <MessageBlock msg={selected.response} idx={-1} />
              </div>
              <details className="rounded border border-border bg-bg/50 p-3">
                <summary className="cursor-pointer font-mono text-[11px] text-muted">
                  raw usage_metadata
                </summary>
                <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[10px] leading-snug text-text/80">
                  {JSON.stringify(selected.usage, null, 2)}
                </pre>
              </details>
              <details className="rounded border border-border bg-bg/50 p-3">
                <summary className="cursor-pointer font-mono text-[11px] text-muted">
                  raw response.response_metadata
                </summary>
                <pre className="mt-2 whitespace-pre-wrap break-words font-mono text-[10px] leading-snug text-text/80">
                  {JSON.stringify(
                    selected.response.response_metadata || {},
                    null,
                    2,
                  )}
                </pre>
              </details>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

const LLMTracePanel = memo(LLMTracePanelImpl);
export default LLMTracePanel;
