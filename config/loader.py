"""
Config loader — reads .claw/.agent config files and merges with env + CLI.
Ported from Rust: runtime/src/config.rs

Config hierarchy (highest priority first):
  CLI flags > environment variables
  > project .claw/settings.local.json > project .claw/settings.json / .agent.json
  > user .claw/settings.json / ~/.agent/settings.json > legacy .claw.json
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from safety.bash_validator import PermissionMode


@dataclass
class HookConfig:
    pre_tool_use:      str | None = None
    post_tool_use:     str | None = None
    post_tool_failure: str | None = None


@dataclass
class SandboxConfig:
    enabled:          bool = True
    network_isolated: bool = True


@dataclass
class McpServerConfig:
    command: str
    args:    list[str]  = field(default_factory=list)
    env:     dict       = field(default_factory=dict)


@dataclass
class AgentConfig:
    """
    Fully merged runtime config.
    Ported from Rust: config.rs RuntimeConfig / RuntimeFeatureConfig
    """
    provider:         str             = "anthropic"
    model:            str             = "claude-opus-4-6"
    # Default fallback matches Rust default_permission_mode → DangerFullAccess
    # (used only when no env var / config file specifies a mode).
    permission_mode:  PermissionMode  = PermissionMode.FULL_ACCESS
    thinking:         bool            = False
    thinking_budget:  int             = 10000
    max_iterations:   int             = 50
    # Long-run budgets — graceful pause (not crash) when exceeded. 0 = unbounded.
    # no_progress_limit is always on: N consecutive identical tool calls ⇒ stall.
    max_run_tokens:   int             = 0
    max_run_seconds:  int             = 0
    no_progress_limit: int            = 8
    workspace:        str             = "."
    sandbox:          SandboxConfig   = field(default_factory=SandboxConfig)
    hooks:            HookConfig      = field(default_factory=HookConfig)
    mcp_servers:      dict[str, McpServerConfig] = field(default_factory=dict)


_MODE_MAP = {
    "read-only":          PermissionMode.READ_ONLY,
    "workspace-write":    PermissionMode.WORKSPACE_WRITE,
    "danger-full-access": PermissionMode.FULL_ACCESS,
    "prompt":             PermissionMode.PROMPT,
    "allow":              PermissionMode.ALLOW,
}


def load_config(
    workspace:       str  = ".",
    cli_provider:    str | None = None,
    cli_model:       str | None = None,
    cli_permission:  str | None = None,
) -> AgentConfig:
    """
    Load and merge config from all sources.
    Ported from Rust: config.rs ConfigLoader::load()
    """
    config = AgentConfig(workspace=str(Path(workspace).resolve()))

    # Config files, lowest → highest priority (later overrides earlier),
    # mirroring Rust config.rs discovery order. `.agent.*` names are kept for
    # back-compat alongside the canonical `.claw.*` names.
    home = Path.home()
    ws = Path(workspace)
    config_files = [
        home / ".claw.json",                 # legacy user
        home / ".claw" / "settings.json",    # user
        home / ".agent" / "settings.json",   # user (back-compat)
        ws / ".claw.json",                   # legacy project
        ws / ".claw" / "settings.json",      # project
        ws / ".agent.json",                  # project (back-compat)
        ws / ".claw" / "settings.local.json",  # local override (highest file tier)
    ]
    for path in config_files:
        if path.exists():
            _merge_json_config(config, path)

    # environment variables (higher priority than files)
    _merge_env(config)

    # CLI flags (highest priority)
    if cli_provider:
        config.provider = cli_provider
    if cli_model:
        config.model = cli_model
    if cli_permission and cli_permission in _MODE_MAP:
        config.permission_mode = _MODE_MAP[cli_permission]

    return config


def _merge_json_config(config: AgentConfig, path: Path) -> None:
    """Read a JSON config file and merge into config."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    if "provider" in data:
        config.provider = data["provider"]
    if "model" in data:
        config.model = data["model"]
    if "permission_mode" in data:
        mode = _MODE_MAP.get(data["permission_mode"])
        if mode:
            config.permission_mode = mode
    if "thinking" in data:
        config.thinking = bool(data["thinking"])
    if "thinking_budget" in data:
        config.thinking_budget = int(data["thinking_budget"])
    if "max_iterations" in data:
        config.max_iterations = int(data["max_iterations"])
    if "max_run_tokens" in data:
        config.max_run_tokens = int(data["max_run_tokens"])
    if "max_run_seconds" in data:
        config.max_run_seconds = int(data["max_run_seconds"])
    if "no_progress_limit" in data:
        config.no_progress_limit = int(data["no_progress_limit"])

    # sandbox config
    if "sandbox" in data:
        s = data["sandbox"]
        if "enabled" in s:
            config.sandbox.enabled = bool(s["enabled"])
        if "network_isolated" in s:
            config.sandbox.network_isolated = bool(s["network_isolated"])

    # hooks config
    if "hooks" in data:
        h = data["hooks"]
        config.hooks.pre_tool_use      = h.get("pre_tool_use")
        config.hooks.post_tool_use     = h.get("post_tool_use")
        config.hooks.post_tool_failure = h.get("post_tool_failure")

    # MCP servers
    if "mcp_servers" in data:
        for name, spec in data["mcp_servers"].items():
            config.mcp_servers[name] = McpServerConfig(
                command = spec.get("command", ""),
                args    = spec.get("args", []),
                env     = spec.get("env", {}),
            )


def _merge_env(config: AgentConfig) -> None:
    """Override config from environment variables."""
    if v := os.getenv("AGENT_PROVIDER"):
        config.provider = v
    if v := os.getenv("AGENT_MODEL"):
        config.model = v
    # Permission mode: AGENT_PERMISSION_MODE, or Rust's RUSTY_CLAUDE_PERMISSION_MODE.
    # When nothing is set the default is danger-full-access (matches Rust
    # default_permission_mode in main.rs).
    perm = os.getenv("AGENT_PERMISSION_MODE") or os.getenv("RUSTY_CLAUDE_PERMISSION_MODE")
    if perm:
        mode = _MODE_MAP.get(perm)
        if mode:
            config.permission_mode = mode
    if v := os.getenv("AGENT_THINKING"):
        config.thinking = v.lower() == "true"
    if v := os.getenv("AGENT_THINKING_BUDGET"):
        config.thinking_budget = int(v)
    if v := os.getenv("AGENT_MAX_ITERATIONS"):
        config.max_iterations = int(v)
    if v := os.getenv("AGENT_MAX_RUN_TOKENS"):
        config.max_run_tokens = int(v)
    if v := os.getenv("AGENT_MAX_RUN_SECONDS"):
        config.max_run_seconds = int(v)
    if v := os.getenv("AGENT_NO_PROGRESS_LIMIT"):
        config.no_progress_limit = int(v)
    if v := os.getenv("AGENT_WORKSPACE"):
        config.workspace = v
