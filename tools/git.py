"""
Git tool — git operations for the agent.

The five "read" operations (status / diff / log / show / blame) mirror the
corresponding rs` (`run_git_*`). They
return `{output: <stdout>}` dicts on success and raise on failure.

The write operations (create_branch, checkout, add, commit, push, pull,
stash, reset) are Python-side conveniences used by the auto-PR workflow.
They have no
"""

from __future__ import annotations

import subprocess

# Most git operations are local + fast (status, log, diff). The ones that
# CAN hang are the network-touching ones (fetch, pull, push, clone, ls-remote)
# — and even local operations can hang if there's an orphan lockfile at
# `.git/index.lock`, or if a hook script is interactive. So every git call
# gets a generous-but-bounded timeout to mirror the safety the bash tool now
# has. 60s is enough for any reasonable local op AND for most repo-sized
# fetches; commands that legitimately need longer (giant clones) should be
# called via `bash` with an explicit longer `timeout`.
_DEFAULT_GIT_TIMEOUT_SECS = 60

def _run(cmd: list[str], cwd: str = ".") -> tuple[str, str, int]:
    """Run a git command, return (stdout, stderr, exit_code).

    Hard timeout + closed stdin so a hung interactive prompt (e.g. credential
    helper waiting for a password) can't wedge the worker thread. A timeout
    surfaces as exit code 124 with a "git command timed out" stderr so the
    caller can detect and report it instead of returning silently."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_DEFAULT_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return "", f"git command timed out after {_DEFAULT_GIT_TIMEOUT_SECS}s: {' '.join(cmd)}", 124
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def _git_stdout(args: list[str], cwd: str = ".") -> str | None:
    out, _, code = _run(["git", *args], cwd)
    return out if code == 0 else None

# ---------------------------------------------------------------------------
# Read-only git operations
# ---------------------------------------------------------------------------

def git_status(short: bool = True, cwd: str = ".") -> dict:
    """"""
    args = ["status"]
    if short:
        args += ["--short", "--branch"]
    output = _git_stdout(args, cwd)
    if output is None:
        raise RuntimeError(
            "git status failed. Ensure the current directory is inside a git repository."
        )
    return {"output": output}

def git_diff(
    staged: bool = False,
    commit: str | None = None,
    commit2: str | None = None,
    path: str | None = None,
    cwd: str = ".",
) -> dict:
    """
    expands to `commit..commit2`.
    """
    args = ["diff"]
    if staged:
        args.append("--cached")
    if commit:
        if commit2:
            args.append(f"{commit}..{commit2}")
        else:
            args.append(commit)
    if path:
        args += ["--", path]
    output = _git_stdout(args, cwd)
    if output is None:
        raise RuntimeError(
            "git diff failed. Ensure the current directory is inside a git repository."
        )
    return {"output": output}

def git_log(
    count: int = 20,
    oneline: bool = False,
    author: str | None = None,
    since: str | None = None,
    until: str | None = None,
    path: str | None = None,
    cwd: str = ".",
) -> dict:
    """"""
    args = ["log", f"-n{count}"]
    if oneline:
        args.append("--oneline")
    if author:
        args.append(f"--author={author}")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if path:
        args += ["--", path]
    output = _git_stdout(args, cwd)
    if output is None:
        raise RuntimeError(
            "git log failed. Ensure the current directory is inside a git repository."
        )
    return {"output": output}

def git_show(
    commit: str = "HEAD",
    stat: bool = False,
    path: str | None = None,
    cwd: str = ".",
) -> dict:
    """
    when a path is provided.
    """
    args = ["show"]
    if stat:
        args.append("--stat")
    if path:
        args.append(f"{commit}:{path}")
    else:
        args.append(commit)
    output = _git_stdout(args, cwd)
    if output is None:
        raise RuntimeError(f"git show {commit} failed. Ensure the commit exists.")
    return {"output": output}

def git_blame(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    cwd: str = ".",
) -> dict:
    """"""
    args = ["blame"]
    if start_line is not None and end_line is not None:
        args.append(f"-L{start_line},{end_line}")
    args.append(path)
    output = _git_stdout(args, cwd)
    if output is None:
        raise RuntimeError(
            f"git blame {path} failed. Ensure the file exists and the directory is inside a git repository."
        )
    return {"output": output}

# ---------------------------------------------------------------------------
# Write git operations — Python-only conveniences
# ---------------------------------------------------------------------------

def _fmt(stdout: str, stderr: str, code: int, success_msg: str = "") -> str:
    if code != 0:
        return f"Error (exit {code}): {stderr or stdout}"
    return stdout or success_msg or "OK"

def git_current_branch(cwd: str = ".") -> str:
    out, _, code = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out if code == 0 else "unknown"

def git_create_branch(branch_name: str, cwd: str = ".") -> str:
    out, err, code = _run(["git", "checkout", "-b", branch_name], cwd)
    return _fmt(out, err, code, f"Created and switched to branch: {branch_name}")

def git_checkout(branch: str, cwd: str = ".") -> str:
    out, err, code = _run(["git", "checkout", branch], cwd)
    return _fmt(out, err, code, f"Switched to: {branch}")

def git_add(files: list[str] | None = None, cwd: str = ".") -> str:
    cmd = ["git", "add"] + (files if files else ["."])
    out, err, code = _run(cmd, cwd)
    return _fmt(out, err, code, "Staged changes")

def git_commit(message: str, cwd: str = ".") -> str:
    out, err, code = _run(["git", "commit", "-m", message], cwd)
    return _fmt(out, err, code)

def git_push(
    branch: str | None = None,
    remote: str = "origin",
    set_upstream: bool = True,
    cwd: str = ".",
) -> str:
    cmd = ["git", "push"]
    if branch and set_upstream:
        cmd += ["--set-upstream", remote, branch]
    elif branch:
        cmd += [remote, branch]
    out, err, code = _run(cmd, cwd)
    return _fmt(out, err, code, "Pushed successfully")

def git_pull(cwd: str = ".") -> str:
    out, err, code = _run(["git", "pull"], cwd)
    return _fmt(out, err, code, "Already up to date")

def git_stash(message: str | None = None, cwd: str = ".") -> str:
    cmd = ["git", "stash"]
    if message:
        cmd += ["push", "-m", message]
    out, err, code = _run(cmd, cwd)
    return _fmt(out, err, code, "Stashed changes")
