"""
WebReporter — ProgressReporter implementation that publishes events to a
per-session asyncio.Queue (and persists them in the events table so a
reconnecting client can replay).

The agent loop calls reporter.tool_start(...) etc. from a worker thread
(LangGraph's runner is sync). We CANNOT touch asyncio.Queue from a non-loop
thread directly, so we use loop.call_soon_threadsafe(queue.put_nowait, …).

A single SessionBus instance per session_id owns:
  - The asyncio.Queue events flow into
  - The asyncio loop the FastAPI app is running on
  - The set of currently-connected WebSockets fanning out from the queue

Lifecycle:
  - bus = get_bus(session_id)                  # idempotent — created on first reference
  - bus.bind_loop(asyncio.get_running_loop())  # called from app startup or first WS
  - reporter = WebReporter(session_id)
  - set_reporter(reporter)                      # the agent's global progress sink
  - bus.subscribe(websocket)                    # streams events to that client
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from agents.reporter import ProgressReporter
from server import db


# ============================================================================
# SessionBus — owns the queue + WebSocket fan-out for ONE session
# ============================================================================

class SessionBus:
    """Per-session pub/sub. Thread-safe enqueue from the agent thread, async
    dequeue + fan-out from the FastAPI event loop.

    Production backpressure: a single slow client must not be able
    to back the bus up indefinitely while the agent publishes. The
    queue is bounded at MAX_QUEUE; when full we DROP the oldest
    queued event (the new event still goes to the DB via
    append_event, so a reconnect with `?since=<ts>` will replay
    the gap). Subscribers that are themselves slow to consume
    (e.g. a stalled network) will see the bus drop events on
    their behalf — exactly the right behavior, since they're
    going to refetch from the DB on reconnect anyway."""

    # Cap on the in-memory fan-out queue. The agent publishes one event
    # per token (~50-100/s for a streaming response), so 1000 events
    # = ~10-20s of buffering — plenty for any normal slow client. A
    # client slower than that is effectively disconnected anyway and
    # will re-subscribe from the DB.
    MAX_QUEUE = 1000

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._subscribers: set = set()
        self._lock = threading.Lock()
        self._fanout_task: asyncio.Task | None = None
        # Diagnostic — number of events dropped by backpressure since
        # the bus was created. Visible in /api/admin/services so
        # operators can spot clients that are too slow to keep up.
        self.dropped_count = 0

    # ---- wiring (called from the event loop) ----------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the FastAPI event loop. Safe to call repeatedly; only the
        first call has any effect."""
        with self._lock:
            if self._loop is not None:
                return
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self.MAX_QUEUE)
            self._fanout_task = loop.create_task(self._fanout())

    def is_bound(self) -> bool:
        return self._loop is not None

    # ---- producer side (agent thread) -----------------------------------
    def publish(self, kind: str, payload: dict) -> None:
        """Called from ANY thread. Persists to SQLite immediately so reconnects
        replay correctly, then enqueues for live subscribers."""
        event = {
            "kind": kind,
            "payload": payload,
            "ts": int(time.time() * 1000),
        }
        # Persist first — if the queue is unbound (no live UI), the event
        # still lands in the DB and shows up when the user opens the session
        # later.
        try:
            db.append_event(self.session_id, kind, payload)
        except Exception:
            pass
        # Diagnostic trace — one line per event with sub-second timing and
        # subscriber count. Wired so we can confirm events ARE being published
        # in real time and aren't getting buffered. Set AGENT_TRACE_EVENTS=0
        # to silence once we've confirmed everything works.
        import os, sys
        if os.getenv("AGENT_TRACE_EVENTS", "1") != "0":
            try:
                t = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
                sub_n = len(self._subscribers)
                bound = "BOUND" if self._loop is not None else "UNBOUND"
                preview = ""
                if kind in ("assistant_text", "thinking_text"):
                    preview = f"+{len(payload.get('text',''))} chars" + (
                        " DONE" if payload.get("done") else "")
                elif kind == "tool_start":
                    preview = f"{payload.get('tool')}({str(payload.get('target',''))[:40]})"
                elif kind == "tool_done":
                    preview = f"{payload.get('tool')}{' ✗' if payload.get('error') else ' ✓'}"
                elif kind == "token_update":
                    preview = (
                        f"+{payload.get('input_delta',0)} in / "
                        f"+{payload.get('output_delta',0)} out"
                    )
                elif kind == "context_update":
                    # The payload carries `used_tokens` + `threshold`; the
                    # chip's "% used" denominator is the auto-compact
                    # threshold (50K default), not the model context
                    # window. Show BOTH so the trace line is actually
                    # useful for triaging "is the chip in sync with the
                    # LLM-reported value" — the previous label read
                    # `payload.get('percent', 0)` which the publisher
                    # never populates, so the trace was a permanent
                    # `0% of 200,000` regardless of the real number.
                    used = int(payload.get("used_tokens", 0) or 0)
                    threshold = int(payload.get("threshold", 0) or 0)
                    pct_of_threshold = (
                        f"{round(used / threshold * 100)}%" if threshold > 0 else "?"
                    )
                    budget = payload.get("budget_tokens")
                    budget_s = f" / {int(budget):,}" if budget else ""
                    flags = []
                    if payload.get("compacting"): flags.append("COMPACTING")
                    flag_s = f" [{','.join(flags)}]" if flags else ""
                    preview = (
                        f"{pct_of_threshold} of {threshold:,} threshold"
                        f" (used={used:,}{budget_s}){flag_s}"
                    )
                elif kind == "turn_summary":
                    preview = (
                        f"tools={payload.get('tools_used')} "
                        f"in={payload.get('input_tokens')} "
                        f"out={payload.get('output_tokens')}"
                    )
                print(
                    f"[trace {t}] {bound} subs={sub_n} kind={kind} {preview}",
                    file=sys.stderr, flush=True,
                )
            except Exception:
                pass
        if self._loop is not None and self._queue is not None:
            try:
                # Backpressure: if the queue is full, evict the OLDEST
                # event to make room for the new one. The dropped
                # event was already persisted to SQLite (above) so
                # a reconnect with `?since=<ts>` will replay the gap.
                # Net effect: a stuck client drops the OLDEST buffered
                # event while the persisted DB history stays intact.
                if self._queue.full():
                    try:
                        dropped = self._queue.get_nowait()
                        self.dropped_count += 1
                        del dropped
                    except Exception:
                        pass
                self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
            except RuntimeError:
                # Loop is closed (server shutting down); drop the event.
                pass

    # ---- consumer side (WebSocket handler) ------------------------------
    def subscribe(self, websocket) -> None:
        """Register a WebSocket. Must be called from the event loop thread."""
        with self._lock:
            self._subscribers.add(websocket)

    def unsubscribe(self, websocket) -> None:
        with self._lock:
            self._subscribers.discard(websocket)

    async def _fanout(self) -> None:
        """Pump events from the queue out to every connected WebSocket."""
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            text = json.dumps(event, default=str)
            # Snapshot subscribers so we can iterate without holding the lock
            # while awaiting sends (a slow client mustn't block the others).
            with self._lock:
                subs = list(self._subscribers)
            for ws in subs:
                try:
                    await ws.send_text(text)
                except Exception:
                    # Drop dead clients silently; the WS handler's finally
                    # block will call unsubscribe() when its task exits.
                    self.unsubscribe(ws)


