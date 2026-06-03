"""
Auto-commit + push for the web backend.

Per the Phase 4 plan:
  - At end of every assistant turn that touched files, commit the workspace.
  - Default branch strategy: `session/{short_id}` so sessions stay isolated
    and can be discarded without polluting `main`.
  - Auto-push is OFF by default. The user can flip it on per-project.
  - Hooks are respected (never `--no-verify`).
  - Force-push and main/master force-push are NEVER attempted.
  - If the workspace isn't a git repo, we skip silently — non-technical
    users shouldn't need to think about VCS to use the chat.

This module is pure-subprocess (no GitPython dep) so we add zero new
requirements. All returns are dataclasses so callers can route results
through the reporter cleanly.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ============================================================================
# Result dataclasses — what session_runner forwards to the WebReporter
# ============================================================================

@dataclass
class CommitResult:
    committed: bool
    sha: str = ""
    branch: str = ""
    message: str = ""
    files: list[str] = field(default_factory=list)
    skip_reason: str = ""   # populated when committed=False
    hook_output: str = ""   # captured stderr on hook failure, etc.


@dataclass
class PushResult:
    pushed: bool
    branch: str = ""
    remote: str = ""
    error: str = ""


@dataclass
class GitInfo:
    is_git_repo: bool
    branch: str = ""
    last_commit_sha: str = ""
    last_commit_subject: str = ""
    has_remote: bool = False
    ahead: int = 0      # commits ahead of upstream (0 if no upstream)
    behind: int = 0
    dirty: bool = False


# ============================================================================
# Low-level helpers — all return (returncode, stdout, stderr)
# ============================================================================

def _run(
    cwd: str,
    *args: str,
    check: bool = False,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """Run a git command. We DON'T disable signing or hooks — if the user
    configured them, they apply."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", "git not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} timed out"
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def is_git_repo(workspace: str) -> bool:
    rc, _, _ = _run(workspace, "rev-parse", "--git-dir")
    return rc == 0


def _short(session_id: str) -> str:
    # 12 hex chars is enough to avoid collisions in any realistic single-user
    # session count while still being scannable in `git branch -a` output.
    safe = re.sub(r"[^a-zA-Z0-9]", "", session_id)
    return safe[:12] or "anon"


def _session_branch_name(session_id: str) -> str:
    return f"session/{_short(session_id)}"


# ============================================================================
# Branch management
# ============================================================================

def current_branch(workspace: str) -> str:
    rc, out, _ = _run(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return ""
    return out.strip()


def _branch_exists(workspace: str, branch: str) -> bool:
    rc, _, _ = _run(
        workspace, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}",
    )
    return rc == 0


def _is_clean_to_switch(workspace: str) -> bool:
    """`git checkout` between branches with uncommitted changes is safe IFF
    no checkout-incompatible changes exist. Easier and safer to require a
    fully clean tree before we switch branches automatically. Callers should
    fall back to staying on the current branch when this is False."""
    rc, out, _ = _run(workspace, "status", "--porcelain")
    if rc != 0:
        return False
    return out.strip() == ""


def prepare_session_branch(
    workspace: str,
    session_id: str,
    strategy: str = "session",
) -> tuple[str, str]:
    """Pick (and possibly create + check out) the branch this session should
    commit on.

    Returns (branch_name, note). `note` is a short human reason when we had
    to deviate from the strategy (e.g. workspace was dirty so we stayed on
    the current branch). Empty string on a clean choice.

    strategy:
      "session"  — use session/{short_id}; create + check out if needed
      "current"  — commit on whatever branch HEAD currently points at
    """
    if strategy not in ("session", "current"):
        strategy = "session"

    on = current_branch(workspace)

    if strategy == "current" or not on:
        return on, ""

    target = _session_branch_name(session_id)
    if on == target:
        return target, ""

    # Switching branches with a dirty tree can fail or carry changes across.
    # Both outcomes are surprising for a non-technical user — bail to the
    # current branch in that case rather than risking a mess.
    if not _is_clean_to_switch(workspace):
        return on, (
            f"workspace had uncommitted changes — staying on '{on}' instead "
            f"of switching to '{target}'"
        )

    if _branch_exists(workspace, target):
        rc, _, err = _run(workspace, "checkout", target)
    else:
        rc, _, err = _run(workspace, "checkout", "-b", target)

    if rc != 0:
        return on, f"failed to switch to '{target}': {err.strip() or 'unknown'}"
    return target, ""


