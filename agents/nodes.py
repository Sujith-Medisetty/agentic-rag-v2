"""
Single iterative agent loop — faithful port of Rust
runtime/src/conversation.rs::run_turn.

The loop is expressed as a 2-node LangGraph cycle (see agents/graph.py):

 node_agent → call the model once, append the assistant message
 │ should_continue: assistant requested tools?
 ├── yes → node_tools (execute tools, append results) → back to node_agent
 └── no → END (terminal: no pending tool uses → break)

 * explicit `iterations` counter, checked against max_iterations (raises if
 exceeded, like conversation.rs:347-355);
 * loop continues while the assistant keeps requesting tools, and stops the
 moment it returns no tool uses (conversation.rs:407-409);
 * compaction is handled by the CompactingCheckpointer on each checkpoint write
 (before the next iteration runs).
"""

from __future__ import annotations

from datetime import date
import json
import platform
import time

from langchain_core.messages import (
    AIMessage, AIMessageChunk, SystemMessage, BaseMessage,
)
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import ToolNode

from agents.state import RunnerState
from agents.prompt import SystemPromptBuilder, ProjectContext, FRONTIER_MODEL_NAME
from tools.wrappers import get_all_tools

# ---------------------------------------------------------------------------
# Global config — set once at startup by server.app
# ---------------------------------------------------------------------------

_model: str = "claude-opus-4-6"
_thinking: bool = False
_thinking_budget: int = 10_000
_mcp_tools: list = []  # extra LangChain tools loaded from MCP servers
_token_counter = None


def configure_model(
    model: str,
    thinking: bool = False,
    thinking_budget: int = 10_000,
) -> None:
    """Called once at startup so the loop uses the configured model + thinking
    settings. (Iteration/token/time limits are the per-invocation run budget —
    see reset_run_budget.)"""
    global _model, _thinking, _thinking_budget
    _model = model
    _thinking = thinking
    _thinking_budget = thinking_budget


def configure_tools(extra_tools: list | None = None) -> None:
    """Register additional LangChain tools (typically loaded from MCP servers
    by server.mcp_loader.load_mcp_tools). Called ONCE at server boot; passing
    [] or omitting the arg leaves the agent with just the native toolset."""
    global _mcp_tools
    _mcp_tools = list(extra_tools or [])


def get_mcp_tools() -> list:
    """Snapshot of the currently-registered MCP tools. Used by the prompt
    builder to surface them in the system prompt."""
    return list(_mcp_tools)

# ---------------------------------------------------------------------------
# Per-invocation run budget — replaces the old hard max_iterations crash with a
# graceful pause. Budget state lives here (NOT in persisted graph state) so a
# resume (same thread_id, new process) starts with a fresh budget and continues
# from the last checkpoint instead of immediately re-pausing.
# ---------------------------------------------------------------------------