# ============================================================================
# Registry — one SessionBus per session_id
# ============================================================================

_buses: dict[str, SessionBus] = {}
_registry_lock = threading.Lock()


def bus_stats() -> list[dict]:
    """Operator-facing snapshot of every active session bus. Used by the
    admin panel so we can spot clients that are too slow to keep up
    (dropped_count > 0) or sessions that have wedged subscribers
    (queue full, subscribers = 0). Returns a list of small dicts —
    no PII, just the session_id + counters."""
    out = []
    for sid, bus in _buses.items():
        qsize = bus._queue.qsize() if bus._queue is not None else 0
        out.append({
            "session_id":     sid,
            "subscribers":    len(bus._subscribers),
            "queue_size":     qsize,
            "queue_max":      bus.MAX_QUEUE,
            "dropped_count":  bus.dropped_count,
            "is_bound":       bus.is_bound(),
        })
    return out


def get_bus(session_id: str) -> SessionBus:
    """Get-or-create the bus for a session."""
    with _registry_lock:
        bus = _buses.get(session_id)
        if bus is None:
            bus = SessionBus(session_id)
            _buses[session_id] = bus
        return bus


def discard_bus(session_id: str) -> None:
    """Drop a session's bus from the in-memory registry on session delete.

    The fanout task and any live WebSocket subscribers are NOT forcibly torn
    down here — they're cheap (one coroutine awaiting an empty queue, a few
    socket references) and the clients close themselves on navigation. The
    goal is just to stop new lookups from finding a stale bus and to let
    the GC reclaim it after the last reference goes away.
    """
    with _registry_lock:
        _buses.pop(session_id, None)


