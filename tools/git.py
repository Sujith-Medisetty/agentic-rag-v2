"""
Git tool — git operations for the agent.

The five "read" operations (status / diff / log / show / blame) mirror the
corresponding Rust dispatchers in `tools/src/lib.rs` (`run_git_*`). They
return `{output: <stdout>}` dicts on success and raise on failure.

The write operations (create_branch, checkout, add, commit, push, pull,
stash, reset) are Python-side conveniences used by the auto-PR workflow.
They have no Rust counterpart at the dispatcher layer.
"""

import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: str = ".") -> tuple[str, str, int]:
    """Run a git command, return (stdout, stderr, exit_code)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


def _git_stdout(args: list[str], cwd: str = ".") -> str | None:
    """Mirrors Rust git_stdout(): None on failure, stdout (trimmed) on success."""
    out, _, code = _run(["git", *args], cwd)
    return out if code == 0 else None


# ---------------------------------------------------------------------------
# Read-only git operations — Rust run_git_* parity
# ---------------------------------------------------------------------------

def git_status(short: bool = True, cwd: str = ".") -> dict:
    """
    Mirrors Rust run_git_status(): default short=true → `git status --short --branch`.
    """
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
    Mirrors Rust run_git_diff(). When both commit and commit2 are given,
    expands to `commit...commit2`.
    """
    args = ["diff"]
    if staged:
        args.append("--cached")
    if commit:
        if commit2:
            args.append(f"{commit}...{commit2}")
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
    """
    Mirrors Rust run_git_log(): default count=20, oneline=false.
    """
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
    Mirrors Rust run_git_show(): default stat=false; uses `commit:path` syntax
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
    """
    Mirrors Rust run_git_blame(). Optional `-Lstart,end` line restriction.
    """
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
# Write git operations — Python-only conveniences (no Rust dispatcher equiv)
# ---------------------------------------------------------------------------

def _fmt(stdout: str, stderr: str, code: int, success_msg: str = "") -> str:
    if code != 0:
        return f"Error (exit {code}): {stderr or stdout}"
    return stdout or success_msg or "OK"


def git_current_branch(cwd: str = ".") -> str:
    out, _, code = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    return out if code == 0 else "unknown"


def git_list_branches(cwd: str = ".", all_branches: bool = False) -> str:
    cmd = ["git", "branch"]
    if all_branches:
        cmd.append("-a")
    out, err, code = _run(cmd, cwd)
    return _fmt(out, err, code, "No branches")


def git_stash_list(cwd: str = ".") -> str:
    out, err, code = _run(["git", "stash", "list"], cwd)
    return _fmt(out, err, code, "No stashes")


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


def git_stash_pop(cwd: str = ".") -> str:
    out, err, code = _run(["git", "stash", "pop"], cwd)
    return _fmt(out, err, code, "Stash applied")


def git_reset(mode: str = "--soft", ref: str = "HEAD~1", cwd: str = ".") -> str:
    out, err, code = _run(["git", "reset", mode, ref], cwd)
    return _fmt(out, err, code, f"Reset to {ref}")


def git_remote_url(cwd: str = ".") -> str:
    out, _, code = _run(["git", "remote", "get-url", "origin"], cwd)
    return out if code == 0 else ""
