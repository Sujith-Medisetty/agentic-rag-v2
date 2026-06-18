"""
Tool registry — one decorated function per tool.
Each tool lives in a SINGLE block:
 schema (Pydantic, declared at module top so it's reusable)
 + @tool decorator (binds name + schema, makes the symbol a BaseTool)
 + @_safe_tool decorator (permissions + hooks + try/except)
 + docstring (the LLM-visible description with usage rules)
 + body (calls the production implementation in tools/*.py)
Registration is just `[bash, edit_file,...]` — no StructuredTool.from_function
ceremony, no _DESC_* constants, no separate underscore-prefixed function block.
Public API:
 configure_safety(...) — wire permission policy / hooks / sandbox / workspace
 get_read_tools() — plan/explore subset (read-only)
 get_all_tools(extra=) — full toolset (read + write + utility + multi-agent)
 READ_TOOLS, WRITE_TOOLS, ALL_TOOLS, MULTI_AGENT_TOOLS, UTILITY_TOOLS
Diffs and live activity stream through agents.reporter.get_reporter() events —
WebReporter publishes them to the per-session WebSocket bus.
"""
from __future__ import annotations
import difflib
import functools
import inspect
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from langchain_core.tools import tool
from pydantic import BaseModel, Field
# Production implementations (untouched). Imports aliased where the public tool
# name collides with the underlying helper name (e.g. `read_file`).
from tools.file_ops import (
    read_file as _read_file_impl,
    write_file as _write_file_impl,
    edit_file as _edit_file_impl,
)
from tools.bash import execute_bash, BashInput
from tools.git import (
    git_status, git_diff, git_log, git_show, git_blame,
    git_create_branch, git_checkout, git_add, git_commit,
    git_push, git_pull, git_stash, git_current_branch,
)
from tools.web import web_fetch, web_search
from tools.utils import todo_write, sleep_tool, brief
# Safety layer (untouched).
from safety.bash_validator import validate_command, PermissionMode
from safety.sandbox import SandboxStatus, execute_sandboxed
from safety.permissions import PermissionPolicy
from safety.hooks import HookRunner
# ============================================================================
# Global runtime config — wired by server.app at startup
# ============================================================================
_permission_policy: PermissionPolicy = PermissionPolicy()
_hook_runner: HookRunner = HookRunner()
_sandbox: SandboxStatus | None = None
_workspace: str = "."
_permission_mode: PermissionMode = PermissionMode.FULL_ACCESS

# Per-thread micro-cache removed: the plan-mode/heartbeat pre-scans that
# required it have been removed. EnterPlanMode/ExitPlanMode are now
# advisory-only — they no longer flip any state.

def configure_safety(
    permission_policy: PermissionPolicy,
    hook_runner: HookRunner,
    sandbox: SandboxStatus | None,
    workspace: str,
    permission_mode: PermissionMode,
) -> None:
    """Wire safety into all tool wrappers. Called once at startup."""
    global _permission_policy, _hook_runner, _sandbox, _workspace, _permission_mode
    _permission_policy = permission_policy
    _hook_runner = hook_runner
    _sandbox = sandbox
    _workspace = workspace
    _permission_mode = permission_mode
# ============================================================================
# Safety decorator — stacks INSIDE @tool. @tool gives us the BaseTool object;
# @_safe_tool runs the permission/hook gauntlet on every invocation.
# ============================================================================
def _check_permission(tool_name: str, input_dict: dict) -> str | None:
    """Returns an error string if denied, None if allowed."""
    input_str = json.dumps(input_dict)
    hook = _hook_runner.pre_tool_use(tool_name, input_str)
    if hook.denied or hook.failed:
        return "; ".join(hook.messages) or f"Hook denied '{tool_name}'"
    perm = _permission_policy.authorize(tool_name, input_str)
    if not perm.allowed:
        return perm.reason
    return None
