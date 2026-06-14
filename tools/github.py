"""
GitHub API tool — issues, PRs, branches, comments.
Our addition: not project.
Used by auto mode to read issues and raise PRs.

Authentication: GITHUB_TOKEN from.env (personal access token).
Needs scopes: repo, pull_requests.
"""

import os
import re
from dataclasses import dataclass, field

import requests

def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN not set. "
            "Add it to your.env file. "
            "Get one from: https://github.com/settings/tokens"
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

BASE = "https://api.github.com"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class GitHubIssue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    author: str
    url: str
    comments: list[dict] = field(default_factory=list)

@dataclass
class GitHubPR:
    number: int
    title: str
    url: str
    state: str
    branch: str

# ---------------------------------------------------------------------------
# Parse repo from URL or "owner/repo" string
# ---------------------------------------------------------------------------

def _parse_repo(repo: str) -> tuple[str, str]:
    """
    Parse owner/repo from:
    - "owner/repo"
    - "https://github.com/owner/repo"
    - "https://github.com/owner/repo/issues/123"
    """
    repo = repo.strip().rstrip("/")

    # full GitHub URL
    m = re.search(r"github\.com/([^/]+)/([^/]+)", repo)
    if m:
        return m.group(1), m.group(2).split("/")[0]

    # owner/repo format
    parts = repo.split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]

    raise ValueError(f"Cannot parse repo from: {repo}")

# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

def get_issue(repo: str, issue_number: int) -> GitHubIssue:
    """Fetch a single issue with all its comments."""
    owner, repo_name = _parse_repo(repo)

    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/issues/{issue_number}",
        headers=_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    # fetch comments
    comments = []
    if data.get("comments", 0) > 0:
        c_resp = requests.get(
            f"{BASE}/repos/{owner}/{repo_name}/issues/{issue_number}/comments",
            headers=_headers(),
            timeout=15,
        )
        if c_resp.ok:
            for c in c_resp.json():
                comments.append({
                    "author": c["user"]["login"],
                    "body": c["body"],
                })

    return GitHubIssue(
        number = data["number"],
        title = data["title"],
        body = data.get("body") or "",
        state = data["state"],
        labels = [l["name"] for l in data.get("labels", [])],
        author = data["user"]["login"],
        url = data["html_url"],
        comments = comments,
    )

# Issue commenting (comment_on_issue), branch listing (list_branches), and
# branch creation (create_branch) helpers were removed alongside the CLI —
# they were never exposed as agent tools and no caller invoked them. If you
# need to re-enable any of them later, route through `github` tool's action
# dispatcher in tools/wrappers.py.

# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------

def create_pr(
    repo: str,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str = "main",
    draft: bool = False,
) -> GitHubPR:
    """Create a pull request."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.post(
        f"{BASE}/repos/{owner}/{repo_name}/pulls",
        headers = _headers(),
        json = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base_branch,
            "draft": draft,
        },
        timeout = 15,
    )
    resp.raise_for_status()
    data = resp.json()
    return GitHubPR(
        number = data["number"],
        title = data["title"],
        url = data["html_url"],
        state = data["state"],
        branch = head_branch,
    )

def list_prs(repo: str, state: str = "open") -> list[dict]:
    """List pull requests."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/pulls",
        headers = _headers(),
        params = {"state": state, "per_page": 20},
        timeout = 15,
    )
    resp.raise_for_status()
    return [
        {"number": p["number"], "title": p["title"], "url": p["html_url"], "branch": p["head"]["ref"]}
        for p in resp.json()
    ]