class _RunBudget:
    """Tracks one invocation's iteration / token / wall-clock spend + a
    no-progress (stall) detector. check() returns a pause-reason dict or None."""

    # Poll-style tools are legitimately repetitive (e.g. the orchestrator waiting
    # on a sub-agent), so they are excluded from the stall signature — otherwise a
    # normal wait would false-trip the no-progress detector.
    _POLL_TOOLS = frozenset({"AgentStatus", "WorkerGet", "WorkerAwaitReady", "WorkerObserve"})

    def __init__(self) -> None:
        self.max_iters = 0
        self.max_tokens = 0
        self.max_seconds = 0
        self.no_progress_limit = 8
        self.iters = 0
        self.start: float | None = None
        self._last_sig: str | None = None
        self._repeat_streak = 0

    def reset(self, *, max_iters: int, max_tokens: int, max_seconds: int,
              no_progress_limit: int) -> None:
        self.max_iters = max(0, int(max_iters or 0))
        self.max_tokens = max(0, int(max_tokens or 0))
        self.max_seconds = max(0, int(max_seconds or 0))
        self.no_progress_limit = max(0, int(no_progress_limit or 0))
        self.iters = 0
        self.start = time.monotonic()
        self._last_sig = None
        self._repeat_streak = 0

    @classmethod
    def _tool_signature(cls, ai: AIMessage) -> str | None:
        """Stable signature of an assistant turn's tool calls (name + args).
        Returns None when the turn requested no tools, or only poll-style tools
        (which are legitimately repetitive and must not count as a stall)."""
        calls = [
            c for c in (getattr(ai, "tool_calls", None) or [])
            if c.get("name") not in cls._POLL_TOOLS
        ]
        if not calls:
            return None
        parts = sorted(
            f"{c.get('name','')}:{json.dumps(c.get('args', {}), sort_keys=True, default=str)}"
            for c in calls
        )
        return "|".join(parts)

    def _tokens_used(self) -> int:
        tc = get_token_counter()
        if not tc:
            return 0
        cum = tc.cumulative
        return cum.input_tokens + cum.output_tokens

    def check(self) -> dict | None:
        """Evaluate budgets BEFORE the next model call. None ⇒ keep going."""
        if self.max_iters and self.iters >= self.max_iters:
            return {"reason": "iterations",
                    "detail": f"reached per-run iteration cap ({self.max_iters})"}
        if self.no_progress_limit and self._repeat_streak >= self.no_progress_limit:
            return {"reason": "no_progress",
                    "detail": (f"{self._repeat_streak} consecutive identical tool "
                               "calls — likely stalled")}
        if self.max_tokens:
            used = self._tokens_used()
            if used >= self.max_tokens:
                return {"reason": "tokens",
                        "detail": f"reached token budget ({used:,}/{self.max_tokens:,})"}
        if self.max_seconds and self.start is not None:
            elapsed = time.monotonic() - self.start
            if elapsed >= self.max_seconds:
                return {"reason": "time",
                        "detail": f"reached wall-clock budget ({int(elapsed)}s/{self.max_seconds}s)"}
        return None

    def record(self, ai: AIMessage) -> None:
        """Account for a completed model call + update the stall detector."""
        self.iters += 1
        sig = self._tool_signature(ai)
        if sig is None:
            # Neutral turn (poll-only / no countable tools): carries no progress
            # signal, so leave the streak and last-signature untouched. Resetting
            # here would let a stall hide behind interleaved polls.
            return
        if sig == self._last_sig:
            self._repeat_streak += 1
        else:
            self._repeat_streak = 0
            self._last_sig = sig


_run_budget = _RunBudget()


def reset_run_budget(
    *, max_iters: int = 0, max_tokens: int = 0, max_seconds: int = 0,
    no_progress_limit: int = 8,
) -> None:
    """Called once per invocation (main._run_graph) before streaming the graph."""
    _run_budget.reset(
        max_iters=max_iters, max_tokens=max_tokens, max_seconds=max_seconds,
        no_progress_limit=no_progress_limit,
    )


def get_token_counter():
    global _token_counter
    if _token_counter is None:
        try:
            from memory.token_counter import TokenCounter
            _token_counter = TokenCounter(model=_model)
        except Exception:
            pass
    return _token_counter


def _tools() -> list:
    # Native tools + any MCP tools registered at startup. bind_tools(...) sees
    # the union; the LLM treats them identically.
    return get_all_tools() + _mcp_tools


def _get_llm() -> ChatAnthropic:
    kwargs: dict = {"model": _model, "streaming": True}
    if _thinking:
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": _thinking_budget}
    return ChatAnthropic(**kwargs)

# ---------------------------------------------------------------------------
# System prompt assembly (done once per run, cached in state)
# ---------------------------------------------------------------------------


def _build_system_prompt(state: RunnerState) -> str:
    workspace = state.get("workspace", ".")
    today = date.today().isoformat()
    ctx = ProjectContext.discover_with_git(workspace, today)

    builder = (
        SystemPromptBuilder()
        .with_os(platform.system() or "unknown", platform.release() or "unknown")
        .with_model_family(FRONTIER_MODEL_NAME)
        .with_project_context(ctx)
        # Top-level loop is the sole orchestrator — sub-agents (multi_agent.py)
        # deliberately do NOT enable this; they cannot spawn further agents.
        .with_orchestration_guidance(True)
        # Surface any MCP-loaded tools so the model knows they exist (in
        # addition to seeing them in its bound tool list).
        .with_mcp_tools(_mcp_tools)
    )

    # Extra instructions not covered by instruction-file discovery (README /
    #.agent.md content injected by the caller) are appended verbatim.
    extra = (state.get("project_context") or "").strip()
    if extra:
        builder.append_section(
            f"# Additional project preferences (follow exactly)\n{extra}"
        )
    return builder.render()

