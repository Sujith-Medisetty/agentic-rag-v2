"""
LangGraph runner — a single iterative tool-calling loop.

  START → node_agent → (should_continue?)
  ├── node_tools → node_agent (assistant requested tools)
  └── END (assistant signalled done — no tool_calls on last AIMessage)

Three-node cycle. No todo-sync gate, no pause node, no budget pause.
The agent decides when it's done.
"""

from langgraph.graph import StateGraph, END, START

from agents.state import RunnerState
from agents.nodes import node_agent, node_tools, should_continue
from memory.checkpointer import CompactingCheckpointer


def build_runner_graph(use_compaction: bool = True):
    """Build and compile the single run_turn-style loop."""
    builder = StateGraph(RunnerState)

    builder.add_node("node_agent", node_agent)
    builder.add_node("node_tools", node_tools)

    builder.add_edge(START, "node_agent")
    builder.add_conditional_edges(
        "node_agent",
        should_continue,
        {"node_tools": "node_tools", "__end__": END},
    )
    builder.add_edge("node_tools", "node_agent")

    if use_compaction:
        checkpointer = CompactingCheckpointer()
    else:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)


runner_graph = build_runner_graph()