# ============================================================================
# Commit
# ============================================================================

# Branches we will NEVER auto-commit to. The user can still commit to them
# manually outside the app.
_PROTECTED_BRANCHES = {"main", "master", "trunk", "production", "prod"}


def _build_commit_message(
    user_prompt: str,
    files: list[str],
    session_id: str,
) -> str:
    """Subject line: first sentence of the user's prompt (≤60 chars).
    Body: list of files + the agentic-rag attribution so users can spot
    auto-commits in `git log`."""
    first = (user_prompt.strip().splitlines() or [""])[0]
    # Strip leading filler that makes the subject vague.
    first = re.sub(r"^(please\s+|can you\s+|could you\s+)", "", first, flags=re.I)
    subject = first[:60].rstrip()
    if len(first) > 60:
        subject += "…"
    if not subject:
        subject = f"Update {len(files)} file(s)"

    body_lines = [
        "",
        f"Auto-committed via agentic-rag session {_short(session_id)}",
        "",
        "Files:",
    ]
    for f in sorted(set(files))[:20]:
        body_lines.append(f"  - {f}")
    if len(set(files)) > 20:
        body_lines.append(f"  - … and {len(set(files)) - 20} more")
    return subject + "\n" + "\n".join(body_lines)


def autocommit(
    workspace: str,
    session_id: str,
    user_prompt: str,
    branch_strategy: str = "session",
) -> CommitResult:
    """Stage all dirty paths (respects .gitignore) and create one commit.
    `user_prompt` is used to draft the commit subject. `branch_strategy`
    is consulted on first commit of the session."""
    if not is_git_repo(workspace):
        return CommitResult(committed=False, skip_reason="not a git repo")

    # Detached HEAD / mid-rebase / mid-merge — too dangerous to touch.
    rc, head, _ = _run(workspace, "symbolic-ref", "--quiet", "HEAD")
    if rc != 0:
        return CommitResult(
            committed=False, skip_reason="detached HEAD or mid-rebase",
        )
    git_dir = Path(workspace) / ".git"
    if any(
        (git_dir / sentinel).exists()
        for sentinel in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG")
    ):
        return CommitResult(
            committed=False,
            skip_reason="mid-merge/rebase/bisect — aborted",
        )

    branch, note = prepare_session_branch(workspace, session_id, branch_strategy)
    if branch in _PROTECTED_BRANCHES and branch_strategy == "session":
        # Branch switch failed AND we're sitting on a protected branch.
        return CommitResult(
            committed=False,
            branch=branch,
            skip_reason=(
                f"refused to auto-commit on protected branch '{branch}'"
                + (f" ({note})" if note else "")
            ),
        )

    # Stage everything dirty. `git add -A` respects .gitignore, so build
    # artifacts properly ignored stay out of the commit. We don't try to
    # stage only the paths the agent reported — bash can change files too
    # (e.g. `npm install` updating package-lock.json) and the user expects
    # those to land in the commit.
    rc, _, err = _run(workspace, "add", "-A")
    if rc != 0:
        return CommitResult(
            committed=False, branch=branch,
            skip_reason=f"git add failed: {err.strip()}",
        )

    # Anything to commit?
    rc, status_out, _ = _run(workspace, "status", "--porcelain")
    staged_files = _parse_porcelain_paths(status_out)
    if not staged_files:
        return CommitResult(
            committed=False, branch=branch,
            skip_reason="no changes to commit",
        )

    message = _build_commit_message(user_prompt, staged_files, session_id)

    # Write the commit. Hooks (pre-commit, commit-msg, etc.) run — we never
    # pass --no-verify. If a hook fails, we report it as a skip rather than
    # silently retrying, so the user sees the actual problem in the UI.
    rc, out, err = _run(workspace, "commit", "-m", message)
    if rc != 0:
        return CommitResult(
            committed=False, branch=branch, files=staged_files,
            skip_reason="commit failed (hook?)",
            hook_output=(err.strip() or out.strip())[:1000],
        )

    rc, sha_out, _ = _run(workspace, "rev-parse", "--short=12", "HEAD")
    sha = sha_out.strip()

    return CommitResult(
        committed=True, sha=sha, branch=branch,
        message=message.splitlines()[0],
        files=staged_files,
    )


