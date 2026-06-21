"""
Wall-clock guards for sync code paths that the per-chunk
`_stream_with_idle_timeout` watchdog (agents/nodes.py) can't see —
history prep, message assembly, LLM SDK request setup, post-LLM
bookkeeping, checkpoint writes.

Public surface:
  _call_with_wall_clock_guard(fn, timeout_s, label) -> result | raises

The agent loop previously had no upper bound on these regions: the
per-chunk watchdog only protected the LLM stream iteration itself,
so a hang in pre-stream (e.g. message-assembly on a 1.1 MB msgpack
checkpoint), post-stream bookkeeping, or a SQLite checkpoint write
that can't acquire `self.lock` would leave the turn alive but silent
forever. The two observed incidents ("test-final" / 19 min, and
"testing..!" / 6+ hours) both sit in these gaps.

The helper is a leaf module so both `agents.nodes` and
`memory.checkpointer` can import it without a cycle — the latter
already imports from `memory.checkpointer`, so it cannot import from
`agents.nodes`.
"""

from __future__ import annotations

import contextvars
import logging
import threading


# Env-var names. Match the existing family split:
#   AGENT_* — per-call knobs that the LLM-call author tunes
#     (see agents/nodes.py AGENT_LLM_TIMEOUT_SECS / _RETRY_* / _STREAM_IDLE_TIMEOUT_S)
#   OJAS_* — system-resource knobs the storage / memory layer tunes
#     (see memory/checkpointer.py OJAS_AUTO_COMPACT_INPUT_TOKENS / _TRUNCATE_TOOL_RESULT_AT)
NODE_BODY_TIMEOUT_ENV = "AGENT_NODE_BODY_TIMEOUT_S"
CHECKPOINT_WRITE_TIMEOUT_ENV = "OJAS_CHECKPOINT_WRITE_TIMEOUT_S"

# 10 min — 2x the existing AGENT_LLM_TIMEOUT_SECS=300 default. Catches
# pre-stream hangs AND slow-but-alive LLM streams the per-chunk
# _stream_with_idle_timeout (90s default) doesn't catch.
DEFAULT_NODE_BODY_TIMEOUT_S = 600.0

# 30s — a healthy SqliteSaver.put on a local DB takes <100ms (single
# row, single transaction, simple msgpack). 30s is 300x headroom for
# slow disk; anything past 30s is a lock conflict or stuck writer.
DEFAULT_CHECKPOINT_WRITE_TIMEOUT_S = 30.0

_log = logging.getLogger(__name__)


def _call_with_wall_clock_guard(fn, timeout_s: float, label: str):
    """Run a sync call in a daemon thread; raise TimeoutError if it
    exceeds `timeout_s` wall-clock.

    Used to bound sections of node_agent and CompactingCheckpointer.put
    that the per-chunk _stream_with_idle_timeout watchdog can't see
    (history prep, message assembly, LLM SDK request setup, post-LLM
    bookkeeping, checkpoint writes).

    On timeout, the worker thread is left running (daemon=True so it
    dies with the process). The hung underlying call — DB read, msgpack
    encode, provider socket — can't be interrupted from outside. Same
    caveat as _stream_with_idle_timeout's orphan worker
    (agents/nodes.py:738-742). Process exit is the only way to free the
    resource; the next agent turn resumes from the last good checkpoint.

    `label` appears in the TimeoutError message and the daemon thread
    name so journalctl / `pgrep -f` / `py-spy` can attribute the hang.

    IMPORTANT — ContextVar propagation: this guard wraps the body in a
    `threading.Thread`, but `contextvars.ContextVar` values do NOT
    propagate to plain Thread workers by default (they only propagate
    through `asyncio.run_in_executor()`). The agent's reporter lives
    in a ContextVar (`agents/reporter.py:_reporter_var`), so without the
    explicit `copy_context()` below every tool_done / assistant_text /
    token_update call inside the worker would silently no-op (the
    default reporter swallows them), and the WebSocket stream would
    show blank even though the agent ran to completion. `ctx.run(fn)`
    inside the thread ensures the worker inherits the calling
    ContextVar state.

    IMPORTANT: this is meant to wrap short-lived sync calls, not to
    protect callers against malicious or infinite loops in `fn`. The
    worker thread runs `fn` directly — if `fn` itself spawns further
    threads, those are NOT covered by the wall-clock budget.
    """
    # Snapshot the current ContextVar state on the parent thread.
    # Must happen on the caller side — once we're inside the daemon
    # thread, copy_context() would only see the worker's empty
    # context (which is exactly the bug we're fixing).
    ctx = contextvars.copy_context()
    holder: list = []

    def _target() -> None:
        try:
            # ctx.run executes `fn` with the captured ContextVar
            # values bound, so reporter / any other context-bound
            # state inside `fn` resolves correctly. Exceptions
            # raised by `fn` surface here unchanged.
            holder.append(("ok", ctx.run(fn)))
        except BaseException as e:  # noqa: BLE001 — any exception surfaces
            holder.append(("err", e))

    t = threading.Thread(target=_target, daemon=True, name=f"ojas-guard-{label}")
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        # Log the wall-clock breach on the main thread. The worker's
        # logging is racy with our timeout message and useless once the
        # worker is hung on a socket/DB call — it can't get back to
        # Python to emit anything. The WARNING is filterable in
        # journalctl: `journalctl -u ojas-backend | grep wall-clock-guard`
        _log.warning(
            "[wall-clock-guard] %s exceeded %.1fs — raising TimeoutError; "
            "worker thread %s left as orphan daemon",
            label, timeout_s, t.name,
        )
        raise TimeoutError(f"{label} exceeded wall-clock budget {timeout_s}s")
    if not holder:
        # Defensive — shouldn't happen. The thread either sets `holder`
        # (ok or err) or times out (t.is_alive()). If we got here the
        # thread exited cleanly without populating `holder` — fail loud
        # rather than return None, which would mask the actual bug.
        raise RuntimeError(
            f"{label} produced no result (worker thread exited cleanly with no return)"
        )
    kind, payload = holder[0]
    if kind == "err":
        raise payload
    return payload