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
 messages: Annotated[list[BaseMessage], add_messages]

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

 # Per-thread plan-mode flag. When True, the agent's write tools (edit_file,
 # write_file, bash, git, github, MCP mutators) are blocked at the
 # node_tools dispatch chokepoint. The model must call ExitPlanMode to
 # unblock. Persisted via the checkpointer, keyed by thread_id, so each
 # session has its own plan-mode state and it survives restarts.
 plan_mode_active: bool

 # TodoWrite heartbeat counter. Counts non-TodoWrite tool calls since the
 # last TodoWrite. When the count reaches `_HEARTBEAT_THRESHOLD` (default
 # 5, env override `OJAS_TODO_HEARTBEAT`), the next non-TodoWrite call
 # is blocked with a `BLOCKED:` message and the model is forced to call
 # TodoWrite to keep the plan panel in sync with the live activity.
 # Reset to 0 by TodoWrite (via the tool-body micro-cache, drained at
 # the top of node_agent). Persisted per-thread via the checkpointer.
 tools_since_last_todowrite: int
