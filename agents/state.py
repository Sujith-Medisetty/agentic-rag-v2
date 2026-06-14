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
 # Full conversation history (assistant turns + tool results). Uses the
 # standard `add_messages` reducer — every node that returns a new message
 # appends it. node_agent uses this channel for persistence-of-record only;
 # the per-turn LLM input source is `live_messages` (below) so the
 # auto-compact summary can REPLACE the old entries instead of being
 # appended on top of an ever-growing list.
 messages: Annotated[list[BaseMessage], add_messages]

 # Per-turn LLM input. Default reducer (no Annotated) means a returned
 # value REPLACES the previous one. node_agent writes the post-compact
 # history + the new AI message here on every call, so the NEXT turn
 # reads from a bounded list (the auto-compact summary + recent tail)
 # rather than the full uncompacted accumulator. The `messages` channel
 # above still gets every message appended for persistence — but the
 # LLM only ever sees `live_messages`.
 #
 # Why a separate field rather than RemoveMessage on `messages`:
 #   1. RemoveMessage needs the message's `id` field, which works for
 #      LangChain's auto-generated ids but adds an O(N) scan per compact.
 #   2. Mixing removals + adds in a single update is brittle when
 #      the next turn is also compacting (the second compact's read
 #      would see the first's mid-flight removals).
 #   3. The `messages` channel stays a clean append-only audit log
 #      — the full history is preserved there for replays / debugging,
 #      while `live_messages` is the working set the LLM actually sees.
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
