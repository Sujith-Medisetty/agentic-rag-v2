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
import sys
import threading
import time

import os

# ---------------------------------------------------------------------------
# LLM stream debug logging — opt-in via AGENT_LLM_DEBUG=1.
#
# When enabled, _stream_with_idle_timeout prints one line per chunk and
# one line per state transition (start / watchdog fire / done / error) to
# stderr. Filter in journalctl with:
#   journalctl -u ojas-backend | grep '[llm-stream]'
#
# Zero overhead when disabled (one env-var lookup at import time).
# ---------------------------------------------------------------------------
_LLM_DBG = os.getenv("AGENT_LLM_DEBUG", "").lower() in ("1", "true", "yes")
_LLM_TAG = "[llm-stream]"


def _llm_dbg(msg: str) -> None:
    if _LLM_DBG:
        print(f"{_LLM_TAG} {msg}", file=sys.stderr, flush=True)

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
from agents._timeouts import (
    _call_with_wall_clock_guard,
    DEFAULT_NODE_BODY_TIMEOUT_S,
    NODE_BODY_TIMEOUT_ENV,
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
# Per-session TokenCounter. Previously a single process-wide singleton,
# which made concurrent sessions' per-turn `before/after` diffs in
# session_runner.run_turn() include each other's LLM calls — inflating
# per-turn and session totals 3-9x. Now keyed by session_id so each
# session owns its own counter and diffs are naturally correct.
_token_counters: dict = {}
_token_counters_lock = threading.Lock()


def configure_model(
    model: str,
    thinking: bool = False,
    thinking_budget: int = 10_000,
    provider: str = "anthropic",
) -> None:
    """Called once at startup so the loop uses the configured provider/model +
    thinking settings. (Iteration/token/time limits are the per-invocation run
    budget — see reset_run_budget.)

    Also drops the cached TokenCounters so the next get_token_counter() call
    rebuilds them against the NEW model. Without this, a model change in the
    same process would silently misprice every turn (the cached counters
    still price under the old model name)."""
    global _provider, _model, _thinking, _thinking_budget
    _provider = (provider or "anthropic").lower()
    _model = model
    _thinking = thinking
    _thinking_budget = thinking_budget
    with _token_counters_lock:
        _token_counters.clear()  # force rebuild on next get_token_counter() call


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
        self.session_id: str = ""  # set in reset(); used by _tokens_used()

    def reset(self, *, max_iters: int, max_tokens: int, max_seconds: int,
              no_progress_limit: int, session_id: str = "") -> None:
        self.max_iters = max(0, int(max_iters or 0))
        self.max_tokens = max(0, int(max_tokens or 0))
        self.max_seconds = max(0, int(max_seconds or 0))
        self.no_progress_limit = max(0, int(no_progress_limit or 0))
        self.iters = 0
        self.start = time.monotonic()
        self._last_sig = None
        self._repeat_streak = 0
        self.session_id = session_id or ""

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
        tc = get_token_counter(self.session_id)
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
    no_progress_limit: int = 8, session_id: str = "",
) -> None:
    """Called once per invocation (main._run_graph) before streaming the graph."""
    _run_budget.reset(
        max_iters=max_iters, max_tokens=max_tokens, max_seconds=max_seconds,
        no_progress_limit=no_progress_limit, session_id=session_id,
    )


def get_token_counter(session_id: str):
    """Return the per-session TokenCounter, creating one on first use.

    The lock guards the get-or-create race between the event-loop thread
    (which calls this at the top and bottom of run_turn to compute the
    per-turn diff) and the executor thread (which calls it from
    `_stream_model_call` to record live token usage). Without the lock,
    a concurrent get-or-create could lose tokens: two callers see no
    entry, both create new counters, only one is stored, and the other
    caller's `record()` calls land in a counter nobody will ever read.
    """
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
        # One stat() per file instead of two (mtime + size from the same
        # stat result); these run on every cache-staleness check.
        head_st = head.stat() if head.exists() else None
        idx_st  = idx.stat()  if idx.exists()  else None
        sig.append(str(head_st.st_mtime_ns) if head_st else "0")
        sig.append(str(head_st.st_size)     if head_st else "0")
        sig.append(str(idx_st.st_mtime_ns)  if idx_st  else "0")
        sig.append(str(idx_st.st_size)      if idx_st  else "0")
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
# Cache diagnostics — pinpoint WHY a turn missed the provider's prefix cache.
#
# Set OJAS_CACHE_DIAG=0 to silence. For every LLM call we log a one-line
# verdict comparing this call's outgoing prompt against the PREVIOUS call's:
#   - cached / fresh token split (from usage_metadata)
#   - whether [static][dynamic] changed (→ a prompt-rebuild bug)
#   - whether the bound tools changed (→ tool-ordering bug)
#   - the longest common message-prefix length vs last call (→ where history
#     diverged; if it covers all of last call's messages, our prefix is stable
#     and a low cache_read is PROVIDER-SIDE, not our code)
# So on the next run, a miss (like the 22k-fresh file-write turn) tells us
# immediately which of those three it was.
# ---------------------------------------------------------------------------

