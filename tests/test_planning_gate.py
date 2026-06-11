"""
Tests for the plan-mode pre-scan in agents/nodes.py.

`EnterPlanMode` flips a per-thread flag in RunnerState; the pre-scan in
`node_tools` honours that flag by stripping every write-tool call from
the last AIMessage and synthesising a `BLOCKED:` ToolMessage for each.
These tests exercise the pure-function helper directly so the policy is
covered without spinning up the full LangGraph runtime.

Run: `/opt/ojas/.venv/bin/python -m pytest tests/test_planning_gate.py -v`
or:  `/opt/ojas/.venv/bin/python tests/test_planning_gate.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the project importable when run directly (`python tests/test_*.py`).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from agents.nodes import (  # noqa: E402
    _classify_write_tools,
    _gate_for_plan_mode,
    _WRITE_LIKE_NAMES,
)
from tools.wrappers import (  # noqa: E402
    _drain_turn_flags,
    _set_turn_flag,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ai_with_calls(*calls: tuple[str, str]) -> AIMessage:
    """Build an AIMessage whose tool_calls is `[(id, name), ...]`."""
    return AIMessage(
        content="",
        tool_calls=[{"id": cid, "name": cname, "args": {}} for cid, cname in calls],
    )


def _state(plan_mode: bool, *calls: tuple[str, str]) -> dict:
    return {
        "plan_mode_active": plan_mode,
        "messages": [_ai_with_calls(*calls)] if calls else [],
    }


def _remaining_call_names(state: dict) -> list[str]:
    """Names of the tool_calls still on the last AIMessage (post-gate)."""
    last_ai = next(
        (m for m in reversed(state["messages"]) if isinstance(m, AIMessage)),
        None,
    )
    if not last_ai:
        return []
    return [tc["name"] for tc in (last_ai.tool_calls or [])]


def _blocked_in_state(state: dict) -> list[ToolMessage]:
    return [
        m for m in state["messages"]
        if isinstance(m, ToolMessage) and "BLOCKED" in str(m.content)
    ]


# ---------------------------------------------------------------------------
# Micro-cache (tool-body → node-agent handoff)
# ---------------------------------------------------------------------------

def test_micro_cache_set_and_drain():
    _set_turn_flag("thread-A", "plan_mode_active", True)
    pending = _drain_turn_flags("thread-A")
    assert pending == {"plan_mode_active": True}, pending
    # Second drain is empty (cleared after first).
    assert _drain_turn_flags("thread-A") == {}


def test_micro_cache_per_thread_isolation():
    _set_turn_flag("thread-A", "plan_mode_active", True)
    _set_turn_flag("thread-B", "plan_mode_active", False)
    assert _drain_turn_flags("thread-A") == {"plan_mode_active": True}
    assert _drain_turn_flags("thread-B") == {"plan_mode_active": False}
    # Both threads drained; neither leaks into the other.
    assert _drain_turn_flags("thread-A") == {}


def test_micro_cache_empty_thread_id_is_noop():
    _set_turn_flag("", "plan_mode_active", True)  # ignored
    assert _drain_turn_flags("") == {}


# ---------------------------------------------------------------------------
# Write-tool classification
# ---------------------------------------------------------------------------

def test_native_write_denylist_is_populated_at_import():
    # The five actual mutators. Utility + read tools must NOT be in here.
    for name in ("write_file", "edit_file", "bash", "git", "github"):
        assert name in _WRITE_LIKE_NAMES, f"{name} missing from import-time denylist"


def test_utility_and_read_tools_are_not_in_native_denylist():
    # These are all in `tools.wrappers.WRITE_TOOLS` but should NOT be
    # blocked in plan mode — they're not mutators.
    for name in (
        "TodoWrite", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion",
        "SendUserMessage", "Sleep", "ToolSearch", "WebFetch", "WebSearch",
        "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskStop",
        "TaskOutput",
    ):
        assert name not in _WRITE_LIKE_NAMES, (
            f"{name} is in the plan-mode denylist but is not a mutator"
        )


def test_classifier_adds_mcp_mutators():
    @tool
    def mcp__jupyter__NotebookEdit():
        """Edit a notebook cell."""
    @tool
    def mcp__jupyter__NotebookRead():
        """Read a notebook cell."""
    @tool
    def mcp__fs__read_file():
        """Read a file."""
    @tool
    def mcp__fs__write_file():
        """Write a file."""

    classified = _classify_write_tools([
        mcp__jupyter__NotebookEdit,
        mcp__jupyter__NotebookRead,
        mcp__fs__read_file,
        mcp__fs__write_file,
    ])
    assert "mcp__jupyter__NotebookEdit" in classified
    assert "mcp__fs__write_file" in classified
    # Read tools must NOT be flagged even if they live on a write-y server.
    assert "mcp__jupyter__NotebookRead" not in classified
    assert "mcp__fs__read_file" not in classified


# ---------------------------------------------------------------------------
# Pre-scan: plan-mode enforcement
# ---------------------------------------------------------------------------

def test_plan_mode_blocks_write_but_lets_read_through():
    state = _state(True, ("1", "edit_file"), ("2", "read_file"))
    synth = _gate_for_plan_mode(state)
    assert len(synth) == 1
    assert "edit_file" in synth[0].content
    assert _remaining_call_names(state) == ["read_file"]
    assert len(_blocked_in_state(state)) == 1


def test_plan_mode_off_does_not_block_writes():
    state = _state(False, ("1", "edit_file"))
    synth = _gate_for_plan_mode(state)
    assert synth == []
    assert _remaining_call_names(state) == ["edit_file"]
    assert _blocked_in_state(state) == []


def test_plan_mode_allows_planning_and_exit_tools():
    state = _state(
        True,
        ("1", "TodoWrite"),
        ("2", "ExitPlanMode"),
        ("3", "EnterPlanMode"),
        ("4", "AskUserQuestion"),
    )
    synth = _gate_for_plan_mode(state)
    assert synth == []
    assert _remaining_call_names(state) == [
        "TodoWrite", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion",
    ]


def test_plan_mode_blocks_bash_and_git_writes():
    state = _state(True, ("1", "bash"), ("2", "git"), ("3", "write_file"))
    synth = _gate_for_plan_mode(state)
    assert len(synth) == 3
    assert _remaining_call_names(state) == []


def test_prescan_is_idempotent():
    state = _state(True, ("1", "edit_file"))
    first = _gate_for_plan_mode(state)
    second = _gate_for_plan_mode(state)
    third = _gate_for_plan_mode(state)
    assert len(first) == 1
    assert second == []
    assert third == []
    # Only one BLOCKED message in state, not three.
    assert len(_blocked_in_state(state)) == 1


def test_prescan_noop_when_no_ai_message():
    state = {"plan_mode_active": True, "messages": []}
    assert _gate_for_plan_mode(state) == []


def test_prescan_noop_when_ai_has_no_tool_calls():
    state = {
        "plan_mode_active": True,
        "messages": [AIMessage(content="hello", tool_calls=[])],
    }
    assert _gate_for_plan_mode(state) == []


def test_prescan_blocked_message_has_matching_tool_call_id():
    state = _state(True, ("call-xyz", "edit_file"))
    synth = _gate_for_plan_mode(state)
    assert len(synth) == 1
    assert synth[0].tool_call_id == "call-xyz"


# ---------------------------------------------------------------------------
# Manual smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run as a plain script (no pytest required).
    import traceback
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
