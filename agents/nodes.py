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
from agents.prompt import SystemPromptBuilder, ProjectContext, current_model_name
from memory.checkpointer import (
    maybe_compact,
    _estimate_tokens as _estimate_msg_tokens,
    _truncate_tool_result,
    CONTEXT_WINDOW_TOKENS,
)
from tools.wrappers import get_all_tools

# ---------------------------------------------------------------------------
# Global config — set once at startup by server.app
# ---------------------------------------------------------------------------

_provider: str = "anthropic"
_model: str = "MiniMax-M3"
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
    budget — see reset_run_budget.)

    Also drops the cached TokenCounter so the next get_token_counter() call
    rebuilds it against the NEW model. Without this, a model change in the
    same process would silently misprice every turn (the cached counter
    still prices under the old model name)."""
    global _provider, _model, _thinking, _thinking_budget, _token_counter
    _provider = (provider or "anthropic").lower()
    _model = model
    _thinking = thinking
    _thinking_budget = thinking_budget
    _token_counter = None  # force rebuild on next get_token_counter() call


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
    _POLL_TOOLS = frozenset({"AgentStatus"})

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


# Module-level wall-clock for the in-flight LLM call. Set at the top
# of `_stream_model_call`, read at the bottom when recording the
# per-session trace. A module-level is fine because the agent loop
# is single-threaded per process (the default asyncio executor is
# single-threaded); concurrent sessions would race here only if we
# ever raised the executor pool, in which case we'd switch this to
# a thread-local.
_t_call_start: float = 0.0


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
    timeout      = float(os.getenv("AGENT_LLM_TIMEOUT_SECS", "300") or 300)

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


def _git_signature(workspace: str) -> tuple:
    """Cheap signature of the git state for cache invalidation. Just reads
    the mtime + size of `.git/HEAD` and `.git/index` — no `git` invocation.
    If either changes, the cached ProjectContext is stale and we re-discover.
    Cost: 2 stat() calls (~microseconds) vs 4 git subprocesses (~10-50ms each
    and 4 chances to break MiniMax's prompt-prefix cache by changing bytes)."""
    sig: list = [str(workspace)]
    try:
        import os as _os
        from pathlib import Path as _P
        gitdir = _P(workspace) / ".git"
        if not gitdir.is_dir():
            return (sig, 0, 0)  # not a git repo — cache is fine
        head = gitdir / "HEAD"
        idx  = gitdir / "index"
        sig.append(str(head.stat().st_mtime_ns) if head.exists() else "0")
        sig.append(str(head.stat().st_size)      if head.exists() else "0")
        sig.append(str(idx.stat().st_mtime_ns)  if idx.exists()  else "0")
        sig.append(str(idx.stat().st_size)      if idx.exists()  else "0")
    except Exception:
        pass
    return tuple(sig)


def _get_cached_project_context(state: RunnerState, workspace: str, today: str):
    """Return a ProjectContext, using a state-keyed cache to avoid re-running
    `git status` / `git log` / `git rev-parse` / `git config user.*` on every
    LLM call. The cache invalidates only when `.git/HEAD` or `.git/index`
    mtime/size changes (i.e. commit, branch switch, or worktree update)."""
    cache = state.get("_project_context_cache")
    sig = _git_signature(workspace)
    if cache and isinstance(cache, dict) and cache.get("sig") == sig and cache.get("today") == today:
        return cache["ctx"]
    ctx = ProjectContext.discover_with_git(workspace, today)
    # We CAN'T mutate state here (RunnerState is a TypedDict snapshot, not
    # a mutable container in some call sites), so the caller is responsible
    # for re-invoking us with the same state object. The cache lives in
    # LangGraph's checkpointer so it survives across turns.
    try:
        state["_project_context_cache"] = {"sig": sig, "today": today, "ctx": ctx}
    except Exception:
        pass
    return ctx


def _build_system_prompt(state: RunnerState) -> tuple[str, str]:
    """Build the system prompt as a `(static_base, dynamic_suffix)` pair.

    The static base is everything that doesn't change turn-to-turn — model
    identity, Ojas app rules, UI quality, orchestration, tool list. MiniMax's
    automatic prefix cache will hit on it from turn 2 onwards.

    The dynamic suffix is everything that changes on git/date/MCP changes —
    today's date, working dir, git status, recent commits, branch, MCP tools.

    Both are returned as strings. The caller (`node_agent`) wraps each in
    its own SystemMessage so the API sees two messages; the cache only busts
    when the dynamic suffix actually changes.
    """
    workspace = state.get("workspace", ".")
    today = date.today().isoformat()
    ctx = _get_cached_project_context(state, workspace, today)

    builder = (
        SystemPromptBuilder()
        .with_os(platform.system() or "unknown", platform.release() or "unknown")
        .with_model_family(current_model_name())
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

    static, dynamic = builder.render_split()
    return (static, dynamic)

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
    """Stream one model call: forward text chunks + tool announcements
    through the reporter, return the aggregated assistant message. Also
    records the full request/response to the per-session trace store
    (memory.llm_trace) for the wire-level debug panel.
    """
    from agents.reporter import get_reporter
    reporter = get_reporter()
    # Wall-clock start so the trace can report per-call duration. Module-
    # level because the default executor is single-threaded and we don't
    # want to thread it through every helper.
    global _t_call_start
    _t_call_start = time.monotonic()

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
            cache_creation, cache_read = _extract_cache_fields(usage)
            tc.record(TokenUsage(
                input_tokens=in_delta,
                output_tokens=out_delta,
                cache_creation_tokens=cache_creation,
                cache_read_tokens=cache_read,
            ))
            try:
                reporter.token_update(
                    input_delta=in_delta,
                    output_delta=out_delta,
                    cache_read_delta=cache_read,
                    cache_creation_delta=cache_creation,
                )
            except Exception:
                pass
    except Exception:
        pass

    # Record the wire-level LLM call to the per-session trace store.
    # Best-effort — a failure here must not break the agent loop.
    try:
        from memory.llm_trace import get_store, LLMCallRecord, serialize_messages
        rep = get_reporter()
        sid = getattr(rep, "session_id", "") or ""
        if sid:
            rec = LLMCallRecord(
                ts=time.time(),
                iteration=_run_budget.iters if _run_budget else 0,
                model=_model,
                request_messages=serialize_messages(messages),
                response=serialize_messages([ai])[0] if ai else {},
                usage=dict(usage) if usage else {},
                duration_ms=int((time.monotonic() - _t_call_start) * 1000),
                finish_reason=str(
                    (getattr(ai, "response_metadata", {}) or {}).get("finish_reason", "")
                ),
            )
            get_store().record(sid, rec)
    except Exception:
        pass

    return ai


# ---------------------------------------------------------------------------
# Cache-field extraction
# ---------------------------------------------------------------------------
# Provider `usage` dicts come in several known shapes depending on the model
# / endpoint:
#   1. Anthropic native: top-level `cache_creation_input_tokens` /
#      `cache_read_input_tokens` (separate write/read breakdown).
#   2. OpenAI standard: nested `prompt_tokens_details.cached_tokens`
#      (read-only — no write side).
#   3. OpenAI-compatible (MiniMax-M3, as confirmed by live probe): nested
#      `input_token_details.cache_read` — same idea, different key name.
#      Returns the read count as `cache_read` (singular) instead of the
#      OpenAI-standard `cached_tokens` (plural). Without probing both keys
#      the cache rate reads as 0 even though the provider IS serving from
#      cache (verified: 754/828 = 91% hit on the 2nd call with the same
#      system prompt).
#   4. Flat / unknown: return (0, 0) and let the UI display "no cache info"
#      rather than crashing.
# We log the raw usage once per process so we can pin down the shape and
# drop the diagnostic once it's confirmed across providers.
_cache_shape_logged = False


def _extract_cache_fields(usage: dict) -> tuple[int, int]:
    """Return (cache_creation_tokens, cache_read_tokens) from a provider
    usage dict. Probes the known shapes and returns (0, 0) if the provider
    doesn't surface cache info. Logs the raw usage once per process so we
    can confirm the active provider's actual shape."""
    global _cache_shape_logged
    if not usage:
        return 0, 0
    # 1. Anthropic native shape.
    try:
        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
    except (TypeError, ValueError):
        cr = cw = 0
    if cr or cw:
        if not _cache_shape_logged:
            import logging
            logging.getLogger(__name__).info(
                "[cache-debug] provider returned Anthropic-shape usage: %r", usage
            )
            _cache_shape_logged = True
        return cw, cr
    # 2 + 3. OpenAI-compatible shapes. The exact nesting and key name vary
    # by provider (OpenAI uses `prompt_tokens_details.cached_tokens`,
    # MiniMax-M3 uses `input_token_details.cache_read`), so probe both.
    details = (
        usage.get("prompt_tokens_details")
        or usage.get("input_token_details")
        or {}
    )
    if isinstance(details, dict):
        cr = (
            int(details.get("cached_tokens") or 0)
            or int(details.get("cache_read") or 0)  # MiniMax-M3 shape
        )
        cw = (
            int(details.get("cache_creation_tokens") or 0)
            or int(details.get("cache_creation") or 0)
        )
        if cr or cw:
            if not _cache_shape_logged:
                import logging
                logging.getLogger(__name__).info(
                    "[cache-debug] provider returned OpenAI-shape usage: %r", usage
                )
                _cache_shape_logged = True
            return cw, cr
    # 3b. LangChain sometimes normalises to flat keys. Try those too.
    cr = int(usage.get("cached_tokens", 0) or 0)
    cw = int(usage.get("cache_creation_tokens", 0) or 0)
    if cr or cw:
        if not _cache_shape_logged:
            import logging
            logging.getLogger(__name__).info(
                "[cache-debug] provider returned flat OpenAI-shape usage: %r", usage
            )
            _cache_shape_logged = True
        return cw, cr
    # 4. Unknown — log once so we know the shape, then return 0/0.
    if not _cache_shape_logged:
        import logging
        logging.getLogger(__name__).info(
            "[cache-debug] provider usage has no cache fields: %r", usage
        )
        _cache_shape_logged = True
    return 0, 0

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


def _truncate_live_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Replace oversized ToolMessage bodies AND oversized AIMessage
    tool_call args with short stubs. Tool CALLS (path + args) on the
    preceding AIMessage stay verbatim — the agent's intent is what
    matters. Uses `model_copy(update=...)` so the on-disk checkpoint
    keeps the full body; only the per-turn request gets the trimmed
    view.

    The most recent K (default 4) ToolMessages are passed through
    verbatim, regardless of size. Without this, the agent's
    immediate post-write verification reads (Read of a freshly
    written 16KB file, `wc -l` of the same, `python3 -m py_compile`,
    `grep -c` checks) get collapsed to a one-line stub and the agent
    concludes the file is corrupt — when actually the on-disk file
    is fine. It then runs `sed`/`python` "repairs" based on its
    truncated view, which is where the REAL corruption enters the
    file. See the 7b4e6289 todo-app build for the canonical example
    of this loop. Older observations (>K back) still get capped —
    `mask_old_observations` handles long-term budget pressure
    separately, so the per-message cap here is just a safety net
    against pathological single-message bloat."""
    # Find the indices of the last K ToolMessages. Walk back from the
    # end; collect the first K ToolMessage positions. Anything before
    # `preserve_from` gets the cap applied.
    KEEP_RECENT = 4
    tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    # Preserve the last K observations verbatim. If there are FEWER
    # than K observations in total, preserve them all (a short
    # history of large tool results is exactly the post-write
    # verification pattern — we don't want to collapse any of them).
    if len(tool_indices) >= KEEP_RECENT:
        preserve_from = tool_indices[-KEEP_RECENT]
    else:
        preserve_from = tool_indices[0] if tool_indices else None

    out: list[BaseMessage] = []
    n_truncated_tool = 0
    n_truncated_call = 0
    n_preserved = 0
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage):
            # Recent observations: pass through verbatim, regardless of
            # length, so the agent can verify its own writes.
            if preserve_from is not None and i >= preserve_from:
                out.append(m)
                n_preserved += 1
                continue
            new_content = _truncate_tool_result(m.content)
            if new_content is not m.content:
                n_truncated_tool += 1
                out.append(m.model_copy(update={"content": new_content}))
            else:
                out.append(m)
        elif isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            new_calls, n = _truncate_tool_call_args(m.tool_calls or [])
            if n:
                n_truncated_call += n
                out.append(m.model_copy(update={"tool_calls": new_calls}))
            else:
                out.append(m)
        else:
            out.append(m)
    if n_truncated_tool or n_truncated_call or n_preserved:
        import logging
        logging.getLogger(__name__).info(
            "[history-trim] truncated %d tool result(s) + %d tool call arg(s); "
            "preserved %d recent observation(s) verbatim",
            n_truncated_tool, n_truncated_call, n_preserved,
        )
    return out


# Heavy tool-call arg keys — these often carry multi-KB strings
# (file bodies for edits, heredocs for bash). Truncating just these
# preserves the agent's intent (file path, function name) while
# shedding the heavy strings.
_TOOL_CALL_LARGE_ARG_KEYS = (
    "new_string",
    "old_string",
    "content",
    "command",
)


def _truncate_tool_call_args(tool_calls: list) -> tuple[list, int]:
    """Replace large string arg values in tool call dicts with a
    one-line stub. Other args (path, query, etc.) pass through
    verbatim. Returns (new_calls, n_truncated)."""
    limit = 800
    if limit <= 0:
        return tool_calls, 0
    out: list = []
    n = 0
    for tc in tool_calls:
        if not isinstance(tc, dict):
            out.append(tc)
            continue
        args = tc.get("args")
        if not isinstance(args, dict):
            out.append(tc)
            continue
        changed = False
        new_args = dict(args)
        for key in _TOOL_CALL_LARGE_ARG_KEYS:
            val = new_args.get(key)
            if isinstance(val, str) and len(val) > limit:
                head = val[:limit].replace("\n", " ⏎ ")
                new_args[key] = (
                    f"[arg truncated: {len(val):,} chars "
                    f"(~{len(val) // 4:,} tokens); first {limit} chars: "
                    f"{head!r}… re-invoke the tool to see the full body]"
                )
                changed = True
                n += 1
        if changed:
            out.append({**tc, "args": new_args})
        else:
            out.append(tc)
    return out, n


# How many of the most recent AIMessages to keep their reasoning_content /
# thinking blocks in `additional_kwargs`. Older AIMessages have their
# thinking blocks stripped — the agent doesn't need its own past reasoning
# to keep working, and every thinking block re-ships on every turn via
# Anthropic's prefix cache. Tuned to 8 because the agent's "I think I
# should..." / "let me reconsider..." reasoning stays useful for the
# next 2-3 tool calls, but a reasoning block from 20 turns ago is just
# dead weight. Override with OJAS_KEEP_RECENT_THINKING.
KEEP_RECENT_THINKING = 8
KEEP_RECENT_THINKING_ENV_VAR = "OJAS_KEEP_RECENT_THINKING"
_THINKING_KEYS = (
    "reasoning_content",       # Anthropic native
    "reasoning",               # OpenAI-style
    "thinking",                # some providers
    "thinking_blocks",         # some providers (list-shaped)
)


def _keep_recent_thinking() -> int:
    import os
    raw = os.getenv(KEEP_RECENT_THINKING_ENV_VAR)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return KEEP_RECENT_THINKING


def _strip_old_thinking(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Remove `reasoning_content` / thinking blocks from AIMessages older
    than the most recent `KEEP_RECENT_THINKING`. The agent doesn't need
    its own past reasoning to keep working — and those blocks are
    re-shipped on every turn via Anthropic's automatic prefix cache,
    inflating per-turn cost on long sessions.

    The visible text content of each AIMessage is preserved. Only the
    `additional_kwargs` keys for thinking are dropped. Operates on a
    shallow copy: original messages are not mutated.
    """
    keep = _keep_recent_thinking()
    # Walk back from the end; count AIMessages. Older than `keep` get
    # their thinking keys stripped.
    ai_indices = [i for i, m in enumerate(messages) if isinstance(m, AIMessage)]
    if len(ai_indices) <= keep:
        return messages
    to_strip = set(ai_indices[:-keep])

    n_stripped = 0
    out: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if i not in to_strip:
            out.append(m)
            continue
        ak = getattr(m, "additional_kwargs", None) or {}
        if not any(k in ak for k in _THINKING_KEYS):
            out.append(m)
            continue
        new_ak = {k: v for k, v in ak.items() if k not in _THINKING_KEYS}
        out.append(m.model_copy(update={"additional_kwargs": new_ak}))
        n_stripped += 1

    if n_stripped:
        import logging
        logging.getLogger(__name__).info(
            "[thinking-strip] stripped thinking blocks from %d old AIMessage(s) "
            "(keeping most recent %d)",
            n_stripped, keep,
        )
    return out


def node_agent(state: RunnerState) -> dict:
    """One model call = one

    Before each call we consult the per-invocation run budget. If a budget is hit
    (iterations / tokens / wall-clock / stall) we PAUSE gracefully: return without
    a new assistant message so should_continue routes to END at a clean,
    checkpointed boundary (tool results already delivered). Re-running the same
    thread_id resumes from here with a fresh budget — no crash, no lost work.
    """
    # Plumb session_id from state into the local scope so we can pass
    # it to maybe_compact / record_llm_input_tokens. See the comment
    # in agents/state.py for why this is needed.
    session_id = state.get("session_id") or ""
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
    # Build the system prompt as a (static, dynamic) pair so MiniMax's
    # automatic prefix cache hits on the static part. We accept either form
    # from state (legacy `system_prompt` string OR the new pair) and rebuild
    # if absent / stale.
    cached = state.get("system_prompt_pair")
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        static_base, dynamic_suffix = cached
    else:
        # Legacy fallback: older checkpoints stored a single string under
        # `system_prompt`. We rebuild the pair and trust render_split's
        # backward-compat path (it returns the whole prompt as static
        # if the boundary marker is missing).
        legacy = state.get("system_prompt")
        if isinstance(legacy, str) and legacy:
            static_base = legacy
            dynamic_suffix = ""
        else:
            static_base, dynamic_suffix = _build_system_prompt(state)

    # LLM input source: `live_messages` (per-turn post-compact working
    # set) if set, else `messages` (append-only accumulator). On the
    # first turn of a session `live_messages` is unset.
    raw_history = list(state.get("live_messages") or state.get("messages", []))
    history = _repair_orphan_tool_calls(raw_history)

    # Auto-compact BEFORE the LLM call (turn that crosses threshold pays
    # for the smaller compacted context, not the giant one). The `put()`
    # safety net only fires on restart or paths that bypass the loop.
    from memory.checkpointer import maybe_compact, mask_old_observations, _auto_compact_threshold
    history, did_compact, compact_info = maybe_compact(history, session_id=session_id)
    if did_compact:
        # Chat-visible notification. No `context_update` here — the
        # post-LLM publish below is the single source of truth for the
        # chip (pre-LLM local estimates have drifted from the provider's
        # real number in the past and made the chip bounce).
        try:
            from agents.reporter import get_reporter
            get_reporter().context_compacted(
                removed=compact_info.get("removed", 0),
                kept=compact_info.get("kept", 0),
                tokens_before=compact_info.get("tokens_before", 0),
                tokens_after=compact_info.get("tokens_after", 0),
                summary_preview=compact_info.get("summary_preview", ""),
                threshold=_auto_compact_threshold(),
            )
        except Exception:
            pass

    # Collapse old tool results to a stub so the prefix cache doesn't
    # re-send the entire previous history on every turn. The agent can
    # always re-invoke the tool for the fresh body.
    history = mask_old_observations(history)

    # Strip old thinking/reasoning blocks — they re-ship every turn via
    # the prefix cache and are the largest single contributor to
    # per-turn cost on long sessions.
    history = _strip_old_thinking(history)

    # Truncate oversized ToolMessage bodies + heavy AIMessage tool_call
    # args. Tool calls (path + args) on the preceding AIMessage stay
    # verbatim — the agent's intent is what matters.
    history = _truncate_live_history(history)

    # NOTE: we no longer publish a pre-LLM `context_update` here. The
    # local estimate (`_estimate_msg_tokens(history)`) drifts from the
    # real `input_tokens` Anthropic reports (no system prompt in the
    # local count, `len//4` rounding error, etc.) — publishing both
    # made the chip bounce between two different numbers within a
    # single turn. The post-LLM publish below (using the provider's
    # authoritative `input_tokens` from `usage_metadata`) is the only
    # source of truth. The chip stays at its last real value during
    # the API call, which is fine.

    # Build the messages list: [static SystemMessage, dynamic SystemMessage,
    # ...history]. Two SystemMessages in a row is the cleanest way to expose
    # the static/dynamic split to providers that prefix-cache on identical
    # prefixes — the second message busts the cache for the dynamic part
    # only.
    messages: list[BaseMessage] = []
    if static_base:
        messages.append(SystemMessage(content=static_base))
    if dynamic_suffix:
        messages.append(SystemMessage(content=dynamic_suffix))
    messages.extend(history)

    llm = _get_llm().bind_tools(get_all_tools() + _mcp_tools)
    ai = _stream_model_call(llm, messages)
    _run_budget.record(ai)

    # Publish the context-used value to the chip. The chip's
    # percentage label uses the NEW (uncached + writes) tokens,
    # NOT the cache-inflated total — so the percentage stays
    # meaningful as a "fill level" indicator relative to the
    # auto-compact threshold. For a "hi" turn on a session
    # running at 99.8% cache hit rate, the chip shows ~0%
    # (only 136 new tokens) instead of 154% (which is the
    # total prompt including the cache-served static system
    # prompt). The auto-compact trigger uses the same number,
    # so chip and trigger stay in lockstep — when the chip
    # goes red (>=100% of threshold), auto-compact fires.
    #
    # The tooltip still shows the total + cache split so the
    # user can see "X new, Y cached" if they hover.
    #
    # Different providers report tokens with different
    # semantics:
    #   - Anthropic native: `input_tokens` is the UNCACHED
    #     portion only; cache fields are reported separately.
    #   - OpenAI standard + MiniMax-M3: `input_tokens` is
    #     the TOTAL prompt (uncached + cache_read +
    #     cache_creation combined). Cache fields live in
    #     `input_token_details.cache_read` /
    #     `cache_creation` (or `prompt_tokens_details.
    #     cached_tokens` for vanilla OpenAI).
    #
    # We compute the NEW number by SUBTRACTING cache fields
    # when they're present (OpenAI / MiniMax shape), or
    # using `input_tokens` as-is if the provider didn't
    # surface them (Anthropic shape).
    #
    # (Earlier the chip used the total prompt number; that
    # gave 154% for a "hi" turn whose actual new content was
    # only 136 tokens, which the user found misleading —
    # "154% is not correct right" — because a session
    # genuinely under the auto-compact ceiling should show a
    # calm chip, not a screaming red one.)
    # The chip shows the TOTAL prompt size the model was given
    # — same number the user sees in the LLM call stats ("In 78k").
    # The auto-compact trigger uses the same number, so chip and
    # trigger stay in lockstep. The cache fields are surfaced in
    # the tooltip so the user can see how much of that 78k is
    # genuinely new content vs cache-served prefix.
    #
    # Provider semantics (verified live against MiniMax M3):
    #   `usage.input_tokens` is the TOTAL prompt — uncached +
    #   cache_read + cache_creation combined. The cache fields
    #   (`input_token_details.cache_read` /
    #   `input_token_details.cache_creation`) are SUBSETS of
    #   `input_tokens`, NOT separate additions. We must NOT
    #   add them on top — that would double-count (e.g. 78k
    #   total + 77k cache = 155k, then chip shows 311% of 50k
    #   threshold for a turn whose actual prompt was 78k).
    #
    # The same shape is used by OpenAI standard (`prompt_tokens`,
    # `prompt_tokens_details.cached_tokens`). On Anthropic
    # native, `input_tokens` is the UNCACHED portion only and
    # cache fields are reported separately as
    # `cache_read_input_tokens` / `cache_creation_input_tokens`.
    # We don't have a runtime way to know which provider we're
    # talking to; the heuristic is "if cache fields are present
    # AND they don't exceed `input_tokens`, the provider is
    # reporting the total in `input_tokens`". (Anthropic's
    # cache fields can exceed `input_tokens` because the
    # report is structured differently — but for OpenAI /
    # MiniMax, the cache fields are always < input_tokens.)
    try:
        from agents.reporter import get_reporter
        from memory.checkpointer import _auto_compact_threshold, record_llm_input_tokens
        _usage = getattr(ai, "usage_metadata", None) or {}
        _input_total = int(_usage.get("input_tokens", 0) or 0)
        _cache_creation, _cache_read = _extract_cache_fields(_usage)
        # Sanity clamp: if the cache fields exceed `input_tokens`,
        # they're probably from a different reporting shape and
        # would double-count. Cap them at `input_tokens` so the
        # breakdown in the tooltip stays sensible.
        if _cache_read > _input_total:
            _cache_read = _input_total
        if _cache_creation > _input_total:
            _cache_creation = _input_total
        if _input_total > 0:
            get_reporter().context_update(
                used_tokens=_input_total,
                budget_tokens=CONTEXT_WINDOW_TOKENS,
                compacting=False,
                threshold=int(_auto_compact_threshold()),
                cache_read=_cache_read,
                cache_creation=_cache_creation,
                input_total=_input_total,
            )
            # Auto-compact trigger uses the same total number.
            # Auto-compact trigger uses the same total. `session_id` keys
            # the cross-turn module dict — without it the per-context
            # ContextVar would reset to 0 next turn and the trigger
            # would never fire.
            record_llm_input_tokens(_input_total, session_id=session_id)
    except Exception:
        pass

    # Persist the post-process history as the LLM input for the NEXT turn.
    # Two channels: `messages` (add_messages reducer — append-only audit
    # log) and `live_messages` (REPLACE reducer — the LLM only ever
    # reads this, so it stays bounded by the auto-compact threshold).
    return {
        "messages": [ai],
        "live_messages": list(history) + [ai],
        "iterations": iterations,
        "system_prompt_pair": [static_base, dynamic_suffix],
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

    result = ToolNode(get_all_tools() + _mcp_tools).invoke(state)

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

    # Mirror the tool results into `live_messages` so the next iteration's
    # LLM call sees them. `messages` already gets them via add_messages
    # (the ToolNode return value flows through the reducer), but the LLM
    # reads from `live_messages` (the post-compact working set), not
    # `messages` (the unbounded audit log). Without this mirror, the
    # next node_agent call would have an AIMessage with tool_calls but
    # no matching ToolMessages in live_messages — and the conversation
    # would re-trigger the orphan-repair path on every iteration.
    new_tool_messages = list(result.get("messages", []))
    if new_tool_messages:
        existing = list(state.get("live_messages") or state.get("messages", []))
        return {
            **result,
            "live_messages": existing + new_tool_messages,
        }
    return result
