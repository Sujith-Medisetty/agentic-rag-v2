"""
Agent loop — the streaming LLM call, the tool execution, the routing decision.

Three LangGraph nodes and one decision function:

  node_agent      — stream ONE LLM call, append the AIMessage to history
  node_tools      — execute the tool calls the LLM just requested
  should_continue — if the last AIMessage has tool_calls → node_tools, else END

That's the whole loop. No wall-clock guard around node bodies. No iter / token /
time / stall budget. No todo-sync nudge gate. No per-tool synthetic error
fallback. No `_truncate_*` / `_strip_old_thinking` / `_mask_old_observations`
helpers. No cache diagnostics.

What's preserved (because it's load-bearing):
  * streaming model call with a per-chunk idle watchdog — a hung provider
    socket that stops emitting chunks raises TimeoutError after
    `AGENT_LLM_STREAM_IDLE_TIMEOUT_S` (default 90s); the retry layer
    catches that and retries once before bubbling;
  * httpx total timeout (set on the chat client, `AGENT_LLM_TIMEOUT_SECS`,
    default 300s) so a fully-frozen request raises instead of hanging;
  * auto-compaction via `maybe_compact(messages)` BEFORE each LLM call so
    the turn that crosses the context budget pays for the smaller compacted
    context, not the giant one;
  * orphan-tool-call repair so an interrupted / cancelled turn doesn't
    leave the conversation in a state the provider will reject on resume;
  * the per-session TokenCounter used by `session_runner` for the
    per-turn `before/after` diff (concurrent sessions were inflating each
    other's totals before this was made per-session).

Faithful port of runtime/src/conversation.rs::run_turn (the Rust agent).
The TodoWrite tool exists in Ojas but isn't synced at end-of-turn — the
agent decides when it's done. Same as Rust.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
from datetime import date

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    SystemMessage,
    BaseMessage,
    ToolMessage,
)
from langgraph.prebuilt import ToolNode

from agents.state import RunnerState
from agents.prompt import SystemPromptBuilder, ProjectContext, current_model_name
from memory.checkpointer import (
    maybe_compact,
    _auto_compact_threshold,
    _truncate_tool_result,
    record_llm_input_tokens,
)
from agents._timeouts import _stream_with_idle_timeout
from tools.wrappers import get_all_tools

# ---------------------------------------------------------------------------
# Module config — set once at startup by server.app
# ---------------------------------------------------------------------------

_provider: str = "anthropic"
_model: str = "MiniMax-M3"
_thinking: bool = False
_thinking_budget: int = 10_000
_mcp_tools: list = []  # extra LangChain tools loaded from MCP servers

# Per-session TokenCounter. Concurrent sessions were inflating each
# other's per-turn diffs before this was made per-session.
_token_counters: dict = {}
_token_counters_lock = threading.Lock()


def configure_model(
    model: str,
    thinking: bool = False,
    thinking_budget: int = 10_000,
    provider: str = "anthropic",
) -> None:
    """Called once at startup so the loop uses the configured provider/model
    + thinking settings. Also drops the cached TokenCounters so a model
    change in the same process doesn't silently misprice tokens under the
    old model name."""
    global _provider, _model, _thinking, _thinking_budget
    _provider = (provider or "anthropic").lower()
    _model = model
    _thinking = thinking
    _thinking_budget = thinking_budget
    with _token_counters_lock:
        _token_counters.clear()


def configure_tools(extra_tools: list | None = None) -> None:
    """Register additional LangChain tools (typically loaded from MCP servers
    by server.mcp_loader.load_mcp_tools). Called ONCE at server boot;
    passing `[]` or omitting the arg leaves the agent with just the native
    toolset."""
    global _mcp_tools
    _mcp_tools = list(extra_tools or [])


def get_mcp_tools() -> list:
    """Snapshot of the currently-registered MCP tools."""
    return list(_mcp_tools)


def get_token_counter(session_id: str):
    """Return the per-session TokenCounter, creating one on first use.
    Lock-guarded so concurrent get-or-create can't lose tokens."""
    with _token_counters_lock:
        tc = _token_counters.get(session_id)
        if tc is None:
            try:
                from memory.token_counter import TokenCounter
                tc = TokenCounter(model=_model)
            except Exception:
                return None
            _token_counters[session_id] = tc
        return tc


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