def _safe_tool(tool_name: str):
    """Wrap a tool function with permission check + pre/post hooks + try/except.
    Apply to tools that have no custom display logic (no diff, no sandbox).
    Tools that need bespoke bodies (write_file/edit_file/bash) inline the same
    checks instead of stacking this decorator."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            inp_dict = kwargs or {}
            if args:
                sig = inspect.signature(fn)
                params = list(sig.parameters.keys())
                inp_dict = {**{params[i]: a for i, a in enumerate(args)}, **kwargs}
            inp_str = json.dumps({k: v for k, v in inp_dict.items()
                if isinstance(v, (str, int, bool, type(None)))})
            hook = _hook_runner.pre_tool_use(tool_name, inp_str)
            if hook.denied or hook.failed:
                return "; ".join(hook.messages) or f"Hook denied '{tool_name}'"
            perm = _permission_policy.authorize(tool_name, inp_str)
            if not perm.allowed:
                return f"BLOCKED: {perm.reason}"
            try:
                result = fn(*args, **kwargs)
                # Repetition guard — server-side detector for stuck loops.
                # The agent is autonomous (no human in the loop), so we
                # can't ask the user to break a loop. Instead, when the
                # SAME (session, tool, args) call fires >THRESHOLD times,
                # prepend a directive that points at the next-best
                # action. Cheaper than letting the loop run 9+ times.
                try:
                    from tools.sandbox import active_session_id
                    from tools.repetition_guard import check_and_record
                    notice = check_and_record(
                        active_session_id(), tool_name, inp_dict,
                    )
                except Exception:
                    notice = None  # guard must never break the tool
                if notice:
                    result = f"{notice}\n\n{result}"
                _hook_runner.post_tool_use(tool_name, inp_str, str(result)[:200])
                return result
            except Exception as e:
                _hook_runner.post_tool_failure(tool_name, inp_str, str(e))
                return f"Error: {e}"
        return wrapper
    return decorator
# ============================================================================
# Diff + reporter helpers used by file/agent tools
# ============================================================================
def _build_unified_diff(path: str, before: str, after: str) -> str:
    """Plain unified-diff string published to the UI via the file_changed event."""
    return "".join(difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
    ))
def _safe_report(fn):
    """Call a reporter method, swallowing any error so a busted reporter
    never breaks a tool call. Used at every reporter call-site."""
    try:
        fn()
    except Exception:
        pass


def _python_syntax_error(path: str, content: str) -> str | None:
    """Compile `content` as Python source for syntax-only validation.

    Returns None if the source parses, or a short human-readable error string
    if it doesn't. We use `compile(..., "exec")` rather than spawning
    `python -m py_compile` because it's ~100× faster (no subprocess) and
    operates on the in-memory content — so we can call it BEFORE persisting
    a broken file to disk.

    Only `.py` files are checked. Anything else returns None unconditionally.
    """
    if not path.lower().endswith(".py"):
        return None
    try:
        compile(content, path, "exec")
        return None
    except SyntaxError as e:
        # e.lineno / e.offset / e.msg are the user-actionable bits — formatted
        # to match what `python` itself would print so the model can act on it.
        line = e.lineno if e.lineno is not None else "?"
        col  = e.offset if e.offset is not None else "?"
        return f"SyntaxError at {path}:{line}:{col} — {e.msg}"
    except Exception as e:  # noqa: BLE001 - catch IndentationError etc. as the same
        return f"{type(e).__name__}: {e}"
# ============================================================================
# Pydantic input schemas — declared up here so @tool(args_schema=...) can bind
# them, and so the LLM sees rich Field(description=...) hints per argument.
# ============================================================================
class ReadFileInput(BaseModel):
    path: str = Field(description="Absolute or relative path to the file")
    offset: int | None = Field(None, description="1-indexed line number to start reading from")
    limit: int | None = Field(None, description="Maximum number of lines to read")
class WriteFileInput(BaseModel):
    path: str = Field(description="Path to write to (creates parent dirs)")
    content: str = Field(description="Full file content to write")
class EditFileInput(BaseModel):
    path: str = Field(description="Path of the file to edit")
    old_string: str = Field(description="Exact text to find and replace (whitespace-sensitive, must be unique unless replace_all)")
    new_string: str = Field(description="Replacement text (must differ from old_string)")
    replace_all: bool = Field(False, description="Replace every occurrence (default: just the first)")
class BashInputSchema(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int | None = Field(None, description="Timeout in MILLISECONDS (max 600000, default 120000)")
    run_in_background: bool = Field(False, description="Run without waiting; the call returns immediately")
class GitInput(BaseModel):
    action: str = Field(description="status|diff|log|show|blame|branch|checkout|add|commit|push|pull|stash")
    path: str | None = Field(None)
    commit: str | None = Field(None)
    staged: bool | None = Field(None)
    count: int | None = Field(None)
    message: str | None = Field(None)
    branch: str | None = Field(None)
class WebFetchInput(BaseModel):
    url: str = Field(description="URL to fetch")
    timeout: int | None = Field(None, description="Timeout in seconds (default 20)")
class WebSearchInput(BaseModel):
    query: str = Field(description="Search query")
    allowed_domains: list | None = Field(None, description="Only return results from these domains")
    blocked_domains: list | None = Field(None, description="Exclude these domains")
class GitHubInput(BaseModel):
    action: str = Field(description="get_issue | create_pr | list_prs")
    repo: str = Field(description="owner/repo e.g. anthropics/claude")
    issue_number: int | None = Field(None)
    pr_number: int | None = Field(None)
    title: str | None = Field(None, description="PR title (keep under 70 chars)")
    body: str | None = Field(None, description="PR body (use ## Summary + ## Test plan format)")
    branch: str | None = Field(None, description="Head branch for PR creation")
    base_branch: str | None = Field(None, description="Base branch (default: main)")
    path: str | None = Field(None)
    state: str | None = Field(None, description="open | closed | all")
    comment: str | None = Field(None)
class TodoItem(BaseModel):
    content: str = Field(description="Task description (imperative form)")
    status: str = Field(description="pending | in_progress | completed")
    activeForm: str = Field(description="Present-continuous label shown in progress UI")
class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(description="The FULL todo list (writes are total, not incremental)")
class SleepInput(BaseModel):
    duration_ms: int = Field(description="Milliseconds to wait (max 300000)")
class AskUserInput(BaseModel):
    question: str = Field(description="The single, grounded question to ask")
    options: list | None = Field(None, description="Optional list of choices for a closed question")
class SendMessageInput(BaseModel):
    message: str = Field(description="Progress update or interim finding to send to the user")
class ToolSearchInput(BaseModel):
    query: str = Field(description="Free-text query, or 'select:Tool1,Tool2' for exact lookup")
class PlanModeInput(BaseModel):
    pass # no parameters
class AgentToolInput(BaseModel):
    description: str = Field(description="Short description of the delegated task")
    prompt: str = Field(description="Self-contained task prompt — sub-agents have no memory of this conversation")
    subagent_type: str | None = Field(None, description="general-purpose | Explore | Plan | Verification | claw-guide | statusline-setup")
    name: str | None = Field(None, description="Optional name for the sub-agent")
    model: str | None = Field(None, description="Optional model override")
    max_idle_seconds: int | None = Field(None, description="Idle timeout in seconds — sub-agent is killed if it goes this long with NO progress events (tool calls / token updates). Default 300 (5min). Productive long-running tasks keep emitting events and never trip this.")
    max_total_seconds: int | None = Field(None, description="Absolute wall-clock cap in seconds, regardless of activity. Default 21600 (6h). Clamped to [idle, 86400]. Belt-and-braces only — most sub-agents finish well within their idle budget.")
class AgentStatusInput(BaseModel):
    agent_id: str = Field(description="The agent_id returned by a prior Agent spawn")
# ============================================================================
# FILE TOOLS
# ============================================================================
@tool("read_file", args_schema=ReadFileInput)
@_safe_tool("read_file")
def read_file(path: str, offset: int | None = None, limit: int | None = None) -> str:
    """Read a text file from disk. Use this (NOT `bash` with cat/head/tail) for any file you need to inspect.
    Usage:
    - `path` may be absolute or relative to the workspace.
    - For large files, pass `offset` (1-indexed start line) and `limit` (max lines) to window the read.
    - Line numbers are returned in the output — use them when referencing code back to the user as `path:line`.
    - You MUST have read a file at least once this conversation before calling `edit_file` on it. Reading is also the safest way to confirm an edit landed correctly.
    - FILES ONLY: passing a directory returns a directive pointing you at `bash ls <path>`. Don't re-issue the same path; the guard will flag it.
    """
    # Files-only guard: if `path` is a directory, return a directive
    # (not a thrown exception). An exception is just another tool
    # result the model will see and possibly retry on the same path.
    # A directive tells it the NEXT action to take — `bash ls <path>`
    # — so the loop breaks in one step.
    try:
        from pathlib import Path as _P
        p = _P(path).expanduser()
        if p.exists() and p.is_dir():
            return (
                f"[error] `{path}` is a directory, not a file. "
                f"Use `bash ls {path}` to list its contents, then "
                f"`read_file` on a specific file inside it."
            )
    except OSError:
        pass  # fall through; the real impl will raise a clearer error
    result = _read_file_impl(path, offset, limit)
    f = result.file
    header = (
        f"File: {f.filePath} "
        f"(lines {f.startLine}-{f.startLine + f.numLines - 1} of {f.totalLines})\n"
        f"{'-' * 40}\n"
    )
    return header + f.content
@tool("write_file", args_schema=WriteFileInput)
def write_file(path: str, content: str) -> str:
    """Create a new file, or overwrite an existing one with brand-new content. Parent directories are created automatically.
    Usage:
    - Prefer `edit_file` for modifying existing files — it only changes the targeted region and is much safer.
    - Only use `write_file` for genuinely new files, or when the entire file is being replaced.
    - If overwriting an existing file, read it first so you don't drop content you didn't mean to.
    - Do NOT create README, docs, or planning files unless the user explicitly asks for them.
    - Do not use emojis in file content unless requested.
    """
    err = _check_permission("write_file", {"path": path})
    if err:
        return f"BLOCKED: {err}"
    # Workspace jail — non-root users can only write inside the session's
    # workspace. Root bypasses the check.
    from tools.sandbox import check_write_path, SandboxViolation
    try:
        check_write_path(path)
    except SandboxViolation as sv:
        return f"BLOCKED: {sv}"
    # Syntax-check Python files BEFORE writing — a broken .py file on disk
    # will crash the uvicorn auto-reloader the moment it lands, taking the
    # whole backend down until the file is fixed (we hit this in the wild —
    # see the duplicate-class IndentationError incident in agents/prompt.py).
    # Validating on the in-memory content means a bad write is rejected
    # without ever touching the filesystem; the model sees the error in the
    # tool result and can immediately retry with a corrected version.
    syntax_err = _python_syntax_error(path, content)
    if syntax_err:
        return f"Error: refusing to write — {syntax_err}"
    try:
        result = _write_file_impl(path, content)
        verb = "Created" if result.type == "create" else "Updated"
        # Notify the UI — diff for overwrites, full content for creates.
        from agents.reporter import get_reporter
        kind = "create" if result.type == "create" else "edit"
        diff_text = (result.content if result.type == "create"
            else _build_unified_diff(result.filePath, result.originalFile or "", result.content))
        _safe_report(lambda: get_reporter().file_changed(
            path=result.filePath, kind=kind,
            diff=diff_text, bytes_count=len(result.content),
        ))
        _hook_runner.post_tool_use(
            "write_file", json.dumps({"path": path}), f"{verb}: {result.filePath}"
        )
        # Append to the durable per-workspace fix log. Same rationale as
        # `edit_file` above — survives auto-compaction. Skip "create"
        # cases: a brand-new file isn't a "fix", and the log is meant to
        # capture the trail of edits to existing code.
        if result.type != "create":
            _append_fix_log(result.filePath, content)
        return f"{verb}: {result.filePath} ({len(result.content)} bytes)"
    except Exception as e:
        _hook_runner.post_tool_failure("write_file", json.dumps({"path": path}), str(e))
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Fix log — durable per-workspace edit trail
# ---------------------------------------------------------------------------
#
# Every successful `edit_file` (and `write_file` overwriting an existing file)
# appends a one-line summary to `<workspace>/.ojas-fixlog.md`. The log lives
# on disk, so it survives auto-compaction — the compaction summary in
# `memory/checkpointer._summarize_messages` truncates edits to 15 entries,
# which would lose ~85% of the trail for a 100-bug session. The next LLM
# call that needs to enumerate past fixes can `Read .ojas-fixlog.md` and
# see the full history verbatim.
#
# Best-effort: any I/O failure (read-only mount, disk full, permission
# error) is swallowed. The fix log is a runtime nicety, not a correctness
# boundary — a logging failure must never turn a successful edit into a
# tool result that says "Error: …".
#
# File is gitignored (see /.gitignore, alongside .clawd-todos.json) so it
# stays out of source control.
_FIX_LOG_NAME = ".ojas-fixlog.md"
_FIX_LOG_PREVIEW_CHARS = 200  # one-line cap; matches the regex summary


def _append_fix_log(file_path: str, new_string: str) -> None:
    """Append a one-line fix-trail entry for a successful edit.

    Args:
        file_path: The path that was edited (relative or absolute). The
            fix log lives at `<cwd>/.ojas-fixlog.md` — the agent runs
            in the project root, so cwd == workspace.
        new_string: The new content that replaced the old. We log a
            single-line preview of this (newlines → " ⏎ "), truncated
            to `_FIX_LOG_PREVIEW_CHARS` chars so the log stays readable.
    """
    try:
        workspace = Path.cwd()
        # Cap per-line to keep the log readable. Most bug fixes fit in
        # one short diff. Truncate at 200 chars with a trailing ellipsis.
        preview = (new_string or "").replace("\n", " ⏎ ")[:_FIX_LOG_PREVIEW_CHARS]
        if len(new_string or "") > _FIX_LOG_PREVIEW_CHARS:
            preview += "…"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"- {ts}  `{file_path}`: → `{preview}`\n"
        log = workspace / _FIX_LOG_NAME
        # mkdir(parents=True, exist_ok=True) is a no-op when the workspace
        # already exists (the common case); only does work on a fresh
        # workspace where the user is editing a brand-new project.
        workspace.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Never break the tool result on a logging failure.
        pass


@tool("edit_file", args_schema=EditFileInput)
def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Make a precise text replacement in an existing file. Strongly preferred over `write_file` for any change to an existing file.
    Usage:
    - You MUST have read the file at least once this conversation before calling `edit_file` on it.
    - `old_string` must match the file EXACTLY, including all whitespace, indentation (tabs vs spaces), and line endings. Copy directly from `read_file` output (excluding the line-number prefix).
    - `old_string` must be unique in the file. If it isn't, add more surrounding context to make it unique, or set `replace_all=true` to replace every occurrence (useful for variable/identifier renames).
    - `new_string` must differ from `old_string`, and the result should remain syntactically valid.
    - For multiple edits to the same file, prefer several focused `edit_file` calls over one large rewrite — easier to review and to rollback if one fails.
    """
    inp = {"path": path, "old_string": old_string, "new_string": new_string}
    err = _check_permission("edit_file", inp)
    if err:
        return f"BLOCKED: {err}"
    from tools.sandbox import check_write_path, SandboxViolation
    try:
        check_write_path(path)
    except SandboxViolation as sv:
        return f"BLOCKED: {sv}"
    try:
        result = _edit_file_impl(path, old_string, new_string, replace_all)
        reps = result.originalFile.count(old_string) if replace_all else 1
        updated = result.originalFile.replace(
            old_string, new_string, 0 if replace_all else 1
        )
        # Syntax-check Python results BEFORE accepting the edit. If the edit
        # produces invalid Python (typos, half-finished refactors, lost
        # indentation), atomically restore the original file and surface the
        # error to the model — no broken file is ever left on disk. See the
        # same rationale in write_file above.
        syntax_err = _python_syntax_error(path, updated)
        if syntax_err:
            try:
                Path(result.filePath).write_text(result.originalFile, encoding="utf-8")
            except Exception:
                pass  # best-effort rollback
            return f"Error: edit reverted — {syntax_err}"
        # Notify the UI — always a unified diff for edits.
        from agents.reporter import get_reporter
        diff_text = _build_unified_diff(result.filePath, result.originalFile, updated)
        _safe_report(lambda: get_reporter().file_changed(
            path=result.filePath, kind="edit",
            diff=diff_text, bytes_count=len(updated),
        ))
        out = f"Edited: {result.filePath}\nReplacements: {reps}"
        # Append to the durable per-workspace fix log. Lives on disk, so it
        # survives auto-compaction (the regex summary in
        # `memory.checkpointer._summarize_messages` truncates edits to 15
        # entries, which would lose ~85% of the trail for a 100-bug
        # session). Best-effort, never raises.
        _append_fix_log(result.filePath, new_string)
        _hook_runner.post_tool_use("edit_file", json.dumps(inp), out)
        return out
    except Exception as e:
        _hook_runner.post_tool_failure("edit_file", json.dumps(inp), str(e))
        return f"Error: {e}"
