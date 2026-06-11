"""
Tests for the plan-mode pre-scan and TodoWrite heartbeat in agents/nodes.py.

Two graph-level guards are exercised here:
  1. `EnterPlanMode` flips a per-thread flag in RunnerState; the
     `_gate_for_plan_mode` pre-scan honours that flag by stripping every
     write-tool call from the last AIMessage and synthesising a
     `BLOCKED:` ToolMessage for each.
  2. The TodoWrite heartbeat (`_gate_for_todo_heartbeat`) counts
     non-TodoWrite tool calls and forces the model to call TodoWrite
     every N calls (default 5), so the plan panel stays in sync with
     what the agent is actually doing.

These tests exercise the pure-function helpers directly so the policy is
covered without spinning up the full LangGraph runtime.

Run: `/opt/ojas/.venv/bin/python -m pytest tests/test_planning_gate.py -v`
or:  `/opt/ojas/.venv/bin/python tests/test_planning_gate.py`
"""

from __future__ import annotations

import os
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
    _gate_for_todo_heartbeat,
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
# TodoWrite heartbeat (_gate_for_todo_heartbeat)
# ---------------------------------------------------------------------------

def _hb_state(counter: int, *calls: tuple[str, str]) -> dict:
    """Build a state for the heartbeat pre-scan. `counter` is the
    pre-existing value of `tools_since_last_todowrite`."""
    return {
        "tools_since_last_todowrite": counter,
        "messages": [_ai_with_calls(*calls)] if calls else [],
    }


def test_heartbeat_allows_calls_below_threshold():
    state = _hb_state(0, ("1", "bash"))
    synth = _gate_for_todo_heartbeat(state)
    assert synth == []
    assert state["tools_since_last_todowrite"] == 1
    assert _remaining_call_names(state) == ["bash"]


def test_heartbeat_allows_exactly_threshold_calls():
    # Counter at 4, one more call goes to 5 — still allowed.
    state = _hb_state(4, ("1", "bash"))
    synth = _gate_for_todo_heartbeat(state)
    assert synth == []
    assert state["tools_since_last_todowrite"] == 5


def test_heartbeat_blocks_call_at_threshold():
    # Counter at 5, next call should be blocked.
    state = _hb_state(5, ("1", "bash"))
    synth = _gate_for_todo_heartbeat(state)
    assert len(synth) == 1
    # Blocked call does NOT increment the counter (it didn't run).
    assert state["tools_since_last_todowrite"] == 5
    # The call is stripped from the AIMessage.
    assert _remaining_call_names(state) == []


def test_heartbeat_blocked_message_includes_counter():
    state = _hb_state(7, ("1", "bash"))
    synth = _gate_for_todo_heartbeat(state)
    assert len(synth) == 1
    # The message should mention the count so the model knows how long
    # it's been. We accept any reasonable phrasing as long as the number
    # is present.
    assert "7" in synth[0].content
    assert "BLOCKED" in synth[0].content
    assert "TodoWrite" in synth[0].content


def test_heartbeat_todowrite_always_allowed_and_does_not_increment():
    # TodoWrite is the reset signal. It must always go through (we want
    # the model to be able to fix the situation), and the heartbeat
    # counter increment for it is wrong (the drain zeroes it via the
    # sentinel — incrementing here would partially undo that).
    state = _hb_state(5, ("1", "TodoWrite"))
    synth = _gate_for_todo_heartbeat(state)
    assert synth == []
    assert state["tools_since_last_todowrite"] == 5  # unchanged
    assert _remaining_call_names(state) == ["TodoWrite"]


def test_heartbeat_blocks_only_first_call_in_message():
    # When the threshold is hit and the AIMessage has multiple calls,
    # we block the first non-TodoWrite call only — the rest are
    # allowed through so the model has somewhere to react. The model
    # will see the BLOCKED in the next iteration and call TodoWrite
    # (or ExitPlanMode, etc.) in response.
    state = _hb_state(5, ("1", "bash"), ("2", "read_file"), ("3", "write_file"))
    synth = _gate_for_todo_heartbeat(state)
    assert len(synth) == 1
    # bash is blocked, the rest are allowed.
    assert _remaining_call_names(state) == ["read_file", "write_file"]
    # Counter: 5 (no increment for blocked) + 2 (read_file + write_file) = 7.
    assert state["tools_since_last_todowrite"] == 7


def test_heartbeat_does_not_increment_for_blocked_calls():
    # A single threshold-hit should not snowball — the blocked call
    # doesn't count, so the next allowed call is also at the threshold,
    # not threshold+1.
    state = _hb_state(5, ("1", "bash"))
    _gate_for_todo_heartbeat(state)
    assert state["tools_since_last_todowrite"] == 5
    # Run the pre-scan again on the SAME state (idempotency).
    _gate_for_todo_heartbeat(state)
    assert state["tools_since_last_todowrite"] == 5


