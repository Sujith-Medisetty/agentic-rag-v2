"""
LangGraph state schema.

The agent loop is a single iterative tool-calling cycle (faithful to Rust
runtime/src/conversation.rs::run_turn), so the state is just the
conversation plus a few knobs.
"""

from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage


class RunnerState(TypedDict, total=False):
    """State for the single run_turn-style agent loop.

    `messages` is the LLM input for the next round-trip. Default reducer
    is REPLACE — node_agent writes the new working set every turn so the
    next LLM call sees the post-auto-compact list, not an unbounded
    accumulator. session_runner reads it at end-of-turn to compute the
    final assistant text and tool count.
    """
    messages: list[BaseMessage]

    # Task / environment info.
    task: str
    workspace: str
    repo: str
    project_context: str  # extra instructions (CLAUDE.md / .agent.md)
    mode: str  # "cli" | "auto"

    # Loop bookkeeping.
    iterations: int  # number of model calls so far this run (informational)

    # Todo-reminder bookkeeping. The loop injects a stateful
    # `<system-reminder>` when the plan panel goes stale (Claude-Code style).
    tools_since_todo: int  # tool calls executed since the last TodoWrite
    tools_total: int  # total tool calls this run (used to detect non-trivial tasks)

    # Done-gate bookkeeping (agents/verify_gate.py). The gate blocks a turn
    # from ending while an Ojas app in the workspace has not passed
    # `npm run verify` for its current code. Bounded so it can't loop forever.
    gate_action: str  # "force" | "pass" — set by node_gate, read by gate_router
    gate_nudges: int  # how many times the gate has forced a re-verify this run
    gate_started_at: float  # epoch of the first gate nudge (budget anchor)

    # Plumbed from session_runner so the agent loop can key cross-turn
    # state (e.g. maybe_compact's session-scoped cache) by session.
    session_id: str