"""
Bash tool — execute shell commands.
Ported from Rust: runtime/src/bash.rs

Handles subprocess spawning, timeout, background execution.
Safety validation happens in safety/bash_validator.py before this runs.
"""

import os
import subprocess
from dataclasses import dataclass, field


# Matches Rust runtime/src/bash.rs MAX_OUTPUT_BYTES = 16_384.
MAX_OUTPUT_BYTES = 16_384


@dataclass
class BashInput:
    command: str
    timeout: int | None = None             # milliseconds; None = no timeout (Rust parity)
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


def execute_bash(input: BashInput) -> BashOutput:
    """
    Execute a shell command and return its output.
    Ported from Rust: runtime/src/bash.rs execute_bash().

    Uses `sh -lc` (login shell) to match Rust's prepare_command/prepare_tokio_command.
    """
    if input.run_in_background:
        return _run_background(input.command)

    return _run_foreground(input.command, input.timeout)


def _run_foreground(command: str, timeout_ms: int | None) -> BashOutput:
    """
    Run command, wait for it, capture output.
    Rust: execute_bash_async(). When timeout is None, runs without timeout.
    """
    timeout_secs = (timeout_ms / 1000.0) if timeout_ms is not None else None

    try:
        result = subprocess.run(
            ["sh", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            env=os.environ.copy(),
        )

        stdout = _truncate_output(result.stdout)
        stderr = _truncate_output(result.stderr)

        no_output = (not stdout.strip()) and (not stderr.strip())
        rci = None
        code = result.returncode
        if code != 0:
            rci = f"exit_code:{code}"

        return BashOutput(
            stdout=stdout,
            stderr=stderr,
            interrupted=False,
            no_output_expected=no_output,
            return_code_interpretation=rci,
        )

    except subprocess.TimeoutExpired:
        return _timeout_output(command, timeout_ms or 0)


def _timeout_output(command: str, timeout_ms: int) -> BashOutput:
    """Build the structured timeout output to match Rust's timeout_output()."""
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
    """Mirrors Rust is_test_command()."""
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
    """Mirrors Rust test_timeout_provenance()."""
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
    """
    Spawn command in background, return immediately with task id.
    Rust: run_in_background branch in execute_bash().
    """
    proc = subprocess.Popen(
        ["sh", "-lc", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    return BashOutput(
        stdout="",
        stderr="",
        background_task_id=str(proc.pid),
        no_output_expected=True,
    )


def _truncate_output(s: str) -> str:
    """
    Truncate output to MAX_OUTPUT_BYTES, appending the marker when trimmed.
    Mirrors Rust truncate_output() at runtime/src/bash.rs.
    """
    encoded = s.encode("utf-8")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return s
    end = MAX_OUTPUT_BYTES
    while end > 0:
        try:
            truncated = encoded[:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        truncated = ""
    return truncated + f"\n\n[output truncated — exceeded {MAX_OUTPUT_BYTES} bytes]"
