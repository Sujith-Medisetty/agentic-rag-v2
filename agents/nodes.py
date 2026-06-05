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

import os

from langchain_core.messages import (
    AIMessage, AIMessageChunk, SystemMessage, BaseMessage, ToolMessage,
)
from langchain_core.language_models import BaseChatModel
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import ToolNode

from agents.state import RunnerState
from agents.prompt import SystemPromptBuilder, ProjectContext, FRONTIER_MODEL_NAME
from tools.wrappers import get_all_tools

# ---------------------------------------------------------------------------
# Global config — set once at startup by server.app
# ---------------------------------------------------------------------------

_provider: str = "anthropic"
_model: str = "claude-opus-4-6"
_thinking: bool = False
_thinking_budget: int = 10_000
_mcp_tools: list = []  # extra LangChain tools loaded from MCP servers
_token_counter = None


def configure_model(
    model: str,
    thinking: bool = False,
    thinking_budget: int = 10_000,
    provider: str = "anthropic",
) -> None:
    """Called once at startup so the loop uses the configured provider/model +
    thinking settings. (Iteration/token/time limits are the per-invocation run
    budget — see reset_run_budget.)"""
    global _provider, _model, _thinking, _thinking_budget
    _provider = (provider or "anthropic").lower()
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


def _llm_request_timeout() -> float:
    # Hard per-call timeout for ANY single LLM request. Without this, a
    # silent provider stall (Anthropic/OpenAI infra hiccup, dropped TCP
    # connection, slow streaming response) would sit forever in the worker
    # thread — the UI freezes, the cancel button only closes the asyncio
    # task while the thread stays blocked on the socket read. With a
    # timeout, the model raises, the agent loop catches it, and the turn
    # closes cleanly via the existing error path.
    try:
        return float(os.getenv("AGENT_LLM_TIMEOUT_SECS", "300"))
    except ValueError:
        return 300.0


