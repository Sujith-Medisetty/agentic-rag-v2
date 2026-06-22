"""
Config loader — reads .agent / .claw config files and merges with env vars.

Config hierarchy (highest priority first):
  environment variables
  > project .claw/settings.local.json
  > project .claw/settings.json / .agent.json
  > user .claw/settings.json / ~/.agent/settings.json
  > legacy .claw.json
"""

from __future__ import annotations

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
    """One entry in .agent.json's `mcp_servers` map.

    Two transport styles are supported (matches LangChain's
    MultiServerMCPClient):
      stdio — spawn a local process (e.g. `npx @modelcontextprotocol/...`).
              Requires `command`; `args` and `env` are optional.
      http / sse / streamable_http — connect to a hosted MCP endpoint over HTTP.
              Requires `url`; `command` / `args` are unused.

    Empty defaults so a partial entry doesn't crash JSON parsing — load-time
    validation in server/mcp_loader.py warns and skips invalid entries instead.
    """
    command:   str       = ""
    args:      list[str] = field(default_factory=list)
    env:       dict      = field(default_factory=dict)
    transport: str       = "stdio"
    url:       str       = ""


@dataclass
class AgentConfig:
    """Fully merged runtime config."""
    provider:          str             = "minimax"
    model:             str             = "MiniMax-M3"
    # Default mode is danger-full-access; only used when no env var / config
    # file specifies an alternative.
    permission_mode:   PermissionMode  = PermissionMode.FULL_ACCESS
    thinking:          bool            = False
    thinking_budget:   int             = 10000
    # Loop bookkeeping.
    max_iterations:    int             = 50
    # Long-run budgets — removed in the 2026-06-22 cleanup. The agent loop
    # no longer enforces a hard cap; only the per-chunk streaming idle
    # timeout (AGENT_LLM_STREAM_IDLE_TIMEOUT_S) and the httpx total
    # timeout (AGENT_LLM_TIMEOUT_SECS) remain. Set `max_iterations` to
    # surface a soft cap in the system prompt.
    workspace:         str             = "."
    sandbox:           SandboxConfig   = field(default_factory=SandboxConfig)
    hooks:             HookConfig      = field(default_factory=HookConfig)
    # Optional MCP servers. Empty dict ⇒ no MCP loading happens, zero overhead.
    # Populated ⇒ tools from each server are loaded at backend boot and added
    # to the agent's toolset (names prefixed with the server name).
    mcp_servers:       dict[str, McpServerConfig] = field(default_factory=dict)


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
    """Load and merge config from all sources."""
    config = AgentConfig(workspace=str(Path(workspace).resolve()))

    # Config files, lowest → highest priority (later overrides earlier).
    # `.agent.*` names are kept for back-compat alongside `.claw.*` names.
    home = Path.home()
    ws = Path(workspace)
    config_files = [
        home / ".claw.json",                    # legacy user
        home / ".claw" / "settings.json",       # user
        home / ".agent" / "settings.json",      # user (back-compat)
        ws / ".claw.json",                      # legacy project
        ws / ".claw" / "settings.json",         # project
        ws / ".agent.json",                     # project (back-compat)
        ws / ".claw" / "settings.local.json",   # local override (highest file tier)
    ]
    for path in config_files:
        if path.exists():
            _merge_json_config(config, path)

    # Environment variables override files.
    _merge_env(config)

    # CLI flags override everything.
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
    # max_run_tokens / max_run_seconds / no_progress_limit / node_body_timeout_s
    # were removed in the 2026-06-22 cleanup. Older agent.json files may
    # still contain them; silently ignored.

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

    # MCP servers — empty/missing block is the no-op default.
    if isinstance(data.get("mcp_servers"), dict):
        for name, spec in data["mcp_servers"].items():
            if not isinstance(spec, dict):
                continue
            config.mcp_servers[name] = McpServerConfig(
                command   = str(spec.get("command", "")),
                args      = list(spec.get("args", []) or []),
                env       = dict(spec.get("env", {}) or {}),
                transport = str(spec.get("transport", "stdio")),
                url       = str(spec.get("url", "")),
            )


def _merge_env(config: AgentConfig) -> None:
    """Override config from environment variables."""
    if v := os.getenv("AGENT_PROVIDER"):
        config.provider = v
    if v := os.getenv("AGENT_MODEL"):
        config.model = v
    # Permission mode: AGENT_PERMISSION_MODE, plus the legacy
    # RUSTY_CLAUDE_PERMISSION_MODE name for back-compat.
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
    # AGENT_MAX_RUN_TOKENS / AGENT_MAX_RUN_SECONDS / AGENT_NO_PROGRESS_LIMIT /
    # AGENT_NODE_BODY_TIMEOUT_S removed in the 2026-06-22 cleanup.
    if v := os.getenv("AGENT_WORKSPACE"):
        config.workspace = v
