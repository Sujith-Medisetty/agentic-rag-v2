"""
GitHub API tool — issues, PRs, branches, comments.
Our addition: not in Rust project.
Used by auto mode to read issues and raise PRs.

Authentication: GITHUB_TOKEN from .env (personal access token).
Needs scopes: repo, pull_requests.
"""

import json
import os
import re
from dataclasses import dataclass, field

import requests


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN not set. "
            "Add it to your .env file. "
            "Get one from: https://github.com/settings/tokens"
        )
    return {
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type":         "application/json",
    }


BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class GitHubIssue:
    number:  int
    title:   str
    body:    str
    state:   str
    labels:  list[str]
    author:  str
    url:     str
    comments: list[dict] = field(default_factory=list)


@dataclass
class GitHubPR:
    number:   int
    title:    str
    url:      str
    state:    str
    branch:   str


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
                    "body":   c["body"],
                })

    return GitHubIssue(
        number   = data["number"],
        title    = data["title"],
        body     = data.get("body") or "",
        state    = data["state"],
        labels   = [l["name"] for l in data.get("labels", [])],
        author   = data["user"]["login"],
        url      = data["html_url"],
        comments = comments,
    )


def list_issues(
    repo: str,
    state: str = "open",
    limit: int = 20,
) -> list[dict]:
    """List issues in a repo."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/issues",
        headers = _headers(),
        params  = {"state": state, "per_page": limit},
        timeout = 15,
    )
    resp.raise_for_status()
    return [
        {"number": i["number"], "title": i["title"], "state": i["state"], "url": i["html_url"]}
        for i in resp.json()
        if "pull_request" not in i   # exclude PRs from issue list
    ]


def comment_on_issue(repo: str, issue_number: int, body: str) -> str:
    """Post a comment on an issue."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.post(
        f"{BASE}/repos/{owner}/{repo_name}/issues/{issue_number}/comments",
        headers = _headers(),
        json    = {"body": body},
        timeout = 15,
    )
    resp.raise_for_status()
    return resp.json()["html_url"]


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------

def create_branch(repo: str, branch_name: str, from_branch: str = "main") -> str:
    """Create a new branch from another branch."""
    owner, repo_name = _parse_repo(repo)

    # get SHA of source branch
    ref_resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/git/ref/heads/{from_branch}",
        headers = _headers(),
        timeout = 15,
    )
    ref_resp.raise_for_status()
    sha = ref_resp.json()["object"]["sha"]

    # create new branch
    resp = requests.post(
        f"{BASE}/repos/{owner}/{repo_name}/git/refs",
        headers = _headers(),
        json    = {"ref": f"refs/heads/{branch_name}", "sha": sha},
        timeout = 15,
    )
    resp.raise_for_status()
    return f"Created branch: {branch_name} from {from_branch}"


def list_branches(repo: str) -> list[str]:
    """List branches in a repo."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/branches",
        headers = _headers(),
        timeout = 15,
    )
    resp.raise_for_status()
    return [b["name"] for b in resp.json()]


# ---------------------------------------------------------------------------
# Pull Requests
# ---------------------------------------------------------------------------

def create_pr(
    repo:        str,
    title:       str,
    body:        str,
    head_branch: str,
    base_branch: str = "main",
    draft:       bool = False,
) -> GitHubPR:
    """Create a pull request."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.post(
        f"{BASE}/repos/{owner}/{repo_name}/pulls",
        headers = _headers(),
        json    = {
            "title": title,
            "body":  body,
            "head":  head_branch,
            "base":  base_branch,
            "draft": draft,
        },
        timeout = 15,
    )
    resp.raise_for_status()
    data = resp.json()
    return GitHubPR(
        number = data["number"],
        title  = data["title"],
        url    = data["html_url"],
        state  = data["state"],
        branch = head_branch,
    )


def get_pr(repo: str, pr_number: int) -> dict:
    """Get PR details."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/pulls/{pr_number}",
        headers = _headers(),
        timeout = 15,
    )
    resp.raise_for_status()
    d = resp.json()
    return {
        "number": d["number"],
        "title":  d["title"],
        "state":  d["state"],
        "url":    d["html_url"],
        "branch": d["head"]["ref"],
        "mergeable": d.get("mergeable"),
    }


def list_prs(repo: str, state: str = "open") -> list[dict]:
    """List pull requests."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/pulls",
        headers = _headers(),
        params  = {"state": state, "per_page": 20},
        timeout = 15,
    )
    resp.raise_for_status()
    return [
        {"number": p["number"], "title": p["title"], "url": p["html_url"], "branch": p["head"]["ref"]}
        for p in resp.json()
    ]


# ---------------------------------------------------------------------------
# Repo info
# ---------------------------------------------------------------------------

def get_repo_info(repo: str) -> dict:
    """Get basic repo information."""
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}",
        headers = _headers(),
        timeout = 15,
    )
    resp.raise_for_status()
    d = resp.json()
    return {
        "name":           d["name"],
        "full_name":      d["full_name"],
        "description":    d.get("description", ""),
        "default_branch": d["default_branch"],
        "language":       d.get("language", ""),
        "url":            d["html_url"],
        "clone_url":      d["clone_url"],
        "private":        d["private"],
    }


def get_file_content(repo: str, path: str, branch: str = "main") -> str:
    """Get contents of a file from GitHub."""
    import base64
    owner, repo_name = _parse_repo(repo)
    resp = requests.get(
        f"{BASE}/repos/{owner}/{repo_name}/contents/{path}",
        headers = _headers(),
        params  = {"ref": branch},
        timeout = 15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return data.get("content", "")
