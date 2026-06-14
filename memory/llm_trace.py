"""
Per-session LLM call trace store.

Records every request/response pair the agent makes to the LLM, so the
user can see the EXACT prompt being sent and the EXACT response coming
back — including the system prompt, the message history (post-compact,
post-mask, post-strip, post-trim), the tool definitions, and the
usage_metadata the provider reports back.

Why: the "In 97k · 114 cached · 97k new" stat the user sees on a turn
is a SUMMARY. To debug why the prompt is large, why the cache isn't
hitting, or why a specific tool call was made, you need to see the
raw bytes. This is that view.

Design:
  - In-memory ring buffer per session, capped at MAX_RECORDS = 50 calls.
  - Records are dropped on session end (no persistence — the LangGraph
    checkpointer already has the canonical message history; the trace
    is a debug-only artifact of "what actually went over the wire").
  - Thread-safe: writes happen from the LangGraph worker thread, reads
    happen from the FastAPI async handler. The store uses a Lock.
  - Defensive serialization: messages can contain non-JSON-able
    objects (e.g. tool_call_id, structured content blocks). We
    `model_dump()` them to plain dicts on write so the GET endpoint
    returns pure JSON.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


MAX_RECORDS = 50  # most-recent N kept per session


@dataclass
class LLMCallRecord:
    """One LLM call captured at the wire level.

    `request_messages` is the list passed to `model.stream(messages)`
    in agents.nodes._stream_model_call. Includes the SystemMessages
    (static + dynamic), all prior HumanMessages / AIMessages /
    ToolMessages, and the freshly-appended HumanMessage for this turn
    (or not, depending on where in the loop we are).

    `response` is the aggregated AIMessageChunk (or AIMessage) returned
    by the model. Includes `content` (text + thinking), `tool_calls`,
    `additional_kwargs`, `response_metadata`, `usage_metadata`.

    `duration_ms` is wall-clock from `model.stream()` start to end —
    includes network time, model thinking, and the streaming send
    back. Useful to distinguish "the model took 8s to think" from
    "we waited 5s for the network".
    """
    ts: float                         # time.time() at end of call
    iteration: int                    # node_agent's iteration counter
    model: str                        # model name (MiniMax-M3, etc.)
    request_messages: list[dict]      # the full prompt sent
    response: dict                    # the full response from the model
    usage: dict                       # usage_metadata, flattened
    duration_ms: int                  # wall-clock for the call
    finish_reason: str = ""           # stop / tool_calls / length / etc.

    def to_json(self) -> dict:
        return {
            "ts": self.ts,
            "iteration": self.iteration,
            "model": self.model,
            "duration_ms": self.duration_ms,
            "finish_reason": self.finish_reason,
            "request_messages": self.request_messages,
            "response": self.response,
            "usage": self.usage,
        }


class LLMTraceStore:
    """Per-session ring buffer of LLMCallRecord.

    Used as a process-wide singleton: a single instance is created at
    server startup and shared across all requests. Keys are session_id.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, deque[LLMCallRecord]] = {}
        self._lock = threading.Lock()

    def record(self, session_id: str, rec: LLMCallRecord) -> None:
        with self._lock:
            buf = self._by_session.get(session_id)
            if buf is None:
                buf = deque(maxlen=MAX_RECORDS)
                self._by_session[session_id] = buf
            buf.append(rec)

    def list(self, session_id: str) -> list[LLMCallRecord]:
        with self._lock:
            buf = self._by_session.get(session_id)
            if not buf:
                return []
            return list(buf)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._by_session.pop(session_id, None)


# Module-level singleton, lazily initialised on first call.
_store: LLMTraceStore | None = None
_store_lock = threading.Lock()


def get_store() -> LLMTraceStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = LLMTraceStore()
    return _store


# ---------------------------------------------------------------------------
# Serialisation helpers — convert BaseMessage / AIMessageChunk / etc. to
# plain dicts so the JSON response is always safe to dump.
# ---------------------------------------------------------------------------

def _message_to_dict(m: Any) -> dict:
    """Best-effort plain-dict conversion of a LangChain message.

    Handles:
      - BaseMessage (use .model_dump() since langchain-core>=0.1)
      - Plain dict (pass through)
      - Anything else: repr()

    We pull the fields a debugger cares about and let the rest fall
    through `additional_kwargs` / `response_metadata` (which model_dump
    already flattens).
    """
    if isinstance(m, dict):
        return m
    if hasattr(m, "model_dump"):
        try:
            d = m.model_dump()
        except Exception:
            d = {
                "type": type(m).__name__,
                "content": getattr(m, "content", None),
                "tool_calls": getattr(m, "tool_calls", None),
                "tool_call_id": getattr(m, "tool_call_id", None),
                "name": getattr(m, "name", None),
                "additional_kwargs": getattr(m, "additional_kwargs", None) or {},
                "response_metadata": getattr(m, "response_metadata", None) or {},
            }
        return {
            "role": _role_of(m),
            "type": type(m).__name__,
            "content": d.get("content"),
            "tool_calls": d.get("tool_calls") or d.get("tool_calls_chunks"),
            "tool_call_id": d.get("tool_call_id"),
            "name": d.get("name"),
            "additional_kwargs": d.get("additional_kwargs") or {},
            "response_metadata": d.get("response_metadata") or {},
            "id": d.get("id"),
        }
    return {"_repr": repr(m)}


def _role_of(m: Any) -> str:
    """Map a LangChain message class to its short role name.

    `AIMessage` → 'assistant', `HumanMessage` → 'user', etc.
    The class name is the most reliable signal — `type` is overloaded."""
    cls = type(m).__name__
    return {
        "SystemMessage": "system",
        "HumanMessage": "user",
        "AIMessage": "assistant",
        "AIMessageChunk": "assistant",
        "ToolMessage": "tool",
        "FunctionMessage": "tool",
        "ToolCallChunk": "assistant",
    }.get(cls, cls.lower())


def serialize_messages(messages: list[Any]) -> list[dict]:
    return [_message_to_dict(m) for m in messages]
