"""
Tool wrappers — wraps all custom tools in LangChain StructuredTool.

Safety is injected INSIDE each wrapper so LangGraph's ToolNode
automatically enforces all safety checks on every tool call.

Display features:
  _show_diffs=True  → colored diff printed after every edit/write (CLI mode)
  progress reporter → tool_start/tool_done events for live progress feed
"""

import difflib
import json
import os
from typing import Optional

from langchain_core.tools import StructuredTool, tool
from pydantic import BaseModel, Field

# ── your production implementations (untouched) ──────────────────────────────
from tools.file_ops import (
    read_file, write_file, edit_file,
    grep_search, glob_search, GrepSearchInput,
)
from tools.bash import execute_bash, BashInput
from tools.git import (
    git_status, git_diff, git_log, git_show, git_blame,
    git_create_branch, git_checkout, git_add, git_commit,
    git_push, git_pull, git_stash, git_stash_pop, git_reset,
    git_current_branch,
)
from tools.web import web_fetch, web_search
from tools.tasks import TaskManager
from tools.github import create_pr, list_prs, get_issue

# ── safety layer (untouched) ─────────────────────────────────────────────────
from safety.bash_validator import validate_command, PermissionMode, ValidationStatus
from safety.sandbox import SandboxStatus, execute_sandboxed, SandboxResult
from safety.permissions import PermissionPolicy, TOOL_REQUIRED_MODES
from safety.hooks import HookRunner


# ---------------------------------------------------------------------------
# Global safety context — set once at startup by main.py
# ---------------------------------------------------------------------------

_permission_policy: PermissionPolicy = PermissionPolicy()
_hook_runner: HookRunner = HookRunner()
_sandbox: Optional[SandboxStatus] = None
_workspace: str = "."
_permission_mode: PermissionMode = PermissionMode.FULL_ACCESS

# ---------------------------------------------------------------------------
# Display config — set at startup by main.py
# ---------------------------------------------------------------------------

_show_diffs: bool = False   # True in CLI mode: show colored diff after every edit


def configure_display(show_diffs: bool) -> None:
    """Called at startup. CLI mode → show_diffs=True. Auto → False."""
    global _show_diffs
    _show_diffs = show_diffs


# ---------------------------------------------------------------------------
# Diff display helpers
# ---------------------------------------------------------------------------

_C_GREEN  = "\033[32m"
_C_RED    = "\033[31m"
_C_CYAN   = "\033[36m"
_C_YELLOW = "\033[33m"
_C_DIM    = "\033[2m"
_C_BOLD   = "\033[1m"
_C_RESET  = "\033[0m"


def _print_diff(path: str, original: str, updated: str) -> None:
    """Print unified diff with ANSI colors — only in CLI mode."""
    if not _show_diffs:
        return

    orig_lines    = (original or "").splitlines(keepends=True)
    updated_lines = (updated  or "").splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        orig_lines, updated_lines,
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=3,
    ))

    if not diff:
        return

    print()
    for line in diff:
        if line.startswith("+++ ") or line.startswith("--- "):
            print(f"{_C_BOLD}{line}{_C_RESET}", end="")
        elif line.startswith("@@"):
            print(f"{_C_CYAN}{line}{_C_RESET}", end="")
        elif line.startswith("+"):
            print(f"{_C_GREEN}{line}{_C_RESET}", end="")
        elif line.startswith("-"):
            print(f"{_C_RED}{line}{_C_RESET}", end="")
        else:
            print(f"{_C_DIM}{line}{_C_RESET}", end="")
    print()


def _print_new_file(path: str, content: str) -> None:
    """Print new file content in green — only in CLI mode."""
    if not _show_diffs:
        return

    lines = (content or "").splitlines()
    print(f"\n{_C_BOLD}{_C_GREEN}+++ {path} [NEW FILE]{_C_RESET}")
    for line in lines[:60]:
        print(f"{_C_GREEN}+{line}{_C_RESET}")
    if len(lines) > 60:
        print(f"{_C_DIM}... {len(lines) - 60} more lines{_C_RESET}")
    print()


def configure_safety(
    permission_policy: PermissionPolicy,
    hook_runner: HookRunner,
    sandbox: Optional[SandboxStatus],
    workspace: str,
    permission_mode: PermissionMode,
) -> None:
    """Called once at startup to wire safety into all tool wrappers."""
    global _permission_policy, _hook_runner, _sandbox, _workspace, _permission_mode
    _permission_policy = permission_policy
    _hook_runner       = hook_runner
    _sandbox           = sandbox
    _workspace         = workspace
    _permission_mode   = permission_mode


