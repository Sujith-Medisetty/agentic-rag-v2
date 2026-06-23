"""
Process management — let a build session stop ONLY the dev/preview processes
IT spawned, without granting general kill access.

Why a dedicated tool instead of unblocking `kill` in bash: a raw `kill` (or
`pkill`/`fuser -k`) from inside a session can hit the Ojas backend (:8765),
the caddy reverse proxy, the main FE/BE/DB, or ANOTHER session's build — the
exact incidents the bash guards in safety/bash_validator.py + tools/bash.py
exist to prevent. Those guards stay fully in place (raw kill is still blocked
in every mode). This tool is the ONE sanctioned way to stop a process, and it
kills a pid/port ONLY when it can prove the target belongs to the CURRENT
session:

  ownership = processes registered for this session (every run_in_background
  spawn is recorded in session_processes) UNION their descendant pids (npm /
  vite / uvicorn fork the real listener under the `sh -lc` wrapper we
  recorded, so the actual port-holder is usually a child).

Anything outside that set — a protected port/pid, another session's process,
or a process this session never spawned — is refused. We also never kill by
process GROUP (the background procs share the agent's own group), only the
specific owned pids, so we can't take ourselves down.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time


# ---------------------------------------------------------------------------
# Process-tree helpers (portable: ps works on Linux + macOS)
# ---------------------------------------------------------------------------

def _pid_ppid_map() -> dict[int, int]:
    """pid -> ppid for every visible process. Empty dict if `ps` is missing."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                table[int(parts[0])] = int(parts[1])
            except ValueError:
                continue
    return table


