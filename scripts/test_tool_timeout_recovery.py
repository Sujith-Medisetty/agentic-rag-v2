#!/usr/bin/env python3
"""
Tests for bash-timeout recovery.

The fix changes tools/bash.py:_timeout_output to format the result as
an explicit `Error:` with recovery options + a "do NOT send a final
summary" line, so the LLM retries instead of ending the turn.

Three checks:
  1. The rendered result starts with "Error:" and contains the recovery
     guidance + the command echo.
  2. The is_error check at agents/nodes.py:1457-1460 flags the result.
  3. The system prompt contains the "TOOL TIMEOUTS" section.

Run from the repo root with the venv active:
    source .venv/bin/activate
    python3 scripts/test_tool_timeout_recovery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.bash import _timeout_output


def _render_bash_result(out) -> str:
    """Replicate the wrapper's join logic at tools/wrappers.py:528-541.

    Note: the wrapper now skips the `[stderr]\\n` prefix when stderr
    already starts with `Error:`, so the rendered ToolMessage content
    starts with `Error:` directly. That keeps the prefix in the head of
    the message so the is_error check in agents/nodes.py:1458 catches it
    and the UI renders the result red.
    """
    parts = []
    if out.stdout:
        parts.append(out.stdout)
    if out.stderr:
        if out.stderr.startswith("Error:"):
            parts.append(out.stderr)
        else:
            parts.append(f"[stderr]\n{out.stderr}")
    if out.return_code_interpretation:
        parts.append(f"[{out.return_code_interpretation}]")
    return "\n".join(parts) or "(no output)"


def test_timeout_result_is_explicit_error() -> None:
    """The LLM-facing string must start with 'Error:' and include recovery
    guidance + a 'do NOT summarize' line + the command echo."""
    out = _timeout_output("sleep 999", 180_000)
    rendered = _render_bash_result(out)

    # Must start with Error: (the wrapper now leaves it as the head of
    # the rendered message so the is_error check catches it).
    assert rendered.startswith("Error:"), (
        f"timeout result must start with 'Error:'; got first 80 chars: "
        f"{rendered[:80]!r}"
    )

    # Both recovery paths must be present.
    assert "run_in_background" in rendered, "missing 'run_in_background' recovery option"
    assert "timeout=600000" in rendered, "missing 'timeout=600000' recovery option"

    # The guard against the reported symptom.
    low = rendered.lower()
    assert "do not" in low, "missing 'do not send a final summary' guard"

    # Command echo so the LLM can identify which call timed out.
    assert "sleep 999" in rendered, "command echo missing"

    # Telemetry / control fields unchanged.
    assert out.interrupted is True
    assert out.return_code_interpretation == "timeout"
    assert out.stdout == ""

    print("ok  test_timeout_result_is_explicit_error")


def test_timeout_flagged_as_error_by_node_tools() -> None:
    """The is_error check at agents/nodes.py:1457-1460 must flag the new
    timeout result so the UI renders it red, not as a silent success."""
    out = _timeout_output("anything", 180_000)
    rendered = _render_bash_result(out)

    first_line = rendered.strip().splitlines()[0] if rendered.strip() else ""
    is_error = (
        rendered.startswith("Error:")
        or rendered.startswith("BLOCKED:")
        or "error" in first_line.lower()[:20]
    )
    assert is_error is True, (
        f"timeout result not flagged as error; first_line={first_line!r}, "
        f"first 60 chars={rendered[:60]!r}"
    )

    print("ok  test_timeout_flagged_as_error_by_node_tools")


def test_prompt_has_timeout_recovery_section() -> None:
    """The system prompt must include the 'TOOL TIMEOUTS' section so the
    LLM has the recovery pattern in its pre-trained context."""
    from pathlib import Path
    from agents.prompt import ProjectContext, SystemPromptBuilder

    # Build a minimal prompt with the builder — skip ProjectContext so
    # we don't trigger filesystem discovery (the test should be hermetic).
    builder = SystemPromptBuilder()
    sections = builder.build()
    prompt = "\n".join(sections)

    # Section header (case-sensitive — must match the inserted text).
    assert "TOOL TIMEOUTS" in prompt, "missing 'TOOL TIMEOUTS' section header"

    # The guard against the reported symptom (case-insensitive — the
    # prompt capitalizes "Do NOT" at the start of the sentence).
    low = prompt.lower()
    assert "do not send a final summary" in low, (
        "missing 'do not send a final summary' guard in prompt"
    )

    # Both recovery paths must be present.
    assert "run_in_background" in prompt, "missing 'run_in_background' in prompt"
    assert "timeout=" in prompt, "missing 'timeout=' parameter in prompt"
    assert "600000" in prompt, "missing '600000' (max timeout) in prompt"

    print("ok  test_prompt_has_timeout_recovery_section")


if __name__ == "__main__":
    test_timeout_result_is_explicit_error()
    test_timeout_flagged_as_error_by_node_tools()
    test_prompt_has_timeout_recovery_section()
    print("\nall tool-timeout-recovery tests passed")