# ============================================================================
# BASH
# ============================================================================
@tool("bash", args_schema=BashInputSchema)
def bash(command: str, timeout: int | None = None, run_in_background: bool = False) -> str:
    """Execute a shell command in the workspace. Use this for operations a dedicated tool doesn't cover — build/test/install/run, package managers, file search (`grep`/`rg`/`find`), `git` operations outside the `git` tool's action set, and any other shell utility.
    DO NOT use bash for:
    - Reading files → use `read_file`.
    - Editing files → use `edit_file` / `write_file`.
    - Searching code → use `bash` with `grep`/`rg`/`find` (with the default excludes below).
    - `cat`, `head`, `tail`, `sed`, `awk`, `echo >file`, `find` for code lookup — all have better dedicated tools above.
    Usage:
    - `timeout` is in MILLISECONDS (default 120000, max 600000).
    - Set `run_in_background=true` for long-running processes (dev servers, watchers); the call returns immediately.
    - Quote paths containing spaces with double quotes.
    - Run independent commands as PARALLEL tool calls in the same message, not chained sequentially. Use `&&` only when a later command truly depends on an earlier one succeeding; use `;` only when you don't care about earlier failures. Avoid splitting commands with newlines.
    - When running `find` or `grep`, scope to a specific subdirectory — never from `/` or the repo root when the project is large. Excludes for `node_modules`, `.git`, `dist`, `build`, `coverage`, `__pycache__` are applied by default.
    - Avoid leading `sleep` loops; the harness enforces caps. Prefer `run_in_background=true` and poll the result.
    Git commits (use ONLY when the user explicitly asks for a commit):
    - Always create NEW commits — never `--amend` unless the user asks.
    - Never `--no-verify`, never skip hooks or signing, never force-push to `main` / `master`.
    - Stage specific files by name. Avoid `git add -A` / `git add.` — they can sweep in secrets or large binaries.
    - Pass commit messages via a HEREDOC for clean formatting:
    git commit -m "$(cat <<'EOF'
    Short imperative subject line.
    Optional body explaining WHY.
    EOF
    )"
    - Before destructive operations (`reset --hard`, `push --force`, `checkout --`, `branch -D`, `clean -f`), prefer a safer alternative and confirm with the user first.
    """
    inp = {"command": command}
    err = _check_permission("bash", inp)
    if err:
        return f"BLOCKED: {err}"
    # Inject default excludes for grep/rg/find so a broad search doesn't
    # recurse into node_modules, .git, dist, build, etc. and return a 1MB blob
    # (this is the failure mode that cost $40 on one todo session). Only
    # adds flags the user didn't already pass.
    command = _inject_search_excludes(command)
    # bash-specific validator
    val = validate_command(command, _permission_mode, _workspace)
    if val.is_blocked:
        return f"BLOCKED: {val.message}"
    if val.is_warning:
        # Warnings surface to the UI via tool_done's preview; nothing to print.
        from agents.reporter import get_reporter
        _safe_report(lambda: get_reporter().message(f"bash warning: {val.message}"))
    try:
        if _sandbox and _sandbox.active:
            timeout_sec = timeout / 1000.0 if timeout else None
            result = execute_sandboxed(command, _sandbox, timeout_sec)
            parts = []
            if result.stdout: parts.append(result.stdout)
            if result.stderr: parts.append(f"[stderr]\n{result.stderr}")
            if result.exit_code != 0:
                parts.append(f"[exit code: {result.exit_code}]")
            out = "\n".join(parts) or "(no output)"
        else:
            raw = execute_bash(BashInput(
                command=command, timeout=timeout,
                run_in_background=run_in_background,
            ))
            parts = []
            if raw.stdout: parts.append(raw.stdout)
            if raw.stderr: parts.append(f"[stderr]\n{raw.stderr}")
            if raw.return_code_interpretation:
                parts.append(f"[{raw.return_code_interpretation}]")
            out = "\n".join(parts) or "(no output)"
        _hook_runner.post_tool_use("bash", json.dumps(inp), out[:500])
        # Smart head+tail truncation with failure-aware weights and a
        # session-scoped spill file. The bash tool's old head-only 16 KB
        # cap dropped the tail (where TS errors / stack traces live); the
        # new strategy:
        #   - 10 KB inline cap by default (≈ 2,500 tokens; tunable via
        #     OJAS_BASH_MAX_OUTPUT_CHARS up to 150 KB to match CC).
        #   - Success: 50/50 head/tail — universal across `cat`, build
        #     logs, test summaries.
        #   - Failure: 28/62 head/tail — error blocks and stack traces
        #     are always at the end, and they need to survive intact.
        #   - Full output is spilled to /tmp/ojas-bash/<sid>/... and
        #     the marker tells the LLM exactly how to `grep` or
        #     `sed -n 'N,Mp'` a middle slice out of it.
        return _smart_truncate_bash_output(out, raw.return_code_interpretation)
    except Exception as e:
        _hook_runner.post_tool_failure("bash", json.dumps(inp), str(e))
        return f"Error: {e}"


