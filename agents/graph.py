"""
LangGraph runner — a single iterative tool-calling loop.

3-node cycle (with end-of-task todo sync gate):

 START → node_agent → (should_continue?)
 ├── node_tools → node_agent (assistant requested tools)
 ├── node_force_todo_sync → node_agent (assistant signalled done
 │   but `last_todos` still has `pending` / `in_progress` items;
 │   the nudge fires ONCE per turn via `todo_sync_nudged`, then
 │   the next `should_continue` check terminates normally)
 └── END (no tool uses AND either no open todos or already nudged)

This replaces the previous 6-phase DAG (understand → plan → approval → workers/
waves → verify → review → commit), none of which exists in the Rust agent.
The todo-sync gate is Ojas-specific — the Rust runtime has no TodoWrite
tool, so the sync branch is a no-op there.
"""

from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver

from agents.state import RunnerState
from agents.nodes import (
    node_agent,
    node_tools,
    node_force_todo_sync,
    should_continue,
)
from memory.checkpointer import CompactingCheckpointer


def build_runner_graph(use_compaction: bool = True) -> StateGraph:
    """Build and compile the single run_turn-style loop."""
    builder = StateGraph(RunnerState)

    builder.add_node("node_agent", node_agent)
    builder.add_node("node_tools", node_tools)
    builder.add_node("node_force_todo_sync", node_force_todo_sync)

    builder.add_edge(START, "node_agent")
    builder.add_conditional_edges(
        "node_agent",
        should_continue,
        {
            "node_tools": "node_tools",
            "node_force_todo_sync": "node_force_todo_sync",
            "__end__": END,
        },
    )
    # After tool execution, go back to the model. The TodoWrite
    # wrapper has already updated the on-disk store; node_tools has
    # captured the fresh state into `last_todos` so the next
    # `should_continue` call sees it.
    builder.add_edge("node_tools", "node_agent")
    # The sync nudge appends a SystemMessage to `live_messages`
    # (not the audit log) and flips `todo_sync_nudged=True`. Send
    # it back through the model so it can emit a TodoWrite call.
    builder.add_edge("node_force_todo_sync", "node_agent")

    checkpointer = CompactingCheckpointer() if use_compaction else MemorySaver()
    return builder.compile(checkpointer=checkpointer)


# Singleton compiled graph — import and call .invoke() / .stream() directly.
runner_graph = build_runner_graph()
