"""
Multi-agent tools — port of the Rust claw-code suite:
  Agent (+ AgentStatus polling) and the Worker* lifecycle.

Source of truth:
  claw-code/rust/crates/tools/src/lib.rs       (tool defs + Agent)
  claw-code/rust/crates/runtime/src/worker_boot.rs  (Worker state machine)

Design notes:
  * `Agent` spawns a REAL background sub-agent (a thread running a fresh agent
    loop with a subagent-type system prompt + restricted tool set, max 32
    iterations) and returns immediately with status "running" — like Rust
    spawn_agent_job. Call it multiple times for parallel independent tasks.
  * `AgentStatus` polls a spawned agent's manifest so the orchestrator can wait
    for completion before starting dependent work.
  * `Worker*` is an in-memory boot-lifecycle state machine driven by terminal
    snapshots (no process spawning), faithful to worker_boot.rs.

Note: the Rust `TeamCreate`/`TeamDelete` metadata registry was intentionally
NOT ported — it was a no-op label (never linked to real spawned agents) and a
deliberate parity deviation; coordination is done via Agent + AgentStatus + the
shared filesystem instead.

langchain/langgraph are imported lazily (only inside the Agent worker thread) so
the registries below remain importable/testable without those deps installed.
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
    m = (model or "").strip()
    return m if m else DEFAULT_AGENT_MODEL


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
        from langchain_anthropic import ChatAnthropic
        from langgraph.prebuilt import create_react_agent
        from langchain_core.messages import HumanMessage
        from tools.wrappers import get_all_tools

        allowed = job["allowed_tools"]
        tools = [t for t in get_all_tools() if getattr(t, "name", None) in allowed]
        llm = ChatAnthropic(model=job["model"], streaming=False)
        agent = create_react_agent(llm, tools=tools, state_modifier=job["system_prompt"])
        result = agent.invoke(
            {"messages": [HumanMessage(content=job["prompt"])]},
            config={"recursion_limit": DEFAULT_AGENT_MAX_ITERATIONS * 2 + 5},
        )
        final = _final_text(result.get("messages", []))
        _persist_terminal_state(job["manifest"], "completed", final, None)
    except Exception as e:  # noqa: BLE001 - record any failure as terminal state
        _persist_terminal_state(job["manifest"], "failed", None, str(e))


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
    except Exception:  # prompt build needs project files; fall back to a minimal note
        system_prompt = (
            f"You are a background sub-agent of type `{norm_type}`. Work only on the "
            "delegated task, use only the tools available to you, do not ask the user "
            "questions, and finish with a concise result."
        )

    job = {
        "manifest": manifest,
        "prompt": prompt,
        "system_prompt": system_prompt,
        "allowed_tools": allowed_tools_for_subagent(norm_type),
        "model": resolved_model,
    }
    try:
        threading.Thread(
            target=_run_agent_job, args=(job,), name=f"clawd-agent-{agent_id}", daemon=True
        ).start()
    except Exception as e:  # noqa: BLE001
        _persist_terminal_state(manifest, "failed", None, f"failed to spawn sub-agent: {e}")
        raise RuntimeError(f"failed to spawn sub-agent: {e}") from e

    return manifest


def get_agent_status(agent_id: str) -> dict:
    """Read a spawned agent's manifest (so callers can poll for completion)."""
    path = _agent_store_dir() / f"{agent_id}.json"
    if not path.is_file():
        raise ValueError(f"agent not found: {agent_id}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Worker boot state machine — faithful port of worker_boot.rs
# ---------------------------------------------------------------------------

# WorkerStatus (snake_case values match Rust serde)
SPAWNING = "spawning"
TRUST_REQUIRED = "trust_required"
TOOL_PERMISSION_REQUIRED = "tool_permission_required"
READY_FOR_PROMPT = "ready_for_prompt"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"


@dataclass
class WorkerFailure:
    kind: str
    message: str
    created_at: int


@dataclass
class WorkerEvent:
    seq: int
    kind: str
    status: str
    detail: str | None
    timestamp: int


@dataclass
class Worker:
    worker_id: str
    cwd: str
    status: str
    trust_auto_resolve: bool
    trust_gate_cleared: bool
    auto_recover_prompt_misdelivery: bool
    prompt_delivery_attempts: int
    prompt_in_flight: bool
    prompt_sent_at: int | None
    last_prompt: str | None
    expected_receipt: dict | None
    replay_prompt: str | None
    last_error: dict | None
    created_at: int
    updated_at: int
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _path_matches_allowlist(cwd: str, root: str) -> bool:
    try:
        Path(cwd).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


# Terminal-snapshot detection heuristics (worker_boot.rs:1240+)
def _detect_trust_prompt(lowered: str) -> bool:
    return any(p in lowered for p in (
        "do you trust the files in this folder",
        "do you trust the authors",
        "yes, proceed",
        "trust the files",
    ))


def _detect_tool_permission_prompt(lowered: str) -> bool:
    return ("allow" in lowered and "tool" in lowered and (
        "to run" in lowered or "permission" in lowered
    ))


def _detect_ready_for_prompt(screen_text: str, lowered: str) -> bool:
    if "ready for input" in lowered or "send a message" in lowered:
        return True
    for line in reversed(screen_text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        # shell-style ready prompts, but NOT bash $ / % / #
        return stripped[-1] in {">", "›", "❯"}
    return False


def _detect_running_cue(lowered: str) -> bool:
    return any(c in lowered for c in (
        "thinking", "working", "running tests", "generating", "analyzing",
    ))


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def _push_event(self, w: Worker, kind: str, status: str, detail: str | None) -> None:
        w.events.append({
            "seq": len(w.events) + 1, "kind": kind, "status": status,
            "detail": detail, "timestamp": _now_secs(),
        })
        w.updated_at = _now_secs()

    def create(self, cwd: str, trusted_roots: list[str], auto_recover: bool) -> Worker:
        with self._lock:
            self._counter += 1
            ts = _now_secs()
            worker_id = f"worker_{ts:08x}_{self._counter}"
            trust_auto = any(_path_matches_allowlist(cwd, r) for r in trusted_roots)
            w = Worker(
                worker_id=worker_id, cwd=cwd, status=SPAWNING,
                trust_auto_resolve=trust_auto, trust_gate_cleared=False,
                auto_recover_prompt_misdelivery=auto_recover,
                prompt_delivery_attempts=0, prompt_in_flight=False,
                prompt_sent_at=None, last_prompt=None, expected_receipt=None,
                replay_prompt=None, last_error=None, created_at=ts, updated_at=ts,
                events=[],
            )
            self._push_event(w, "spawning", SPAWNING, "worker created")
            self._workers[worker_id] = w
            return w

    def _require(self, worker_id: str) -> Worker:
        w = self._workers.get(worker_id)
        if w is None:
            raise ValueError(f"worker not found: {worker_id}")
        return w

    def get(self, worker_id: str) -> Worker:
        with self._lock:
            return self._require(worker_id)

    def observe(self, worker_id: str, screen_text: str) -> Worker:
        with self._lock:
            w = self._require(worker_id)
            lowered = screen_text.lower()

            if _detect_tool_permission_prompt(lowered):
                w.status = TOOL_PERMISSION_REQUIRED
                w.last_error = {"kind": "tool_permission_gate",
                                "message": "tool permission prompt detected",
                                "created_at": _now_secs()}
                self._push_event(w, "tool_permission_required", w.status, "tool permission prompt")
                return w

            if not w.trust_gate_cleared and _detect_trust_prompt(lowered):
                w.status = TRUST_REQUIRED
                w.last_error = {"kind": "trust_gate", "message": "trust prompt detected",
                                "created_at": _now_secs()}
                self._push_event(w, "trust_required", w.status, "trust prompt detected")
                if w.trust_auto_resolve:
                    w.trust_gate_cleared = True
                    w.last_error = None
                    w.status = SPAWNING
                    self._push_event(w, "trust_resolved", SPAWNING, "auto-allowlisted")
                return w

            if w.prompt_in_flight and w.last_prompt and _detect_misdelivery(screen_text, lowered, w):
                self._push_event(w, "prompt_misdelivery", FAILED, "prompt misdelivery detected")
                if w.auto_recover_prompt_misdelivery:
                    w.replay_prompt = w.last_prompt
                    w.prompt_in_flight = False
                    w.status = READY_FOR_PROMPT
                    self._push_event(w, "prompt_replay_armed", READY_FOR_PROMPT, "replay armed")
                else:
                    w.status = FAILED
                    w.last_error = {"kind": "prompt_delivery", "message": "prompt misdelivered",
                                    "created_at": _now_secs()}
                return w

            if _detect_running_cue(lowered) and w.prompt_in_flight:
                w.prompt_in_flight = False
                w.status = RUNNING
                w.last_error = None
                self._push_event(w, "running", RUNNING, "running cue detected")
                return w

            if _detect_ready_for_prompt(screen_text, lowered) and w.status != READY_FOR_PROMPT:
                w.status = READY_FOR_PROMPT
                w.prompt_in_flight = False
                if w.last_error and w.last_error.get("kind") == "trust_gate":
                    w.last_error = None
                self._push_event(w, "ready_for_prompt", READY_FOR_PROMPT, "ready handshake")
            return w

    def resolve_trust(self, worker_id: str) -> Worker:
        with self._lock:
            w = self._require(worker_id)
            if w.status != TRUST_REQUIRED:
                raise ValueError(
                    f"worker {worker_id} is not waiting on trust; current status: {w.status}"
                )
            w.trust_gate_cleared = True
            w.last_error = None
            w.status = SPAWNING
            self._push_event(w, "trust_resolved", SPAWNING, "trust prompt resolved manually")
            return w

    def await_ready(self, worker_id: str) -> dict:
        with self._lock:
            w = self._require(worker_id)
            return {
                "worker_id": w.worker_id,
                "status": w.status,
                "ready": w.status == READY_FOR_PROMPT,
                "blocked": w.status in (TRUST_REQUIRED, TOOL_PERMISSION_REQUIRED, FAILED),
                "replay_prompt_ready": w.replay_prompt is not None,
                "last_error": w.last_error,
            }

    def send_prompt(self, worker_id: str, prompt: str | None, task_receipt: dict | None) -> Worker:
        with self._lock:
            w = self._require(worker_id)
            if w.status != READY_FOR_PROMPT:
                raise ValueError(
                    f"worker {worker_id} is not ready for prompt delivery; current status: {w.status}"
                )
            next_prompt = (prompt or "").strip() or (w.replay_prompt or "")
            if not next_prompt:
                raise ValueError(f"worker {worker_id} has no prompt to send or replay")
            w.prompt_delivery_attempts += 1
            w.prompt_in_flight = True
            w.prompt_sent_at = _now_secs()
            w.last_prompt = next_prompt
            w.expected_receipt = task_receipt
            w.replay_prompt = None
            w.last_error = None
            w.status = RUNNING
            self._push_event(w, "running", RUNNING, f"prompt dispatched: {next_prompt[:60]}")
            return w

    def restart(self, worker_id: str) -> Worker:
        with self._lock:
            w = self._require(worker_id)
            w.status = SPAWNING
            w.trust_gate_cleared = False
            w.last_prompt = None
            w.replay_prompt = None
            w.last_error = None
            w.prompt_delivery_attempts = 0
            w.prompt_in_flight = False
            w.prompt_sent_at = None
            self._push_event(w, "restarted", SPAWNING, "worker restarted")
            return w

    def terminate(self, worker_id: str) -> Worker:
        with self._lock:
            w = self._require(worker_id)
            w.status = FINISHED
            w.prompt_in_flight = False
            self._push_event(w, "finished", FINISHED, "worker terminated by control plane")
            return w

    def observe_completion(self, worker_id: str, finish_reason: str, tokens_output: int) -> Worker:
        with self._lock:
            w = self._require(worker_id)
            is_provider_failure = (finish_reason == "unknown" and tokens_output == 0) or finish_reason == "error"
            if is_provider_failure:
                msg = (
                    "session completed with finish='unknown' and zero output — provider "
                    "degraded or context exhausted"
                    if finish_reason == "unknown" and tokens_output == 0
                    else f"session failed with finish='{finish_reason}' — provider error"
                )
                w.last_error = {"kind": "provider", "message": msg, "created_at": _now_secs()}
                w.status = FAILED
                w.prompt_in_flight = False
                self._push_event(w, "failed", FAILED, "provider failure classified")
            else:
                w.status = FINISHED
                w.prompt_in_flight = False
                w.last_error = None
                self._push_event(
                    w, "finished", FINISHED,
                    f"session completed: finish='{finish_reason}', tokens={tokens_output}",
                )
            return w


def _detect_misdelivery(screen_text: str, lowered: str, w: Worker) -> bool:
    """Simplified misdelivery detection (worker_boot.rs detect_prompt_misdelivery):
    shell error after dispatch, or an expected task-receipt objective absent from
    the screen. Faithful to the intent; the full target-classification is reduced
    to the common signals."""
    shell_errors = ("command not found", "no such file or directory", "syntax error")
    if any(e in lowered for e in shell_errors):
        return True
    receipt = w.expected_receipt or {}
    objective = (receipt.get("objective_preview") or "").strip().lower()
    if objective and _detect_running_cue(lowered) and objective not in lowered:
        return True
    return False


_worker_registry = WorkerRegistry()


# Thin functional wrappers returning JSON-able dicts (used by tool wrappers)
def worker_create(cwd: str, trusted_roots: list[str] | None = None,
                  auto_recover_prompt_misdelivery: bool = True) -> dict:
    return _worker_registry.create(cwd, trusted_roots or [], auto_recover_prompt_misdelivery).to_dict()


def worker_get(worker_id: str) -> dict:
    return _worker_registry.get(worker_id).to_dict()


def worker_observe(worker_id: str, screen_text: str) -> dict:
    return _worker_registry.observe(worker_id, screen_text).to_dict()


def worker_resolve_trust(worker_id: str) -> dict:
    return _worker_registry.resolve_trust(worker_id).to_dict()


def worker_await_ready(worker_id: str) -> dict:
    return _worker_registry.await_ready(worker_id)


def worker_send_prompt(worker_id: str, prompt: str | None = None,
                       task_receipt: dict | None = None) -> dict:
    return _worker_registry.send_prompt(worker_id, prompt, task_receipt).to_dict()


def worker_restart(worker_id: str) -> dict:
    return _worker_registry.restart(worker_id).to_dict()


def worker_terminate(worker_id: str) -> dict:
    return _worker_registry.terminate(worker_id).to_dict()


def worker_observe_completion(worker_id: str, finish_reason: str, tokens_output: int) -> dict:
    return _worker_registry.observe_completion(worker_id, finish_reason, tokens_output).to_dict()