# Default noise dirs to skip on broad search/find commands. Matches the
# excludes injected into the LLM-facing tool descriptions and the prompt.
_SEARCH_EXCLUDE_DIRS = (
    "node_modules", ".git", "dist", "build",
    "coverage", "__pycache__", ".next", ".cache",
)

def _inject_search_excludes(command: str) -> str:
    """Add default --exclude-dir flags to grep/rg/find commands if the user
    didn't already pass any. The goal: a broad `grep -rn "foo" .` from the
    project root doesn't return 900 KB of node_modules matches."""
    if not command:
        return command
    cmd = command.strip()
    low = cmd.lower()
    try:
        # ripgrep
        if re.search(r"\brg\b", low) and "--no-ignore" not in low:
            # rg respects .gitignore by default — leave it alone unless user
            # disabled it. Just ensure a sensible -uu doesn't add noise.
            return command
        # grep / egrep
        if re.search(r"\b(grep|egrep|fgrep)\b", low):
            # Don't double-inject if user already passed --exclude-dir
            if "--exclude-dir=" in cmd or "--exclude" in cmd:
                return command
            excl = " ".join(f"--exclude-dir={d}" for d in _SEARCH_EXCLUDE_DIRS)
            return f"{cmd} {excl}"
        # find
        if re.search(r"\bfind\b", low):
            # Skip if user already uses -path '*/X' or -prune
            if "-prune" in cmd or "-path " in cmd:
                return command
            # Insert -path '*/<dir>' -prune before any -print / -o
            # Simpler approach: just prefix the find with a prune clause
            prune_parts = " ".join(
                f"-path './{d}' -prune -o" for d in _SEARCH_EXCLUDE_DIRS
            )
            # The prune clauses need an "or" terminator — and find needs a
            # default action at the end. Insert after the first path arg.
            # We don't try to be clever — just append an "or print the rest"
            # clause at the end of the find command.
            return f"{cmd} \\( {prune_parts} -print \\)"
    except Exception:
        return command
    return command


def _truncate_output(out: str, *, max_chars: int = 50_000) -> str:
    """Cap tool output at `max_chars` chars. Keeps the first 70% and last
    15% so the agent still sees both the head and the tail (errors usually
    appear at the end), with a clear marker in between saying what was
    dropped and how to re-run with a tighter filter."""
    if not out or len(out) <= max_chars:
        return out
    head_budget = int(max_chars * 0.70)
    tail_budget = int(max_chars * 0.15)
    head = out[:head_budget]
    tail = out[-tail_budget:]
    dropped = len(out) - head_budget - tail_budget
    return (
        head
        + f"\n\n[…truncated {dropped:,} chars; re-run with a tighter filter "
          f"(e.g. --include='*.ts', --exclude-dir=node_modules, "
          f"\\| head -200) to see this section…]\n\n"
        + tail
    )


