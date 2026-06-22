"""
Per-chunk streaming watchdog.

This is the ONLY timeout left in the agent loop:
  * Streams from the LLM are wrapped in `_stream_with_idle_timeout` so a
    mid-stream stall (TCP freeze, provider deadlock, silent socket drop)
    raises TimeoutError after `AGENT_LLM_STREAM_IDLE_TIMEOUT_S` of no
    chunks. The LLM retry layer catches that and retries once.

Everything else — wall-clock guards around node bodies, iter caps, token
caps, checkpoint write budgets — was removed in the 2026-06-22 cleanup.
The provider timeout (`AGENT_LLM_TIMEOUT_SECS`, default 300s, set on the
httpx client) is still there but it's a wall-clock ceiling on the
*individual* HTTP request, not on the agent loop.

Tool execution timeouts live inside each tool (bash.py, file_ops.py, …)
and are not affected by this module.
"""

from __future__ import annotations

import queue as _queue
import threading as _threading
import time
import os
import sys


_LLM_DBG = os.getenv("AGENT_LLM_DEBUG", "").lower() in ("1", "true", "yes")
_LLM_TAG = "[llm-stream]"


def _llm_dbg(msg: str) -> None:
    if _LLM_DBG:
        print(f"{_LLM_TAG} {msg}", file=sys.stderr, flush=True)


def _stream_with_idle_timeout(stream_iter, idle_timeout_s: float):
    """Wrap a sync stream iterator with an idle-timeout watchdog.

    Sync streams can't be timed out cleanly with `asyncio.wait_for` (the
    iterator isn't a coroutine) and `signal.alarm` doesn't interrupt
    blocking network IO across threads. So we run the iterator in a
    daemon worker thread that pushes chunks onto a `queue.Queue`; the
    main thread pulls with `queue.get(timeout=idle_timeout_s)` and
    raises `TimeoutError` if no chunk arrives within the window.

    Note on the orphan worker: we can't actually cancel the underlying
    httpx request from the consumer side. When the consumer raises
    TimeoutError, the worker keeps pulling until the socket dies
    naturally (the 300s httpx total timeout will eventually fire). The
    worker is a daemon, so it dies with the agent loop process.
    """
    q: "_queue.Queue[tuple]" = _queue.Queue(maxsize=1024)
    _start_t = time.monotonic()
    _llm_dbg(f"START idle_timeout_s={idle_timeout_s} stream={type(stream_iter).__name__}")

    def _producer() -> None:
        try:
            for chunk in stream_iter:
                q.put(("chunk", chunk))
        except BaseException as e:  # noqa: BLE001 — surface to consumer
            q.put(("error", e))
        finally:
            q.put(("done", None))

    t = _threading.Thread(target=_producer, daemon=True, name="ojas-llm-stream")
    t.start()

    while True:
        try:
            kind, payload = q.get(timeout=idle_timeout_s)
        except _queue.Empty:
            _llm_dbg(
                f"idle {idle_timeout_s}s — no chunk received "
                f"(producer_alive={t.is_alive()})"
            )
            raise TimeoutError(
                f"LLM stream idle for {idle_timeout_s}s — no chunk received"
            )

        if kind == "done":
            return
        if kind == "error":
            # Re-raise the producer's exception verbatim — the retry layer
            # catches transient shapes and lets poison-pill errors bubble.
            raise payload

        yield payload