def _get_llm(
    *,
    provider: str | None = None,
    model: str | None = None,
    streaming: bool = True,
    thinking: bool | None = None,
) -> BaseChatModel:
    """Build the LangChain chat client for the configured provider.

    All kwargs are optional — defaults come from the module-level config set
    by `configure_model()`. Pass overrides when callers (e.g. sub-agents) need
    a different model or non-streaming mode without mutating module state, which
    matters because the orchestrator and sub-agents may run concurrently.

    Providers:
      anthropic — native ChatAnthropic. Uses ANTHROPIC_API_KEY.
      minimax / openai-compatible — ChatOpenAI pointed at the provider's
        OpenAI-compatible endpoint. Lets us drop in MiniMax / DeepSeek /
        Together / Groq / any local OpenAI-shaped server without code changes.
    """
    eff_provider = (provider or _provider).lower()
    eff_model    = model or _model
    eff_thinking = _thinking if thinking is None else thinking
    timeout      = _llm_request_timeout()

    if eff_provider == "anthropic":
        kwargs: dict = {
            "model": eff_model, "streaming": streaming, "timeout": timeout,
        }
        if eff_thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": _thinking_budget}
        return ChatAnthropic(**kwargs)

    # ChatOpenAI is imported lazily so installs that only ever use Anthropic
    # never pay the import cost.
    from langchain_openai import ChatOpenAI

    if eff_provider == "minimax":
        api_key = os.getenv("MINIMAX_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "MINIMAX_API_KEY is not set. Export it before starting the server."
            )
        base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
        return ChatOpenAI(
            model=eff_model, api_key=api_key, base_url=base_url,
            streaming=streaming, timeout=timeout,
            # OpenAI-compatible streaming hides usage by default — without this,
            # `usage_metadata` is empty on every AIMessage and the per-call
            # token_update event never fires (the UI's "X in / Y out" badge
            # under each LLM call stays blank). `stream_usage=True` opts in to
            # `stream_options={"include_usage": true}` upstream so usage
            # arrives in the final stream chunk.
            stream_usage=True,
        )

    if eff_provider in ("openai-compatible", "openai"):
        api_key = (
            os.getenv("AGENT_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        base_url = os.getenv("AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        if not api_key:
            raise RuntimeError(
                "AGENT_API_KEY (or OPENAI_API_KEY) is not set for openai-compatible provider."
            )
        kwargs = {
            "model": eff_model, "api_key": api_key,
            "streaming": streaming, "timeout": timeout,
            "stream_usage": True,   # see minimax branch above for why
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    raise RuntimeError(
        f"unknown provider '{eff_provider}'. Supported: anthropic, minimax, openai-compatible."
    )

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


class _ThinkingTagSplitter:
    """Streaming splitter for inline `<think>...</think>` blocks.

    OpenAI-compatible models (MiniMax M2, DeepSeek-R1, Qwen-thinking, etc.)
    emit chain-of-thought as plain text wrapped in `<think>` / `</think>`
    tags. Without parsing, that reasoning bleeds into the visible response.

    The splitter is a tiny state machine: feed it raw text chunks, get back
    a list of `(channel, text)` pairs where `channel` is `"thinking"` or
    `"assistant"`. It buffers partial tags across chunk boundaries so a tag
    split mid-stream (e.g. `…<th` then `ink>…`) is never misrouted."""

    _OPEN  = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self.in_thinking = False
        self._buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._buf += text
        while self._buf:
            tag = self._CLOSE if self.in_thinking else self._OPEN
            idx = self._buf.find(tag)
            if idx >= 0:
                head = self._buf[:idx]
                if head:
                    out.append(
                        ("thinking" if self.in_thinking else "assistant", head)
                    )
                self._buf = self._buf[idx + len(tag):]
                self.in_thinking = not self.in_thinking
                continue
            # No full tag — check whether the buffer ENDS with a prefix of
            # the tag we're looking for. If so, hold those trailing bytes
            # back; the next chunk may complete the tag.
            hold = self._partial_tail(self._buf, tag)
            safe = self._buf[: len(self._buf) - hold] if hold else self._buf
            if safe:
                out.append(
                    ("thinking" if self.in_thinking else "assistant", safe)
                )
            self._buf = self._buf[len(self._buf) - hold:] if hold else ""
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        out = [("thinking" if self.in_thinking else "assistant", self._buf)]
        self._buf = ""
        return out

    @staticmethod
    def _partial_tail(s: str, tag: str) -> int:
        """Length of the longest suffix of `s` that is a strict prefix of `tag`."""
        max_n = min(len(s), len(tag) - 1)
        for n in range(max_n, 0, -1):
            if tag.startswith(s[-n:]):
                return n
        return 0


def _stream_model_call(llm_with_tools, messages: list[BaseMessage]) -> AIMessage:
    """Stream a single model call: forward text chunks + tool announcements
    through the reporter, return the aggregated assistant message."""
    from agents.reporter import get_reporter
    reporter = get_reporter()

    aggregate: AIMessageChunk | None = None
    announced: set[str] = set()
    splitter = _ThinkingTagSplitter()

    def _emit_text(t: str) -> None:
        if not t:
            return
        for channel, piece in splitter.feed(t):
            if channel == "thinking":
                reporter.thinking_text(piece, done=False)
            else:
                reporter.assistant_text(piece, done=False)

    for chunk in llm_with_tools.stream(messages):
        aggregate = chunk if aggregate is None else aggregate + chunk

        # Stream text chunks to the UI via reporter. Two content shapes:
        #   - str  : OpenAI-compatible providers (incl. MiniMax) — routed
        #            through the <think> splitter so reasoning stays separate.
        #   - list : Anthropic block format — already typed; map type=text
        #            into assistant_text and type=thinking into thinking_text.
        content = chunk.content
        if isinstance(content, str) and content:
            _emit_text(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text", "")
                    if t:
                        # Even Anthropic occasionally returns raw <think> tags
                        # if a sub-tool prompted the model that way — keep the
                        # splitter on for safety.
                        _emit_text(t)
                elif btype == "thinking":
                    t = block.get("thinking") or block.get("text") or ""
                    if t:
                        reporter.thinking_text(t, done=False)

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

    # Drain anything still in the splitter buffer (e.g. trailing text with
    # no closing tag) so we don't silently swallow the tail of a response.
    for channel, piece in splitter.flush():
        if channel == "thinking":
            reporter.thinking_text(piece, done=False)
        else:
            reporter.assistant_text(piece, done=False)

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

    # record token usage + publish a live delta so the UI's per-turn
    # counter ticks (instead of waiting for the end-of-turn summary).
    try:
        tc = get_token_counter()
        usage = getattr(ai, "usage_metadata", None)
        if tc and usage:
            from memory.token_counter import TokenUsage
            in_delta  = int(usage.get("input_tokens", 0) or 0)
            out_delta = int(usage.get("output_tokens", 0) or 0)
            tc.record(TokenUsage(input_tokens=in_delta, output_tokens=out_delta))
            try:
                reporter.token_update(input_delta=in_delta, output_delta=out_delta)
            except Exception:
                pass
    except Exception:
        pass

    return ai

# ---------------------------------------------------------------------------
# Loop nodes
# ---------------------------------------------------------------------------


def _repair_orphan_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Repair message-history corruption left behind by a cancelled / killed turn.

    Both OpenAI-compatible providers (MiniMax, DeepSeek, …) and Anthropic
    require that every `AIMessage.tool_calls[i].id` is followed by a matching
    `ToolMessage(tool_call_id=...)`. If a turn was cancelled mid-iteration the
    checkpoint can land with an AIMessage requesting tools that never ran —
    on the next turn the LLM call returns 400 ("tool call result does not
    follow tool call (2013)" for MiniMax).

    Fix: walk the history, and for any AIMessage whose tool_call ids are not
    fully satisfied by the immediately-following run of ToolMessages, STRIP
    the unsatisfied tool_calls. The AIMessage stays in history as a plain
    text reply ("I was going to call X but didn't"), and the conversation can
    continue. Synthesizing fake ToolMessage results would be worse — it lies
    to the model about what executed.
    """
    if not messages:
        return messages
    repaired: list[BaseMessage] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            call_ids = [tc.get("id") for tc in m.tool_calls if tc.get("id")]
            # Look ahead: collect the contiguous ToolMessage run that follows.
            j = i + 1
            satisfied: set[str] = set()
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                tcid = getattr(messages[j], "tool_call_id", None)
                if tcid:
                    satisfied.add(tcid)
                j += 1
            missing = [cid for cid in call_ids if cid not in satisfied]
            if missing:
                # Drop the unsatisfied tool_calls. Keep tool_calls that ARE
                # satisfied (their ToolMessages are right behind them).
                kept = [tc for tc in m.tool_calls if tc.get("id") in satisfied]
                content = m.content if m.content else ""
                if not kept and not content:
                    # Empty AIMessage adds no signal — synthesize a brief note
                    # so the model has SOMETHING to see and the conversation
                    # has a clean breadcrumb of what happened.
                    content = "(previous turn was interrupted before tools could run)"
                repaired.append(AIMessage(
                    content=content,
                    tool_calls=kept,
                    additional_kwargs=getattr(m, "additional_kwargs", {}) or {},
                    response_metadata=getattr(m, "response_metadata", {}) or {},
                ))
            else:
                repaired.append(m)
        else:
            repaired.append(m)
        i += 1

    # Second pass: drop orphan ToolMessages whose tool_call_id is never
    # introduced by a PRECEDING AIMessage in the surviving history. This
    # catches the OpenAI / MiniMax shape of the same problem from the other
    # side: when compaction summarises away an AIMessage that issued a
    # tool_call, its matching ToolMessage stays in the preserved tail —
    # MiniMax then rejects the request with "tool result's tool id(...) not
    # found (2013)". Dropping the orphan silently is correct: synthesising a
    # fake AIMessage to "own" the ToolMessage would lie to the model about
    # what it had asked for.
    cleaned: list[BaseMessage] = []
    known_ids: set[str] = set()
    for m in repaired:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                tcid = tc.get("id")
                if tcid:
                    known_ids.add(tcid)
            cleaned.append(m)
        elif isinstance(m, ToolMessage):
            tcid = getattr(m, "tool_call_id", None)
            if tcid in known_ids:
                cleaned.append(m)
            # else: drop the orphan silently
        else:
            cleaned.append(m)
    return cleaned


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

    # Repair any orphaned tool_calls left by a previously cancelled/killed turn
    # BEFORE we send the message history to the LLM. Without this, MiniMax /
    # OpenAI-compatible providers reject the conversation with a 400.
    raw_history = list(state.get("messages", []))
    history = _repair_orphan_tool_calls(raw_history)

    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]
    messages.extend(history)

    llm = _get_llm().bind_tools(_tools())
    ai = _stream_model_call(llm, messages)
    _run_budget.record(ai)

    # If we had to repair, surface the repaired messages back into LangGraph
    # state so subsequent turns / checkpoints see the cleaned history. Using
    # RemoveMessage + re-add would be cleaner, but LangGraph's default `add`
    # reducer on `messages` appends — so the simplest fix is to overwrite via
    # the same list we used for the call. For now we just record `ai`; the
    # orphans remain in checkpoint but the repair runs again next turn (cheap
    # and idempotent).
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
        # The collapsed UI view derives its own first-line / 110-char summary
        # from `preview`. The expanded view renders the WHOLE thing. Earlier
        # this code shipped only the first line / 80 chars to the reporter —
        # which meant double-click-to-expand had nothing to expand because
        # the rest of the output was thrown away here, before persistence.
        # Now we ship the full content; reporter.tool_done caps at ~100KB so
        # truly enormous outputs (rare) still get a graceful upper bound,
        # and `previewTruncated` surfaces in the UI so the user knows.
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        is_error = (
            content.startswith("Error:") or content.startswith("BLOCKED:")
            or "error" in first_line.lower()[:20]
        )
        reporter.tool_done(name, content or "(no output)", error=is_error)

    return result