def _check_permission(tool_name: str, input_dict: dict) -> Optional[str]:
    """Returns error string if denied, None if allowed."""
    input_str = json.dumps(input_dict)

    # pre-hook
    hook = _hook_runner.pre_tool_use(tool_name, input_str)
    if hook.denied or hook.failed:
        return "; ".join(hook.messages) or f"Hook denied '{tool_name}'"

    # permission policy
    perm = _permission_policy.authorize(tool_name, input_str)
    if not perm.allowed:
        return perm.reason

    return None

import functools

# ---------------------------------------------------------------------------
# Safety decorator — permission check + pre-hook + try/except + post-hook
# Apply to tools that don't need special diff display or sandbox logic
# ---------------------------------------------------------------------------

def _safe_tool(tool_name: str):
    """
    Wraps a tool function with:
      1. Permission check (returns BLOCKED if denied)
      2. Pre-hook (user-configured shell script)
      3. try/except (returns Error: message on exception)
      4. Post-hook on success or failure

    Use for tools that have no special display logic (no diff, no sandbox).
    _write_file, _edit_file, _bash keep their explicit bodies for diff/sandbox.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            inp_dict = kwargs or {}
            if args:
                import inspect
                sig    = inspect.signature(fn)
                params = list(sig.parameters.keys())
                inp_dict = {**{params[i]: a for i, a in enumerate(args)}, **kwargs}

            inp_str = json.dumps({k: v for k, v in inp_dict.items()
                                  if isinstance(v, (str, int, bool, type(None)))})

            # pre-hook
            hook = _hook_runner.pre_tool_use(tool_name, inp_str)
            if hook.denied or hook.failed:
                return "; ".join(hook.messages) or f"Hook denied '{tool_name}'"

            # permission check
            perm = _permission_policy.authorize(tool_name, inp_str)
            if not perm.allowed:
                return f"BLOCKED: {perm.reason}"

            try:
                result = fn(*args, **kwargs)
                _hook_runner.post_tool_use(tool_name, inp_str, str(result)[:200])
                return result
            except Exception as e:
                _hook_runner.post_tool_failure(tool_name, inp_str, str(e))
                return f"Error: {e}"
        return wrapper
    return decorator



# ---------------------------------------------------------------------------
# Pydantic schemas for each tool
# ---------------------------------------------------------------------------

class ReadFileInput(BaseModel):
    path:   str            = Field(description="Absolute or relative path to the file")
    offset: Optional[int]  = Field(None, description="Line number to start reading from")
    limit:  Optional[int]  = Field(None, description="Maximum number of lines to read")

class WriteFileInput(BaseModel):
    path:    str = Field(description="Path to write to (creates parent dirs)")
    content: str = Field(description="Full file content to write")

class EditFileInput(BaseModel):
    path:        str  = Field(description="Path of the file to edit")
    old_string:  str  = Field(description="Exact text to find and replace")
    new_string:  str  = Field(description="Replacement text")
    replace_all: bool = Field(False, description="Replace all occurrences (default: first only)")

class GrepInput(BaseModel):
    pattern:          str            = Field(description="Regex pattern to search for")
    path:             Optional[str]  = Field(None, description="Directory to search in")
    glob:             Optional[str]  = Field(None, description="File glob filter e.g. *.py")
    output_mode:      Optional[str]  = Field(None, description="files_with_matches | content | count")
    before:           Optional[int]  = Field(None, description="Lines of context before match")
    after:            Optional[int]  = Field(None, description="Lines of context after match")
    case_insensitive: Optional[bool] = Field(None, description="Case-insensitive search")
    file_type:        Optional[str]  = Field(None, description="Filter by extension e.g. py")
    head_limit:       Optional[int]  = Field(None, description="Max results to return")
    multiline:        Optional[bool] = Field(None, description="Enable dot to match newlines")
    line_numbers:     Optional[bool] = Field(None, description="Show line numbers (default True)")

class GlobInput(BaseModel):
    pattern: str           = Field(description="Glob pattern e.g. **/*.py")
    path:    Optional[str] = Field(None, description="Base directory (default: cwd)")

class BashInputSchema(BaseModel):
    command:           str           = Field(description="Shell command to execute")
    timeout:           Optional[int] = Field(None, description="Timeout in milliseconds")
    run_in_background: bool          = Field(False, description="Run without waiting for output")

class GitInput(BaseModel):
    action: str            = Field(description="status|diff|log|show|blame|branch|commit|push|pull|stash")
    path:   Optional[str]  = Field(None)
    commit: Optional[str]  = Field(None)
    staged: Optional[bool] = Field(None)
    count:  Optional[int]  = Field(None)
    message: Optional[str] = Field(None)
    branch:  Optional[str] = Field(None)

class WebFetchInput(BaseModel):
    url:     str           = Field(description="URL to fetch")
    timeout: Optional[int] = Field(None, description="Timeout in seconds")

class WebSearchInput(BaseModel):
    query:           str             = Field(description="Search query")
    allowed_domains: Optional[list]  = Field(None)
    blocked_domains: Optional[list]  = Field(None)


# ---------------------------------------------------------------------------
# Tool wrapper functions — safety injected here
# ---------------------------------------------------------------------------

@_safe_tool("read_file")
def _read_file(path: str, offset: Optional[int] = None, limit: Optional[int] = None) -> str:
    result = read_file(path, offset, limit)
    f = result.file
    header = f"File: {f.filePath} (lines {f.startLine}-{f.startLine + f.numLines - 1} of {f.totalLines})\n{'-'*40}\n"
    return header + f.content


def _write_file(path: str, content: str) -> str:
    err = _check_permission("write_file", {"path": path})
    if err:
        return f"BLOCKED: {err}"
    try:
        result = write_file(path, content)
        verb   = "Created" if result.type == "create" else "Updated"

        # show diff / new file content in CLI mode
        if result.type == "create":
            _print_new_file(result.filePath, result.content)
        else:
            _print_diff(result.filePath, result.originalFile or "", result.content)

        _hook_runner.post_tool_use("write_file", json.dumps({"path": path}),
                                   f"{verb}: {result.filePath}")

        return f"{verb}: {result.filePath} ({len(result.content)} bytes)"
    except Exception as e:
        _hook_runner.post_tool_failure("write_file", json.dumps({"path": path}), str(e))
        return f"Error: {e}"


def _edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    inp = {"path": path, "old_string": old_string, "new_string": new_string}
    err = _check_permission("edit_file", inp)
    if err:
        return f"BLOCKED: {err}"
    try:
        result = edit_file(path, old_string, new_string, replace_all)
        reps   = result.originalFile.count(old_string) if replace_all else 1

        # show colored diff in CLI mode — computed from original vs updated content
        updated = result.originalFile.replace(old_string, new_string,
                                               0 if replace_all else 1)
        _print_diff(result.filePath, result.originalFile, updated)

        out = f"Edited: {result.filePath}\nReplacements: {reps}"
        _hook_runner.post_tool_use("edit_file", json.dumps(inp), out)

        return out
    except Exception as e:
        _hook_runner.post_tool_failure("edit_file", json.dumps(inp), str(e))
        return f"Error: {e}"


def _grep_search(
    pattern: str,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    output_mode: Optional[str] = None,
    before: Optional[int] = None,
    after: Optional[int] = None,
    case_insensitive: Optional[bool] = None,
    file_type: Optional[str] = None,
    head_limit: Optional[int] = None,
    multiline: Optional[bool] = None,
    line_numbers: Optional[bool] = None,
) -> str:
    try:
        inp = GrepSearchInput(
            pattern=pattern, path=path, glob=glob,
            output_mode=output_mode, before=before, after=after,
            case_insensitive=case_insensitive, file_type=file_type,
            head_limit=head_limit, multiline=multiline, line_numbers=line_numbers,
        )
        result = grep_search(inp)
        if result.content:
            return result.content
        return f"Found in {result.numFiles} files: {', '.join(result.filenames[:10])}"
    except Exception as e:
        return f"Error: {e}"


@_safe_tool("glob_search")
def _glob_search(pattern: str, path: Optional[str] = None) -> str:
    result = glob_search(pattern, path)
    if not result.filenames:
        return "No files found."
    lines = [f"Found {result.numFiles} files ({result.durationMs}ms):"] + result.filenames[:50]
    if result.truncated:
        lines.append("... (truncated to 100 results)")
    return "\n".join(lines)


def _bash(command: str, timeout: Optional[int] = None, run_in_background: bool = False) -> str:
    inp = {"command": command}
    err = _check_permission("bash", inp)
    if err:
        return f"BLOCKED: {err}"

    # bash-specific validator
    val = validate_command(command, _permission_mode, _workspace)
    if val.is_blocked:
        return f"BLOCKED: {val.message}"
    if val.is_warning:
        print(f"\033[33m⚠️  {val.message}\033[0m")

    try:
        if _sandbox and _sandbox.active:
            timeout_sec = timeout / 1000.0 if timeout else None
            result = execute_sandboxed(command, _sandbox, timeout_sec)
            parts = []
            if result.stdout:  parts.append(result.stdout)
            if result.stderr:  parts.append(f"[stderr]\n{result.stderr}")
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
        return out
    except Exception as e:
        _hook_runner.post_tool_failure("bash", json.dumps(inp), str(e))
        return f"Error: {e}"


def _git(
    action: str,
    path: Optional[str] = None,
    commit: Optional[str] = None,
    staged: Optional[bool] = None,
    count: Optional[int] = None,
    message: Optional[str] = None,
    branch: Optional[str] = None,
) -> str:
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


class GitHubInput(BaseModel):
    action:       str            = Field(description="get_issue | list_issues | comment_issue | create_pr | get_pr | list_prs | get_repo_info | get_file")
    repo:         str            = Field(description="owner/repo e.g. anthropics/claude")
    issue_number: Optional[int]  = Field(None)
    pr_number:    Optional[int]  = Field(None)
    title:        Optional[str]  = Field(None)
    body:         Optional[str]  = Field(None)
    branch:       Optional[str]  = Field(None)
    base_branch:  Optional[str]  = Field(None)
    path:         Optional[str]  = Field(None)
    state:        Optional[str]  = Field(None)
    comment:      Optional[str]  = Field(None)


def _github(action: str, repo: str, **kwargs) -> str:
    err = _check_permission("github", {"action": action, "repo": repo})
    if err:
        return f"BLOCKED: {err}"
    try:
        from tools.github import get_issue, list_prs, create_pr
        if action == "get_issue":
            result = get_issue(repo=repo, issue_number=kwargs.get("issue_number", 1))
            return str(result)
        elif action in ("list_prs", "get_pr"):
            result = list_prs(repo=repo, state=kwargs.get("state", "open"))
            return str(result)
        elif action == "create_pr":
            result = create_pr(
                repo        = repo,
                title       = kwargs.get("title", ""),
                body        = kwargs.get("body", ""),
                head_branch = kwargs.get("branch", ""),
                base_branch = kwargs.get("base_branch", "main"),
            )
            return f"PR created: {result.url}"
        else:
            return f"Unknown github action: {action}"
    except Exception as e:
        return f"Error: {e}"

@_safe_tool("WebFetch")
def _web_fetch(url: str, timeout: Optional[int] = None) -> str:
    return web_fetch(url, timeout=timeout or 20).result[:8000]


def _web_search(
    query: str,
    allowed_domains: Optional[list] = None,
    blocked_domains: Optional[list] = None,
) -> str:
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


# ---------------------------------------------------------------------------
# Utility tools — TodoWrite, Sleep, AskUserQuestion, SendUserMessage,
#                 ToolSearch, EnterPlanMode, ExitPlanMode,
#                 TaskCreate/Update/Get/List/Stop/Output
# ---------------------------------------------------------------------------

from tools.utils import todo_write, sleep_tool, ask_user_question, brief


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class TodoItem(BaseModel):
    content:    str = Field(description="Task description")
    status:     str = Field(description="pending | in_progress | completed")
    activeForm: str = Field(description="Identifier for the task form")

class TodoWriteInput(BaseModel):
    todos: list[TodoItem] = Field(description="Full todo list to write")

class SleepInput(BaseModel):
    duration_ms: int = Field(description="Milliseconds to wait (max 300000)")

class AskUserInput(BaseModel):
    question: str             = Field(description="Question to ask the user")
    options:  Optional[list]  = Field(None, description="Optional list of choices")

class SendMessageInput(BaseModel):
    message: str = Field(description="Message or status update to send to the user")

class ToolSearchInput(BaseModel):
    query: str = Field(description="Search query e.g. 'file edit', 'select:bash,git'")

class PlanModeInput(BaseModel):
    pass   # no parameters

class TaskCreateInput(BaseModel):
    prompt:      str           = Field(description="Task description / what to track")
    description: Optional[str] = Field(None, description="Longer description")

class TaskIdInput(BaseModel):
    task_id: str = Field(description="Task ID returned by TaskCreate")

class TaskUpdateInput(BaseModel):
    task_id: str = Field(description="Task ID")
    message: str = Field(description="Progress message to append")

class TaskListInput(BaseModel):
    status: Optional[str] = Field(None, description="Filter by status e.g. running")


# ── Tool functions ────────────────────────────────────────────────────────────

def _todo_write(todos: list) -> str:
    try:
        items = [
            {"content": t["content"], "status": t["status"], "activeForm": t["activeForm"]}
            if isinstance(t, dict) else
            {"content": t.content, "status": t.status, "activeForm": t.activeForm}
            for t in todos
        ]
        result = todo_write(items)
        icons  = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        lines  = ["Updated todo list:"]
        for t in result.new_todos:
            lines.append(f"  {icons.get(t['status'], '[ ]')} {t['content']}")
        if result.verification_nudge_needed:
            lines.append("(consider adding a verification step)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@_safe_tool("Sleep")
def _sleep(duration_ms: int) -> str:
    return f"Slept for {sleep_tool(duration_ms)['slept_ms']}ms."


def _ask_user_question(question: str, options: Optional[list] = None) -> str:
    """
    In LangGraph: uses interrupt() to pause the graph and wait for user input.
    Falls back to terminal input if not inside a graph execution.
    """
    try:
        from langgraph.types import interrupt
        answer = interrupt({"type": "question", "question": question, "options": options or []})
        return str(answer) if answer else ""
    except Exception:
        return ask_user_question(question, options)


def _send_user_message(message: str) -> str:
    try:
        from ui.progress import get_reporter
        get_reporter().message(message)
        result = brief(message)
        return f"Message sent: {result.message}"
    except Exception as e:
        return f"Error: {e}"


def _tool_search(query: str) -> str:
    """Search all registered tools by name or description."""
    import re
    query_lower = query.strip().lower()

    if query_lower.startswith("select:"):
        names   = [n.strip() for n in query_lower[7:].split(",") if n.strip()]
        matches = [t for t in ALL_TOOLS if t.name.lower() in names]
    else:
        tokens  = re.split(r"\s+", query_lower)
        matches = []
        for t in ALL_TOOLS:
            haystack = f"{t.name.lower()} {(t.description or '').lower()}"
            score    = sum(2 if tok in t.name.lower() else 1
                          for tok in tokens if tok in haystack)
            if score > 0:
                matches.append((score, t))
        matches = [t for _, t in sorted(matches, key=lambda x: -x[0])][:5]

    if not matches:
        return f"No tools found matching '{query}'"
    lines = [f"Tools matching '{query}':"]
    for t in matches:
        lines.append(f"  {t.name}: {(t.description or '')[:100]}")
    return "\n".join(lines)


# Plan mode — in LangGraph, Phases 1+2 are read-only by design.
# These tools signal Claude's intent and update a session-level flag.
_plan_mode_active: bool = False

@_safe_tool("EnterPlanMode")
def _enter_plan_mode() -> str:
    global _plan_mode_active
    _plan_mode_active = True
    return "Plan mode ON. Explore and search freely. Do NOT write/edit/bash. Call ExitPlanMode when ready."

@_safe_tool("ExitPlanMode")
def _exit_plan_mode() -> str:
    global _plan_mode_active
    _plan_mode_active = False
    return "Plan mode OFF. Write and execute tools available."


# Task tools — backed by tools/tasks.py TaskManager
_task_manager = None

def _get_task_manager():
    global _task_manager
    if _task_manager is None:
        from tools.tasks import TaskManager
        _task_manager = TaskManager()
    return _task_manager

@_safe_tool("TaskCreate")
def _task_create(prompt: str, description: Optional[str] = None) -> str:
    task = _get_task_manager().create(prompt=prompt, description=description or "")
    return f"Task created: {task.task_id} | {task.status.value}"

@_safe_tool("TaskUpdate")
def _task_update(task_id: str, message: str) -> str:
    _get_task_manager().append_output(task_id, message)
    return f"Task {task_id} updated."

@_safe_tool("TaskGet")
def _task_get(task_id: str) -> str:
    t = _get_task_manager().get(task_id)
    return f"Task {t.task_id}: {t.status.value}\n{t.prompt}"

def _task_list(status: Optional[str] = None) -> str:
    try:
        mgr   = _get_task_manager()
        tasks = mgr.list_all(status_filter=status)
        if not tasks:
            return "No tasks."
        lines = [f"Tasks ({len(tasks)}):"]
        for t in tasks:
            lines.append(f"  [{t.status.value}] {t.task_id}: {t.prompt[:60]}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

@_safe_tool("TaskStop")
def _task_stop(task_id: str) -> str:
    _get_task_manager().stop(task_id)
    return f"Task {task_id} stopped."

def _task_output(task_id: str) -> str:
    try:
        mgr  = _get_task_manager()
        task = mgr.get(task_id)
        msgs = getattr(task, "messages", []) or getattr(task, "output", []) or []
        if not msgs:
            return f"Task {task_id}: no output yet."
        lines = [f"Task {task_id} output:"]
        for m in msgs:
            if isinstance(m, str):
                lines.append(f"  {m}")
            elif hasattr(m, "content"):
                lines.append(f"  {m.content}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


# ── Tool definitions ──────────────────────────────────────────────────────────

UTILITY_TOOLS = [
    StructuredTool.from_function(func=_todo_write, name="TodoWrite",
        args_schema=TodoWriteInput,
        description="Create or update your todo list. Use at the start of complex tasks. Status: pending | in_progress | completed."),
    StructuredTool.from_function(func=_sleep, name="Sleep",
        args_schema=SleepInput,
        description="Wait for duration_ms milliseconds (max 300000). Use to wait for background processes or servers to start."),
    StructuredTool.from_function(func=_ask_user_question, name="AskUserQuestion",
        args_schema=AskUserInput,
        description="Ask the user a clarifying question and wait for their answer. Ask one specific question at a time."),
    StructuredTool.from_function(func=_send_user_message, name="SendUserMessage",
        args_schema=SendMessageInput,
        description="Send a progress update or result to the user. Use for long tasks to report findings."),
    StructuredTool.from_function(func=_tool_search, name="ToolSearch",
        args_schema=ToolSearchInput,
        description="Search available tools by name or keyword. Use 'select:Tool1,Tool2' for exact lookup."),
    StructuredTool.from_function(func=_enter_plan_mode, name="EnterPlanMode",
        args_schema=PlanModeInput,
        description="Switch to plan-only mode. Explore and read freely. All write/execute tools are blocked until ExitPlanMode."),
    StructuredTool.from_function(func=_exit_plan_mode, name="ExitPlanMode",
        args_schema=PlanModeInput,
        description="Exit plan mode. Call after presenting your plan and receiving approval. Write tools become available again."),
    StructuredTool.from_function(func=_task_create, name="TaskCreate",
        args_schema=TaskCreateInput,
        description="Create a task to track work. Returns a task_id. Status: created → running → completed/failed/stopped."),
    StructuredTool.from_function(func=_task_update, name="TaskUpdate",
        args_schema=TaskUpdateInput,
        description="Append a progress message to a task's log."),
    StructuredTool.from_function(func=_task_get, name="TaskGet",
        args_schema=TaskIdInput,
        description="Get details of a specific task by ID."),
    StructuredTool.from_function(func=_task_list, name="TaskList",
        args_schema=TaskListInput,
        description="List all tasks, optionally filtered by status."),
    StructuredTool.from_function(func=_task_stop, name="TaskStop",
        args_schema=TaskIdInput,
        description="Stop a running task."),
    StructuredTool.from_function(func=_task_output, name="TaskOutput",
        args_schema=TaskIdInput,
        description="Get accumulated output and messages for a task."),
]

# ---------------------------------------------------------------------------
# Build StructuredTool list
# ---------------------------------------------------------------------------


READ_TOOLS = [
    StructuredTool.from_function(func=_read_file,    name="read_file",   args_schema=ReadFileInput,
        description="Read a text file, optionally windowed by offset/limit lines"),
    StructuredTool.from_function(func=_grep_search,  name="grep_search", args_schema=GrepInput,
        description="Regex search across files with context lines, file type filtering"),
    StructuredTool.from_function(func=_glob_search,  name="glob_search", args_schema=GlobInput,
        description="Find files by glob pattern (e.g. **/*.py), sorted by modified time"),
    StructuredTool.from_function(func=_web_fetch,    name="WebFetch",    args_schema=WebFetchInput,
        description="Fetch a URL and return clean readable text"),
    StructuredTool.from_function(func=_web_search,   name="WebSearch",   args_schema=WebSearchInput,
        description="Search the web with DuckDuckGo, optional domain filtering"),
    StructuredTool.from_function(func=lambda action="status", **kw: _git(action, **kw),
        name="git_read",
        description="Git read operations: status, diff, log, show, blame"),
] + UTILITY_TOOLS   # utility tools available in read/plan mode too

WRITE_TOOLS = [
    StructuredTool.from_function(func=_write_file,  name="write_file",  args_schema=WriteFileInput,
        description="Create or overwrite a file. Creates parent directories automatically"),
    StructuredTool.from_function(func=_edit_file,   name="edit_file",   args_schema=EditFileInput,
        description="Precise text replacement in a file. Preferred over write_file for edits"),
    StructuredTool.from_function(func=_bash,        name="bash",        args_schema=BashInputSchema,
        description="Execute shell commands with 6-layer safety validation"),
    StructuredTool.from_function(func=_git,         name="git",         args_schema=GitInput,
        description="Git operations: status, diff, log, commit, push, pull, branch, stash"),
    StructuredTool.from_function(func=_web_fetch,   name="WebFetch",    args_schema=WebFetchInput,
        description="Fetch a URL and return clean readable text"),
    StructuredTool.from_function(func=_web_search,  name="WebSearch",   args_schema=WebSearchInput,
        description="Search the web with DuckDuckGo, optional domain filtering"),
    StructuredTool.from_function(func=_github,      name="github",      args_schema=GitHubInput,
        description="GitHub API: get_issue, list_issues, create_pr, get_pr, list_prs, get_repo_info"),
] + UTILITY_TOOLS

ALL_TOOLS = WRITE_TOOLS   # superset of read + write + utility


# ---------------------------------------------------------------------------
# Multi-agent tools — port of Rust Agent / Worker suite (+ AgentStatus polling).
# (see tools/multi_agent.py). Permission modes mirror lib.rs required_permission.
# ---------------------------------------------------------------------------

class AgentToolInput(BaseModel):
    description:   str           = Field(description="Short description of the delegated task")
    prompt:        str           = Field(description="The full task prompt for the sub-agent")
    subagent_type: Optional[str] = Field(None, description="general-purpose | Explore | Plan | Verification | claw-guide | statusline-setup")
    name:          Optional[str] = Field(None, description="Optional name for the sub-agent")
    model:         Optional[str] = Field(None, description="Optional model override")

class AgentStatusInput(BaseModel):
    agent_id: str = Field(description="The agent_id returned by a prior Agent spawn")

class WorkerCreateInput(BaseModel):
    cwd:           str       = Field(description="Working directory for the worker")
    trusted_roots: list[str] = Field(default_factory=list, description="Directories that auto-clear the trust gate")
    auto_recover_prompt_misdelivery: bool = Field(True, description="Auto-arm a replay if a prompt is misdelivered")

class WorkerIdInput(BaseModel):
    worker_id: str = Field(description="Worker ID")

class WorkerObserveInput(BaseModel):
    worker_id:   str = Field(description="Worker ID")
    screen_text: str = Field(description="Latest terminal snapshot to feed the boot detector")

class WorkerSendPromptInput(BaseModel):
    worker_id:    str            = Field(description="Worker ID (must be ready_for_prompt)")
    prompt:       Optional[str]  = Field(None, description="Prompt to send; omit to replay a recovered prompt")
    task_receipt: Optional[dict] = Field(None, description="Optional task receipt {repo, task_kind, source_surface, objective_preview, expected_artifacts}")

class WorkerObserveCompletionInput(BaseModel):
    worker_id:     str = Field(description="Worker ID")
    finish_reason: str = Field(description="Session finish reason")
    tokens_output: int = Field(description="Tokens the session produced")


@_safe_tool("Agent")
def _agent_tool(description: str, prompt: str, subagent_type: Optional[str] = None,
                name: Optional[str] = None, model: Optional[str] = None) -> str:
    from tools.multi_agent import run_agent
    return json.dumps(run_agent(description, prompt, subagent_type, name, model))

@_safe_tool("AgentStatus")
def _agent_status(agent_id: str) -> str:
    from tools.multi_agent import get_agent_status
    return json.dumps(get_agent_status(agent_id))

@_safe_tool("WorkerCreate")
def _worker_create(cwd: str, trusted_roots: Optional[list] = None,
                   auto_recover_prompt_misdelivery: bool = True) -> str:
    from tools.multi_agent import worker_create
    return json.dumps(worker_create(cwd, trusted_roots, auto_recover_prompt_misdelivery))

@_safe_tool("WorkerGet")
def _worker_get(worker_id: str) -> str:
    from tools.multi_agent import worker_get
    return json.dumps(worker_get(worker_id))

@_safe_tool("WorkerObserve")
def _worker_observe(worker_id: str, screen_text: str) -> str:
    from tools.multi_agent import worker_observe
    return json.dumps(worker_observe(worker_id, screen_text))

@_safe_tool("WorkerResolveTrust")
def _worker_resolve_trust(worker_id: str) -> str:
    from tools.multi_agent import worker_resolve_trust
    return json.dumps(worker_resolve_trust(worker_id))

@_safe_tool("WorkerAwaitReady")
def _worker_await_ready(worker_id: str) -> str:
    from tools.multi_agent import worker_await_ready
    return json.dumps(worker_await_ready(worker_id))

@_safe_tool("WorkerSendPrompt")
def _worker_send_prompt(worker_id: str, prompt: Optional[str] = None,
                        task_receipt: Optional[dict] = None) -> str:
    from tools.multi_agent import worker_send_prompt
    return json.dumps(worker_send_prompt(worker_id, prompt, task_receipt))

@_safe_tool("WorkerRestart")
def _worker_restart(worker_id: str) -> str:
    from tools.multi_agent import worker_restart
    return json.dumps(worker_restart(worker_id))

@_safe_tool("WorkerTerminate")
def _worker_terminate(worker_id: str) -> str:
    from tools.multi_agent import worker_terminate
    return json.dumps(worker_terminate(worker_id))

@_safe_tool("WorkerObserveCompletion")
def _worker_observe_completion(worker_id: str, finish_reason: str, tokens_output: int) -> str:
    from tools.multi_agent import worker_observe_completion
    return json.dumps(worker_observe_completion(worker_id, finish_reason, tokens_output))


MULTI_AGENT_TOOLS = [
    StructuredTool.from_function(func=_agent_tool, name="Agent", args_schema=AgentToolInput,
        description="Launch a background sub-agent (subagent_type sets its restricted tools + prompt). Returns immediately with status 'running' and an agent_id; call multiple times for parallel independent tasks. Poll completion with AgentStatus."),
    StructuredTool.from_function(func=_agent_status, name="AgentStatus", args_schema=AgentStatusInput,
        description="Poll a spawned sub-agent by agent_id. Returns its manifest (status running|completed|failed, plus output_file). Read the output_file with read_file once status is completed."),
    StructuredTool.from_function(func=_worker_create, name="WorkerCreate", args_schema=WorkerCreateInput,
        description="Create a coding worker boot session with trust-gate and prompt-delivery guards."),
    StructuredTool.from_function(func=_worker_get, name="WorkerGet", args_schema=WorkerIdInput,
        description="Fetch the current worker boot state, last error, and event history."),
    StructuredTool.from_function(func=_worker_observe, name="WorkerObserve", args_schema=WorkerObserveInput,
        description="Feed a terminal snapshot into worker boot detection (trust gate, ready handshake, misdelivery)."),
    StructuredTool.from_function(func=_worker_resolve_trust, name="WorkerResolveTrust", args_schema=WorkerIdInput,
        description="Resolve a detected trust prompt so worker boot can continue."),
    StructuredTool.from_function(func=_worker_await_ready, name="WorkerAwaitReady", args_schema=WorkerIdInput,
        description="Return the current ready-handshake verdict for a coding worker."),
    StructuredTool.from_function(func=_worker_send_prompt, name="WorkerSendPrompt", args_schema=WorkerSendPromptInput,
        description="Send a task prompt once the worker is ready_for_prompt; can replay a recovered prompt."),
    StructuredTool.from_function(func=_worker_restart, name="WorkerRestart", args_schema=WorkerIdInput,
        description="Restart worker boot state after a failed or stale startup."),
    StructuredTool.from_function(func=_worker_terminate, name="WorkerTerminate", args_schema=WorkerIdInput,
        description="Terminate a worker and mark the lane finished from the control plane."),
    StructuredTool.from_function(func=_worker_observe_completion, name="WorkerObserveCompletion",
        args_schema=WorkerObserveCompletionInput,
        description="Report session completion, classifying finish_reason into Finished or Failed."),
]

ALL_TOOLS = WRITE_TOOLS + MULTI_AGENT_TOOLS   # write + utility + multi-agent


def get_read_tools() -> list:
    """Tools for plan/explore mode — no writes, no bash."""
    return READ_TOOLS


def get_all_tools(extra_tools: list | None = None) -> list:
    """All tools including write + bash + multi-agent. Optionally append MCP tools."""
    tools = list(ALL_TOOLS)
    if extra_tools:
        tools.extend(extra_tools)
    return tools
