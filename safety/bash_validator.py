"""
Bash command validator — full safety pipeline.

Six validation submodules run in sequence before any bash command executes:
 1. read_only_validation — block writes in read-only mode
 2. mode_validation — enforce workspace boundary
 3. sed_validation — block sed -i in read-only
 4. destructive_check — block rm -rf /, fork bombs, disk writes
 5. path_validation — warn on traversal / home dir references
 6. classify_command — classify intent for logging
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class ValidationStatus(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    WARN = "warn"

@dataclass
class ValidationResult:
    status: ValidationStatus
    message: str = ""

    @classmethod
    def allow(cls) -> "ValidationResult":
        return cls(ValidationStatus.ALLOW)

    @classmethod
    def block(cls, reason: str) -> "ValidationResult":
        return cls(ValidationStatus.BLOCK, reason)

    @classmethod
    def warn(cls, message: str) -> "ValidationResult":
        return cls(ValidationStatus.WARN, message)

    @property
    def is_allowed(self) -> bool:
        return self.status == ValidationStatus.ALLOW

    @property
    def is_blocked(self) -> bool:
        return self.status == ValidationStatus.BLOCK

    @property
    def is_warning(self) -> bool:
        return self.status == ValidationStatus.WARN

# ---------------------------------------------------------------------------
# Command lists
# ---------------------------------------------------------------------------

WRITE_COMMANDS = {
    "cp", "mv", "rm", "mkdir", "rmdir", "touch", "chmod", "chown",
    "chgrp", "ln", "install", "tee", "truncate", "shred", "mkfifo",
    "mknod", "dd",
}

STATE_MODIFYING_COMMANDS = {
    "apt", "apt-get", "yum", "dnf", "pacman", "brew",
    "pip", "pip3", "npm", "yarn", "pnpm", "bun",
    "cargo", "gem", "go", "rustup",
    "docker", "systemctl", "service",
    "mount", "umount",
    "kill", "pkill", "killall",
    "reboot", "shutdown", "halt", "poweroff",
    "useradd", "userdel", "usermod",
    "groupadd", "groupdel",
    "crontab", "at",
}

WRITE_REDIRECTIONS = [">", ">>", ">&"]

GIT_READ_ONLY_SUBCOMMANDS = {
    "status", "log", "diff", "show", "branch", "tag",
    "stash", "remote", "fetch", "ls-files", "ls-tree",
    "cat-file", "rev-parse", "describe", "shortlog",
    "blame", "bisect", "reflog", "config",
}

SEMANTIC_READ_ONLY_COMMANDS = {
    "ls", "cat", "head", "tail", "less", "more", "wc", "sort", "uniq",
    "grep", "egrep", "fgrep", "find", "which", "whereis", "whatis",
    "man", "info", "file", "stat", "du", "df", "free", "uptime",
    "uname", "hostname", "whoami", "id", "groups", "env", "printenv",
    "echo", "printf", "date", "cal", "bc", "expr", "test", "true",
    "false", "pwd", "tree", "diff", "cmp", "md5sum", "sha256sum",
    "sha1sum", "xxd", "od", "hexdump", "strings", "readlink",
    "realpath", "basename", "dirname", "seq", "yes", "tput",
    "column", "jq", "yq", "xargs", "tr", "cut", "paste", "awk", "sed",
}

NETWORK_COMMANDS = {
    "curl", "wget", "ssh", "scp", "rsync", "ftp", "sftp",
    "nc", "ncat", "telnet", "ping", "traceroute", "dig",
    "nslookup", "host", "whois", "ifconfig", "ip",
    "netstat", "ss", "nmap",
}

PROCESS_COMMANDS = {
    "kill", "pkill", "killall", "ps", "top", "htop",
    "bg", "fg", "jobs", "nohup", "disown", "wait", "nice", "renice",
}

PACKAGE_COMMANDS = {
    "apt", "apt-get", "yum", "dnf", "pacman", "brew",
    "pip", "pip3", "npm", "yarn", "pnpm", "bun",
    "cargo", "gem", "go", "rustup", "snap", "flatpak",
}

SYSTEM_ADMIN_COMMANDS = {
    "sudo", "su", "chroot", "mount", "umount", "fdisk", "parted",
    "lsblk", "blkid", "systemctl", "service", "journalctl", "dmesg",
    "modprobe", "insmod", "rmmod", "iptables", "ufw", "firewall-cmd",
    "sysctl", "crontab", "at", "useradd", "userdel", "usermod",
    "groupadd", "groupdel", "passwd", "visudo",
}

# Destructive patterns —
DESTRUCTIVE_PATTERNS = [
    ("rm -rf /", "Recursive forced deletion at root — this will destroy the system"),
    ("rm -rf ~", "Recursive forced deletion of home directory"),
    ("rm -rf *", "Recursive forced deletion of all files in current directory"),
    ("rm -rf.", "Recursive forced deletion of current directory"),
    ("mkfs", "Filesystem creation will destroy existing data on the device"),
    ("dd if=", "Direct disk write — can overwrite partitions or devices"),
    ("> /dev/sd", "Writing to raw disk device"),
    ("chmod -R 777", "Recursively setting world-writable permissions"),
    ("chmod -R 000", "Recursively removing all permissions"),
    (":(){ :|:& };:", "Fork bomb — will crash the system"),
]

ALWAYS_DESTRUCTIVE_COMMANDS = {"shred", "wipefs"}

# Process-management commands that are ALWAYS forbidden, regardless of
# permission mode. These have no legitimate use inside a build session and
# can take down the Ojas backend (which listens on :8765) if the agent
# `kill`s the wrong pid. The validator pipeline returns `block` for these
# in every mode — see check_process_management().
ALWAYS_FORBIDDEN_PROCESS_COMMANDS = {
    "kill", "pkill", "killall", "fuser", "pgrep",
}

# System paths the agent must NEVER target with a write command. Note we
# intentionally exclude `/opt/` because Ojas itself lives at `/opt/ojas/`
# and the deploy pipeline writes there. Subpaths under /opt (like
# /opt/ojas-apps/) are still inside the deploy scope, so blanket-blocking
# /opt/ would false-positive on every `cd /opt/ojas/...`.
SYSTEM_PATHS = {
    "/etc/", "/usr/", "/boot/",
    "/sys/", "/proc/", "/dev/", "/sbin/", "/lib/",
}

# ---------------------------------------------------------------------------
# Permission modes
# ---------------------------------------------------------------------------

class PermissionMode(Enum):
    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    FULL_ACCESS = "danger-full-access"
    PROMPT = "prompt"
    ALLOW = "allow"

# ---------------------------------------------------------------------------
# 1. Read-only validation
# ---------------------------------------------------------------------------

def validate_read_only(command: str, mode: PermissionMode) -> ValidationResult:
    if mode != PermissionMode.READ_ONLY:
        return ValidationResult.allow()

    first = _extract_first_command(command)

    if first in WRITE_COMMANDS:
        return ValidationResult.block(
            f"Command '{first}' modifies the filesystem and is not allowed in read-only mode"
        )

    if first in STATE_MODIFYING_COMMANDS:
        return ValidationResult.block(
            f"Command '{first}' modifies system state and is not allowed in read-only mode"
        )

    if first == "sudo":
        inner = _extract_sudo_inner(command)
        if inner:
            return validate_read_only(inner, mode)

    for redir in WRITE_REDIRECTIONS:
        if redir in command:
            return ValidationResult.block(
                f"Command contains write redirection '{redir}' which is not allowed in read-only mode"
            )

    if first == "git":
        return _validate_git_read_only(command)

    return ValidationResult.allow()

def _validate_git_read_only(command: str) -> ValidationResult:
    parts = command.split()
    subcommand = next((p for p in parts[1:] if not p.startswith("-")), None)
    if subcommand is None:
        return ValidationResult.allow()
    if subcommand in GIT_READ_ONLY_SUBCOMMANDS:
        return ValidationResult.allow()
    return ValidationResult.block(
        f"Git subcommand '{subcommand}' modifies repository state and is not allowed in read-only mode"
    )

# ---------------------------------------------------------------------------
# 2. Mode validation
# ---------------------------------------------------------------------------

def validate_mode(command: str, mode: PermissionMode) -> ValidationResult:
    if mode == PermissionMode.READ_ONLY:
        return validate_read_only(command, mode)

    if mode == PermissionMode.WORKSPACE_WRITE:
        if _command_targets_outside_workspace(command):
            return ValidationResult.warn(
                "Command appears to target files outside the workspace — requires elevated permission"
            )

    return ValidationResult.allow()

def _command_targets_outside_workspace(command: str) -> bool:
    """True if the command appears to write to a system path that's outside
    the workspace. We anchor the match to a path boundary (slash, space, or
    end of string) so a substring like `var` inside `/tmp/myvar/` doesn't
    trip the `/var/` check."""
    first = _extract_first_command(command)
    is_write = first in WRITE_COMMANDS or first in STATE_MODIFYING_COMMANDS
    if not is_write:
        return False
    return any(
        (sys_path in command)
        and _path_at_boundary(command, sys_path)
        for sys_path in SYSTEM_PATHS
    )


def _path_at_boundary(command: str, sys_path: str) -> bool:
    """True if `sys_path` appears in `command` preceded by a non-alphanumeric
    character (or at the start), so `/var/` inside `/tmp/myvar/x` doesn't
    count as a system-path hit."""
    idx = 0
    while True:
        i = command.find(sys_path, idx)
        if i < 0:
            return False
        if i == 0 or not command[i - 1].isalnum():
            return True
        idx = i + 1

# ---------------------------------------------------------------------------
# 3. Sed validation
# ---------------------------------------------------------------------------

def validate_sed(command: str, mode: PermissionMode) -> ValidationResult:
    first = _extract_first_command(command)
    if first != "sed":
        return ValidationResult.allow()

    if " -i" in command or command.startswith("sed -i"):
        if mode == PermissionMode.READ_ONLY:
            return ValidationResult.block(
                "sed -i (in-place editing) is not allowed in read-only mode"
            )

    return ValidationResult.allow()

# ---------------------------------------------------------------------------
# 4. Destructive check
# ---------------------------------------------------------------------------

def validate_destructive_with_protected_pids(command: str) -> ValidationResult:
    """Wraps check_destructive and re-runs the protected-pid BLOCK inside
    it, so even if the pipeline ordering ever changes, a kill of a
    protected pid is a hard BLOCK (not a warn from a side-check).
    """
    r = check_destructive(command)
    if not r.is_allowed:
        return r
    return validate_protected_pids(command)


def check_destructive(command: str) -> ValidationResult:
    for pattern, warning in DESTRUCTIVE_PATTERNS:
        if pattern in command:
            return ValidationResult.warn(f"Destructive command detected: {warning}")

    first = _extract_first_command(command)
    if first in ALWAYS_DESTRUCTIVE_COMMANDS:
        return ValidationResult.warn(
            f"Command '{first}' is inherently destructive and may cause data loss"
        )

    if "rm " in command and "-r" in command and "-f" in command:
        return ValidationResult.warn(
            "Recursive forced deletion detected — verify the target path is correct"
        )

    # Process-management commands are blocked in every mode, not just
    # read-only. A misfired `kill <pid>` from inside a build session can
    # take down the Ojas backend (port 8765) and crash the parent agent's
    # own session. Always block, never warn.
    first_proc = _extract_first_command(command)
    if first_proc in ALWAYS_FORBIDDEN_PROCESS_COMMANDS:
        return ValidationResult.block(
            f"Command '{first_proc}' is forbidden — killing processes from "
            f"inside a build session can take down the Ojas backend (port "
            f"8765) or another session. To stop a dev/preview server YOU "
            f"started, use the StopProcess tool: StopProcess(port=<your port>) "
            f"or StopProcess(pid=<your pid>) — it stops only processes this "
            f"session spawned. If you just need a free port, pick a different one."
        )

    # Indirect kill: a kill-family verb (kill / pkill / killall / fuser -k)
    # fed by stdin, a pipe, or process substitution. Static analysis can't
    # see the actual pid, so we can't whitelist "agent's own child" — we
    # just refuse the shape. Catches `xargs kill < pids.txt`, `echo 476086
    # | kill`, `kill <(echo 476086)`, and friends.
    if re.search(
        r"\b(kill|pkill|killall)\b", command, re.IGNORECASE
    ) and re.search(r"(\|\s*(xargs\s+)?(kill|pkill|killall)\b|<\s*\(\s*.*\b(kill|pkill|killall)\b|\b(kill|pkill|killall)\b\s*<)",
        command, re.IGNORECASE,
    ):
        return ValidationResult.block(
            "Refused: kill-family verb fed by stdin/pipe/substitution. "
            "Static analysis cannot verify the target is not the Ojas "
            "backend. If you need to stop a child process, terminate the "
            "session (the cleanup will SIGTERM it) or use the exact pid "
            "from your earlier bash output with `kill <pid>`."
        )

    return ValidationResult.allow()

# ---------------------------------------------------------------------------
# 5. Path validation
# ---------------------------------------------------------------------------

def validate_paths(command: str, workspace: str) -> ValidationResult:
    if "../" in command:
        # resolve workspace to absolute path for reliable comparison
        import os
        abs_workspace = os.path.abspath(workspace)
        # warn if workspace path is NOT present as an absolute anchor
        # use resolved path, not "." which matches any dot in the string
        if abs_workspace not in command:
            return ValidationResult.warn(
                "Command contains directory traversal pattern '../' — "
                "verify the target path resolves within the workspace"
            )

    if "~/" in command or "$HOME" in command:
        return ValidationResult.warn(
            "Command references home directory — verify it stays within the workspace scope"
        )

    return ValidationResult.allow()

def validate_protected_pids(command: str) -> ValidationResult:
    """Hard BLOCK on any kill-family verb whose args include a protected
    pid (the live Ojas backend uvicorn, caddy reverse proxy, or any pid
    pinned in OJAS_PROTECTED_PIDS). Runs FIRST in the pipeline so it
    cannot be skipped by a warn from validate_mode (e.g. a kill with
    `>/dev/null` redirect in workspace-write mode used to return the
    workspace-warn and never reach the kill check).
    """
    if not re.search(
        r"\b(kill|pkill|killall)\b", command, re.IGNORECASE
    ):
        return ValidationResult.allow()

    protected = _discover_protected_pids()
    if not protected:
        return ValidationResult.allow()

    for pid in protected:
        if re.search(rf"(?<!\w){pid}(?!\w)", command):
            return ValidationResult.block(
                f"Refused: command targets protected pid {pid} "
                f"(Ojas backend or caddy reverse proxy) with a kill verb. "
                f"Ojas processes are untouchable from inside a build "
                f"session. If a different process needs to stop, end the "
                f"session (the cleanup will SIGTERM it) or pick a "
                f"different free port for your dev server."
            )

    return ValidationResult.allow()


def _discover_protected_pids() -> set[int]:
    """Discover the live pids of the Ojas uvicorn backend and caddy
    reverse proxy. Used by validate_protected_pids. Discovery is cached
    on first call and only re-runs if the cache is cleared.
    """
    # Import here to keep the module import order happy; tools.bash also
    # does this discovery, but bash_validator is the FIRST line of
    # defence and shouldn't depend on tools.bash.
    try:
        from tools.bash import _protected_pids
        return _protected_pids()
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Full pipeline — run all validations in order
# ---------------------------------------------------------------------------

def validate_command(
    command: str,
    mode: PermissionMode,
    workspace: str = ".",
) -> ValidationResult:
    """
    Run the full validation pipeline on a bash command.
    Returns the first non-Allow result, or Allow if all pass.

    Order
    0. protected-pid BLOCK — runs first, can't be skipped by a mode-warn
    1. mode validation (includes read-only check)
    2. sed validation
    3. destructive check
    4. path validation
    """
    for check in [
        lambda: validate_protected_pids(command),
        lambda: validate_mode(command, mode),
        lambda: validate_sed(command, mode),
        lambda: validate_destructive_with_protected_pids(command),
        lambda: validate_paths(command, workspace),
    ]:
        result = check()
        if not result.is_allowed:
            return result

    return ValidationResult.allow()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_first_command(command: str) -> str:
    """
    Extract the first bare command, skipping env var assignments.
    """
    remaining = command.strip()

    # skip env var assignments like KEY=val cmd
    while remaining:
        remaining = remaining.lstrip()
        if "=" in remaining:
            before_eq = remaining[:remaining.index("=")]
            if before_eq and all(c.isalnum() or c == "_" for c in before_eq):
                after_eq = remaining[remaining.index("=") + 1:]
                space = _find_end_of_value(after_eq)
                if space is not None:
                    remaining = after_eq[space:]
                    continue
                return ""
        break

    parts = remaining.split()
    return parts[0] if parts else ""

def _extract_sudo_inner(command: str) -> str:
    """Extract the command after sudo, skipping sudo flags."""
    parts = command.split()
    i = 1
    while i < len(parts):
        if parts[i].startswith("-"):
            i += 1
        elif "=" in parts[i]:  # sudo -u user=val
            i += 1
        else:
            return " ".join(parts[i:])
    return ""

def _find_end_of_value(s: str) -> int | None:
    """Find end of a shell variable value (handles quotes)."""
    if not s:
        return None
    if s[0] in ('"', "'"):
        quote = s[0]
        end = s.find(quote, 1)
        if end == -1:
            return None
        return end + 1
    space = next((i for i, c in enumerate(s) if c.isspace()), None)
    return space
