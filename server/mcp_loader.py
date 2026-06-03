"""
Load LangChain tools from MCP (Model Context Protocol) servers configured in
.agent.json's `mcp_servers` block. Called ONCE during FastAPI lifespan startup;
results are passed to agents.nodes.configure_tools() so every concurrent
session sees the same MCP toolset.

Design notes:
  - Empty / missing `mcp_servers` config ⇒ returns [] immediately. Zero side
    effects, zero log noise. This is the default for fresh installs.
  - Per-server validation: stdio needs `command`; http/sse/streamable_http
    need `url`. Invalid entries are logged + skipped, never crash the boot.
  - Tool names are prefixed with the server name (e.g. `postgres_query`,
    `filesystem_read_file`) to avoid collisions with native tools and across
    multiple MCP servers. Different versions of langchain-mcp-adapters expose
    that flag under different names; we try the known ones and fall back to
    no-prefix if none match.
  - Connection failures of one server don't kill the rest — each is loaded in
    isolation so a flaky server doesn't take down the whole agent.
  - If `langchain-mcp-adapters` isn't installed, we warn and return [] rather
    than crashing — keeps the backend running even with a misconfigured env.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


# kwarg names different langchain-mcp-adapters versions have used for the
# "prefix tool names with server name" feature. Tried in order; first one
# the constructor accepts wins.
_PREFIX_KWARGS = ("tool_name_prefix", "prefix_tool_name", "prefix_tool_names")


def _build_server_spec(name: str, cfg) -> dict | None:
    """Translate one McpServerConfig into the dict shape MultiServerMCPClient
    expects. Returns None when the entry is unusable."""
    transport = (cfg.transport or "stdio").strip().lower()

    if transport == "stdio":
        if not cfg.command:
            log.warning(
                "MCP server '%s': stdio transport requires 'command' — skipping",
                name,
            )
            return None
        spec: dict = {
            "transport": "stdio",
            "command": cfg.command,
            "args": list(cfg.args),
        }
        if cfg.env:
            spec["env"] = dict(cfg.env)
        return spec

    if transport in ("http", "sse", "streamable_http"):
        if not cfg.url:
            log.warning(
                "MCP server '%s': %s transport requires 'url' — skipping",
                name, transport,
            )
            return None
        return {"transport": transport, "url": cfg.url}

    log.warning(
        "MCP server '%s': unknown transport '%s' — skipping "
        "(expected stdio | http | sse | streamable_http)",
        name, transport,
    )
    return None


def _make_client(spec: dict):
    """Instantiate MultiServerMCPClient with tool-name prefixing enabled if
    the installed adapter version supports it. Tries known kwarg names; falls
    back to no-prefix construction with a warning so we still get tools."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    for kw in _PREFIX_KWARGS:
        try:
            return MultiServerMCPClient(spec, **{kw: True})
        except TypeError:
            continue   # this version doesn't accept that kwarg; try the next
    log.warning(
        "langchain-mcp-adapters doesn't accept any known tool-prefix kwarg; "
        "tool names from different MCP servers may collide with each other "
        "or with native tools",
    )
    return MultiServerMCPClient(spec)


async def load_mcp_tools(mcp_servers: dict) -> list[Any]:
    """Load LangChain tools from every configured MCP server.

    Always safe to call:
      - empty `mcp_servers` ⇒ returns [] immediately
      - missing langchain-mcp-adapters dep ⇒ logs warning, returns []
      - per-server validation/connection failures ⇒ logged + skipped
    """
    if not mcp_servers:
        return []

    try:
        # Import lazily so the backend boots even when the package isn't
        # installed (the empty-config path above doesn't even need it).
        import langchain_mcp_adapters.client  # noqa: F401
    except ImportError:
        log.warning(
            "langchain-mcp-adapters not installed; %d MCP server(s) in config "
            "will be ignored. Install it with: pip install langchain-mcp-adapters",
            len(mcp_servers),
        )
        return []

    # Build the per-server spec map; invalid entries are dropped here.
    spec: dict = {}
    for name, cfg in mcp_servers.items():
        s = _build_server_spec(name, cfg)
        if s is not None:
            spec[name] = s
    if not spec:
        return []

    try:
        client = _make_client(spec)
        tools = await client.get_tools()
    except Exception as e:   # noqa: BLE001 — never crash backend boot on MCP failure
        log.warning("Failed to load MCP tools (%d server(s)): %s", len(spec), e)
        return []

    log.info(
        "Loaded %d MCP tool(s) from %d server(s): %s",
        len(tools), len(spec), ", ".join(sorted(spec)),
    )
    return list(tools)
