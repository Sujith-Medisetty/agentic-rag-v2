"""
Bash tool — execute shell commands.

Handles subprocess spawning, timeout, background execution.
Safety validation happens in safety/bash_validator.py before this runs.

PROCESS-SAFETY HARD GUARD (added Jun 2026 after the agent ran
`fuser -k 8765/tcp` and killed its own parent backend):
A small allowlist-style check below refuses any command that tries to
kill a process bound to the Ojas backend's port, or that uses
`fuser -k` / `pkill` / `killall` against the parent. The system prompt
also tells the agent to NEVER kill processes — pick a different free
port instead. The hard guard is belt-and-braces in case the model
ignores the prompt.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass


# Per-stream ceiling before we hand off to the wrapper for smart truncation.
# The wrapper applies a small head+tail cap inline (≈4 KB) and spills the
# full output to a session-scoped temp file. This 1 MB cap is just an
# OOM guardrail for the rare command that dumps gigabytes (e.g. `cat /dev/urandom`).
_MAX_RAW_OUTPUT_BYTES = 1_048_576  # 1 MiB per stream


@dataclass
class BashInput:
    command: str
    timeout: int | None = None             # milliseconds; None = no timeout
    description: str | None = None
    run_in_background: bool = False


@dataclass
class BashOutput:
    stdout: str
    stderr: str
    interrupted: bool = False
    background_task_id: str | None = None
    no_output_expected: bool | None = None
    return_code_interpretation: str | None = None
    structured_content: list[dict] | None = None


# ---------------------------------------------------------------------------
# Process-safety hard guard
# ---------------------------------------------------------------------------
# Ports the agent must never touch with a kill-family verb. 8765 is the
# parent Ojas backend's uvicorn port — `fuser -k 8765/tcp` literally killed
# our own service in production. The list is extensible (commas / spaces /
# env override).

def _protected_ports() -> set[int]:
    raw = os.getenv("OJAS_PROTECTED_PORTS", "8765")
    out: set[int] = set()
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            continue
    return out or {8765}


# Kill-family verbs we look for. Detection is intentionally generous —
# easier to false-positive a `fuser -k` than to miss one.
_KILL_VERBS = re.compile(
    r"\b(fuser\s+[^|;&]*-k|kill\b|pkill\b|killall\b)",
    re.IGNORECASE,
)


def _check_self_destruct(command: str) -> str | None:
    """Return a block-reason string if the command would (or could) kill a
    protected process. Return None if safe.

    Triggers:
      1. ANY `fuser -k` is refused outright (the agent has no business
         doing this — pick a different port instead).
      2. ANY `pkill`, `killall`, or `kill -9` on the WHOLE host is refused
         (we can't tell from a string whether the target is our backend or
         a child of it; safer to block all of them than to whitelist).
      3. ANY combination of a kill-family verb + a protected port number
         (8765 by default) anywhere in the command — extra defence layer.

    The agent can still terminate its OWN children via `kill <pid>` with a
    specific pid it spawned itself; we only refuse pkill/killall (broad
    targeting) and fuser -k (port-based targeting), which are the two
    that can hit the parent.
    """
    cmd = command.strip()
    if not cmd:
        return None

    # (1) fuser -k anywhere → block
    if re.search(r"\bfuser\s+[^|;&]*-k\b", cmd, re.IGNORECASE):
        return (
            "Refused: `fuser -k` is forbidden — it can kill the Ojas "
            "backend (PID bound to port 8765). If a port is in use, pick "
            "a DIFFERENT free port for your dev server instead of killing "
            "what's holding it."
        )

    # (2) pkill / killall anywhere → block (broad targeting, can hit
    # parent uvicorn). Specific `kill <pid>` of a known child is OK.
    if re.search(r"\bpkill\b", cmd, re.IGNORECASE):
        return (
            "Refused: `pkill` is forbidden — pattern-matching kill can "
            "match the Ojas backend. To stop a dev server you started in "
            "this session, use `kill <pid>` with the specific pid from "
            "the bash output; better, start the server with "
            "`run_in_background=true` so the session-delete cleanup "
            "handles it automatically."
        )
    if re.search(r"\bkillall\b", cmd, re.IGNORECASE):
        return (
            "Refused: `killall` is forbidden — name-matching kill can "
            "match the Ojas backend (`killall uvicorn` would kill us). "
            "Use `kill <pid>` with a specific pid, or pick a different "
            "port for your dev server."
        )

    # (3) Any kill-family verb mentioning a protected port. Catches
    # creative `kill -9 $(lsof -ti :8765)` style commands.
    protected = _protected_ports()
    if _KILL_VERBS.search(cmd):
        for port in protected:
            # Look for the port number near a kill verb: ':8765', '8765/tcp',
            # 'port 8765', or bare '8765' near a kill verb.
            pat = rf"(:{port}\b|\b{port}/(?:tcp|udp)\b|\bport\s+{port}\b|\b{port}\b)"
            if re.search(pat, cmd):
                return (
                    f"Refused: command targets the Ojas backend port "
                    f"({port}) with a kill-family verb. Pick a different "
                    f"free port (try 3000-3999 or 5000-9999, just NOT "
                    f"{port}) instead of killing whatever is on it."
                )

    return None


def _self_destruct_output(reason: str) -> BashOutput:
    """Return a BashOutput representing a refused command, in the format
    the agent expects so it can read the reason and adapt."""
    return BashOutput(
        stdout="",
        stderr=reason,
        interrupted=False,
        return_code_interpretation="refused by Ojas process-safety guard",
    )


def execute_bash(input: BashInput) -> BashOutput:
    """Execute a shell command and return its output.

    Uses `sh -lc` (login shell) so the command sees the user's PATH,
    aliases, and shell environment the same way an interactive run would.

    The active session sandbox (if any) is consulted for `cwd` so commands
    run inside the session's workspace by default — `cd /tmp && rm -rf /`
    is no longer a footgun for a non-root session.

    Process-safety guard runs FIRST: any `fuser -k`, `pkill`, `killall`,
    or kill-family verb targeting a protected port (defaults to 8765, the
    Ojas backend) is refused before subprocess.run is even called.
    """
    # Hard guard against the agent killing its own parent.
    block = _check_self_destruct(input.command)
    if block is not None:
        return _self_destruct_output(block)
    if input.run_in_background:
        return _run_background(input.command)
    return _run_foreground(input.command, input.timeout)


# 60s was too tight: `npm install`, `pip install`, `vite build`, etc. routinely
# run 1-3 minutes and were getting killed mid-execution — the agent then burned
# iterations retrying. 180s default covers normal package installs and builds;
# the model can still pass a smaller timeout for fast commands or a bigger one
# (up to the hard ceiling) for slow installs. Override via env var.
def _default_bash_timeout_ms() -> int:
    try:
        return max(1_000, int(os.getenv("AGENT_BASH_DEFAULT_TIMEOUT_MS", "180000")))
    except ValueError:
        return 180_000

_HARD_BASH_TIMEOUT_MS    = 600_000  # 10m — absolute ceiling, even if the model asks for more


def _run_foreground(command: str, timeout_ms: int | None) -> BashOutput:
    """Run command, wait for it, capture output.

    Hard timeout: every foreground bash call gets a timeout. None / 0 →
    default 60s, any model-supplied value is clamped to [1, _HARD_BASH_TIMEOUT_MS].
    Without this, a command that waits on stdin or fills the pipe buffer
    deadlocks the worker thread and the whole agent turn goes silent — which is
    exactly the 14-minute hang we hit before. Better to fail loud at the
    timeout boundary and let the loop recover than to wait forever.
    """
    effective_ms = timeout_ms if timeout_ms else _default_bash_timeout_ms()
    effective_ms = max(1_000, min(effective_ms, _HARD_BASH_TIMEOUT_MS))
    timeout_secs = effective_ms / 1000.0

    try:
        result = subprocess.run(
            ["sh", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            # Close stdin so any command that tries to read from it (interactive
            # prompts, `read`, pagers like `less`) fails immediately instead of
            # blocking forever. Pairs with the timeout as belt-and-braces.
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
            # Anchor the cwd to the session's workspace if one is set. Root
            # users still inherit the process default (full filesystem).
            cwd=_sandbox_cwd(),
        )
    except subprocess.TimeoutExpired:
        return _timeout_output(command, effective_ms)

    stdout = _truncate_output(result.stdout)
    stderr = _truncate_output(result.stderr)
    no_output = (not stdout.strip()) and (not stderr.strip())
    rci = None
    if result.returncode != 0:
        rci = f"exit_code:{result.returncode}"

    return BashOutput(
        stdout=stdout,
        stderr=stderr,
        interrupted=False,
        no_output_expected=no_output,
        return_code_interpretation=rci,
    )


def _timeout_output(command: str, timeout_ms: int) -> BashOutput:
    """Structured timeout payload routed through the telemetry channel."""
    is_test = _is_test_command(command)
    rci = "test.hung" if is_test else "timeout"
    return BashOutput(
        stdout="",
        stderr=f"Command exceeded timeout of {timeout_ms} ms",
        interrupted=True,
        no_output_expected=True,
        return_code_interpretation=rci,
        structured_content=[_test_timeout_provenance(command, timeout_ms, is_test)],
    )


def _is_test_command(command: str) -> bool:
    """Detect whether `command` looks like a test invocation (pytest, npm
    test, cargo test, …). Used to classify timeouts as 'test hang' vs
    'command timeout' in the truncated-output telemetry."""
    normalized = " ".join(command.split()).lower()
    return any(
        marker in normalized
        for marker in (
            "cargo test",
            "cargo nextest",
            "npm test",
            "pnpm test",
            "yarn test",
            "pytest",
        )
    )


def _test_timeout_provenance(command: str, timeout_ms: int, is_test: bool) -> dict:
    """Classify a timed-out command for the telemetry channel —
    'test.hung' if it looked like a test, 'command.timeout' otherwise."""
    event = "test.hung" if is_test else "command.timeout"
    failure_class = "test_hang" if is_test else "timeout"
    classification = "test.hung" if is_test else "timeout"
    return {
        "event": event,
        "failureClass": failure_class,
        "data": {
            "command": command,
            "timeoutMs": timeout_ms,
            "provenance": "bash.timeout",
            "classification": classification,
        },
    }


def _run_background(command: str) -> BashOutput:
    """Spawn command in background, return immediately with task id. Also
    registers the PID against the active session (if any) so session
    delete + the admin endpoints can find / kill it later."""
    proc = subprocess.Popen(
        ["sh", "-lc", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
        cwd=_sandbox_cwd(),
    )
    # Best-effort process tracking so the root admin panel can see what's
    # running and so session-delete kills its children.
    try:
        from tools.sandbox import active_session_id
        sid = active_session_id()
        if sid is not None:
            from server import db
            db.register_process(sid, proc.pid, command, port=_guess_port(command))
    except Exception:
        pass
    return BashOutput(
        stdout="",
        stderr="",
        background_task_id=str(proc.pid),
        no_output_expected=True,
    )


def _sandbox_cwd() -> str | None:
    """Resolve the active sandbox's workspace as the cwd for spawned
    commands. Returns None when no sandbox is active (CLI / test) so
    subprocess inherits the parent's cwd."""
    try:
        from tools.sandbox import active_sandbox
        cfg = active_sandbox()
        if cfg is None:
            return None
        return str(cfg.workspace)
    except Exception:
        return None


