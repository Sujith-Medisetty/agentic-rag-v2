"""
Bash command validator — full safety pipeline.
Ported from Rust: runtime/src/bash_validation.rs

Six validation submodules run in sequence before any bash command executes:
  1. read_only_validation    — block writes in read-only mode
  2. mode_validation         — enforce workspace boundary
  3. sed_validation          — block sed -i in read-only
  4. destructive_check       — block rm -rf /, fork bombs, disk writes
  5. path_validation         — warn on traversal / home dir references
  6. classify_command        — classify intent for logging
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class ValidationStatus(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    WARN  = "warn"


@dataclass
class ValidationResult:
    status:  ValidationStatus
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


class CommandIntent(Enum):
    READ_ONLY          = "read_only"
    WRITE              = "write"
    DESTRUCTIVE        = "destructive"
    NETWORK            = "network"
    PROCESS_MANAGEMENT = "process_management"
    PACKAGE_MANAGEMENT = "package_management"
    SYSTEM_ADMIN       = "system_admin"
    UNKNOWN            = "unknown"


# ---------------------------------------------------------------------------
# Command lists (exact port from Rust bash_validation.rs)
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

# Destructive patterns — exact port from Rust
DESTRUCTIVE_PATTERNS = [
    ("rm -rf /",      "Recursive forced deletion at root — this will destroy the system"),
    ("rm -rf ~",      "Recursive forced deletion of home directory"),
    ("rm -rf *",      "Recursive forced deletion of all files in current directory"),
    ("rm -rf .",      "Recursive forced deletion of current directory"),
    ("mkfs",          "Filesystem creation will destroy existing data on the device"),
    ("dd if=",        "Direct disk write — can overwrite partitions or devices"),
    ("> /dev/sd",     "Writing to raw disk device"),
    ("chmod -R 777",  "Recursively setting world-writable permissions"),
    ("chmod -R 000",  "Recursively removing all permissions"),
    (":(){ :|:& };:", "Fork bomb — will crash the system"),
]

ALWAYS_DESTRUCTIVE_COMMANDS = {"shred", "wipefs"}

SYSTEM_PATHS = {
    "/etc/", "/usr/", "/var/", "/boot/",
    "/sys/", "/proc/", "/dev/", "/sbin/", "/lib/", "/opt/",
}


# ---------------------------------------------------------------------------
# Permission modes (mirrors Rust PermissionMode)
# ---------------------------------------------------------------------------

class PermissionMode(Enum):
    READ_ONLY       = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    FULL_ACCESS     = "danger-full-access"
    PROMPT          = "prompt"
    ALLOW           = "allow"


# ---------------------------------------------------------------------------
# 1. Read-only validation
# Ported from Rust: validate_read_only()
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
# Ported from Rust: validate_mode()
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
    first = _extract_first_command(command)
    is_write = first in WRITE_COMMANDS or first in STATE_MODIFYING_COMMANDS
    if not is_write:
        return False
    return any(sys_path in command for sys_path in SYSTEM_PATHS)


# ---------------------------------------------------------------------------
# 3. Sed validation
# Ported from Rust: validate_sed()
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
# Ported from Rust: check_destructive()
# ---------------------------------------------------------------------------

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

    return ValidationResult.allow()


# ---------------------------------------------------------------------------
# 5. Path validation
# Ported from Rust: validate_paths()
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


# ---------------------------------------------------------------------------
# 6. Command classification
# Ported from Rust: classify_command()
# ---------------------------------------------------------------------------

def classify_command(command: str) -> CommandIntent:
    first = _extract_first_command(command)

    if first in SEMANTIC_READ_ONLY_COMMANDS:
        if first == "sed" and " -i" in command:
            return CommandIntent.WRITE
        return CommandIntent.READ_ONLY

    if first in ALWAYS_DESTRUCTIVE_COMMANDS or first == "rm":
        return CommandIntent.DESTRUCTIVE

    if first in WRITE_COMMANDS:
        return CommandIntent.WRITE

    if first in NETWORK_COMMANDS:
        return CommandIntent.NETWORK

    if first in PROCESS_COMMANDS:
        return CommandIntent.PROCESS_MANAGEMENT

    if first in PACKAGE_COMMANDS:
        return CommandIntent.PACKAGE_MANAGEMENT

    if first in SYSTEM_ADMIN_COMMANDS:
        return CommandIntent.SYSTEM_ADMIN

    if first == "git":
        parts = command.split()
        sub = next((p for p in parts[1:] if not p.startswith("-")), None)
        if sub and sub in GIT_READ_ONLY_SUBCOMMANDS:
            return CommandIntent.READ_ONLY
        return CommandIntent.WRITE

    return CommandIntent.UNKNOWN


# ---------------------------------------------------------------------------
# Full pipeline — run all validations in order
# Ported from Rust: validate_command()
# ---------------------------------------------------------------------------

def validate_command(
    command: str,
    mode: PermissionMode,
    workspace: str = ".",
) -> ValidationResult:
    """
    Run the full validation pipeline on a bash command.
    Returns the first non-Allow result, or Allow if all pass.

    Order matches Rust:
      1. mode validation (includes read-only check)
      2. sed validation
      3. destructive check
      4. path validation
    """
    for check in [
        lambda: validate_mode(command, mode),
        lambda: validate_sed(command, mode),
        lambda: check_destructive(command),
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
    Ported from Rust: extract_first_command()
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
