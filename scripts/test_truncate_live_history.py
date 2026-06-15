#!/usr/bin/env python3
"""
Test for `_truncate_live_history` recent-observation preservation.

Regression test for the bug fixed in commit <truncate-cap-fix>:
the 800-char tool-result cap was collapsing the agent's immediate
post-write verification reads to a one-line stub, so the agent
concluded the on-disk file was corrupt (when actually it was fine)
and ran sed/python "repairs" that introduced real corruption.

The fix preserves the last K (default 4) ToolMessage bodies verbatim
regardless of size. Older observations still get the cap applied.

Run from /opt/ojas with the venv active:
    source .venv/bin/activate
    python3 scripts/test_truncate_live_history.py
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.nodes import _truncate_live_history
from memory.checkpointer import _truncate_tool_result


def _assert_eq(actual, expected, msg: str) -> None:
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def test_recent_observation_preserved_verbatim() -> None:
    """The most-recent K=4 ToolMessages must NOT be collapsed, even
    when their body exceeds TOOL_RESULT_TRUNCATE_AT_CHARS.

    Pre-fix behaviour: the latest 16KB Read result was collapsed to
    a one-line stub. The agent then read the stub, concluded the
    file was corrupt, and ran sed/python repairs that introduced
    real corruption. Post-fix: the latest Read result is preserved
    verbatim so the agent can see its own write."""
    big_body = 'className="flex"\n' * 2000  # ~34KB
    assert len(big_body) > 4000, "test body must exceed the 4000-char cap"

    messages = [
        HumanMessage(content="build me a todo app"),
        AIMessage(content="thinking", tool_calls=[{"id": "tc1", "name": "write_file", "args": {}}]),
        ToolMessage(content="Created: /tmp/foo.tsx (34000 bytes)", tool_call_id="tc1"),
        AIMessage(content="let me verify", tool_calls=[{"id": "tc2", "name": "read_file", "args": {}}]),
        # The 16KB Read result — this is what the agent used to see
        # collapsed to a 1-line stub.
        ToolMessage(content=big_body, tool_call_id="tc2"),
    ]

    out = _truncate_live_history(messages)
    tool_messages = [m for m in out if isinstance(m, ToolMessage)]
    _assert_eq(len(tool_messages), 2, "tool message count")

    # The most-recent ToolMessage must be the verbatim 34KB body.
    _assert_eq(
        tool_messages[-1].content,
        big_body,
        "most-recent ToolMessage was collapsed — recent-observation "
        "preservation is broken; agent will mis-diagnose this as "
        "file corruption",
    )

    # The older ToolMessage (a short "Created: ..." string) is also
    # preserved because it's < the 4000-char cap.
    _assert_eq(
        tool_messages[0].content,
        "Created: /tmp/foo.tsx (34000 bytes)",
        "older short ToolMessage should pass through",
    )


def test_old_observations_still_capped() -> None:
    """Observations older than the most-recent K=4 still get the
    per-message cap applied. The cap is the long-term budget
    safety net; it should still trigger for genuinely large
    observations from many turns ago."""
    big_body = "x" * 5000  # 5KB, over the 4000-char cap
    messages = [
        HumanMessage(content="hi"),
        # Five tool messages, all big. Only the last 4 should be
        # preserved verbatim; the first one gets the cap.
        AIMessage(content="1", tool_calls=[{"id": "tc1", "name": "bash", "args": {}}]),
        ToolMessage(content=big_body, tool_call_id="tc1"),
        AIMessage(content="2", tool_calls=[{"id": "tc2", "name": "bash", "args": {}}]),
        ToolMessage(content=big_body, tool_call_id="tc2"),
        AIMessage(content="3", tool_calls=[{"id": "tc3", "name": "bash", "args": {}}]),
        ToolMessage(content=big_body, tool_call_id="tc3"),
        AIMessage(content="4", tool_calls=[{"id": "tc4", "name": "bash", "args": {}}]),
        ToolMessage(content=big_body, tool_call_id="tc4"),
        AIMessage(content="5", tool_calls=[{"id": "tc5", "name": "bash", "args": {}}]),
        ToolMessage(content=big_body, tool_call_id="tc5"),
    ]

    out = _truncate_live_history(messages)
    tool_messages = [m for m in out if isinstance(m, ToolMessage)]
    _assert_eq(len(tool_messages), 5, "tool message count")

    # The first (oldest) one should be collapsed.
    first = tool_messages[0].content
    assert first.startswith("[output truncated:"), (
        f"oldest ToolMessage should be collapsed, got: {first[:80]!r}"
    )

    # The last 4 should be verbatim.
    for i in (1, 2, 3, 4):
        _assert_eq(
            tool_messages[i].content,
            big_body,
            f"recent ToolMessage[{i}] should be preserved verbatim",
        )


def test_no_tool_messages_passes_through() -> None:
    """Edge case: an empty history. Should return the same list
    unchanged (no tool messages to cap)."""
    messages = [
        HumanMessage(content="hi"),
        AIMessage(content="thinking"),
    ]
    out = _truncate_live_history(messages)
    _assert_eq(len(out), 2, "no-tool-message history passes through")
    _assert_eq(out[0].content, "hi", "human content preserved")
    _assert_eq(out[1].content, "thinking", "ai content preserved")


def test_below_cap_passes_through() -> None:
    """A small ToolMessage body (<4000 chars) should pass through
    verbatim, regardless of where it is in the history. The cap
    only applies when the body is genuinely too large."""
    small_body = "ok" * 100  # 200 chars
    messages = [
        AIMessage(content="1", tool_calls=[{"id": "tc1", "name": "bash", "args": {}}]),
        ToolMessage(content=small_body, tool_call_id="tc1"),
    ]
    out = _truncate_live_history(messages)
    _assert_eq(out[1].content, small_body, "small body passes through")


def test_truncate_at_default_is_4000() -> None:
    """Defensive: the cap should be 4000 chars, not 800. The old
    value was too aggressive — it collapsed every post-write Read
    to a stub. With 4000 (~1KB of tokens), most file reads and
    test results fit in one observation. Bumping this further
    without also raising mask_old_observations' threshold would
    blow the context budget on long sessions."""
    from memory.checkpointer import TOOL_RESULT_TRUNCATE_AT_CHARS
    _assert_eq(TOOL_RESULT_TRUNCATE_AT_CHARS, 4000, "truncation cap value")


def main() -> int:
    tests = [
        test_recent_observation_preserved_verbatim,
        test_old_observations_still_capped,
        test_no_tool_messages_passes_through,
        test_below_cap_passes_through,
        test_truncate_at_default_is_4000,
    ]
    passed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            return 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            return 1
        print(f"  ✓ {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
