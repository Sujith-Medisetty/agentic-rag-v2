"""
Per-chunk streaming watchdog.

This is the ONLY timeout left in the agent loop:
  * Streams from the LLM are wrapped in `_stream_with_idle_timeout` so a
    mid-stream stall (TCP freeze, provider deadlock, silent socket drop)
    raises TimeoutError after `AGENT_LLM_STREAM_IDLE_TIMEOUT_S` of no
    chunks. The LLM retry layer catches that and retries (up to
    AGENT_LLM_RETRY_ATTEMPTS).

Everything else — wall-clock guards around node bodies, iter caps, token
caps, checkpoint write budgets, httpx total timeouts — was removed in
the 2026-06-22 cleanup. This single watchdog is the only timeout on
the LLM call.

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

    Orphan-worker containment: we can't forcibly cancel the underlying
    httpx request, but when the consumer abandons us (TimeoutError, the
    generator being closed, or an exception downstream) the `finally`
    below sets a stop Event. The producer checks it between chunks and on
    every bounded `put`, so it stops pulling and returns within ~0.5s
    instead of blocking forever on a full queue holding a socket + thread.
    Returning from the producer also closes the wrapped iterator, which
    releases the provider connection. Without this, repeated retries each
    leaked a stuck producer thread until process exit (there is no httpx
    total timeout to reap them).
    """
    q: "_queue.Queue[tuple]" = _queue.Queue(maxsize=1024)
    stop = _threading.Event()
    _start_t = time.monotonic()
    _llm_dbg(f"START idle_timeout_s={idle_timeout_s} stream={type(stream_iter).__name__}")

    def _put(item) -> bool:
        """Bounded put that bails out if the consumer has abandoned us."""
        while not stop.is_set():
            try:
                q.put(item, timeout=0.5)
                return True
            except _queue.Full:
                continue
        return False

    def _producer() -> None:
        try:
            for chunk in stream_iter:
                if stop.is_set():
                    break
                if not _put(("chunk", chunk)):
                    break
        except BaseException as e:  # noqa: BLE001 — surface to consumer
            if not stop.is_set():
                _put(("error", e))
        finally:
            # Best-effort sentinel; ignored if the consumer is already gone.
            if not stop.is_set():
                _put(("done", None))

    t = _threading.Thread(target=_producer, daemon=True, name="ojas-llm-stream")
    t.start()

    try:
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
    finally:
        # Consumer is done with us (normal return, TimeoutError, generator
        # close, or a downstream exception): signal the producer to stop so it
        # can't keep pulling on an abandoned stream.
        stop.set()