# ---------------------------------------------------------------------------
# Smart bash output truncation
# ---------------------------------------------------------------------------
# The bash tool's output is the most expensive piece of context we hand
# to the LLM: `npm run verify`, `cargo test`, `pytest -v`, and `vite build`
# routinely produce 5-50 KB of stdout, and the agent's first instinct on
# a failure is to retry — which re-injects the same large tool_result into
# the next turn's prompt, paying the cost N times.
#
# Strategy (head+tail with failure-aware weights + spill to temp file):
#   1. Keep an inline preview of <max_chars> bytes (default 10 KB, tunable
#      via OJAS_BASH_MAX_OUTPUT_CHARS; ceiling 150 KB to match the CC
#      default — going higher than that means the inline preview is
#      costing more than the full file would).
#   2. For SUCCESSES, split ~50/50 head/tail — universal enough for
#      `cat`, `git log`, `find`, build logs, and test summaries alike.
#      For FAILURES, flip to ~30 head / ~65 tail — the error block, exit
#      code, and concluding line are always at the end, and the LLM
#      needs them intact to diagnose.
#   3. Anything that was cut is written to a session-scoped temp file
#      (`/tmp/ojas-bash/<session_id>/bash-<ts>-<hash>.log`) and the
#      path is embedded in the truncation marker. The agent can
#      `read_file` that path (full content) or `sed -n 'N,Mp' <spill>`
#      (a 1–2 KB slice) or `grep -E 'error|Error' <spill>` (a filtered
#      view) — all of these are cheap, single bash calls.
#   4. The marker explicitly tells the LLM how to recover middle
#      content. Without that hint, a model that sees "everything looks
#      fine" in the inline preview will sometimes declare success
#      instead of grepping the spill for the real error.

import os

def _bash_inline_max_chars() -> int:
    """Resolve the inline cap from the env var, clamped to a sane range.
    The env-var ceiling of 150 KB matches the CC spec — going higher
    would defeat the point of truncation (the inline preview costs
    ~37,500 tokens at 150 KB, and the full file would have been
    cheaper)."""
    raw = os.getenv("OJAS_BASH_MAX_OUTPUT_CHARS", "10000")
    try:
        n = int(raw)
    except ValueError:
        n = 10_000
    return max(2_000, min(n, 150_000))

def _bash_spill_dir() -> Path:
    """Resolve the spill dir from the env var (default /tmp/ojas-bash)."""
    return Path(os.getenv("OJAS_BASH_SPILL_DIR", "/tmp/ojas-bash"))

# Per-session cap on the spill dir, so a runaway session can't fill /tmp.
# 100 MiB is ~25× the largest realistic session, so the cap is a true
# safety net — not a budget.
_BASH_SPILL_DIR_MAX_BYTES = 100 * 1024 * 1024


def _bash_spill_dir_for_session(session_id: str | None) -> Path:
    """Return (and create) the spill dir for this session. Sessions scope
    spill files so a `read_file` call gets a clear, self-explanatory path
    and so cleanup can target one dir per session. Falls back to
    `_global` when there's no active session (CLI, tests)."""
    sub = session_id or "_global"
    d = _bash_spill_dir() / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _bash_spill_dir_size(d: Path) -> int:
    total = 0
    try:
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _spill_bash_output(out: str, session_id: str | None) -> str | None:
    """Write the full bash output to a session-scoped temp file. Returns
    the file path (as a string) so the caller can embed it in the
    truncation marker, or None if the spill couldn't be written.

    Failure is non-fatal — the truncation marker just won't reference a
    file, and the agent will see the inline preview only.

    Naming: nanosecond timestamp + content hash. The hash lets us
    visually confirm two spills are identical (cheap dedup-by-eyeball
    for the agent), and the nanosecond timestamp keeps two consecutive
    identical-output commands from overwriting each other. (Millisecond
    resolution wasn't enough — fast test suites or parallel bash calls
    can produce two truncations within the same ms.)"""
    if not out:
        return None
    try:
        d = _bash_spill_dir_for_session(session_id)
        # Soft cap: skip the spill if the session's spill dir is already
        # over budget. The inline preview still goes to the LLM.
        if _bash_spill_dir_size(d) > _BASH_SPILL_DIR_MAX_BYTES:
            return None
        import hashlib
        import time
        h = hashlib.sha1(out.encode("utf-8", errors="replace")).hexdigest()[:10]
        ts = time.time_ns()
        path = d / f"bash-{ts}-{h}.log"
        path.write_text(out, encoding="utf-8")
        return str(path)
    except Exception:
        return None


def _smart_truncate_bash_output(
    out: str,
    return_code_interp: str | None,
    *,
    max_chars: int | None = None,
) -> str:
    """Head+tail truncate bash output with failure-aware weights, and
    spill the full output to a temp file the agent can read.

    Args:
        out: The combined stdout+stderr+exit-code string, already
            formatted by the bash wrapper.
        return_code_interp: e.g. "exit_code:1" — non-None + starts with
            "exit_code:" indicates a failure. None / anything else is a
            success or a control flow event (timeout, refused-by-guard).
        max_chars: Inline cap override. Defaults to OJAS_BASH_MAX_OUTPUT_CHARS
            (10 KB), clamped to [2 KB, 150 KB]. 10 KB ≈ 2,500 tokens —
            rich enough to fit a build summary and most pytest failures,
            cheap enough that 20 bash calls in a session costs ~50K
            tokens (vs 150K at the 30K CC default).
    """
    if max_chars is None:
        max_chars = _bash_inline_max_chars()
    if not out or len(out) <= max_chars:
        # Output fits in the inline cap. Emit a small status line so the
        # chat UI can show "X chars / Y cap, sent in full" for every
        # bash call, not just the truncated ones. Without this, the UI
        # would silently pass small commands through with no indicator
        # of what size was sent — the user would have to guess whether
        # the bash output was complete or not.
        total_chars = len(out)
        cap = max_chars
        status_marker = (
            f"\n\n[bash-output: total={total_chars}, cap={cap}, status=passed_through]\n"
        )
        # Append at the end so the LLM still gets a clean output. The
        # UI parses this line for the status display; the LLM can
        # safely ignore the trailing bracket (it doesn't trigger
        # any new behaviour).
        return out + status_marker

    is_failure = bool(return_code_interp) and return_code_interp.startswith("exit_code:")
    # Reserve ~5% of the budget for the marker so the LLM always gets
    # a clear pointer to the spill file and a recipe for fetching a
    # middle slice. The actual marker is 400-700 chars depending on
    # whether the spill was written and how long the path is.
    if is_failure:
        head_budget = int(max_chars * 0.28)
        tail_budget = int(max_chars * 0.62)
    else:
        # 50/50 split is more universal than 70/30: it works equally
        # well for `cat`, `git log`, `find`, and build outputs.
        head_budget = int(max_chars * 0.45)
        tail_budget = int(max_chars * 0.45)

    head = out[:head_budget]
    tail = out[-tail_budget:]
    dropped = len(out) - head_budget - tail_budget

    # Try to spill the full output to a session-scoped temp file so the
    # agent can `read_file` the original if the inline preview isn't
    # enough.
    sid: str | None = None
    try:
        from tools.sandbox import active_session_id
        sid = active_session_id()
    except Exception:
        sid = None
    spill_path = _spill_bash_output(out, sid)

    # Build the marker. The marker text is the single most important
    # defence against the "model hallucinates success after a middle
    # error" footgun — we explicitly tell the LLM what to do if it
    # doesn't see an error in the inline preview.
    #
    # The marker also carries structured numbers (kept_first / kept_last /
    # dropped / total) that the chat UI parses to render a single
    # status line under the bash tool card. Format is stable: the UI
    # greps for `[bash-output: total=T, cap=C, status=truncated,
    # kept_first=H, kept_last=L, dropped=D, verdict=V, spill=P]`.
    verdict = "FAILURE" if is_failure else "SUCCESS"
    total_chars = len(out)
    marker = (
        f"\n\n[…truncated {dropped:,} chars; this was a {verdict}."
        f"\n[bash-output: total={total_chars}, cap={max_chars}, status=truncated, "
        f"kept_first={head_budget}, kept_last={tail_budget}, dropped={dropped}, "
        f"verdict={verdict}, spill={spill_path or 'null'}]"
    )
    if spill_path:
        marker += (
            f"\nFull output saved to `{spill_path}`."
        )
        if is_failure:
            # Specific recipe for the footgun case: the error might be
            # in the middle, not in the tail. Don't trust the inline
            # preview alone.
            marker += (
                f"\nIf you don't see the error here, it may be in the "
                f"truncated middle — scan the spill with: "
                f"`grep -E 'error|Error|ERROR|ENOENT|EACCES|TS[0-9]+' {spill_path}`."
            )
        else:
            marker += (
                f"\nTo see a specific middle section, run e.g. "
                f"`sed -n '5000,5500p' {spill_path}` for a small slice."
            )
    else:
        marker += (
            f"\nRe-run with a tighter filter (e.g. `… 2>&1 | tail -200`, "
            f"`… 2>&1 | grep -E 'error|Error|ERROR'`) to see the missing "
            f"section inline."
        )
    marker += "\n…]\n\n"

    return head + marker + tail
