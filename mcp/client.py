"""
MCP client — replaces all 3 files in original mcp/ (~709 lines)
with ~60 lines using langchain-mcp-adapters.

Original:  mcp/client.py (376) + mcp/server_manager.py (333) + mcp/tool_bridge.py
This file: MultiServerMCPClient does all of that.
"""

import asyncio
from typing import Any
from langchain_core.tools import BaseTool


async def get_mcp_tools(mcp_servers: dict) -> list[BaseTool]:
    """
    Connect to all configured MCP servers and return their tools
    as standard LangChain BaseTool objects.

    mcp_servers format (same as .agent.json):
    {
        "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"], "transport": "stdio"},
        "github":     {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "transport": "stdio"},
    }

    Returns list[BaseTool] — plug directly into any LangGraph agent or ToolNode.
    """
    if not mcp_servers:
        return []

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        print("[mcp] langchain-mcp-adapters not installed. Run: pip install langchain-mcp-adapters")
        return []

    # normalize config format
    normalized = {}
    for name, cfg in mcp_servers.items():
        if isinstance(cfg, dict) and "command" in cfg:
            normalized[name] = {
                "command":   cfg["command"],
                "args":      cfg.get("args", []),
                "transport": cfg.get("transport", "stdio"),
                "env":       cfg.get("env"),
            }

    if not normalized:
        return []

    try:
        client = MultiServerMCPClient(normalized)
        await client.__aenter__()
        tools = client.get_tools()
        print(f"[mcp] Connected to {len(normalized)} server(s), {len(tools)} tools available")
        return tools
    except Exception as e:
        print(f"[mcp] Failed to connect: {e}")
        return []


def get_mcp_tools_sync(mcp_servers: dict) -> list[BaseTool]:
    """Synchronous wrapper for environments that can't use async."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, get_mcp_tools(mcp_servers))
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(get_mcp_tools(mcp_servers))
    except Exception as e:
        print(f"[mcp] Error loading MCP tools: {e}")
        return []
