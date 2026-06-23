"""
LangGraph runner — a single iterative tool-calling loop.

  START → node_agent → (should_continue?)
  ├── node_tools → node_agent          (assistant requested tools)
  └── node_gate → (gate_router?)        (assistant signalled done)
        ├── node_agent  (an Ojas app hasn't passed `npm run verify` yet —
        │                forces a re-verify + fix)
        └── END         (verify is green, or no app present, or the gate
                         budget is spent)

The gate is the only thing standing between "model stopped calling tools"
and END. It's a cheap filesystem check (agents/verify_gate.py) and is
bounded / kill-switchable, so the loop stays autonomous but can't ship a
broken app.
"""

from langgraph.graph import StateGraph, END, START

from agents.state import RunnerState
from agents.nodes import node_agent, node_tools, should_continue, node_gate, gate_router
from memory.checkpointer import CompactingCheckpointer


def build_runner_graph(use_compaction: bool = True):
    """Build and compile the single run_turn-style loop."""
    builder = StateGraph(RunnerState)

    builder.add_node("node_agent", node_agent)
    builder.add_node("node_tools", node_tools)
    builder.add_node("node_gate", node_gate)

    builder.add_edge(START, "node_agent")
    builder.add_conditional_edges(
        "node_agent",
        should_continue,
        {"node_tools": "node_tools", "node_gate": "node_gate"},
    )
    builder.add_edge("node_tools", "node_agent")
    builder.add_conditional_edges(
        "node_gate",
        gate_router,
        {"node_agent": "node_agent", "__end__": END},
    )

    if use_compaction:
        checkpointer = CompactingCheckpointer()
    else:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


runner_graph = build_runner_graph()