# ============================================================================
# GIT (single tool covering read + write; mode is gated by the safety layer)
# ============================================================================
def _git_dispatch(
    action: str,
    path: str | None = None,
    commit: str | None = None,
    staged: bool | None = None,
    count: int | None = None,
    message: str | None = None,
    branch: str | None = None,
) -> str:
    """Shared implementation behind the `git` tool. Routes the action
    to the matching tools.git helper and enforces permission checks on the
    write-side actions (add / commit / push)."""
    cwd = path or _workspace
    try:
        if action == "status":
            return git_status(cwd=cwd)["output"]
        elif action == "diff":
            return git_diff(staged=staged or False, commit=commit, cwd=cwd)["output"]
        elif action == "log":
            return git_log(count=count or 20, cwd=cwd)["output"]
        elif action == "show":
            return git_show(commit=commit or "HEAD", cwd=cwd)["output"]
        elif action == "blame":
            return git_blame(path=path or ".", cwd=cwd)["output"]
        elif action == "branch":
            return git_create_branch(branch, cwd=cwd) if branch else git_current_branch(cwd=cwd)
        elif action == "checkout":
            return git_checkout(branch, cwd=cwd)
        elif action == "add":
            err = _check_permission("git", {"action": "add"})
            if err: return f"BLOCKED: {err}"
            return git_add(cwd=cwd)
        elif action == "commit":
            err = _check_permission("git", {"action": "commit"})
            if err: return f"BLOCKED: {err}"
            return git_commit(message or "auto commit", cwd=cwd)
        elif action == "push":
            err = _check_permission("git", {"action": "push"})
            if err: return f"BLOCKED: {err}"
            return git_push(branch=branch, cwd=cwd)
        elif action == "pull":
            return git_pull(cwd=cwd)
        elif action == "stash":
            return git_stash(message=message, cwd=cwd)
        else:
            return f"Unknown git action: {action}"
    except Exception as e:
        return f"Error: {e}"
@tool("git", args_schema=GitInput)
def git(
    action: str,
    path: str | None = None,
    commit: str | None = None,
    staged: bool | None = None,
    count: int | None = None,
    message: str | None = None,
    branch: str | None = None,
) -> str:
    """Git write operations: `status`, `diff`, `log`, `show`, `blame`, `branch`, `checkout`, `add`, `commit`, `push`, `pull`, `stash`. Use this in preference to `bash git...` for the actions it covers.
    For anything outside this set (rebase, cherry-pick, complex log queries, or HEREDOC-formatted commit messages), fall back to `bash` and follow the git-commit protocol documented in the `bash` tool description.
    """
    return _git_dispatch(action, path, commit, staged, count, message, branch)
# ============================================================================
# WEB
# ============================================================================
@tool("WebFetch", args_schema=WebFetchInput)
@_safe_tool("WebFetch")
def WebFetch(url: str, timeout: int | None = None) -> str:
    """Fetch a URL and return its content as clean, readable text (HTML stripped). Use for documentation pages, RFCs, or any URL the user supplies. Output is truncated to ~8000 characters."""
    return web_fetch(url, timeout=timeout or 20).result[:8000]