def _guess_port(command: str) -> int | None:
    """Cheap heuristic: scan the command string for `--port N`, `-p N`,
    `:PORT`, or `PORT=N`. Used to surface preview/dev ports in the admin
    UI without parsing real stdout. Returns None if no number found."""
    import re
    patterns = [
        r"--port[= ](\d{2,5})",
        r"\b-p[= ](\d{2,5})",
        r"\bPORT[= ](\d{2,5})",
        r":(\d{2,5})\b",
    ]
    for pat in patterns:
        m = re.search(pat, command)
        if m:
            try:
                p = int(m.group(1))
                if 1 <= p <= 65535:
                    return p
            except ValueError:
                pass
    return None


def _truncate_output(s: str) -> str:
    """OOM guardrail only. Caps each stream at 1 MiB and appends a clear
    marker. Smart head+tail truncation happens in the bash wrapper, where
    the return code is available to weight head vs tail differently for
    failures vs successes, and the full output can be spilled to a
    session-scoped temp file the agent can read_file on demand."""
    encoded = s.encode("utf-8")
    if len(encoded) <= _MAX_RAW_OUTPUT_BYTES:
        return s
    end = _MAX_RAW_OUTPUT_BYTES
    while end > 0:
        try:
            truncated = encoded[:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        truncated = ""
    return truncated + f"\n\n[output capped at {_MAX_RAW_OUTPUT_BYTES} bytes — wrapper will truncate further]"
