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
 # Append-only audit log. node_agent appends the new AI message here
 # every turn; the LLM never reads this for input (it reads
 # `live_messages` below). The full history is preserved for replays.
 messages: Annotated[list[BaseMessage], add_messages]

 # Per-turn LLM input (post-compact, post-mask, post-strip, post-trim).
 # Default reducer = REPLACE: node_agent writes the new working set
 # every turn so the next LLM call sees a bounded list (summary +
 # recent tail), not the full uncompacted accumulator.
 live_messages: list[BaseMessage]

 # Task / environment info.
 task: str
 workspace: str
 repo: str
 project_context: str # extra instructions (CLAUDE.md /.agent.md)
 mode: str # "cli" | "auto"

 # Loop bookkeeping.
 iterations: int # number of model calls so far this run
 max_iterations: int # hard cap; raises when exceeded
 system_prompt: str # assembled once on first iteration, reused

 # Graceful-pause signalling (set when a run budget is hit; see nodes._RunBudget).
 paused: bool # True when the loop stopped on a budget, not completion
 pause_reason: dict # {"reason":..., "detail":...}

 # Plumbed from session_runner.run_turn so the agent loop can
 # key cross-turn state (e.g. maybe_compact's session-scoped
 # cache) by session. Without this the auto-compact trigger
 # can't tell "this turn was billed 80k tokens" from "no turn
 # has happened yet" — it would always fall back to the local
 # estimator, which underestimates by 5-6× and never trips the
 # 80k threshold.
 session_id: str