@tool("WebSearch", args_schema=WebSearchInput)
def WebSearch(
    query: str,
    allowed_domains: list | None = None,
    blocked_domains: list | None = None,
) -> str:
    """Search the web (DuckDuckGo). Returns a ranked list of title + URL results. Pair with `WebFetch` to read the most relevant hit. Use `allowed_domains` / `blocked_domains` to scope results."""
    try:
        result = web_search(query, allowed_domains, blocked_domains)
        if not result.results:
            return "No results found."
        lines = [f"Search results for: {query}"]
        for r in result.results:
            lines.append(f"- {r['title']}: {r['url']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"
# ============================================================================
# GITHUB
# ============================================================================
@tool("github", args_schema=GitHubInput)
def github(action: str, repo: str, **kwargs) -> str:
    """GitHub API (via `gh` CLI). Actions: `get_issue`, `create_pr`, `list_prs`.
    PR creation:
    - Keep titles under 70 characters; use the body for detail.
    - Body format: `## Summary` with 1–3 bullets, then `## Test plan` with a markdown checklist of how to verify.
    - When constructing the body in `bash`, use a HEREDOC (`gh pr create --body "$(cat <<'EOF'... EOF)"`) to preserve formatting.
    - Only create a PR when the user explicitly asks for one.
    """
    err = _check_permission("github", {"action": action, "repo": repo})
    if err:
        return f"BLOCKED: {err}"
    try:
        from tools.github import get_issue, list_prs, create_pr
        if action == "get_issue":
            return str(get_issue(repo=repo, issue_number=kwargs.get("issue_number", 1)))
        elif action == "list_prs":
            return str(list_prs(repo=repo, state=kwargs.get("state", "open")))
        elif action == "create_pr":
            result = create_pr(
                repo = repo,
                title = kwargs.get("title", ""),
                body = kwargs.get("body", ""),
                head_branch = kwargs.get("branch", ""),
                base_branch = kwargs.get("base_branch", "main"),
            )
            return f"PR created: {result.url}"
        else:
            return f"Unknown github action: {action}"
    except Exception as e:
        return f"Error: {e}"
# ============================================================================
# UTILITY — TodoWrite / Sleep / AskUserQuestion / SendUserMessage / ToolSearch
# / EnterPlanMode / ExitPlanMode / Task*
# ============================================================================
@tool("TodoWrite", args_schema=TodoWriteInput)
def TodoWrite(todos: list) -> str:
    """Create or update your todo list. The UI shows this as a LIVE plan
    panel that the user is watching — every state transition (pending →
    in_progress → completed) is rendered immediately. Get this wrong and
    the user can't see what you're doing. MANDATORY for any task with
    3+ distinct steps; the system prompt calls this out as a CRITICAL
    rule.

    WHEN TO CALL (mandatory — not a suggestion):
    - MUST be your FIRST tool call of any task with 3+ distinct steps.
      Emit the full plan with every item `pending` on turn 1, BEFORE
      any other tool call. The user is forming an opinion in the first
      5 seconds; an empty plan panel then is a UX failure.
    - MUST call on every state transition. One TodoWrite call per
      transition, never batched.
    - Skip ONLY for genuinely trivial single-step requests (a one-line
      edit, a single question, a quick lookup). If in doubt, plan it —
      2+ tool calls in the plan = plan it.

    ITEM SHAPE (each item is a dict):
    - `content`: imperative form, e.g. "Add tone/style section". Used for
      pending and completed rows.
    - `activeForm`: present-continuous, e.g. "Adding tone/style section".
      Used for the in_progress row — make it describe what you're doing
      RIGHT NOW, not what the eventual result is.
    - `status`: `pending` | `in_progress` | `completed`.

    CALLS ARE TOTAL: pass the FULL current list every time. Writes are
    not incremental — adding a new item means re-sending the whole
    array with the new item added and statuses updated.

    PARALLEL WORK: you MAY mark multiple items `in_progress` at the
    same time when you're working on them in parallel (e.g. writing
    three independent files in one assistant turn). The UI renders
    every in_progress item with a left bar and the activeForm text —
    so parallel work is visible to the user as a batch, not collapsed.

    TRANSITIONS (one TodoWrite call per transition):
    - Starting work on an item: flip it to `in_progress` (and any
      parallel siblings) — emit BEFORE the tool calls that do the
      work.
    - Finishing an item: flip it to `completed` IMMEDIATELY in the
      same turn as the tool that finished it. Do NOT wait until all
      parallel items finish to update — emit a TodoWrite call for
      each completion, even if the batch is still in flight. One
      completion = one call.
    - Plan changes (new step, dropped step, reorder): emit a
      TodoWrite call with the full updated list. Plans are not set
      in stone — change them freely, but EVERY change goes through
      a TodoWrite call so the panel reflects reality.
    - Dropping a no-longer-relevant item: just remove it from the
      next call's array. No need for a separate "abandoned" status.
    """
    try:
        items = [
            {"content": t["content"], "status": t["status"], "activeForm": t["activeForm"]}
            if isinstance(t, dict) else
            {"content": t.content, "status": t.status, "activeForm": t.activeForm}
            for t in todos
        ]
        result = todo_write(items)
        # Notify the UI — full current list so reload / late-joining clients
        # see the same state without replaying every prior TodoWrite call.
        from agents.reporter import get_reporter
        _safe_report(lambda: get_reporter().todo_update(list(result.new_todos)))
        icons = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        lines = ["Updated todo list:"]
        for t in result.new_todos:
            lines.append(f" {icons.get(t['status'], '[ ]')} {t['content']}")
        if result.verification_nudge_needed:
            lines.append("(consider adding a verification step)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"
@tool("Sleep", args_schema=SleepInput)
@_safe_tool("Sleep")
def Sleep(duration_ms: int) -> str:
    """Pause for `duration_ms` milliseconds (max 300000). Use sparingly — only when you need to wait for a backgrounded process to make progress (e.g. dev server boot).
    Prefer polling the actual state (read a file, check a port) over a fixed sleep when possible.
    """
    return f"Slept for {sleep_tool(duration_ms)['slept_ms']}ms."
@tool("AskUserQuestion", args_schema=AskUserInput)
def AskUserQuestion(question: str, options: list | None = None) -> str:
    """Ask the user a single clarifying question and wait for their answer. Asking has a cost — it interrupts the user and is often answerable by looking at the codebase yourself.
    Before asking:
    - Spend up to ~1 minute on read-only investigation (grep, read config, check docs) so your question is specific and grounded.
    - Prefer "I found A and B in `config.yaml` — which do you want?" over "what config?"
    Usage:
    - One question at a time, phrased so it's answerable in a short reply.
    - Optionally pass `options` (list of choices) for a closed question.
    """
    # LangGraph's `interrupt()` pauses the graph and surfaces the question
    # to the runner. The web backend will route this to a UI modal in a
    # later phase; for now, if no UI is listening (or the call fails)
    # we return a clear marker so the model can adapt rather than blocking
    # on terminal input that no longer exists.
    try:
        from langgraph.types import interrupt
        answer = interrupt(
            {"type": "question", "question": question, "options": options or []}
        )
        return str(answer) if answer else ""
    except Exception:
        return (
            "(no interactive user available — please proceed with your best "
            "guess, then explain the assumption in your reply)"
        )
@tool("SendUserMessage", args_schema=SendMessageInput)
def SendUserMessage(message: str) -> str:
    """Send a progress update or interim finding to the user during a long task. Use sparingly — only for milestones the user actually wants to see (e.g. "finished exploration, starting implementation").
    Do not use as a substitute for the end-of-turn summary.
    """
    try:
        from agents.reporter import get_reporter
        get_reporter().message(message)
        result = brief(message)
        return f"Message sent: {result.message}"
    except Exception as e:
        return f"Error: {e}"
@tool("ToolSearch", args_schema=ToolSearchInput)
def ToolSearch(query: str) -> str:
    """Search available tools by name or keyword. Use to discover tools whose schemas aren't loaded yet, or to find the right tool for a task.
    Usage:
    - `select:Tool1,Tool2,...` — load these exact tool schemas (use when you already know the names).
    - Free-text query — ranked keyword search across tool names and descriptions; top 5 returned.
    """
    query_lower = query.strip().lower()
    if query_lower.startswith("select:"):
        names = [n.strip() for n in query_lower[7:].split(",") if n.strip()]
        matches = [t for t in ALL_TOOLS if t.name.lower() in names]
    else:
        tokens = re.split(r"\s+", query_lower)
        scored: list = []
        for t in ALL_TOOLS:
            haystack = f"{t.name.lower()} {(t.description or '').lower()}"
            score = sum(
                2 if tok in t.name.lower() else 1
                for tok in tokens if tok in haystack
            )
            if score > 0:
                scored.append((score, t))
        matches = [t for _, t in sorted(scored, key=lambda x: -x[0])][:5]
    if not matches:
        return f"No tools found matching '{query}'"
    lines = [f"Tools matching '{query}':"]
    for t in matches:
        lines.append(f" {t.name}: {(t.description or '')[:100]}")
    return "\n".join(lines)
@tool("EnterPlanMode", args_schema=PlanModeInput)
@_safe_tool("EnterPlanMode")
def EnterPlanMode() -> str:
    """Switch to plan-only mode. Read, search, and explore freely; all write, edit, bash, and execute tools are BLOCKED until `ExitPlanMode`.
    Use when the user asks for a plan, design, or analysis before any changes are made.

    Enforcement is per-thread: the flag lives in this session's RunnerState
    (not a process global), so flipping it here only affects this conversation.
    The pre-scan in node_tools strips write-tool calls from the next model
    response and synthesises a `BLOCKED:` ToolMessage for each, so the model
    can react to the rejection just like any other tool error.
    """
    return "Plan mode ON. Explore and search freely. Do NOT write/edit/bash. Call ExitPlanMode when ready."
@tool("ExitPlanMode", args_schema=PlanModeInput)
@_safe_tool("ExitPlanMode")
def ExitPlanMode() -> str:
    """Leave plan mode and re-enable write / edit / bash tools. Call ONLY after presenting your plan to the user AND receiving explicit approval to proceed.
    Do not call it implicitly to bypass plan mode.
    """
    return "Plan mode OFF. Write/edit/bash tools are now available."
UTILITY_TOOLS = [
    TodoWrite, Sleep, AskUserQuestion, SendUserMessage, ToolSearch,
    EnterPlanMode, ExitPlanMode,
]
# ============================================================================
# READ / WRITE TOOL LISTS
# ============================================================================
READ_TOOLS = [
    read_file, WebFetch, WebSearch,
] + UTILITY_TOOLS # utility tools are available in read/plan mode too
WRITE_TOOLS = [
    write_file, edit_file, bash, git, WebFetch, WebSearch, github,
] + UTILITY_TOOLS
# ============================================================================
# MULTI-AGENT — Agent + AgentStatus + Worker* lifecycle
#
# ============================================================================
@tool("Agent", args_schema=AgentToolInput)
@_safe_tool("Agent")
def Agent(
    description: str,
    prompt: str,
    subagent_type: str | None = None,
    name: str | None = None,
    model: str | None = None,
    max_idle_seconds: int | None = None,
    max_total_seconds: int | None = None,
) -> str:
    """Launch a BACKGROUND sub-agent in a fresh conversation. Returns immediately with `{agentId, status: "running", outputFile,...}` — you must poll completion with `AgentStatus`.
    When to use:
    - Large, separable work that would bloat your own context (broad codebase exploration, multi-file research, running long verification suites).
    - Truly INDEPENDENT parallel work — spawn multiple agents in the same message and they run concurrently.
    - When the orchestration playbook in the system prompt applies.
    When NOT to use:
    - Small or tightly-coupled work — do it yourself. The sub-agent's fresh context costs more than the savings.
    - Sub-agents CANNOT spawn further sub-agents. You are the sole orchestrator.
    subagent_type (defaults to `general-purpose`):
    - `Explore` — read-only research (read_file, grep, glob, web). Use for "where is X defined / what references Y".
    - `Plan` — read-only + TodoWrite. Use to produce a roadmap or implementation plan before any writes.
    - `Verification` — read + bash. Use to run tests, type-check, build, or otherwise verify a deterministic check.
    - `general-purpose` — full write/execute toolset. Use for self-contained build tasks.
    - `claw-guide` — guidance/Q&A about the agent harness itself.
    - `statusline-setup` — narrow tool set for configuring status line.
    Briefing the sub-agent (critical):
    - The sub-agent sees ONLY your `prompt` + what it reads from disk — it has no memory of this conversation. Brief it like a smart colleague who just walked in: goal, what you've already ruled out, the exact files/lines to look at, and the form the answer should take.
    - Terse command-style prompts produce shallow work. Specific, context-rich prompts produce good work.
    - Never delegate UNDERSTANDING — don't write "based on what you find, decide what to do." Decide yourself, then delegate the execution.
    """
    from tools.multi_agent import run_agent
    manifest = run_agent(
        description, prompt, subagent_type, name, model,
        max_idle_seconds=max_idle_seconds,
        max_total_seconds=max_total_seconds,
    )
    # Notify the UI — populates the sub-agent tree the moment the spawn lands.
    from agents.reporter import get_reporter
    _safe_report(lambda: get_reporter().agent_spawn(
        agent_id = str(manifest.get("agentId", "")),
        description = description,
        subagent_type = str(manifest.get("subagentType", subagent_type or "general-purpose")),
        name = str(manifest.get("name", name or "")),
        model = str(manifest.get("model", model or "")),
    ))
    return json.dumps(manifest)
@tool("AgentStatus", args_schema=AgentStatusInput)
@_safe_tool("AgentStatus")
def AgentStatus(agent_id: str) -> str:
    """Poll a spawned sub-agent's manifest by `agent_id`. Returns `{status: running | completed | failed, outputFile, error?,...}`.
    Usage:
    - Poll until `status` becomes `completed` or `failed`. The harness's stall detector treats `AgentStatus` as a poll-style call, so it won't false-trip when you wait.
    - On `completed`: read the `outputFile` with `read_file` to get the actual result. Do not assume — the agent may have done something different from what you asked.
    - On `failed`: read the `error`, then adapt (fix input, retry with a narrower scope, or spawn a debugger sub-agent). Never silently proceed past a failure.
    - A dependent stage starts only after `completed` AND a deterministic check has passed (tests / type-check / compile).
    """
    from tools.multi_agent import get_agent_status
    manifest = get_agent_status(agent_id)
    # Notify the UI — keeps the sub-agent tree's status badge in sync.
    from agents.reporter import get_reporter
    _safe_report(lambda: get_reporter().agent_status_update(
        agent_id = str(manifest.get("agentId", agent_id)),
        status = str(manifest.get("status", "")),
        output_file = str(manifest.get("outputFile", "")),
        error = str(manifest.get("error") or ""),
    ))
    return json.dumps(manifest)
MULTI_AGENT_TOOLS = [Agent, AgentStatus]
# ============================================================================
# ALL_TOOLS + public API
# ============================================================================
# Union of READ + WRITE + MULTI_AGENT, deduped while preserving order so the
# read tools (read_file, WebFetch, WebSearch) actually reach the LLM.
# Previously this was just WRITE_TOOLS + MULTI_AGENT_TOOLS, which silently
# dropped every read tool — the model would call `read_file` and get "not
# a valid tool" errors and fall back to bash.
def _dedupe_tools(*groups):
    seen: set = set()
    out: list = []
    for group in groups:
        for t in group:
            name = getattr(t, "name", None)
            if name and name in seen:
                continue
            if name:
                seen.add(name)
            out.append(t)
    return out

ALL_TOOLS: list = _dedupe_tools(READ_TOOLS, WRITE_TOOLS, MULTI_AGENT_TOOLS)
def get_read_tools() -> list:
    """Tools for plan/explore mode — no writes, no bash."""
    return list(READ_TOOLS)
def get_all_tools(extra_tools: list | None = None) -> list:
    """All tools (read + write + utility + multi-agent). Optionally append MCP tools."""
    tools = list(ALL_TOOLS)
    if extra_tools:
        tools.extend(extra_tools)
    return tools
