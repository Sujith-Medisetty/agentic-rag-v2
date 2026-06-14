"""
Per-session LLM call trace store. In-memory ring buffer (capped at
MAX_RECORDS=50) of the wire-level request/response pair for each LLM
call. Process-local (no persistence — the LangGraph checkpointer has
the canonical history; the trace is a debug-only view of "what
actually went over the wire"). Thread-safe via a Lock.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


MAX_RECORDS = 50  # most-recent N kept per session


@dataclass
class LLMCallRecord:
    """One LLM call captured at the wire level.

    `request_messages` is the list passed to `model.stream(messages)`
    in agents.nodes._stream_model_call. Includes the SystemMessages
    (static + dynamic), all prior HumanMessages / AIMessages /
    (system + history + tools) and the model's reply. `duration_ms` is
    wall-clock from `model.stream()` start to end (includes network,
    model thinking, and the streaming send-back)."""
    ts: float                         # time.time() at end of call
    iteration: int                    # node_agent's iteration counter
    model: str                        # model name (MiniMax-M3, etc.)
    request_messages: list[dict]      # the full prompt sent
    response: dict                    # the full response from the model
    usage: dict                       # usage_metadata, flattened
    duration_ms: int                  # wall-clock for the call
    finish_reason: str = ""           # stop / tool_calls / length / etc.

    def to_json(self) -> dict:
        """Plain-dict form for JSON responses. Messages go through
        `serialize_messages` so LangChain message objects (which FastAPI
        can't json-serialise directly) become safe dicts."""
        return {
            "ts": self.ts,
            "iteration": self.iteration,
            "model": self.model,
            "request_messages": serialize_messages(self.request_messages),
            "response": (
                serialize_messages([self.response])[0]
                if not isinstance(self.response, dict)
                else self.response
            ),
            "usage": dict(self.usage) if self.usage else {},
            "duration_ms": self.duration_ms,
            "finish_reason": self.finish_reason,
        }


class LLMTraceStore:
    """Per-session ring buffer of LLMCallRecord. Process-wide singleton
    shared across all requests. Keys are session_id."""

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


# Lazy module-level singleton.
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
    """Plain-dict conversion of a LangChain message for JSON output.
    Falls back to manual attr read if model_dump() isn't available."""
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
    """Map a LangChain message class to its short role name (AIMessage →
    'assistant', HumanMessage → 'user', etc). Class name is the most
    reliable signal — `type` is overloaded."""
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