_CACHE_DIAG_PREV: dict[str, dict] = {}


def _cache_diag_enabled() -> bool:
    # Opt-in. The per-turn cost (hashing the prompt + fingerprinting every
    # message in history) is only worth paying when actively debugging a
    # prompt-cache miss. Off by default; set OJAS_CACHE_DIAG=1 to enable.
    return (os.getenv("OJAS_CACHE_DIAG", "0").strip().lower()
            in ("1", "true", "yes", "on"))


def _message_fingerprint(m) -> str:
    """Stable short hash of one message's wire-relevant bytes (type + content
    + tool_calls). Two messages with the same fingerprint serialize to the
    same prefix bytes, so the provider's cache should treat them identically."""
    import hashlib
    cls = type(m).__name__
    content = getattr(m, "content", "")
    try:
        content_s = content if isinstance(content, str) else json.dumps(content, default=str, sort_keys=True)
    except Exception:
        content_s = str(content)
    tcs = ""
    if getattr(m, "tool_calls", None):
        try:
            tcs = json.dumps(
                [(tc.get("name"), tc.get("id"), tc.get("args")) for tc in m.tool_calls],
                default=str, sort_keys=True,
            )
        except Exception:
            tcs = str(m.tool_calls)
    h = hashlib.sha1(f"{cls}\x00{content_s}\x00{tcs}".encode("utf-8", "replace"))
    return h.hexdigest()[:12]


def _log_cache_diag(
    *, session_id: str, static_base: str, dynamic_suffix: str,
    tool_names: list[str], messages: list, cache_read: int,
    cache_creation: int, input_total: int,
) -> None:
    """Emit the per-call cache verdict and stash this call's fingerprints for
    the next call to diff against. Never raises — diagnostics must not break a
    turn."""
    if not _cache_diag_enabled():
        return
    try:
        import hashlib
        import logging
        st_hash = hashlib.sha1(
            (static_base or "").encode("utf-8", "replace")
        ).hexdigest()[:12]
        dyn_hash = hashlib.sha1(
            (dynamic_suffix or "").encode("utf-8", "replace")
        ).hexdigest()[:12]
        tools_hash = hashlib.sha1(
            ("\x00".join(tool_names)).encode("utf-8", "replace")
        ).hexdigest()[:12]
        fps = [_message_fingerprint(m) for m in messages]
        fresh = max(0, input_total - cache_read - cache_creation)

        key = session_id or "_default"
        prev = _CACHE_DIAG_PREV.get(key)
        verdict_parts: list[str] = []
        if prev is None:
            verdict_parts.append("first-call(cold)")
        else:
            # STATIC-CHANGED is the expensive one (busts the big ~14k cached
            # system prompt). DYN-CHANGED only busts the small dynamic suffix +
            # history after it (the static prompt before it stays cached).
            if prev["st"] != st_hash:
                verdict_parts.append("STATIC-CHANGED")
            if prev["dyn"] != dyn_hash:
                verdict_parts.append("DYN-CHANGED")
            if prev["tools"] != tools_hash:
                verdict_parts.append("TOOLS-CHANGED")
            # Longest common prefix between this call's history fingerprints
            # and the previous call's.
            common = 0
            for a, b in zip(prev["fps"], fps):
                if a != b:
                    break
                common += 1
            prev_len = len(prev["fps"])
            if common >= prev_len:
                verdict_parts.append(f"prefix-stable(+{len(fps) - common}new)")
            else:
                verdict_parts.append(
                    f"PREFIX-DIVERGED@msg{common}/{prev_len}"
                )

        logging.getLogger(__name__).info(
            "cache-diag: in=%d cached=%d fresh=%d (%.0f%% cached) | msgs=%d "
            "st=%s dyn=%s tools=%s | %s",
            input_total, cache_read, fresh,
            (100.0 * cache_read / input_total) if input_total else 0.0,
            len(fps), st_hash, dyn_hash, tools_hash, " ".join(verdict_parts),
        )
        # Also append to a dedicated file so the line is retrievable even when
        # the host's logging config (uvicorn/systemd) doesn't surface app
        # INFO logs. Path override: OJAS_CACHE_DIAG_FILE.
        try:
            from pathlib import Path
            from datetime import datetime, timezone
            diag_path = os.getenv("OJAS_CACHE_DIAG_FILE") or str(
                Path.home() / ".agent" / "cache-diag.log"
            )
            p = Path(diag_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with p.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{stamp} sid={key[:8]} in={input_total} cached={cache_read} "
                    f"fresh={fresh} ({(100.0*cache_read/input_total) if input_total else 0:.0f}% cached) "
                    f"msgs={len(fps)} st={st_hash} dyn={dyn_hash} tools={tools_hash} | "
                    f"{' '.join(verdict_parts)}\n"
                )
        except Exception:
            pass
        _CACHE_DIAG_PREV[key] = {
            "st": st_hash, "dyn": dyn_hash, "tools": tools_hash, "fps": fps,
        }
    except Exception:
        pass

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


