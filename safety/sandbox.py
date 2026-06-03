"""
Sandbox — isolate bash execution to protect the host system.

Isolation is namespace-based and Linux-only, exactly like Rust:
 namespace_supported = (os == linux) AND `unshare --user` works
If unsupported (non-Linux, or unshare unavailable) the sandbox is inactive and
bash runs directly (still gated by permission mode + bash validation).
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

class SandboxMode(Enum):
    UNSHARE = "unshare" # Linux user-namespace isolation
    NONE = "none" # no isolation (validation only)

@dataclass
class SandboxStatus:
    """Current sandbox state — what's available and active."""
    mode: SandboxMode = SandboxMode.NONE
    active: bool = False
    workspace: str = "."
    network_isolated: bool = False
    fallback_reason: str = ""
    in_container: bool = False

@dataclass
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int
    interrupted: bool = False

# ---------------------------------------------------------------------------
# Container detection — port of detect_container_environment()
# ---------------------------------------------------------------------------

def detect_container_environment() -> bool:
    """Return True if we're already running inside a container."""
    markers = [
        Path("/.dockerenv").exists(),
        Path("/run/.containerenv").exists(),
        bool(os.environ.get("container")),
        bool(os.environ.get("DOCKER")),
        bool(os.environ.get("KUBERNETES_SERVICE_HOST")),
    ]
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        for needle in ["docker", "containerd", "kubepods", "podman", "libpod"]:
            if needle in cgroup:
                markers.append(True)
                break
    except OSError:
        pass
    return any(markers)

# ---------------------------------------------------------------------------
# Sandbox resolver — port of resolve_sandbox_status_for_request()
# namespace_supported = (os == linux) && unshare_user_namespace_works()
# ---------------------------------------------------------------------------

def resolve_sandbox(
    workspace: str = ".",
    enabled: bool = True,
    network_isolated: bool = True,
) -> SandboxStatus:
    """Determine the sandbox mode. Linux + working `unshare` → UNSHARE, else NONE."""
    in_container = detect_container_environment()

    if not enabled:
        return SandboxStatus(
            mode=SandboxMode.NONE, active=False, workspace=workspace,
            in_container=in_container, fallback_reason="sandbox disabled by config",
        )

    if in_container:
        return SandboxStatus(
            mode=SandboxMode.NONE, active=False, workspace=workspace,
            in_container=True, fallback_reason="already running inside a container",
        )

    namespace_supported = sys.platform.startswith("linux") and _unshare_available()
    if namespace_supported:
        return SandboxStatus(
            mode=SandboxMode.UNSHARE, active=True, workspace=workspace,
            network_isolated=network_isolated, in_container=False,
        )

    return SandboxStatus(
        mode=SandboxMode.NONE, active=False, workspace=workspace,
        network_isolated=False, in_container=False,
        fallback_reason="namespace isolation unavailable (requires Linux with `unshare`)",
    )

# ---------------------------------------------------------------------------
# Execute in sandbox
# ---------------------------------------------------------------------------

def execute_sandboxed(
    command: str,
    status: SandboxStatus,
    timeout_secs: float = 120.0,
) -> SandboxResult:
    """Run a shell command inside the namespace sandbox, or directly if inactive."""
    if not status.active:
        return _run_direct(command, timeout_secs)
    if status.mode == SandboxMode.UNSHARE:
        return _run_unshare(command, status, timeout_secs)
    return _run_direct(command, timeout_secs)

# ---------------------------------------------------------------------------
# Linux unshare execution — port of build_linux_sandbox_command()
# ---------------------------------------------------------------------------

def _run_unshare(
    command: str,
    status: SandboxStatus,
    timeout_secs: float,
) -> SandboxResult:
    """Run command in a Linux user namespace."""
    workspace = Path(status.workspace).resolve()

    args = [
        "unshare",
        "--user", "--map-root-user",
        "--mount", "--ipc",
        "--pid", "--uts",
        "--fork",
    ]
    if status.network_isolated:
        args.append("--net")
    args += ["sh", "-lc", command]

    env = os.environ.copy()
    env["HOME"] = str(workspace / ".sandbox-home")
    env["TMPDIR"] = str(workspace / ".sandbox-tmp")

    return _run_subprocess(args, timeout_secs, env=env)

def _run_direct(command: str, timeout_secs: float) -> SandboxResult:
    """Run command directly on host. No isolation."""
    return _run_subprocess(["sh", "-c", command], timeout_secs)

# ---------------------------------------------------------------------------
# Shared subprocess runner
# ---------------------------------------------------------------------------

def _run_subprocess(
    cmd: list[str],
    timeout_secs: float,
    env: dict | None = None,
) -> SandboxResult:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            env=env or os.environ.copy(),
        )
        return SandboxResult(
            stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode,
        )
    except subprocess.TimeoutExpired as e:
        return SandboxResult(
            stdout=(e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            stderr=f"[timed out after {timeout_secs:.0f}s]",
            exit_code=-1, interrupted=True,
        )
    except Exception as e: # noqa: BLE001
        return SandboxResult(stdout="", stderr=f"Failed to execute: {e}", exit_code=-1)

# ---------------------------------------------------------------------------
# Availability check — port of unshare_user_namespace_works()
# ---------------------------------------------------------------------------

def _unshare_available() -> bool:
    """Check if `unshare --user` actually works on this system."""
    if not shutil.which("unshare"):
        return False
    try:
        result = subprocess.run(
            ["unshare", "--user", "--map-root-user", "true"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception: # noqa: BLE001
        return False