# ============================================================================
# WebReporter — ProgressReporter that pipes events through a SessionBus
# ============================================================================

class WebReporter(ProgressReporter):
    """ProgressReporter that publishes structured events to a SessionBus
    (per-session asyncio.Queue → WebSocket fan-out), so a browser sees the
    agent's activity live. The base ProgressReporter is a no-op, so any
    code path without a reporter scope keeps working silently.

    Also tracks the set of paths changed in the current turn so
    session_runner can drive the Phase 4 auto-commit at turn end. The set
    accumulates across `file_changed()` calls (which happen on the agent's
    worker thread) and is drained by session_runner from the asyncio loop
    thread; guarded by a Lock so concurrent access is safe."""

    def __init__(self, session_id: str, agent_id: str = "") -> None:
        self.session_id = session_id
        # If set, every event published by this reporter is stamped with this
        # agent_id. The frontend uses that tag to route the event into the
        # right sub-agent's nested tree instead of the turn's main activity.
        # Orchestrator reporter has agent_id="" → events route to the turn.
        self.agent_id = agent_id
        self._bus = get_bus(session_id)
        self._changed_paths: set[str] = set()
        self._changed_lock = threading.Lock()

    # ---- internal publish — stamps every payload with the agent_id ------
    def _pub(self, kind: str, payload: dict) -> None:
        if self.agent_id:
            payload = {**payload, "agent_id": self.agent_id}
            # Sub-agent progress signal — every event from a tagged reporter
            # (tool_start, tool_done, token_update, etc.) means the sub-agent
            # is still productively working, so the idle watchdog resets.
            try:
                from tools.multi_agent import note_agent_progress
                note_agent_progress(self.agent_id)
            except Exception:
                pass
        self._bus.publish(kind, payload)

    # ---- ProgressReporter methods ---------------------------------------
    def tool_start(self, tool: str, target: str = "") -> None:
        self._pub("tool_start", {"tool": tool, "target": target[:200]})

    def tool_done(self, tool: str, preview: str = "", error: bool = False) -> None:
        # Cap is generous (~100KB) so "expand" in the UI actually shows the full
        # output for normal tool runs (npm install, vite build, file reads,
        # test logs all fit). Without a generous cap, the collapsed/expanded
        # toggle was a lie — both views showed the same truncated string.
        # Outputs above the cap are extremely rare for tool calls; truncating
        # there is reasonable since a human can't usefully skim 100KB anyway.
        # The model still sees the full untruncated tool result regardless.
        MAX_PREVIEW_CHARS = 100_000
        self._pub("tool_done", {
            "tool": tool,
            "preview": preview[:MAX_PREVIEW_CHARS],
            "preview_truncated": len(preview) > MAX_PREVIEW_CHARS,
            "error": error,
        })

    def message(self, text: str) -> None:
        self._pub("message", {"text": text})

    def token_update(
        self,
        input_delta: int = 0,
        output_delta: int = 0,
        cache_read_delta: int = 0,
        cache_creation_delta: int = 0,
    ) -> None:
        self._pub("token_update", {
            "input_delta":          int(input_delta or 0),
            "output_delta":         int(output_delta or 0),
            "cache_read_delta":     int(cache_read_delta or 0),
            "cache_creation_delta": int(cache_creation_delta or 0),
        })

    def context_update(
        self,
        *,
        used_tokens: int,
        budget_tokens: int = 0,
        warning: bool = False,
        compacting: bool = False,
        threshold: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> None:
        # `used_tokens` is the *new* (uncached + writes) tokens the
        # model processed this turn — already net of cache_read and
        # cache_creation. The chip reads this as the "% used"
        # denominator. We also surface the cache split so the UI
        # can show "X new, Y cache hits" — without that, a long
        # session running at 95% cache hit rate looks like 95%
        # context used when really only 5% of the prompt is fresh.
        # `threshold` is the auto-compact ceiling (50K default).
        # Persist `used_tokens` so the chip shows the same number on
        # WS reconnect as it does mid-turn (single-row UPDATE per
        # turn). We also persist the cache fields so a reconnect
        # mid-turn can show the same "X new, Y cache hits" split.
        self._pub("context_update", {
            "used_tokens":   int(used_tokens),
            "compacting":    bool(compacting),
            "threshold":     int(threshold),
            "cache_read":    int(cache_read or 0),
            "cache_creation": int(cache_creation or 0),
        })
        try:
            db.set_session_context_used(self.session_id, int(used_tokens))
        except Exception:
            pass

    def context_compacted(
        self,
        *,
        removed: int,
        kept: int,
        tokens_before: int,
        tokens_after: int,
        summary_preview: str = "",
        threshold: int = 0,
    ) -> None:
        # Chat-visible event. See agents/reporter.py:context_compacted
        # for the contract. The summary_preview is truncated to 280 chars
        # upstream; we re-cap defensively here in case a future caller
        # sends a longer string.
        preview = (summary_preview or "")[:280]
        self._pub("context_compacted", {
            "removed":         int(removed),
            "kept":            int(kept),
            "tokens_before":   int(tokens_before),
            "tokens_after":    int(tokens_after),
            "summary_preview": preview,
            "threshold":       int(threshold),
        })

    def thinking_text(self, text: str, done: bool = False) -> None:
        if not text and not done:
            return
        self._pub("thinking_text", {"text": text, "done": bool(done)})

    # ---- Phase 2 — rich UI events ---------------------------------------
    def todo_update(self, items: list[dict]) -> None:
        self._pub("todo_update", {"items": items})

    def agent_spawn(
        self,
        agent_id: str,
        description: str,
        subagent_type: str,
        name: str = "",
        model: str = "",
    ) -> None:
        self._pub("agent_spawn", {
            "agent_id": agent_id,
            "description": description,
            "subagent_type": subagent_type,
            "name": name,
            "model": model,
        })

    def agent_status_update(
        self,
        agent_id: str,
        status: str,
        output_file: str = "",
        error: str = "",
    ) -> None:
        self._pub("agent_status_update", {
            "agent_id": agent_id,
            "status": status,
            "output_file": output_file,
            "error": error,
        })

    def file_changed(
        self,
        path: str,
        kind: str,
        diff: str,
        bytes_count: int = 0,
    ) -> None:
        with self._changed_lock:
            self._changed_paths.add(path)
        self._pub("file_changed", {
            "path": path,
            "kind": kind,
            "diff": diff[:50_000],   # cap so a 10MB file doesn't choke the WS
            "bytes": bytes_count,
        })

    # ---- Phase 4 — commit / push lifecycle ------------------------------
    def commit_made(
        self,
        sha: str,
        branch: str,
        message: str,
        files: list[str],
    ) -> None:
        self._pub("commit_made", {
            "sha": sha, "branch": branch, "message": message,
            "files": list(files),
        })

    def commit_skipped(
        self,
        reason: str,
        branch: str = "",
        hook_output: str = "",
    ) -> None:
        self._pub("commit_skipped", {
            "reason": reason, "branch": branch,
            "hook_output": hook_output[:2000],
        })

    def push_done(
        self,
        branch: str,
        ok: bool,
        remote: str = "",
        error: str = "",
    ) -> None:
        self._pub("push_done", {
            "branch": branch, "ok": ok, "remote": remote,
            "error": error[:500],
        })

    # ---- helpers used by the session runner (not part of the base API) --
    def consume_changed_paths(self) -> list[str]:
        """Return + clear the paths changed since the last call. Called by
        session_runner at the end of a turn to decide whether to auto-commit."""
        with self._changed_lock:
            paths = sorted(self._changed_paths)
            self._changed_paths.clear()
        return paths

    def assistant_text(self, text: str, done: bool = False) -> None:
        """Streaming assistant text chunk. `done=True` marks end-of-turn."""
        self._pub("assistant_text", {"text": text, "done": done})

    def user_message(self, text: str) -> None:
        """Echoed back so reconnecting clients see the prompt that started
        this turn."""
        self._pub("user_message", {"text": text})

    def turn_summary(
        self,
        tools_used: int,
        duration_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
        cost_input_usd: float = 0.0,
        cost_output_usd: float = 0.0,
        cost_cache_read_usd: float = 0.0,
        cost_cache_write_usd: float = 0.0,
    ) -> None:
        """End-of-turn metrics. Token counts are PER-TURN (diff from the
        process-wide counter before vs after this turn). The frontend sums
        across turns to display session totals.

        `cost_*_usd` are the per-component cost sub-totals (input / output /
        cache_read / cache_write) — the same model-priced breakdown that
        `CostEstimate` already computes server-side. The total `cost_usd` is
        still the authoritative sum; the sub-totals are emitted so the UI can
        show "cost-of-in vs cost-of-out" and the cache-savings split without
        having to know the model pricing client-side. They round to 6
        decimals like the total."""
        self._pub("turn_summary", {
            "tools_used":           tools_used,
            "duration_ms":          duration_ms,
            "input_tokens":         input_tokens,
            "output_tokens":        output_tokens,
            "cache_read_tokens":    cache_read_tokens,
            "cache_write_tokens":   cache_write_tokens,
            "cost_usd":             round(cost_usd, 6),
            "cost_input_usd":       round(cost_input_usd, 6),
            "cost_output_usd":      round(cost_output_usd, 6),
            "cost_cache_read_usd":  round(cost_cache_read_usd, 6),
            "cost_cache_write_usd": round(cost_cache_write_usd, 6),
        })

    def session_renamed(
        self,
        new_name: str,
        previous_name: str,
        was_suffixed: bool = False,
    ) -> None:
        """The session's display name just changed — most often because
        the background LLM-suggested rename fired, but also fires for
        manual user renames (so the sidebar in Workspace.tsx, the chat
        header in ChatPage.tsx, and any other open views stay in sync
        with the DB without a manual refresh).

        `was_suffixed=True` if the server auto-appended "-2"/"-3" to
        disambiguate a duplicate. The frontend uses that to show the
        same "renamed to X — Y was taken" toast the inline rename uses."""
        self._pub("session_renamed", {
            "new_name":      new_name,
            "previous_name": previous_name,
            "was_suffixed":  was_suffixed,
        })

    def error(self, message: str) -> None:
        self._pub("error", {"message": message})