# ---------------------------------------------------------------------------
# LLM retry-on-transient-error
# ---------------------------------------------------------------------------
# The chat client already enforces a wall-clock timeout via
# `AGENT_LLM_TIMEOUT_SECS` (default 300s), so a hung provider raises
# TimeoutError cleanly. What was MISSING before: any try/except around
# the streaming call, so a single timeout killed the turn (the audit
# noted "provider hang raises uncaught → uvicorn 500"). This layer
# catches transient errors and retries once before bubbling up.
#
# Retried (safe — same request, idempotent):
#   - TimeoutError, ConnectionError
#   - anthropic.APITimeoutError / APIConnectionError / RateLimitError
#   - openai.{APITimeoutError,APIConnectionError,RateLimitError}
#
# NOT retried (would mask a bug):
#   - 4xx / BadRequestError / Auth errors / OutputGuardException /
#     ValueError — those will never succeed on retry.
#
# Per-tool failures (bash, git, web, etc.) are NOT retried here — the
# tool surfaces its timeout as a structured error string to the LLM and
# the LLM decides what to do. Auto-retrying a bash / write_file could
# re-run a side-effecting command.
#
# File tools (read_file/write_file/edit_file/grep/glob) intentionally
# have NO timeout and NO retry — a stuck NFS mount or 10 MB file on slow
# disk can hang the worker, but the cost of a false-positive timeout
# (cancelling a 20 s legitimate read) is worse than the rare hang.
#
# Env knobs:
#   AGENT_LLM_RETRY_ATTEMPTS        total attempts incl. initial (default 2 = 1 retry).
#                                    Set to 3 to give a stalled provider 2 retries.
#   AGENT_LLM_RETRY_BACKOFF_S       seconds to sleep before the retry (default 2.0)
#   AGENT_LLM_STREAM_IDLE_TIMEOUT_S raise TimeoutError if no chunk arrives within N
#                                    seconds (default 90). Set higher (e.g. 300) for
#                                    OpenAI-compatible providers that sometimes stall
#                                    silently mid-response. Catches the same hang
#                                    shapes as the 300s httpx total timeout, just
#                                    faster.
#   AGENT_LLM_DEBUG                 set to 1 to log every LLM-stream chunk + state
#                                    transition to stderr (filter with
#                                    `grep '[llm-stream]'`). Zero overhead when off.
def _get_retryable_exceptions() -> tuple:
    """Lazy-load transient-error classes from installed provider SDKs.
    A provider that isn't installed is silently skipped (its classes
    won't appear in the retry tuple)."""
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