# ---------------------------------------------------------------------------
# Streaming display + token accounting for one model call
# ---------------------------------------------------------------------------


def _stream_model_call(llm_with_tools, messages: list[BaseMessage]) -> AIMessage:
    """Stream a single model call: forward text chunks + tool announcements
    through the reporter, return the aggregated assistant message."""
    from agents.reporter import get_reporter
    reporter = get_reporter()

    aggregate: AIMessageChunk | None = None
    announced: set[str] = set()

    for chunk in llm_with_tools.stream(messages):
        aggregate = chunk if aggregate is None else aggregate + chunk

        # Stream text chunks to the UI via reporter.assistant_text. WebReporter
        # publishes each chunk over the WebSocket so the browser bubble grows
        # in real time; the noop base reporter just drops them.
        content = chunk.content
        if isinstance(content, str) and content:
            reporter.assistant_text(content, done=False)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text", "")
                    if t:
                        reporter.assistant_text(t, done=False)

        # announce tool calls as soon as they have a name (dedupe by id)
        for tc in getattr(aggregate, "tool_calls", None) or []:
            tcid = tc.get("id") or tc.get("name", "")
            if tc.get("name") and tcid not in announced:
                announced.add(tcid)
                args = tc.get("args", {}) or {}
                target = (
                    args.get("path") or args.get("command") or args.get("query")
                    or args.get("pattern") or args.get("url") or args.get("action") or ""
                )
                reporter.tool_start(tc["name"], str(target)[:70])

    if aggregate is None:
        return AIMessage(content="")

    # Normalize the aggregated chunk into a plain AIMessage for clean re-sends.
    ai = AIMessage(
        content=aggregate.content,
        tool_calls=list(getattr(aggregate, "tool_calls", None) or []),
        additional_kwargs=getattr(aggregate, "additional_kwargs", {}) or {},
        response_metadata=getattr(aggregate, "response_metadata", {}) or {},
        usage_metadata=getattr(aggregate, "usage_metadata", None),
    )

    # record token usage
    try:
        tc = get_token_counter()
        usage = getattr(ai, "usage_metadata", None)
        if tc and usage:
            from memory.token_counter import TokenUsage
            tc.record(TokenUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
            ))
    except Exception:
        pass

    return ai

# ---------------------------------------------------------------------------
# Loop nodes
# ---------------------------------------------------------------------------


def node_agent(state: RunnerState) -> dict:
    """One model call = one

    Before each call we consult the per-invocation run budget. If a budget is hit
    (iterations / tokens / wall-clock / stall) we PAUSE gracefully: return without
    a new assistant message so should_continue routes to END at a clean,
    checkpointed boundary (tool results already delivered). Re-running the same
    thread_id resumes from here with a fresh budget — no crash, no lost work.
    """
    pause = _run_budget.check()
    if pause is not None:
        from agents.reporter import get_reporter
        try:
            get_reporter().tool_done("budget", f"paused: {pause['detail']}", error=False)
        except Exception:
            pass
        # No "messages" update ⇒ last message stays a ToolMessage/Human ⇒ END.
        return {"paused": True, "pause_reason": pause}

    iterations = int(state.get("iterations", 0)) + 1

    # Assemble the system prompt once, then reuse for the rest of the run
    #.
    system_prompt = state.get("system_prompt") or _build_system_prompt(state)

    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    messages.extend(state.get("messages", []))

    llm = _get_llm().bind_tools(_tools())
    ai = _stream_model_call(llm, messages)
    _run_budget.record(ai)

    return {
        "messages": [ai],
        "iterations": iterations,
        "system_prompt": system_prompt,
    }


def should_continue(state: RunnerState) -> str:
    """Terminal check: continue while the assistant requests tools."""
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "node_tools"
    return "__end__"


def node_tools(state: RunnerState) -> dict:
    """Execute the assistant's requested tools (permissions enforced inside each
    tool wrapper) and append the results."""
    from agents.reporter import get_reporter
    reporter = get_reporter()

    result = ToolNode(_tools()).invoke(state)

    for msg in result.get("messages", []):
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        name = getattr(msg, "name", "")
        first = content.strip().splitlines()
        preview = first[0][:80] if first else "(no output)"
        is_error = (
            content.startswith("Error:") or content.startswith("BLOCKED:")
            or "error" in preview.lower()[:20]
        )
        reporter.tool_done(name, preview, error=is_error)

    return result
