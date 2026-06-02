"""
LangGraph state schemas.

The runner is a single iterative tool-calling loop (faithful to Rust
runtime/src/conversation.rs::run_turn), so the state is just the conversation
plus loop bookkeeping — no phases, waves, workers, or shared api_contract.
"""

from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class RunnerState(TypedDict, total=False):
    """State for the single run_turn-style agent loop."""
    # Full conversation history (assistant turns + tool results).
    messages:        Annotated[list[BaseMessage], add_messages]

    # Task / environment info.
    task:            str
    workspace:       str
    repo:            str
    project_context: str          # extra instructions (CLAUDE.md / .agent.md)
    mode:            str          # "cli" | "auto"

    # Loop bookkeeping (mirror Rust run_turn).
    iterations:      int          # number of model calls so far this run
    max_iterations:  int          # hard cap; raises when exceeded
    system_prompt:   str          # assembled once on first iteration, reused

    # Graceful-pause signalling (set when a run budget is hit; see nodes._RunBudget).
    paused:          bool         # True when the loop stopped on a budget, not completion
    pause_reason:    dict         # {"reason": ..., "detail": ...}