def _stream_with_idle_timeout(stream_iter, idle_timeout_s: float):
    """Wrap a sync stream iterator with an idle-timeout watchdog.

    Sync streams can't be timed out cleanly with `asyncio.wait_for` (the
    iterator isn't a coroutine) and `signal.alarm` doesn't interrupt
    blocking network IO across threads. So we run the iterator in a
    daemon worker thread that pushes chunks onto a `queue.Queue`; the
    main thread pulls with `queue.get(timeout=idle_timeout_s)` and
    raises `TimeoutError` if no chunk arrives within the window.

    The watchdog catches ALL three hang shapes that the bare httpx
    `Timeout(total=...)` misses:
      1. Socket silently stops sending chunks mid-stream (TCP window
         exhaustion, server-side stall, dropped connection that hasn't
         yet surfaced as an exception).
      2. Provider keeps the HTTP connection open but stops emitting
         tokens (model deadlocked server-side, queue backed up).
      3. LangChain internal generator wedged before reaching the SDK.

    Note on the orphan worker: we can't actually cancel the underlying
    httpx request from the consumer side. When the consumer raises
    TimeoutError, the worker keeps pulling until the socket dies
    naturally (the 300s httpx total timeout will eventually fire). The
    worker is a daemon, so it dies with the agent loop process.
    """
    import queue as _queue
    import threading as _threading

    q: "_queue.Queue[tuple]" = _queue.Queue(maxsize=1024)
    _start_t = time.monotonic()
    _llm_dbg(f"START idle_timeout_s={idle_timeout_s} stream={type(stream_iter).__name__}")

    def _producer() -> None:
        try:
            for chunk in stream_iter:
                if _LLM_DBG:
                    _c = getattr(chunk, "content", None)
                    _ctype = type(_c).__name__
                    if isinstance(_c, str):
                        _clen = len(_c)
                    elif isinstance(_c, list):
                        _clen = f"list[{len(_c)}]"
                    elif _c is None:
                        _clen = "None"
                    else:
                        _clen = "?"
                    _tc = getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None)
                    _tn = len(_tc) if _tc else 0
                    _t = time.monotonic() - _start_t
                    _llm_dbg(
                        f"t={_t:8.2f}s chunk content_type={_ctype} content_len={_clen} "
                        f"tool_chunks={_tn} qsize={q.qsize()}"
                    )
                q.put(("chunk", chunk))
        except BaseException as e:  # noqa: BLE001 — any exception type surfaces to consumer
            _t = time.monotonic() - _start_t
            _llm_dbg(f"t={_t:8.2f}s producer EXC {type(e).__name__}: {e!r}")
            q.put(("error", e))
        finally:
            _t = time.monotonic() - _start_t
            _llm_dbg(f"t={_t:8.2f}s producer DONE")
            q.put(("done", None))

    t = _threading.Thread(target=_producer, daemon=True, name="ojas-llm-stream")
    t.start()
    _chunk_count = 0

    while True:
        try:
            kind, payload = q.get(timeout=idle_timeout_s)
        except _queue.Empty:
            _t = time.monotonic() - _start_t
            _llm_dbg(
                f"t={_t:8.2f}s WATCHDOG FIRED idle_timeout_s={idle_timeout_s} "
                f"producer_alive={t.is_alive()} qsize={q.qsize()} chunks_seen={_chunk_count}"
            )
            raise TimeoutError(
                f"LLM stream idle for {idle_timeout_s}s — no chunk received"
            )

        if kind == "done":
            _t = time.monotonic() - _start_t
            _llm_dbg(
                f"t={_t:8.2f}s CONSUMER DONE chunks={_chunk_count} "
                f"producer_alive={t.is_alive()} qsize={q.qsize()}"
            )
            return
        if kind == "error":
            _t = time.monotonic() - _start_t
            _llm_dbg(f"t={_t:8.2f}s CONSUMER RAISE {type(payload).__name__}: {payload!r}")
            # Re-raise the producer's exception verbatim — it'll flow
            # through the retry layer's `except retryable as e:` arm
            # if it matches; otherwise it bubbles uncaught.
            raise payload

        _chunk_count += 1
        yield payload


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

    Each attempt gets a fresh `aggregate`, `announced` set, and
    `splitter` so a failed attempt's partial state doesn't bleed into
    the next attempt. On retryable failure, surfaces a one-line notice
    to the UI via `reporter.assistant_text`, sleeps `backoff_s`, then
    re-streams from scratch.

    The idle-timeout watchdog (`_stream_with_idle_timeout`) sits inside
    the retry loop so a mid-stream socket pause triggers `TimeoutError`
    on the same attempt — the retry layer catches it identically to a
    total-timeout from httpx. Without the watchdog, a stream that
    stops emitting at second 5 wouldn't surface until the 300s httpx
    total budget expired.

    Returns `(aggregate, splitter)` on success.
    Raises `RuntimeError` (wrapping the last provider exception) when
    all attempts fail.
    """
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        # Per-attempt state. Critical to reset these so a retry doesn't
        # concatenate partial output from a failed attempt with chunks
        # from the next attempt.
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
            # Idle-timeout watchdog sits INSIDE the retry loop so a
            # mid-stream socket pause raises TimeoutError on the current
            # attempt — caught by the same `except retryable as e` arm
            # below, retried once, then bubbles.
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

            return aggregate, splitter  # success — exit retry loop

        except retryable as e:
            last_err = e
            if attempt + 1 >= max_attempts:
                # Out of retries — re-raise as a more informative error.
                # The wrapping makes the failure mode obvious in the
                # agent trace (was a hidden provider exception before).
                raise RuntimeError(
                    f"LLM call failed after {max_attempts} attempt(s): "
                    f"{type(e).__name__}: {e}"
                ) from e
            # Map the exception class to a short, user-friendly reason so the
            # chat surfaces a clear "why we're retrying" instead of a raw SDK
            # class name. Falls back to "provider error" for anything else.
            _reason = {
                "TimeoutError": "timeout",
                "APITimeoutError": "timeout",
                "ConnectionError": "connection error",
                "APIConnectionError": "connection error",
                "RateLimitError": "rate limit",
            }.get(type(e).__name__, "provider error")
            # The retry we're about to start is numbered (attempt+1) of 0..max-1,
            # so surface it as "retry attempt N of M" where M = max_attempts-1
            # retries remain after the current failed one.
            _retry_n = attempt + 1
            _retries_left = max_attempts - 1
            # Notify UI before backing off. Best-effort — never let the
            # notification itself fail the retry.
            try:
                reporter.assistant_text(
                    f"\n\n[LLM provider {_reason} — "
                    f"retry attempt {_retry_n} of {_retries_left}, "
                    f"waiting {backoff_s:.1f}s]\n\n",
                    done=False,
                )
            except Exception:
                pass
            time.sleep(backoff_s)

    # Defensive — the for-loop above always either returns or raises.
    raise RuntimeError(f"LLM call failed: {last_err}")


def _stream_model_call(llm_with_tools, messages: list[BaseMessage], session_id: str = "") -> AIMessage:
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

    # Retry config. `AGENT_LLM_RETRY_ATTEMPTS` defaults to 2 = 1 retry.
    # Set to 3 to give a stalled provider two chances to recover; the chat
    # surfaces "[LLM provider <reason> — retry attempt N of M, waiting Xs]"
    # before each retry so the user sees progress instead of silence.
    max_attempts = max(1, int(os.getenv("AGENT_LLM_RETRY_ATTEMPTS", "2")))
    backoff_s = max(0.0, float(os.getenv("AGENT_LLM_RETRY_BACKOFF_S", "2.0")))
    # Idle-timeout watchdog (default 90s). Catches mid-stream socket
    # pauses that the 300s httpx total timeout would otherwise wait
    # minutes to surface. Set to a very large value to effectively
    # disable (e.g. 86400 for a 24h ceiling). 300 (5 min) is the recommended
    # setting for slow OpenAI-compatible providers (e.g. MiniMax).
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
        tc = get_token_counter(session_id)
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


# Heavy tool-call arg keys — DISABLED 2026-06-15 per user direction
# (the bash-output and per-tool-result caps downstream are sufficient
# protection; the agent needs the full write_file content / edit_file
# new_string + old_string / bash command on each turn).
_TOOL_CALL_LARGE_ARG_KEYS = ()


def _truncate_tool_call_args(tool_calls: list) -> tuple[list, int]:
    """No-op: A2 truncation was removed 2026-06-15 per user direction.
    Bash-output caps and per-tool-result caps downstream are sufficient
    protection, so the per-arg cap (was 800 chars on new_string /
    old_string / content / command) is no longer needed.

    Returns the input list reference untouched plus n=0. Kept as a
    function so the call site at line 841 still type-checks; can be
    deleted when the cap is restored or the call site is rewritten."""
    return tool_calls, 0


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


def _handle_budget_pause(pause: dict) -> dict:
    """Emit the pause notice and return the no-message state update that routes
    should_continue → END at a clean, checkpointed boundary. Re-running the same
    thread_id resumes from here with a fresh budget — no crash, no lost work."""
    from agents.reporter import get_reporter
    try:
        get_reporter().tool_done("budget", f"paused: {pause['detail']}", error=False)
    except Exception:
        pass
    # No "messages" update ⇒ last message stays a ToolMessage/Human ⇒ END.
    return {"paused": True, "pause_reason": pause}


def _resolve_system_prompt_pair(state: RunnerState) -> tuple[str, str]:
    """Return the (static_base, dynamic_suffix) system-prompt pair, reused for
    the whole run so MiniMax's automatic prefix cache hits on the static part.
    Accepts either form from state (legacy `system_prompt` string OR the new
    pair) and rebuilds if absent/stale."""
    cached = state.get("system_prompt_pair")
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        return cached[0], cached[1]
    # Legacy fallback: older checkpoints stored a single string under
    # `system_prompt`. Rebuild the pair and trust render_split's backward-compat
    # path (it returns the whole prompt as static if the boundary is missing).
    legacy = state.get("system_prompt")
    if isinstance(legacy, str) and legacy:
        return legacy, ""
    return _build_system_prompt(state)


def _prepare_history(state: RunnerState, session_id: str) -> list:
    """Build the LLM input history: take `live_messages` (per-turn post-compact
    working set) if set else `messages` (append-only accumulator), repair any
    orphaned tool calls, then auto-compact BEFORE the LLM call so the turn that
    crosses the threshold pays for the smaller compacted context, not the giant
    one.

    History is APPEND-ONLY between compactions — we deliberately do NOT
    mask/strip/truncate the middle every turn. Those per-turn rewrites changed
    bytes partway through the prompt, busting the provider's prefix cache from
    the first changed message onward (the "cache keeps missing" symptom). With
    append-only history the whole prior prompt is served from cache and only the
    newest message is billed fresh; total size is bounded by maybe_compact (one
    infrequent cache reset). (`mask_old_observations` / `_strip_old_thinking` /
    `_truncate_live_history` are kept defined for tests/back-compat, not applied.)
    """
    from memory.checkpointer import maybe_compact, _auto_compact_threshold
    raw_history = list(state.get("live_messages") or state.get("messages", []))
    history = _repair_orphan_tool_calls(raw_history)
    # Pass the live todo list so it survives the pure-replace compaction
    # (re-injected verbatim after the summary; otherwise it lives only in the
    # TodoWrite tool results that compaction discards).
    history, did_compact, compact_info = maybe_compact(
        history, session_id=session_id, todos=state.get("last_todos") or [],
    )
    if did_compact:
        # Chat-visible notification. The post-LLM publish is the single source
        # of truth for the chip, so no `context_update` here (pre-LLM local
        # estimates have drifted from the provider's real number and made the
        # chip bounce).
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


def _assemble_messages(static_base: str, dynamic_suffix: str,
                       history: list) -> list[BaseMessage]:
    """[static SystemMessage, dynamic SystemMessage, *history]. Two
    SystemMessages in a row exposes the static/dynamic split to providers that
    prefix-cache on identical prefixes — the second message busts the cache for
    the dynamic part only."""
    messages: list[BaseMessage] = []
    if static_base:
        messages.append(SystemMessage(content=static_base))
    if dynamic_suffix:
        messages.append(SystemMessage(content=dynamic_suffix))
    messages.extend(history)
    return messages


def _publish_context_usage(ai, session_id: str) -> None:
    """Publish the context-used value to the chip and feed the auto-compact
    trigger, using the provider's authoritative `input_tokens`.

    The chip shows the TOTAL prompt size (same "In 78k" the user sees in the LLM
    stats); the auto-compact trigger uses the same number so chip and trigger
    stay in lockstep. Cache fields are surfaced in the tooltip.

    Provider semantics (verified live against MiniMax M3): `usage.input_tokens`
    is the TOTAL prompt — uncached + cache_read + cache_creation combined. The
    cache fields are SUBSETS of `input_tokens`, NOT separate additions, so we
    must not add them on top (that double-counts: e.g. 78k total + 77k cache
    would show 311% of a 50k threshold for a 78k prompt). Same shape for OpenAI
    standard (`prompt_tokens` / `prompt_tokens_details.cached_tokens`). On
    Anthropic native, `input_tokens` is the UNCACHED portion only and cache
    fields are reported separately; we clamp cache fields to `input_tokens` so
    the tooltip breakdown stays sensible across shapes."""
    try:
        from agents.reporter import get_reporter
        from memory.checkpointer import _auto_compact_threshold, record_llm_input_tokens
        _usage = getattr(ai, "usage_metadata", None) or {}
        _input_total = int(_usage.get("input_tokens", 0) or 0)
        _cache_creation, _cache_read = _extract_cache_fields(_usage)
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
            # session_id keys the cross-turn module dict — without it the
            # per-context ContextVar would reset to 0 next turn and the trigger
            # would never fire.
            record_llm_input_tokens(_input_total, session_id=session_id)
    except Exception:
        pass


def _emit_cache_diag(ai, session_id: str, static_base: str,
                     dynamic_suffix: str, messages: list) -> None:
    """Per-call cache verdict (log-only; opt-in via OJAS_CACHE_DIAG=1). Tells us
    WHICH thing busted the cache on a low-cache_read turn: our system prompt,
    our tool order, or a history-prefix divergence — vs a stable prefix (→ the
    miss is provider-side, not our code)."""
    try:
        _tool_names = [getattr(t, "name", type(t).__name__)
                       for t in (get_all_tools() + _mcp_tools)]
        _u = getattr(ai, "usage_metadata", None) or {}
        _it = int(_u.get("input_tokens", 0) or 0)
        _cc, _cr = _extract_cache_fields(_u)
        if _cr > _it:
            _cr = _it
        if _cc > _it:
            _cc = _it
        _log_cache_diag(
            session_id=session_id, static_base=static_base,
            dynamic_suffix=dynamic_suffix, tool_names=_tool_names,
            messages=messages, cache_read=_cr, cache_creation=_cc,
            input_total=_it,
        )
    except Exception:
        pass


def node_agent(state: RunnerState) -> dict:
    """One model call = one node invocation.

    Before each call we consult the per-invocation run budget. If a budget is
    hit (iterations / tokens / wall-clock / stall) we PAUSE gracefully: return
    without a new assistant message so should_continue routes to END at a clean,
    checkpointed boundary (tool results already delivered). Re-running the same
    thread_id resumes from here with a fresh budget — no crash, no lost work.

    Wall-clock guard: the entire body runs inside _call_with_wall_clock_guard
    with a budget of AGENT_NODE_BODY_TIMEOUT_S (default 600s). This catches
    hangs in the pre-stream phase (`_prepare_history` / `maybe_compact` /
    SqliteSaver read), the SDK request setup, the LLM call itself when the
    per-chunk _stream_with_idle_timeout doesn't fire (slow-but-alive stream),
    and the post-stream bookkeeping. On timeout, TimeoutError propagates out
    of the LangGraph stream and lands in `run_turn:318`'s `except Exception`
    arm, which publishes an `error` event + `assistant_text(done=True)` +
    persists `[error] <msg>` + fires `turn_summary`. The next user message
    resumes from the last good LangGraph checkpoint — no work lost beyond the
    hung call's output.

    Why not _handle_budget_pause? Two reasons: (1) the pause pattern returns
    `{"paused": True}` BEFORE the LangGraph checkpoint write at the pause
    boundary — there's no checkpoint at the hang point to pause AT, so the
    next resume would re-enter the same hung call. (2) `reporter` is bound to
    a ContextVar that doesn't propagate across `threading.Thread` boundaries
    (only across `asyncio.run_in_executor` + `ctx.run`), so `tool_done` calls
    from inside the guard's worker would silently vanish. The raise path runs
    in the parent thread where the reporter is live.
    """
    session_id = state.get("session_id") or ""
    pause = _run_budget.check()
    if pause is not None:
        return _handle_budget_pause(pause)

    # Read the timeout once per node invocation. Defensive parsing: bad env
    # values fall back to the default rather than crashing the turn.
    try:
        body_timeout_s = float(
            os.getenv(NODE_BODY_TIMEOUT_ENV, str(DEFAULT_NODE_BODY_TIMEOUT_S))
            or DEFAULT_NODE_BODY_TIMEOUT_S
        )
    except ValueError:
        body_timeout_s = DEFAULT_NODE_BODY_TIMEOUT_S
    body_timeout_s = max(1.0, body_timeout_s)

    def _node_agent_body() -> dict:
        # Closure captures `state` and `session_id` from the outer scope.
        # Both are read-only for the lifetime of this call — Python closures
        # are thread-safe to invoke. If `node_agent`'s signature is ever
        # refactored to take `state` as a kwarg, this closure needs to be
        # updated.
        iterations = int(state.get("iterations", 0)) + 1
        static_base, dynamic_suffix = _resolve_system_prompt_pair(state)
        history = _prepare_history(state, session_id)
        messages = _assemble_messages(static_base, dynamic_suffix, history)

        llm = _get_llm()
        # For OpenAI-compatible providers (openai / openai-compatible / minimax),
        # explicitly request parallel tool calls. `parallel_tool_calls=True` is
        # the Chat Completions API default, but custom OpenAI-compatible
        # endpoints sometimes default to `false` or strip the flag entirely —
        # passing it explicitly makes the intent unambiguous in the wire request.
        # Anthropic supports parallel tool_use blocks natively and doesn't take
        # this flag, so we skip it for that provider.
        bind_kwargs: dict = {}
        if _provider != "anthropic":
            bind_kwargs["parallel_tool_calls"] = True
        llm_with_tools = llm.bind_tools(get_all_tools() + _mcp_tools, **bind_kwargs)
        ai = _stream_model_call(llm_with_tools, messages, session_id=session_id)
        _run_budget.record(ai)

        _publish_context_usage(ai, session_id)
        _emit_cache_diag(ai, session_id, static_base, dynamic_suffix, messages)

        # Persist the post-process history as the LLM input for the NEXT turn.
        # Two channels: `messages` (add_messages reducer — append-only audit log)
        # and `live_messages` (REPLACE reducer — the LLM only ever reads this, so
        # it stays bounded by the auto-compact threshold).
        return {
            "messages": [ai],
            "live_messages": list(history) + [ai],
            "iterations": iterations,
            "system_prompt_pair": [static_base, dynamic_suffix],
        }

    return _call_with_wall_clock_guard(
        _node_agent_body, body_timeout_s, label="node_agent"
    )


def _read_todo_store(session_id: str | None) -> list[dict]:
    """Read the on-disk todo store for a session.

    Returns the current todo list (empty list if the file is missing,
    malformed, or there is no session). The TodoWrite tool writes to
    `<session_state_dir(session_id)>/.clawd-todos.json` (see
    `tools.utils._todo_store_path` and `server.session_runner.
    session_state_dir`). Reading the file directly is the only
    reliable way to know the agent's current plan, since LangGraph
    tools can't write back into RunnerState from inside a tool call.

    Used by `node_tools` (to capture post-TodoWrite state into
    `state["last_todos"]`) and by `node_force_todo_sync` (to print
    the open items in the nudge message).
    """
    if not session_id:
        return []
    try:
        from server.session_runner import session_state_dir
        path = session_state_dir(session_id) / ".clawd-todos.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        # Never let a sync-nudge crash the loop because the todo
        # file is unreadable — fall back to "no open todos" and
        # let the agent terminate normally.
        return []


def _capture_todos_from_tool_results(state: RunnerState, tool_messages: list) -> list[dict]:
    """Return the freshest todo list observed in the just-finished tool batch.

    Walks the tool result messages in order; if any of them is a
    TodoWrite result (the wrapper returns a `Updated todo list: ...`
    string), the underlying `todo_write` function already wrote the
    new list to disk, so a `_read_todo_store` call picks it up. We
    don't try to parse the string (the JSON is on disk, not in the
    message); we just trigger the read.

    If no TodoWrite happened in this batch, return whatever was
    already in `state["last_todos"]` (so the gate has a stable
    view across turns where TodoWrite isn't called).
    """
    if any(getattr(m, "name", "") == "TodoWrite" for m in tool_messages):
        return _read_todo_store(state.get("session_id"))
    return list(state.get("last_todos") or [])


def node_force_todo_sync(state: RunnerState) -> dict:
    """Final-turn sync nudge.

    The agent has signalled completion (no tool_calls on its last
    message) but the todo store still has open items. This is the
    bug the user has been reporting: the agent finishes the work,
    sends its summary, and the UI plan panel sits there with
    `pending` / `in_progress` rows the user has to stare at.

    We inject a hard SystemMessage that lists the open items by
    name and demands a TodoWrite call before the next reply. We
    also set `todo_sync_nudged=True` so `should_continue` only
    fires this gate ONCE per turn — after the nudge, the agent
    either calls TodoWrite (which updates `last_todos` via
    `node_tools`) or sends a final message; either way we
    terminate on the next `should_continue` check.

    The nudge is appended to `live_messages` (not `messages`) so
    the audit log stays clean — only the working set the LLM sees
    is mutated. Without this, the audit log would carry a
    SystemMessage the user never wrote and that the UI would
    render as a "system" line in the conversation.
    """
    last_todos = list(state.get("last_todos") or [])
    open_items = [t for t in last_todos if t.get("status") in ("pending", "in_progress")]
    if not open_items:
        # Edge case: `last_todos` was just emptied by a TodoWrite
        # in `node_tools`. Nothing to nudge. Just flip the flag
        # and let `should_continue` route to __end__.
        return {"todo_sync_nudged": True}

    # Build a precise, scannable list. Showing the agent its own
    # todo content (not a generic "you have N open todos") is the
    # single biggest correctness lever — the model recognises its
    # own plan and either ticks the boxes or admits the items
    # were dropped/skipped.
    bullets = "\n".join(
        f"  - [{t.get('status', '?')}] {t.get('content', '?')}"
        for t in open_items
    )
    nudge = (
        "FINAL-TURN TODO SYNC (mandatory). Before you finish this "
        "turn you MUST call TodoWrite to reconcile the following "
        "open items. For each, set status to `completed` (if you "
        "actually did it) or remove it from the list (if you "
        "decided to skip it). DO NOT leave any of these in "
        "`pending` or `in_progress` when you finish — the user "
        "watches the plan panel in real time and a stale row means "
        "they think the work is unfinished.\n\n"
        f"Open items:\n{bullets}\n\n"
        "After this TodoWrite call, you may send your final summary "
        "message and the loop will end."
    )

    # Mirror to live_messages only — keep the audit log untouched
    # so the WS feed doesn't show a phantom system line.
    existing = list(state.get("live_messages") or state.get("messages", []))
    return {
        "live_messages": existing + [SystemMessage(content=nudge)],
        "todo_sync_nudged": True,
    }


def should_continue(state: RunnerState) -> str:
    """Terminal check: continue while the assistant requests tools.

    Force-sync gate: if the assistant is about to return no-tool-calls
    (i.e. the loop is about to terminate) AND we have a non-empty
    `last_todos` snapshot with at least one `pending` or `in_progress`
    item, route to `node_force_todo_sync` instead of `__end__`. The
    sync node injects a hard SystemMessage asking the agent to call
    TodoWrite to either complete or drop every open item, then loops
    back to `node_agent`. After ONE sync nudge we terminate regardless
    of state — we'd rather end with stale todos than infinite-loop.
    """
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "node_tools"

    # Force-sync nudge at task end. Skip if the agent already
    # synced this turn (last assistant message was the TodoWrite
    # itself, so last_todos reflects the agent's final state and
    # re-prompting is pointless). Also skip if the nudger already
    # fired once this turn — `todo_sync_nudged` flips True in
    # `node_force_todo_sync` and we honor a single nudge.
    last_todos = state.get("last_todos") or []
    nudged = bool(state.get("todo_sync_nudged"))
    if last_todos and not nudged:
        open_items = [t for t in last_todos if t.get("status") in ("pending", "in_progress")]
        if open_items:
            return "node_force_todo_sync"
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
        # Capture fresh todo state right after a TodoWrite call so
        # `should_continue`'s end-of-task sync gate has the latest
        # list. The TodoWrite wrapper writes to disk, so the on-disk
        # file is now authoritative; we read it back here. Falls
        # through to the prior `last_todos` value if no TodoWrite
        # happened in this batch.
        last_todos = _capture_todos_from_tool_results(state, new_tool_messages)
        return {
            **result,
            "live_messages": existing + new_tool_messages,
            "last_todos": last_todos,
        }
    return result