def _descendants(roots: set[int], ppid_map: dict[int, int]) -> set[int]:
    """All transitive children of `roots` (roots themselves not included)."""
    children: dict[int, list[int]] = {}
    for pid, ppid in ppid_map.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        p = stack.pop()
        for c in children.get(p, []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def _listeners_on_port(port: int) -> set[int]:
    """PIDs listening on `port`. Tries lsof, then ss (Linux). Best-effort."""
    pids: set[int] = set()
    # lsof — available on Linux + macOS
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for tok in out.split():
            try:
                pids.add(int(tok))
            except ValueError:
                continue
    except (OSError, subprocess.SubprocessError):
        pass
    if pids:
        return pids
    # ss fallback (Linux) — parse `pid=NNN` out of the process column
    try:
        out = subprocess.run(
            ["ss", "-ltnp", "sport", "=", f":{port}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for m in re.finditer(r"pid=(\d+)", out):
            pids.add(int(m.group(1)))
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


# ---------------------------------------------------------------------------
# Ownership + protection
# ---------------------------------------------------------------------------

def _registered_pids(session_id: str) -> set[int]:
    """PIDs this session spawned via run_in_background (session_processes)."""
    try:
        from server import db
        return {int(p["pid"]) for p in db.list_processes_for_session(session_id)}
    except Exception:
        return set()


def _owned_pids(session_id: str) -> set[int]:
    """The full set of pids this session may stop: every pid it registered,
    plus all of their descendants (the real listeners forked under the
    recorded `sh -lc` wrapper)."""
    registered = _registered_pids(session_id)
    if not registered:
        return set()
    owned = set(registered)
    owned |= _descendants(registered, _pid_ppid_map())
    return owned


def _protected() -> tuple[set[int], set[int]]:
    """(protected_ports, protected_pids) — the Ojas backend/caddy/etc. that
    NO session may ever kill. Reuses the same sources as the bash guard."""
    try:
        from tools.bash import _protected_ports, _protected_pids
        return _protected_ports(), _protected_pids()
    except Exception:
        return {8765}, set()


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------

def _terminate(pids: set[int], protected_pids: set[int]) -> list[int]:
    """SIGTERM each pid (skipping protected ones), wait briefly, SIGKILL any
    survivor. Kills INDIVIDUAL pids only — never a process group — so a
    process sharing the agent's own group can't take the agent down. Returns
    the pids that were signalled."""
    targets = sorted(p for p in pids if p > 1 and p not in protected_pids)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    # Grace period for clean shutdown, then SIGKILL stragglers.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        alive = [p for p in targets if _alive(p)]
        if not alive:
            break
        time.sleep(0.1)
    for pid in targets:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    return targets


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _unregister(pids: set[int]) -> None:
    try:
        from server import db
        for pid in pids:
            db.unregister_process(pid)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public entry points (wrapped by the StopProcess tool / called at turn end)
# ---------------------------------------------------------------------------

def stop_all_session_processes(session_id: str) -> list[int]:
    """Stop EVERY process a session spawned (its registered pids + their
    descendants) — the end-of-turn cleanup for dev/debug servers the agent
    started while building. Skips protected pids defensively and clears the
    session's session_processes rows. Returns the pids signalled. Best-effort;
    callers run it in an executor so the SIGTERM grace period doesn't block
    the event loop.

    Unlike stop_process(), this needs no ownership proof — by definition every
    row in session_processes for `session_id` IS this session's, so we stop the
    lot. Protected pids (backend/caddy) are never in that table, but we filter
    them anyway as belt-and-braces.
    """
    if not session_id:
        return []
    registered = _registered_pids(session_id)
    if not registered:
        return []
    _, protected_pids = _protected()
    kill_set = set(registered) | _descendants(registered, _pid_ppid_map())
    killed = _terminate(kill_set, protected_pids)
    _unregister(set(registered))  # clear the rows even if a pid was already gone
    return killed


def stop_process(port: int | None = None, pid: int | None = None) -> dict:
    """Stop a dev/preview process THIS session started.

    - No args → list the processes this session can stop (discovery).
    - `port`  → stop whatever owned process is listening on that port.
    - `pid`   → stop that owned pid (and its children).

    Refuses protected ports/pids and anything this session didn't spawn.
    Returns a dict with `ok`, a human `message`, and details.
    """
    from tools.sandbox import active_session_id
    sid = active_session_id()
    if not sid:
        return {
            "ok": False,
            "message": "No active session — StopProcess only works inside a build session.",
        }

    protected_ports, protected_pids = _protected()
    owned = _owned_pids(sid)

    # Discovery mode — show what's stoppable.
    if port is None and pid is None:
        try:
            from server import db
            rows = db.list_processes_for_session(sid)
        except Exception:
            rows = []
        listing = [
            {"pid": r["pid"], "port": r.get("port"), "command": (r.get("command") or "")[:120]}
            for r in rows
        ]
        return {
            "ok": True,
            "message": (
                f"{len(listing)} process(es) this session started and can stop. "
                f"Call StopProcess(port=…) or StopProcess(pid=…). Protected ports "
                f"(never killable): {sorted(protected_ports)}."
            ),
            "processes": listing,
        }

    # Port mode.
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            return {"ok": False, "message": f"Invalid port {port!r}."}
        if port in protected_ports:
            return {
                "ok": False,
                "message": (
                    f"Refused: port {port} is a protected Ojas port (backend / "
                    f"reverse proxy). It is never killable from a build session. "
                    f"Use a different port for your dev server."
                ),
            }
        listeners = _listeners_on_port(port)
        if not listeners:
            return {"ok": True, "message": f"No process is listening on port {port}.", "killed": []}
        # Only listeners (or their ancestors) that this session owns.
        ours = {p for p in listeners if p in owned}
        # A listener whose ANCESTOR we own also counts (e.g. lsof reports the
        # node child but we registered the npm wrapper).
        if not ours:
            ppid_map = _pid_ppid_map()
            for lp in listeners:
                cur = lp
                hops = 0
                while cur > 1 and hops < 40:
                    if cur in owned:
                        ours.add(lp)
                        break
                    cur = ppid_map.get(cur, 0)
                    hops += 1
        foreign = listeners - ours
        if not ours:
            return {
                "ok": False,
                "message": (
                    f"Refused: port {port} is held by process(es) {sorted(listeners)} "
                    f"that THIS session did not start (another session, or a "
                    f"non-session service). You can only stop processes you "
                    f"spawned with run_in_background. Pick a different port."
                ),
            }
        # Kill the owned listeners + their descendants.
        kill_set = set(ours) | _descendants(ours, _pid_ppid_map())
        killed = _terminate(kill_set, protected_pids)
        _unregister(set(killed))
        msg = f"Stopped {len(killed)} process(es) on port {port} (pids {sorted(killed)})."
        if foreign:
            msg += f" Left {sorted(foreign)} running — not started by this session."
        return {"ok": True, "message": msg, "killed": killed}

    # PID mode.
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid pid {pid!r}."}
    if pid in protected_pids:
        return {
            "ok": False,
            "message": (
                f"Refused: pid {pid} is a protected Ojas process (backend / "
                f"reverse proxy) and is never killable from a build session."
            ),
        }
    if pid not in owned:
        return {
            "ok": False,
            "message": (
                f"Refused: pid {pid} was not started by this session. You can "
                f"only stop processes you spawned with run_in_background "
                f"(call StopProcess() with no args to list them)."
            ),
        }
    kill_set = {pid} | _descendants({pid}, _pid_ppid_map())
    killed = _terminate(kill_set, protected_pids)
    _unregister(set(killed))
    return {
        "ok": True,
        "message": f"Stopped pid {pid} and its children (pids {sorted(killed)}).",
        "killed": killed,
    }