def _parse_porcelain_paths(porcelain: str) -> list[str]:
    """`git status --porcelain` lines look like ` M src/foo.py` (status XY +
    space + path). We need just the path portion. Renames look like
    `R  old -> new` — we keep the destination."""
    out: list[str] = []
    for raw in porcelain.splitlines():
        if len(raw) < 4:
            continue
        rest = raw[3:].strip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        # Strip surrounding quotes git adds when paths contain spaces.
        if rest.startswith('"') and rest.endswith('"'):
            rest = rest[1:-1]
        out.append(rest)
    return out


# ============================================================================
# Push
# ============================================================================

def has_remote(workspace: str, name: str = "origin") -> bool:
    rc, out, _ = _run(workspace, "remote")
    if rc != 0:
        return False
    return name in {ln.strip() for ln in out.splitlines() if ln.strip()}


def push_to_remote(
    workspace: str,
    branch: str | None = None,
    remote: str = "origin",
) -> PushResult:
    """Plain (non-force) push. Force flags are NEVER passed."""
    if not is_git_repo(workspace):
        return PushResult(pushed=False, error="not a git repo")
    if not has_remote(workspace, remote):
        return PushResult(pushed=False, error=f"no remote named '{remote}'")
    branch = branch or current_branch(workspace)
    if not branch:
        return PushResult(pushed=False, error="no current branch")

    # `--set-upstream` makes the first push easy without forcing the user
    # to configure tracking. Subsequent pushes are no-ops on the flag.
    rc, _, err = _run(
        workspace, "push", "--set-upstream", remote, branch, timeout=60.0,
    )
    if rc != 0:
        return PushResult(
            pushed=False, branch=branch, remote=remote,
            error=(err.strip() or "push failed")[:500],
        )
    return PushResult(pushed=True, branch=branch, remote=remote)


# ============================================================================
# Read-only inspection — used by the /api/sessions/{id}/git endpoint
# ============================================================================

def get_git_info(workspace: str) -> GitInfo:
    if not is_git_repo(workspace):
        return GitInfo(is_git_repo=False)

    branch = current_branch(workspace)

    sha, subject = "", ""
    rc, out, _ = _run(workspace, "log", "-1", "--pretty=%h%x09%s")
    if rc == 0 and out.strip():
        parts = out.strip().split("\t", 1)
        sha = parts[0]
        subject = parts[1] if len(parts) > 1 else ""

    remote = has_remote(workspace, "origin")

    ahead = behind = 0
    if remote and branch:
        rc, ab_out, _ = _run(
            workspace, "rev-list", "--left-right", "--count",
            f"origin/{branch}...HEAD",
        )
        if rc == 0 and ab_out.strip():
            try:
                b_str, a_str = ab_out.split()
                behind, ahead = int(b_str), int(a_str)
            except (ValueError, IndexError):
                pass

    rc, dirty_out, _ = _run(workspace, "status", "--porcelain")
    dirty = rc == 0 and bool(dirty_out.strip())

    return GitInfo(
        is_git_repo=True,
        branch=branch,
        last_commit_sha=sha,
        last_commit_subject=subject,
        has_remote=remote,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
    )