def _get_llm(
    *,
    provider: str | None = None,
    model: str | None = None,
    streaming: bool = True,
    thinking: bool | None = None,
):
    """Build the LangChain chat client for the configured provider.

    All kwargs are optional — defaults come from the module-level config
    set by `configure_model()`. Sub-agents (`tools/multi_agent.py`) pass
    overrides so they can use a different model without mutating module
    state.
    """
    eff_provider = (provider or _provider).lower()
    eff_model = model or _model
    eff_thinking = _thinking if thinking is None else thinking
    timeout = float(os.getenv("AGENT_LLM_TIMEOUT_SECS", "300") or 300)

    if eff_provider == "anthropic":
        kwargs = {"model": eff_model, "streaming": streaming, "timeout": timeout}
        if eff_thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": _thinking_budget}
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(**kwargs)

    from langchain_openai import ChatOpenAI

    if eff_provider == "minimax":
        api_key = os.getenv("MINIMAX_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("MINIMAX_API_KEY is not set.")
        base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1")
        return ChatOpenAI(
            model=eff_model, api_key=api_key, base_url=base_url,
            streaming=streaming, timeout=timeout,
            # OpenAI-compatible streaming hides usage by default — without
            # this, `usage_metadata` is empty on every AIMessage and the
            # per-call token_update event never fires. `stream_usage=True`
            # opts in to `stream_options={"include_usage": true}` upstream.
            stream_usage=True,
        )

    if eff_provider in ("openai-compatible", "openai"):
        api_key = os.getenv("AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("AGENT_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        if not api_key:
            raise RuntimeError(
                "AGENT_API_KEY (or OPENAI_API_KEY) is not set for openai-compatible provider."
            )
        kwargs = {
            "model": eff_model, "api_key": api_key,
            "streaming": streaming, "timeout": timeout,
            "stream_usage": True,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)

    raise RuntimeError(
        f"unknown provider '{eff_provider}'. Supported: anthropic, minimax, openai-compatible."
    )


# ---------------------------------------------------------------------------
# <think>...</think> splitter for OpenAI-compatible providers (MiniMax,
# DeepSeek, Qwen-thinking, etc.) that emit chain-of-thought as plain text.
# ---------------------------------------------------------------------------

class _ThinkingTagSplitter:
    _OPEN = "<think>"
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
                    out.append(("thinking" if self.in_thinking else "assistant", head))
                self._buf = self._buf[idx + len(tag):]
                self.in_thinking = not self.in_thinking
                continue
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
        max_n = min(len(s), len(tag) - 1)
        for n in range(max_n, 0, -1):
            if tag.startswith(s[-n:]):
                return n
        return 0


# ---------------------------------------------------------------------------
# Retry-on-transient-error for the LLM stream
# ---------------------------------------------------------------------------
# Retried (safe — same request, idempotent):
#   - TimeoutError, ConnectionError
#   - anthropic.{APITimeoutError,APIConnectionError,RateLimitError}
#   - openai.{APITimeoutError,APIConnectionError,RateLimitError}
# NOT retried (would mask a bug):
#   - 4xx / BadRequest / Auth errors / ValueError — won't succeed on retry.
# Env knobs:
#   AGENT_LLM_RETRY_ATTEMPTS        total attempts incl. initial (default 2 = 1 retry)
#   AGENT_LLM_RETRY_BACKOFF_S       seconds to sleep before retry (default 2.0)
#   AGENT_LLM_STREAM_IDLE_TIMEOUT_S raise TimeoutError if no chunk arrives within N
#                                    seconds (default 90).
#   AGENT_LLM_DEBUG                 set to 1 to log every LLM-stream chunk to stderr.
#                                    Filter with `grep '[llm-stream]'`.
_LLM_DBG = os.getenv("AGENT_LLM_DEBUG", "").lower() in ("1", "true", "yes")
_LLM_TAG = "[llm-stream]"


def _llm_dbg(msg: str) -> None:
    if _LLM_DBG:
        print(f"{_LLM_TAG} {msg}", file=sys.stderr, flush=True)


def _get_retryable_exceptions() -> tuple:
    """Lazy-load transient-error classes from installed provider SDKs.
    A provider that isn't installed is silently skipped."""
    classes: list = [TimeoutError, ConnectionError]
    try:
        from anthropic import (
            APITimeoutError as _Ato,
            APIConnectionError as _Acn,
            RateLimitError as _Arl,
        )
        classes += [_Ato, _Acn, _Arl]
    except ImportError:
        pass
    try:
        from openai import (
            APITimeoutError as _Oto,
            APIConnectionError as _Ocn,
            RateLimitError as _Orl,
        )
        classes += [_Oto, _Ocn, _Orl]
    except ImportError:
        pass
    return tuple(classes)


def _stream_with_retry(
    llm_with_tools,
    messages: list,
    reporter,
    *,
    max_attempts: int,
    backoff_s: float,
    idle_timeout_s: float,
    retryable: tuple,
):
    """Stream the LLM response, retrying on transient errors.

    Each attempt gets a fresh `aggregate`, `announced` set, and `splitter`
    so a failed attempt's partial state doesn't bleed into the next. On
    retryable failure, surfaces a one-line notice to the UI, sleeps
    `backoff_s`, then re-streams from scratch.

    Returns `(aggregate, splitter)` on success.
    Raises `RuntimeError` (wrapping the last provider exception) when
    all attempts fail.
    """
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        aggregate = None
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

        try:
            stream_iter = _stream_with_idle_timeout(
                llm_with_tools.stream(messages), idle_timeout_s,
            )
            for chunk in stream_iter:
                aggregate = chunk if aggregate is None else aggregate + chunk

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
                                _emit_text(t)
                        elif btype == "thinking":
                            t = block.get("thinking") or block.get("text") or ""
                            if t:
                                reporter.thinking_text(t, done=False)

                for tc in getattr(aggregate, "tool_calls", None) or []:
                    tcid = tc.get("id") or tc.get("name", "")
                    if tc.get("name") and tcid not in announced:
                        announced.add(tcid)
                        args = tc.get("args", {}) or {}
                        target = (
                            args.get("path") or args.get("command")
                            or args.get("query") or args.get("pattern")
                            or args.get("url") or args.get("action") or ""
                        )
                        reporter.tool_start(tc["name"], str(target)[:70])

            return aggregate, splitter

        except retryable as e:
            last_err = e
            if attempt + 1 >= max_attempts:
                raise RuntimeError(
                    f"LLM call failed after {max_attempts} attempt(s): "
                    f"{type(e).__name__}: {e}"
                ) from e
            _reason = {
                "TimeoutError": "timeout",
                "APITimeoutError": "timeout",
                "ConnectionError": "connection error",
                "APIConnectionError": "connection error",
                "RateLimitError": "rate limit",
            }.get(type(e).__name__, "provider error")
            try:
                reporter.assistant_text(
                    f"\n\n[LLM provider {_reason} — "
                    f"retry attempt {attempt + 1} of {max_attempts - 1}, "
                    f"waiting {backoff_s:.1f}s]\n\n",
                    done=False,
                )
            except Exception:
                pass
            time.sleep(backoff_s)

    raise RuntimeError(f"LLM call failed: {last_err}")


def _stream_model_call(llm_with_tools, messages: list[BaseMessage], session_id: str = "") -> AIMessage:
    """Stream one model call: forward text chunks + tool announcements
    through the reporter, return the aggregated assistant message.
    Also records the call to the per-session trace store (best-effort)."""
    from agents.reporter import get_reporter
    reporter = get_reporter()
    _t_call_start = time.monotonic()

    max_attempts = max(1, int(os.getenv("AGENT_LLM_RETRY_ATTEMPTS", "2")))
    backoff_s = max(0.0, float(os.getenv("AGENT_LLM_RETRY_BACKOFF_S", "2.0")))
    idle_timeout_s = max(1.0, float(os.getenv("AGENT_LLM_STREAM_IDLE_TIMEOUT_S", "90")))
    retryable = _get_retryable_exceptions()

    aggregate, splitter = _stream_with_retry(
        llm_with_tools,
        messages,
        reporter,
        max_attempts=max_attempts,
        backoff_s=backoff_s,
        idle_timeout_s=idle_timeout_s,
        retryable=retryable,
    )

    # Drain anything still in the splitter buffer (e.g. trailing text with
    # no closing tag) so we don't silently swallow the tail of a response.
    for channel, piece in splitter.flush():
        if channel == "thinking":
            reporter.thinking_text(piece, done=False)
        else:
            reporter.assistant_text(piece, done=False)

    if aggregate is None:
        return AIMessage(content="")

    ai = AIMessage(
        content=aggregate.content,
        tool_calls=list(getattr(aggregate, "tool_calls", None) or []),
        additional_kwargs=getattr(aggregate, "additional_kwargs", {}) or {},
        response_metadata=getattr(aggregate, "response_metadata", {}) or {},
        usage_metadata=getattr(aggregate, "usage_metadata", None),
    )

    # Record token usage + publish a live delta so the UI's per-turn
    # counter ticks (instead of waiting for the end-of-turn summary).
    try:
        tc = get_token_counter(session_id)
        usage = getattr(ai, "usage_metadata", None)
        if tc and usage:
            from memory.token_counter import TokenUsage
            in_delta = int(usage.get("input_tokens", 0) or 0)
            out_delta = int(usage.get("output_tokens", 0) or 0)
            cr = int((usage.get("cache_read_input_tokens") or
                      (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or
                      (usage.get("input_token_details") or {}).get("cache_read") or 0))
            cw = int((usage.get("cache_creation_input_tokens") or
                      (usage.get("prompt_tokens_details") or {}).get("cache_creation_tokens") or
                      (usage.get("input_token_details") or {}).get("cache_creation") or 0))
            tc.record(TokenUsage(
                input_tokens=in_delta,
                output_tokens=out_delta,
                cache_creation_tokens=cw,
                cache_read_tokens=cr,
            ))
            try:
                reporter.token_update(
                    input_delta=in_delta,
                    output_delta=out_delta,
                    cache_read_delta=cr,
                    cache_creation_delta=cw,
                )
            except Exception:
                pass
    except Exception:
        pass

    # Best-effort wire-level trace record.
    try:
        from memory.llm_trace import get_store, LLMCallRecord, serialize_messages
        sid = getattr(reporter, "session_id", "") or ""
        if sid:
            rec = LLMCallRecord(
                ts=time.time(),
                iteration=0,  # we don't track iter counts anymore
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
# History prep — build the LLM input from state
# ---------------------------------------------------------------------------

def _repair_orphan_tool_calls(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Strip tool_calls whose ToolMessage follow-up was lost (cancelled
    turn, etc.). Without this, OpenAI-compatible providers raise 400 on
    the next turn because every tool_call id must be followed by a
    matching ToolMessage. Synthesising fake results would lie to the
    model — better to drop the orphan tool_calls and let the model
    re-decide."""
    if not messages:
        return messages
    repaired: list[BaseMessage] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            call_ids = [tc.get("id") for tc in m.tool_calls if tc.get("id")]
            j = i + 1
            satisfied: set[str] = set()
            while j < len(messages) and isinstance(messages[j], ToolMessage):
                tcid = getattr(messages[j], "tool_call_id", None)
                if tcid:
                    satisfied.add(tcid)
                j += 1
            missing = [cid for cid in call_ids if cid not in satisfied]
            if missing:
                kept = [tc for tc in m.tool_calls if tc.get("id") in satisfied]
                content = m.content if m.content else ""
                if not kept and not content:
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

    # Drop orphan ToolMessages whose tool_call_id has no preceding AIMessage
    # in the surviving history (compaction can introduce these on MiniMax).
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
        else:
            cleaned.append(m)
    return cleaned


def _prepare_history(state: RunnerState, session_id: str) -> list:
    """Build the LLM input: take `messages` from state, repair orphans,
    auto-compact before the call. Auto-compaction runs here so the turn
    that crosses the threshold pays for the smaller compacted context,
    not the giant one."""
    raw = list(state.get("messages") or [])
    history = _repair_orphan_tool_calls(raw)
    history, did_compact, compact_info = maybe_compact(
        history, session_id=session_id,
    )
    if did_compact:
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
    return history


# ---------------------------------------------------------------------------
# Context chip — single helper, no diagnostics
# ---------------------------------------------------------------------------

def _publish_context(ai, session_id: str) -> None:
    """One call: tell the UI how full the context is, and feed the
    auto-compact trigger. Uses the provider's authoritative `input_tokens`."""
    try:
        from agents.reporter import get_reporter
        from memory.checkpointer import CONTEXT_WINDOW_TOKENS
        usage = getattr(ai, "usage_metadata", None) or {}
        input_total = int(usage.get("input_tokens", 0) or 0)
        if input_total <= 0:
            return
        get_reporter().context_update(
            used_tokens=input_total,
            budget_tokens=CONTEXT_WINDOW_TOKENS,
            compacting=False,
            threshold=int(_auto_compact_threshold()),
            cache_read=0,
            cache_creation=0,
            input_total=input_total,
        )
        record_llm_input_tokens(input_total, session_id=session_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# System prompt — cached per (workspace, git-sig, today)
# ---------------------------------------------------------------------------

def _git_signature(workspace: str) -> tuple:
    """Cheap signature for cache invalidation. Just .git/HEAD and
    .git/index mtime+size — no `git` subprocess."""
    sig: list = [str(workspace)]
    try:
        from pathlib import Path
        gitdir = Path(workspace) / ".git"
        if not gitdir.is_dir():
            return (sig, 0, 0)
        head = gitdir / "HEAD"
        idx = gitdir / "index"
        head_st = head.stat() if head.exists() else None
        idx_st = idx.stat() if idx.exists() else None
        sig.append(str(head_st.st_mtime_ns) if head_st else "0")
        sig.append(str(head_st.st_size) if head_st else "0")
        sig.append(str(idx_st.st_mtime_ns) if idx_st else "0")
        sig.append(str(idx_st.st_size) if idx_st else "0")
    except Exception:
        pass
    return tuple(sig)


def _build_system_prompt(state: RunnerState) -> tuple[str, str]:
    """Build the (static_base, dynamic_suffix) system-prompt pair. Two
    SystemMessages let MiniMax's automatic prefix cache hit on the static
    part turn-to-turn."""
    workspace = state.get("workspace", ".")
    today = date.today().isoformat()

    cache = state.get("_project_context_cache")
    sig = _git_signature(workspace)
    ctx = None
    if cache and isinstance(cache, dict) and cache.get("sig") == sig and cache.get("today") == today:
        ctx = cache["ctx"]
    else:
        ctx = ProjectContext.discover_with_git(workspace, today)
        try:
            state["_project_context_cache"] = {"sig": sig, "today": today, "ctx": ctx}
        except Exception:
            pass

    builder = (
        SystemPromptBuilder()
        .with_os(platform.system() or "unknown", platform.release() or "unknown")
        .with_model_family(current_model_name())
        .with_project_context(ctx)
        .with_orchestration_guidance(True)  # top-level loop only
        .with_mcp_tools(_mcp_tools)
    )
    extra = (state.get("project_context") or "").strip()
    if extra:
        builder.append_section(
            f"# Additional project preferences (follow exactly)\n{extra}"
        )
    return builder.render_split()


# ---------------------------------------------------------------------------
# The three nodes
# ---------------------------------------------------------------------------

def node_agent(state: RunnerState) -> dict:
    """One model call = one node invocation.

    Reads state.messages, auto-compacts if needed, runs the LLM call,
    writes the new AIMessage back as state.messages (REPLACE — the LLM
    always reads the latest list, with compaction already applied).
    """
    session_id = state.get("session_id") or ""
    history = _prepare_history(state, session_id)

    static_base, dynamic_suffix = _build_system_prompt(state)
    messages: list[BaseMessage] = []
    if static_base:
        messages.append(SystemMessage(content=static_base))
    if dynamic_suffix:
        messages.append(SystemMessage(content=dynamic_suffix))
    messages.extend(history)

    llm = _get_llm()
    bind_kwargs: dict = {}
    if _provider != "anthropic":
        # Anthropic supports parallel tool_use natively; OpenAI-compatible
        # providers need this flag explicitly.
        bind_kwargs["parallel_tool_calls"] = True
    llm_with_tools = llm.bind_tools(get_all_tools() + _mcp_tools, **bind_kwargs)
    ai = _stream_model_call(llm_with_tools, messages, session_id=session_id)

    _publish_context(ai, session_id)

    return {
        "messages": list(history) + [ai],
        "iterations": int(state.get("iterations", 0)) + 1,
    }


def node_tools(state: RunnerState) -> dict:
    """Execute the assistant's requested tools. Permissions + hooks run
    inside each tool's @_safe_tool wrapper — we don't duplicate that
    here. Returns the new tool messages; node_agent appends them on the
    next round-trip."""
    from agents.reporter import get_reporter
    reporter = get_reporter()

    result = ToolNode(get_all_tools() + _mcp_tools).invoke(state)

    for msg in result.get("messages", []):
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        name = getattr(msg, "name", "")
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        is_error = (
            content.startswith("Error:") or content.startswith("BLOCKED:")
            or "error" in first_line.lower()[:20]
        )
        reporter.tool_done(name, content or "(no output)", error=is_error)

    new_tool_messages = list(result.get("messages", []))
    existing = list(state.get("messages") or [])
    return {
        **result,
        "messages": existing + new_tool_messages,
    }


def should_continue(state: RunnerState) -> str:
    """Continue while the assistant requests tools. Otherwise END.

    Simple rule: the LAST message in the history is an AIMessage with
    tool_calls → node_tools. Anything else (no tool_calls, or the last
    message is a ToolMessage / HumanMessage) → __end__.

    Note: invalid_tool_calls is intentionally NOT routed back to
    node_tools — the model emitted malformed JSON it can't recover from
    without re-asking. Best to terminate the turn cleanly and let the
    user re-prompt.
    """
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "node_tools"
    return "__end__"