def test_heartbeat_threshold_zero_disables(monkeypatch=None):
    # Simulate OJAS_TODO_HEARTBEAT=0. We test by patching the threshold
    # directly because the module-level constant is captured at import.
    import agents.nodes as nodes_mod
    original = nodes_mod._HEARTBEAT_THRESHOLD
    nodes_mod._HEARTBEAT_THRESHOLD = 0
    try:
        state = _hb_state(100, ("1", "bash"), ("2", "write_file"))
        synth = _gate_for_todo_heartbeat(state)
        assert synth == []
        # Counter still increments even when disabled? No — the function
        # returns early when threshold <= 0, so the counter is unchanged.
        # (Not a useful state in practice; the disabled mode is meant
        # for "trust the prompt" debugging.)
    finally:
        nodes_mod._HEARTBEAT_THRESHOLD = original


def test_heartbeat_composes_with_plan_mode():
    # Plan mode on (blocks writes) AND heartbeat at threshold (blocks
    # the first surviving non-TodoWrite). Both rules fire; both
    # BLOCKED messages land in state. The AIMessage has two calls:
    # write_file (a write, plan-mode blocks it) and read_file (a read,
    # plan-mode lets it through, but heartbeat then blocks it because
    # the counter is at the threshold).
    state = {
        "plan_mode_active": True,
        "tools_since_last_todowrite": 5,
        "messages": [_ai_with_calls(
            ("1", "write_file"),
            ("2", "read_file"),
        )],
    }
    _gate_for_plan_mode(state)
    _gate_for_todo_heartbeat(state)
    blocked = _blocked_in_state(state)
    # 1 from plan-mode (write_file) + 1 from heartbeat (read_file).
    assert len(blocked) == 2
    assert any("write_file" in str(m.content) for m in blocked)
    assert any("TodoWrite" in str(m.content) for m in blocked)
    # Nothing survives — both rules fired.
    assert _remaining_call_names(state) == []


def test_heartbeat_no_ai_message_is_noop():
    state = {"tools_since_last_todowrite": 5, "messages": []}
    assert _gate_for_todo_heartbeat(state) == []


def test_heartbeat_no_tool_calls_is_noop():
    state = {
        "tools_since_last_todowrite": 5,
        "messages": [AIMessage(content="hello", tool_calls=[])],
    }
    assert _gate_for_todo_heartbeat(state) == []
    # Counter is unchanged because the function returns early.
    assert state["tools_since_last_todowrite"] == 5


def test_heartbeat_default_counter_when_key_missing():
    # State with no `tools_since_last_todowrite` key — TypedDict
    # total=False, so .get(..., 0) must return 0 and the function must
    # not blow up.
    state = {"messages": [_ai_with_calls(("1", "bash"))]}
    synth = _gate_for_todo_heartbeat(state)
    assert synth == []
    assert state["tools_since_last_todowrite"] == 1


def test_heartbeat_full_lifecycle_matches_session_reality():
    # Simulates the kind of session the user reported: 5 reads/bashes,
    # 1 TodoWrite, 5 more, another TodoWrite, etc. Verifies the
    # counter is in the expected state at each checkpoint.
    state = {"tools_since_last_todowrite": 0, "messages": []}

    # Block 1: 5 reads, then TodoWrite is forced on the 6th.
    for i in range(5):
        state["messages"] = [_ai_with_calls((str(i), "read_file"))]
        synth = _gate_for_todo_heartbeat(state)
        assert synth == [], f"unexpected block at iteration {i}"
    # 6th call would be blocked.
    state["messages"] = [_ai_with_calls(("6", "write_file"))]
    synth = _gate_for_todo_heartbeat(state)
    assert len(synth) == 1  # blocked
    # Counter unchanged at 5 because the blocked call didn't run.
    assert state["tools_since_last_todowrite"] == 5

    # The model calls TodoWrite (drain zeroes the counter for the next
    # AIMessage).
    state["messages"] = [_ai_with_calls(("7", "TodoWrite"))]
    synth = _gate_for_todo_heartbeat(state)
    assert synth == []
    # Counter unchanged in this AIMessage; the drain happens in
    # node_agent and materialises the reset on the next iteration.
    # The pre-scan here is a no-op for TodoWrite; the test of the
    # drain lives in the integration flow, not in this unit test.

    # Sanity: after 5 more calls, the cycle repeats.
    state["tools_since_last_todowrite"] = 0  # simulate the drain
    for i in range(5):
        state["messages"] = [_ai_with_calls((str(i), "bash"))]
        synth = _gate_for_todo_heartbeat(state)
        assert synth == [], f"unexpected block at iteration {i+5}"
    state["messages"] = [_ai_with_calls(("x", "write_file"))]
    synth = _gate_for_todo_heartbeat(state)
    assert len(synth) == 1


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
