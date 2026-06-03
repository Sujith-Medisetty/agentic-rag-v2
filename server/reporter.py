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
    dequeue + fan-out from the FastAPI event loop."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._subscribers: set = set()
        self._lock = threading.Lock()
        self._fanout_task: asyncio.Task | None = None

    # ---- wiring (called from the event loop) ----------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to the FastAPI event loop. Safe to call repeatedly; only the
        first call has any effect."""
        with self._lock:
            if self._loop is not None:
                return
            self._loop = loop
            self._queue = asyncio.Queue()
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
        if self._loop is not None and self._queue is not None:
            try:
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


def get_bus(session_id: str) -> SessionBus:
    """Get-or-create the bus for a session."""
    with _registry_lock:
        bus = _buses.get(session_id)
        if bus is None:
            bus = SessionBus(session_id)
            _buses[session_id] = bus
        return bus


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

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._bus = get_bus(session_id)
        self._changed_paths: set[str] = set()
        self._changed_lock = threading.Lock()

    # ---- ProgressReporter methods ---------------------------------------
    def tool_start(self, tool: str, target: str = "") -> None:
        self._bus.publish("tool_start", {"tool": tool, "target": target[:200]})

    def tool_done(self, tool: str, preview: str = "", error: bool = False) -> None:
        self._bus.publish("tool_done", {
            "tool": tool, "preview": preview[:500], "error": error,
        })

    def message(self, text: str) -> None:
        self._bus.publish("message", {"text": text})

    # ---- Phase 2 — rich UI events ---------------------------------------
    def todo_update(self, items: list[dict]) -> None:
        self._bus.publish("todo_update", {"items": items})

    def agent_spawn(
        self,
        agent_id: str,
        description: str,
        subagent_type: str,
        name: str = "",
        model: str = "",
    ) -> None:
        self._bus.publish("agent_spawn", {
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
        self._bus.publish("agent_status_update", {
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
        self._bus.publish("file_changed", {
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
        self._bus.publish("commit_made", {
            "sha": sha, "branch": branch, "message": message,
            "files": list(files),
        })

    def commit_skipped(
        self,
        reason: str,
        branch: str = "",
        hook_output: str = "",
    ) -> None:
        self._bus.publish("commit_skipped", {
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
        self._bus.publish("push_done", {
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
        self._bus.publish("assistant_text", {"text": text, "done": done})

    def user_message(self, text: str) -> None:
        """Echoed back so reconnecting clients see the prompt that started
        this turn."""
        self._bus.publish("user_message", {"text": text})

    def turn_summary(
        self,
        tools_used: int,
        duration_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """End-of-turn metrics. Token counts are PER-TURN (diff from the
        process-wide counter before vs after this turn). The frontend sums
        across turns to display session totals."""
        self._bus.publish("turn_summary", {
            "tools_used":         tools_used,
            "duration_ms":        duration_ms,
            "input_tokens":       input_tokens,
            "output_tokens":      output_tokens,
            "cache_read_tokens":  cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
            "cost_usd":           round(cost_usd, 6),
        })

    def error(self, message: str) -> None:
        self._bus.publish("error", {"message": message})
