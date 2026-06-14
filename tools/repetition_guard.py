"""
Repetition guard — server-side detector for stuck tool-call loops.

When the agent (which is autonomous — no human in the loop to break a
loop) re-issues the same tool call with the same args multiple times,
every additional call is wasted context: another model invocation costs
~12.7K cache_read on top of accumulated history. The clearest signal of
"the agent is stuck" is the same call N times in a row.

This module keeps a tiny in-memory ledger keyed by
`(session_id, tool_name, args_fingerprint)`. After `THRESHOLD` identical
calls, the next call's result is prefixed with a directive that points
the agent at the next-best action (`bash ls` for directories, etc.) —
turning an unbounded loop into a single correction step.

Threading: this is a process-local dict, guarded by a lock. Safe across
asyncio tasks (which already serialize per-thread) and the executor
thread that runs the agent graph (which inherits the per-session
ContextVar from session_runner). The session_id comes from
`tools.sandbox.active_session_id()` so we don't need a new ContextVar.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field


# After this many IDENTICAL calls in one session, prefix the result with
# a "you're stuck" notice. 3 means: 2 wasted calls + 1 corrective. Tuned
# to catch the 9x read_file('/home') loop without nagging on legitimate
# 2-call patterns (e.g. read-then-edit).
THRESHOLD = 3

# How long an entry lives before the count is forgotten. 30 minutes is
# long enough to catch a 10-call loop on a slow build, short enough that
# a later "build me a SECOND app" in the same session doesn't inherit
# a high count from the first build.
ENTRY_TTL_SECS = 30 * 60

# Maximum entries to retain across all sessions. Bounded to keep the
# dict small even if a single long-running session churns through many
# distinct (tool, args) pairs. When over the cap, the oldest entry is
# evicted.
MAX_ENTRIES = 2048


@dataclass
class _Record:
    tool: str
    fingerprint: str
    count: int = 0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_args_repr: str = ""  # for the directive message


_lock = threading.Lock()
_calls: dict[tuple[str, str], _Record] = {}


def _fingerprint_args(args: dict) -> str:
    """Stable hash of the tool's input args.

    We strip whitespace and lowercase string values so that
    `read_file("/home")` and `read_file("/home ")` collapse to the same
    key — minor formatting variations from the model shouldn't bypass
    the guard. Args are sorted so dict ordering is irrelevant.
    """
    def _norm(v):
        if isinstance(v, str):
            return re.sub(r"\s+", " ", v.strip()).lower()
        if isinstance(v, dict):
            return {k: _norm(val) for k, val in sorted(v.items())}
        if isinstance(v, list):
            return [_norm(x) for x in v]
        return v
    normalized = {k: _norm(val) for k, val in sorted(args.items())
                  if not k.startswith("_")}
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _suggest_next_action(tool: str, args: dict) -> str:
    """One-line nudge: when the agent is stuck on a tool, point at the
    next-best action. Keeps the directive short and concrete."""
    path = ""
    if isinstance(args, dict):
        for k in ("path", "file_path", "filepath", "target", "command"):
            v = args.get(k)
            if isinstance(v, str) and v:
                path = v
                break
    if tool == "read_file":
        if path:
            return (f"`{path}` is a directory or unreadable as a file. "
                    f"Use `bash ls {path}` to list its contents, then "
                    f"`read_file` on a specific file inside it.")
        return "use `bash ls <path>` to list directory contents."
    if tool in ("edit_file", "write_file"):
        return (f"`{path}` keeps failing — re-read the file with "
                f"`read_file {path}` to confirm its current state, then "
                f"edit again with the exact `old_string` from that read.")
    if tool == "bash":
        return (f"this exact bash command has failed several times. "
                f"Inspect the error, change the command, and try a "
                f"different approach.")
    return f"this exact `{tool}` call has repeated. Try a different approach."


def check_and_record(
    session_id: str | None,
    tool_name: str,
    args: dict,
) -> str | None:
    """Bump the count for `(session_id, tool, args)` and return a
    directive notice if the call is over the threshold, else None.

    The notice is meant to be PREPENDED to the tool's normal result —
    the model sees both its old error and the new hint in the same
    tool-result message, so it can recover in one step.
    """
    fingerprint = _fingerprint_args(args)
    key = (session_id or "_global", tool_name, fingerprint)
    args_repr = ", ".join(f"{k}={v!r}" for k, v in args.items()
                          if not k.startswith("_"))[:200]

    now = time.time()
    with _lock:
        # Garbage-collect expired entries (cheap to do on every call).
        if len(_calls) > MAX_ENTRIES or int(now) % 50 == 0:
            expired = [k for k, r in _calls.items()
                       if now - r.last_seen >= ENTRY_TTL_SECS]
            for k in expired:
                _calls.pop(k, None)

        rec = _calls.get(key)
        if rec is None:
            rec = _Record(tool=tool_name, fingerprint=fingerprint)
            _calls[key] = rec
        rec.count += 1
        rec.last_seen = now
        rec.last_args_repr = args_repr
        count = rec.count

    if count <= THRESHOLD:
        return None

    # 4th+ call: emit a directive. Wording is chosen so the model
    # doesn't treat this as "retry harder" — it says "STOP, try this
    # different thing instead."
    suggestion = _suggest_next_action(tool_name, args)
    return (
        f"[repetition guard] you have called `{tool_name}({args_repr})` "
        f"{count} times in this session. {suggestion} "
        f"Do not call `{tool_name}` again on the same input until you "
        f"have changed your approach."
    )


def reset_session(session_id: str) -> None:
    """Drop all entries for a session. Called when a session is closed
    or after a manual reset — keeps the dict from accumulating dead
    sessions indefinitely."""
    with _lock:
        for k in list(_calls.keys()):
            if k[0] == session_id:
                _calls.pop(k, None)


def stats() -> dict:
    """Debug/observability — how many distinct call fingerprints are
    tracked, and how many are over threshold."""
    with _lock:
        over = sum(1 for r in _calls.values() if r.count > THRESHOLD)
        return {
            "tracked": len(_calls),
            "over_threshold": over,
            "threshold": THRESHOLD,
        }
