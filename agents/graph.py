"""
LangGraph runner — a single iterative tool-calling loop.

2-node cycle:

 START → node_agent → (should_continue?)
 ├── node_tools → node_agent (assistant requested tools)
 └── END (no tool uses → break)

This replaces the previous 6-phase DAG (understand → plan → approval → workers/
waves → verify → review → commit), none of which exists in the Rust agent.
"""

from langgraph.graph import StateGraph, END, START
from langgraph.checkpoint.memory import MemorySaver

from agents.state import RunnerState
from agents.nodes import node_agent, node_tools, should_continue
from memory.checkpointer import CompactingCheckpointer

def build_runner_graph(use_compaction: bool = True) -> StateGraph:
 """Build and compile the single run_turn-style loop."""
 builder = StateGraph(RunnerState)

 builder.add_node("node_agent", node_agent)
 builder.add_node("node_tools", node_tools)

 builder.add_edge(START, "node_agent")
 builder.add_conditional_edges(
 "node_agent",
 should_continue,
 {
 "node_tools": "node_tools",
 "__end__": END,
 },
 )
 builder.add_edge("node_tools", "node_agent")

 checkpointer = CompactingCheckpointer() if use_compaction else MemorySaver()
 return builder.compile(checkpointer=checkpointer)

# Singleton compiled graph — import and call.invoke()/.stream() directly.
runner_graph = build_runner_graph()
