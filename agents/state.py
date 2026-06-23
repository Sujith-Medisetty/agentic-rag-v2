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
    project_context: str  # extra instructions (CLAUDE.md / .agent.md)
    mode: str  # "cli" | "auto"

    # Loop bookkeeping.
    iterations: int  # number of model calls so far this run (informational)

    # Consecutive thinking-only model responses (reasoning, but no user-facing
    # text and no tool call). should_continue re-prompts while this is within
    # AGENT_MAX_EMPTY_CONTINUATIONS so a turn isn't ended by a bare <think>
    # block; any real text or tool call resets it to 0. Per-turn — session_runner
    # zeroes it in initial_state (see the reset note below).
    empty_responses: int

    # Per-turn bookkeeping — session_runner zeroes ALL of the fields below in
    # the per-turn initial_state. The checkpointer persists RunnerState per
    # session (thread_id) and only the keys passed each turn overwrite it, so
    # any "this run" counter MUST be reset there or it silently accumulates
    # across the whole session.

    # Todo-reminder bookkeeping. The loop injects a stateful
    # `<system-reminder>` when the plan panel goes stale (Claude-Code style).
    tools_since_todo: int  # tool calls executed since the last TodoWrite (this turn)
    tools_total: int  # total tool calls this turn (used to detect non-trivial tasks)

    # Done-gate bookkeeping (agents/verify_gate.py). The gate blocks a turn
    # from ending while an Ojas app in the workspace has not passed
    # `npm run verify` for its current code. Bounded so it can't loop forever;
    # the budget is PER-TURN (reset each turn by session_runner).
    gate_action: str  # "force" | "pass" — set by node_gate, read by gate_router
    gate_nudges: int  # how many times the gate has forced a re-verify this turn
    gate_started_at: float  # epoch of the first gate nudge this turn (budget anchor)

    # End-of-turn todo-finalize nudge. node_gate, when the turn is about to
    # END, forces ONE extra turn if the plan still has pending/in_progress
    # items — so a short final burst of tool calls (below the staleness
    # threshold) can't leave the panel out of sync. Bounded by this counter
    # (reset per turn) so it nudges at most once and can't loop.
    todo_finalize_nudges: int

    # Plumbed from session_runner so the agent loop can key cross-turn
    # state (e.g. maybe_compact's session-scoped cache) by session.
    session_id: str