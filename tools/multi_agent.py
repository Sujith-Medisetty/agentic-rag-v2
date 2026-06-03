"""
Multi-agent tools — Agent (+ AgentStatus polling).

 * `Agent` spawns a REAL background sub-agent (a thread running a fresh agent
 loop with a subagent-type system prompt + restricted tool set, max 32
 iterations) and returns immediately with status "running". Call it
 multiple times for parallel independent tasks.
 * `AgentStatus` polls a spawned agent's manifest so the orchestrator can
 wait for completion before starting dependent work.

langchain/langgraph are imported lazily (only inside the Agent worker thread)
so the registries below remain importable/testable without those deps
installed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (mirror lib.rs)
# ---------------------------------------------------------------------------

DEFAULT_AGENT_MODEL = "claude-opus-4-6"
DEFAULT_AGENT_MAX_ITERATIONS = 32

# Tool allowlists per subagent type (lib.rs allowed_tools_for_subagent).
# NOTE: `Agent` is intentionally absent from every set — sub-agents cannot spawn
# further sub-agents.
_ALLOWED_TOOLS_BY_TYPE: dict[str, set[str]] = {
    "Explore": {
        "read_file", "glob_search", "grep_search", "WebFetch", "WebSearch",
        "ToolSearch", "Skill", "StructuredOutput",
    },
    "Plan": {
        "read_file", "glob_search", "grep_search", "WebFetch", "WebSearch",
        "ToolSearch", "Skill", "TodoWrite", "StructuredOutput", "SendUserMessage",
    },
    "Verification": {
        "bash", "read_file", "glob_search", "grep_search", "WebFetch", "WebSearch",
        "ToolSearch", "TodoWrite", "StructuredOutput", "SendUserMessage", "PowerShell",
    },
    "claw-guide": {
        "read_file", "glob_search", "grep_search", "WebFetch", "WebSearch",
        "ToolSearch", "Skill", "StructuredOutput", "SendUserMessage",
    },
    "statusline-setup": {
        "bash", "read_file", "write_file", "edit_file", "glob_search",
        "grep_search", "ToolSearch",
    },
    "general-purpose": {
        "bash", "read_file", "write_file", "edit_file", "glob_search", "grep_search",
        "WebFetch", "WebSearch", "TodoWrite", "Skill", "ToolSearch", "NotebookEdit",
        "Sleep", "SendUserMessage", "Config", "StructuredOutput", "REPL", "PowerShell",
    },
}

def _canonical_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())

def normalize_subagent_type(subagent_type: str | None) -> str:
    """Faithful port of normalize_subagent_type (lib.rs:5532)."""
    trimmed = (subagent_type or "").strip()
    if not trimmed:
        return "general-purpose"
    tok = _canonical_token(trimmed)
    mapping = {
        "general": "general-purpose", "generalpurpose": "general-purpose",
        "generalpurposeagent": "general-purpose",
        "explore": "Explore", "explorer": "Explore", "exploreagent": "Explore",
        "plan": "Plan", "planagent": "Plan",
        "verification": "Verification", "verificationagent": "Verification",
        "verify": "Verification", "verifier": "Verification",
        "clawguide": "claw-guide", "clawguideagent": "claw-guide", "guide": "claw-guide",
        "statusline": "statusline-setup", "statuslinesetup": "statusline-setup",
    }
    return mapping.get(tok, trimmed)

def allowed_tools_for_subagent(subagent_type: str) -> set[str]:
    return set(_ALLOWED_TOOLS_BY_TYPE.get(subagent_type, _ALLOWED_TOOLS_BY_TYPE["general-purpose"]))

def resolve_agent_model(model: str | None) -> str:
    """Pick the model for a sub-agent.

    Priority:
      1. Explicit `model` arg (what the orchestrator's Agent() call passed).
      2. The currently-configured orchestrator model (so sub-agents on a
         MiniMax / OpenAI-compat / Anthropic setup match the parent and we
         don't accidentally send `claude-opus-4-6` to MiniMax's API).
      3. The hardcoded fallback (only used in tests where configure_model
         was never called).
    """
    m = (model or "").strip()
    if m:
        return m
    try:
        from agents.nodes import _model as _orchestrator_model
        if _orchestrator_model:
            return _orchestrator_model
    except Exception:
        pass
    return DEFAULT_AGENT_MODEL

def _slugify_agent_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:32]

def _agent_store_dir() -> Path:
    return Path(os.getenv("CLAWD_AGENT_STORE", ".clawd-agents"))

def _now_secs() -> int:
    return int(time.time())

def _iso8601_now() -> str:
    return str(_now_secs())

def _make_agent_id() -> str:
    return f"agent-{int(time.time() * 1_000_000_000)}"

# ---------------------------------------------------------------------------
# Agent tool — spawns a real background sub-agent
# ---------------------------------------------------------------------------

def _final_text(messages: list) -> str:
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content:
            return content if isinstance(content, str) else str(content)
    return ""

def _write_manifest(manifest: dict) -> None:
    path = Path(manifest["manifestFile"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

def _persist_terminal_state(manifest: dict, status: str, final_text: str | None, error: str | None) -> None:
    manifest = dict(manifest)
    manifest["status"] = status
    manifest["derivedState"] = "done" if status == "completed" else "errored"
    manifest["completedAt"] = _iso8601_now()
    if error:
        manifest["error"] = error
    _write_manifest(manifest)
    if final_text is not None:
        try:
            out = Path(manifest["outputFile"])
            with out.open("a", encoding="utf-8") as fh:
                fh.write(f"\n\n## Result\n\n{final_text}\n")
        except OSError:
            pass

def _run_agent_job(job: dict) -> None:
    """Background thread body — runs the sub-agent loop to completion."""
    try:
        from langgraph.prebuilt import create_react_agent
        from langchain_core.messages import HumanMessage
        from tools.wrappers import get_all_tools
        # Use the orchestrator's LLM factory so sub-agents respect the
        # configured provider (anthropic / minimax / openai-compatible).
        from agents.nodes import _get_llm
        from agents.reporter import reporter_scope

        allowed = job["allowed_tools"]
        tools = [t for t in get_all_tools() if getattr(t, "name", None) in allowed]
        # Sub-agents don't stream — their output is just the final text.
        llm = _get_llm(model=job["model"], streaming=False)
        agent = create_react_agent(llm, tools=tools, prompt=job["system_prompt"])

        # Set a reporter scope tagged with this agent's id so every tool /
        # text / token event published from inside the sub-agent's tools
        # gets stamped with `agent_id` and the frontend can nest it under
        # this sub-agent's tree (instead of dropping the event into the
        # main turn's activity or — worse — into the no-op default).
        sub_reporter = job.get("reporter")
        ctx_mgr = (
            reporter_scope(sub_reporter) if sub_reporter is not None
            else _nullctx()
        )
        with ctx_mgr:
            result = agent.invoke(
                {"messages": [HumanMessage(content=job["prompt"])]},
                config={"recursion_limit": DEFAULT_AGENT_MAX_ITERATIONS * 2 + 5},
            )
        final = _final_text(result.get("messages", []))
        _persist_terminal_state(job["manifest"], "completed", final, None)
    except Exception as e: # noqa: BLE001 - record any failure as terminal state
        _persist_terminal_state(job["manifest"], "failed", None, str(e))


from contextlib import contextmanager

@contextmanager
def _nullctx():
    yield

def _build_subagent_system_prompt(subagent_type: str, model: str) -> str:
    """Mirror build_agent_system_prompt: base system prompt + sub-agent note."""
    from datetime import date
    import platform
    from agents.prompt import SystemPromptBuilder, ProjectContext, FRONTIER_MODEL_NAME

    ctx = ProjectContext.discover_with_git(os.getcwd(), date.today().isoformat())
    builder = (
        SystemPromptBuilder()
        .with_os(platform.system() or "unknown", platform.release() or "unknown")
        .with_model_family(FRONTIER_MODEL_NAME)
        .with_project_context(ctx)
        .append_section(
            f"You are a background sub-agent of type `{subagent_type}`. Work only on "
            "the delegated task, use only the tools available to you, do not ask the "
            "user questions, and finish with a concise result."
        )
    )
    return builder.render()

def run_agent(
    description: str,
    prompt: str,
    subagent_type: str | None = None,
    name: str | None = None,
    model: str | None = None,
) -> dict:
    """Spawn a sub-agent. Faithful port of execute_agent_with_spawn (lib.rs:3928)."""
    if not description.strip():
        raise ValueError("description must not be empty")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")

    agent_id = _make_agent_id()
    out_dir = _agent_store_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file = out_dir / f"{agent_id}.md"
    manifest_file = out_dir / f"{agent_id}.json"

    norm_type = normalize_subagent_type(subagent_type)
    resolved_model = resolve_agent_model(model)
    agent_name = _slugify_agent_name(name) if name and _slugify_agent_name(name) else _slugify_agent_name(description)
    created_at = _iso8601_now()

    output_file.write_text(
        f"# Agent Task\n- id: {agent_id}\n- name: {agent_name}\n"
        f"- description: {description}\n- subagent_type: {norm_type}\n"
        f"- created_at: {created_at}\n\n## Prompt\n\n{prompt}\n",
        encoding="utf-8",
    )

    manifest = {
        "agentId": agent_id,
        "name": agent_name,
        "description": description,
        "subagentType": norm_type,
        "model": resolved_model,
        "status": "running",
        "outputFile": str(output_file),
        "manifestFile": str(manifest_file),
        "createdAt": created_at,
        "startedAt": created_at,
        "completedAt": None,
        "derivedState": "working",
        "error": None,
    }
    _write_manifest(manifest)

    # Build the sub-agent's system prompt + tool allowlist, then spawn the thread.
    try:
        system_prompt = _build_subagent_system_prompt(norm_type, resolved_model)
    except Exception: # prompt build needs project files; fall back to a minimal note
        system_prompt = (
            f"You are a background sub-agent of type `{norm_type}`. Work only on the "
            "delegated task, use only the tools available to you, do not ask the user "
            "questions, and finish with a concise result."
        )

    # Build a tagged WebReporter for the sub-agent if the parent is web-backed,
    # so its events publish to the same session bus with this agent_id stamp.
    # If the parent is the noop reporter (e.g. CLI / tests), we just don't tag.
    sub_reporter = None
    try:
        from agents.reporter import get_reporter
        from server.reporter import WebReporter as _WR
        parent = get_reporter()
        if isinstance(parent, _WR):
            sub_reporter = _WR(parent.session_id, agent_id=agent_id)
    except Exception:
        sub_reporter = None

    job = {
        "manifest": manifest,
        "prompt": prompt,
        "system_prompt": system_prompt,
        "allowed_tools": allowed_tools_for_subagent(norm_type),
        "model": resolved_model,
        "reporter": sub_reporter,
    }
    try:
        threading.Thread(
            target=_run_agent_job, args=(job,), name=f"clawd-agent-{agent_id}", daemon=True
        ).start()
    except Exception as e: # noqa: BLE001
        _persist_terminal_state(manifest, "failed", None, f"failed to spawn sub-agent: {e}")
        raise RuntimeError(f"failed to spawn sub-agent: {e}") from e

    return manifest

def get_agent_status(agent_id: str) -> dict:
    """Read a spawned agent's manifest (so callers can poll for completion)."""
    path = _agent_store_dir() / f"{agent_id}.json"
    if not path.is_file():
        raise ValueError(f"agent not found: {agent_id}")
    return json.loads(path.read_text(encoding="utf-8"))
