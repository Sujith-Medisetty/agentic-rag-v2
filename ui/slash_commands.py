"""
Full slash command system.
Ported from Rust: commands/src/lib.rs SLASH_COMMAND_SPECS
"""

from dataclasses import dataclass
from pathlib import Path
import os
import json


@dataclass
class SlashCommand:
    name:         str
    aliases:      list[str]
    summary:      str
    argument_hint: str | None
    handler:      callable


class SlashCommandRegistry:
    """
    Handles all /command parsing and dispatch.
    Ported from Rust: commands/src/lib.rs
    """

    def __init__(self):
        self._commands: dict[str, SlashCommand] = {}
        self._context  = None   # set by REPL to give commands access to loop state

    def set_context(self, ctx) -> None:
        """Give commands access to session, tokens, config etc."""
        self._context = ctx

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def handle(self, line: str) -> str | None:
        """
        Try to handle a line as a slash command.
        Returns output string if handled, None if not a slash command.
        """
        if not line.startswith("/"):
            return None

        parts = line[1:].split(maxsplit=1)
        if not parts:
            return None

        cmd_name = parts[0].lower()
        args     = parts[1] if len(parts) > 1 else ""

        cmd = self._commands.get(cmd_name)
        if not cmd:
            return f"Unknown command: /{cmd_name}\nType /help to see available commands."

        try:
            return cmd.handler(args, self._context)
        except Exception as e:
            return f"Command /{cmd_name} failed: {e}"

    def is_command(self, line: str) -> bool:
        if not line.startswith("/"):
            return False
        cmd_name = line[1:].split()[0].lower() if line[1:].split() else ""
        return cmd_name in self._commands

    def completions(self) -> list[str]:
        """Return all command names for tab completion."""
        return sorted(f"/{name}" for name in set(
            cmd.name for cmd in self._commands.values()
        ))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _handle_help(args: str, ctx) -> str:
    cmds = []
    if ctx and hasattr(ctx, "slash_commands"):
        seen = set()
        for cmd in ctx.slash_commands._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                hint  = f" {cmd.argument_hint}" if cmd.argument_hint else ""
                alias = f" (alias: {', '.join(cmd.aliases)})" if cmd.aliases else ""
                cmds.append(f"  /{cmd.name}{hint:<30} {cmd.summary}{alias}")
    return "Available commands:\n" + "\n".join(sorted(cmds))


def _handle_status(args: str, ctx) -> str:
    if not ctx:
        return "No context available."
    lines = []
    if hasattr(ctx, "session"):
        s = ctx.session
        lines.append(f"Session ID:  {s.session_id}")
        lines.append(f"Messages:    {len(s.messages)}")
        lines.append(f"Workspace:   {s.workspace_root or '.'}")
        if s.model:
            lines.append(f"Model:       {s.model}")
    if hasattr(ctx, "tokens"):
        lines.append(f"Tokens:      {ctx.tokens.summary()}")
    if hasattr(ctx, "loop") and hasattr(ctx.loop, "permissions"):
        lines.append(f"Permission:  {ctx.loop.permissions.mode.value}")
    if hasattr(ctx, "loop") and hasattr(ctx.loop, "sandbox"):
        sb = ctx.loop.sandbox
        if sb and sb.active:
            lines.append(f"Sandbox:     {sb.mode.value} (active)")
        else:
            lines.append(f"Sandbox:     inactive")
    return "\n".join(lines)


def _handle_cost(args: str, ctx) -> str:
    if ctx and hasattr(ctx, "tokens"):
        return ctx.tokens.summary()
    return "No token data available."


def _handle_usage(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "tokens"):
        return "No usage data."
    t = ctx.tokens
    c = t.cost()
    lines = [
        f"Input tokens:        {t._input:,}",
        f"Output tokens:       {t._output:,}",
        f"Cache write tokens:  {t._cache_write:,}",
        f"Cache read tokens:   {t._cache_read:,}",
        f"Total turns:         {t._turns}",
        f"Estimated cost:      {c.format()}",
    ]
    return "\n".join(lines)


def _handle_clear(args: str, ctx) -> str:
    if ctx and hasattr(ctx, "session"):
        count = len(ctx.session.messages)
        ctx.session.messages.clear()
        return f"Cleared {count} messages. Session reset."
    return "Nothing to clear."


def _handle_compact(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "loop"):
        return "No session to compact."
    from memory.checkpointer import _estimate_tokens, _compact_messages
    msgs = list(getattr(ctx.loop.session, "messages", []) or [])
    before = _estimate_tokens(msgs)
    compacted = _compact_messages(msgs)
    removed = len(msgs) - len(compacted)
    ctx.loop.session.messages = compacted
    return (
        f"Compacted session: removed {removed} messages.\n"
        f"Tokens before: ~{before:,} | After: ~{_estimate_tokens(compacted):,}"
    )


def _handle_model(args: str, ctx) -> str:
    if not args:
        model = ctx.session.model if ctx and hasattr(ctx, "session") else "unknown"
        return f"Current model: {model}"
    return f"Model switching not yet implemented. Current: {ctx.session.model if ctx else 'unknown'}"


def _handle_permissions(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "loop"):
        return "No permission info available."
    loop = ctx.loop
    if not args:
        return f"Permission mode: {loop.permissions.mode.value}"
    from safety.bash_validator import PermissionMode
    mode_map = {
        "read-only":          PermissionMode.READ_ONLY,
        "workspace-write":    PermissionMode.WORKSPACE_WRITE,
        "danger-full-access": PermissionMode.FULL_ACCESS,
        "prompt":             PermissionMode.PROMPT,
        "allow":              PermissionMode.ALLOW,
    }
    new_mode = mode_map.get(args.strip())
    if not new_mode:
        return f"Unknown mode '{args}'. Options: {', '.join(mode_map.keys())}"
    loop.permissions.mode = new_mode
    return f"Permission mode changed to: {new_mode.value}"


def _handle_sandbox(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "loop"):
        return "No sandbox info."
    sb = ctx.loop.sandbox
    if not sb:
        return "Sandbox: not configured"
    lines = [
        f"Mode:     {sb.mode.value}",
        f"Active:   {sb.active}",
        f"Network:  {'isolated' if sb.network_isolated else 'open'}",
    ]
    if sb.fallback_reason:
        lines.append(f"Note:     {sb.fallback_reason}")
    if sb.in_container:
        lines.append("Info:     already running inside a container")
    return "\n".join(lines)


def _handle_session(args: str, ctx) -> str:
    if not args or args == "list":
        sessions_dir = Path.home() / ".agent" / "sessions"
        if not sessions_dir.exists():
            return "No saved sessions."
        files = sorted(sessions_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return "No saved sessions."
        lines = ["Saved sessions (most recent first):"]
        for f in files[:20]:
            size = f.stat().st_size
            lines.append(f"  {f.stem[:8]}  {f.name}  ({size:,} bytes)")
        return "\n".join(lines)
    return f"Unknown session subcommand: {args}"


def _handle_resume(args: str, ctx) -> str:
    if not args:
        return ("Usage: /resume <session-id>  "
                "(sessions also auto-resume by task + workspace via the checkpointer)")
    sessions_dir = Path.home() / ".agent" / "sessions"
    candidates = list(sessions_dir.glob(f"{args}*.jsonl")) if sessions_dir.exists() else []
    if not candidates:
        return f"Session not found: {args}"
    from memory.session import SessionStore
    store = SessionStore(candidates[0], args)
    recs = store.load()
    return (
        f"Session {candidates[0].name}: {len(recs)} records. "
        "Re-run the same task in this workspace to continue from the checkpoint."
    )


def _handle_config(args: str, ctx) -> str:
    config_path = Path(".agent.json")
    if not config_path.exists():
        config_path = Path.home() / ".agent" / "settings.json"
    if not config_path.exists():
        return "No .agent.json found. Create one to configure the agent."
    try:
        data = json.loads(config_path.read_text())
        if args:
            section = data.get(args)
            return json.dumps(section, indent=2) if section else f"No section '{args}' in config."
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error reading config: {e}"


def _handle_diff(args: str, ctx) -> str:
    workspace = "."
    if ctx and hasattr(ctx, "session") and ctx.session.workspace_root:
        workspace = ctx.session.workspace_root
    from tools.git import git_diff
    return git_diff(cwd=workspace)["output"]


def _handle_commit(args: str, ctx) -> str:
    return "Use: /commit — runs in Phase 4 (auto mode). For now use bash tool: git add . && git commit -m '...'"


def _handle_export(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "session"):
        return "No session to export."
    path = Path(args) if args else Path(f"conversation_{ctx.session.session_id[:8]}.md")
    lines = [f"# Conversation Export\nSession: {ctx.session.session_id}\n"]
    for msg in ctx.session.messages:
        role = msg.role.value
        for block in msg.blocks:
            from api.types import TextBlock, ToolUseContentBlock, ToolResultBlock
            if isinstance(block, TextBlock):
                lines.append(f"**{role}:** {block.text}\n")
            elif isinstance(block, ToolUseContentBlock):
                lines.append(f"**tool call:** `{block.name}`\n```json\n{block.input}\n```\n")
            elif isinstance(block, ToolResultBlock):
                lines.append(f"**tool result:** `{block.tool_name}`\n```\n{block.output[:500]}\n```\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return f"Exported to: {path}"


def _handle_memory(args: str, ctx) -> str:
    workspace = "."
    if ctx and hasattr(ctx, "session") and ctx.session.workspace_root:
        workspace = ctx.session.workspace_root
    for name in ["CLAUDE.md", "claude.md", ".agent.md"]:
        p = Path(workspace) / name
        if p.exists():
            return f"Memory file: {p}\n\n{p.read_text(encoding='utf-8')[:2000]}"
    return "No memory file found (CLAUDE.md / .agent.md)."


def _handle_init(args: str, ctx) -> str:
    path = Path(".agent.md")
    if path.exists():
        return f"{path} already exists."
    workspace = "."
    if ctx and hasattr(ctx, "session") and ctx.session.workspace_root:
        workspace = ctx.session.workspace_root
    content = f"""# Agent Instructions

## Project
[Describe your project here]

## Stack
[Languages, frameworks, tools]

## Conventions
[Coding style, naming conventions]

## Important Files
[Key files Claude should know about]

## Workspace
{workspace}
"""
    path.write_text(content, encoding="utf-8")
    return f"Created {path}. Edit it to add project context for the agent."


def _handle_doctor(args: str, ctx) -> str:
    checks = []

    # check API key
    import os
    key = os.getenv("ANTHROPIC_API_KEY", "")
    checks.append(("ANTHROPIC_API_KEY", "✅ set" if key else "❌ missing"))

    # check .env file
    env_exists = Path(".env").exists()
    checks.append((".env file", "✅ present" if env_exists else "⚠️  missing"))

    # check Docker
    import shutil
    docker = shutil.which("docker")
    checks.append(("Docker", "✅ installed" if docker else "⚠️  not found (sandbox disabled)"))

    # check git
    git = shutil.which("git")
    checks.append(("git", "✅ installed" if git else "❌ not found"))

    # check requests
    try:
        import requests
        checks.append(("requests", "✅ installed"))
    except ImportError:
        checks.append(("requests", "❌ missing — run: pip install requests"))

    # check workspace
    workspace = "."
    if ctx and hasattr(ctx, "session") and ctx.session.workspace_root:
        workspace = ctx.session.workspace_root
    ws_exists = Path(workspace).exists()
    checks.append(("workspace", f"✅ {workspace}" if ws_exists else f"❌ not found: {workspace}"))

    lines = ["Agent health check:"]
    lines.extend(f"  {name:<25} {status}" for name, status in checks)
    return "\n".join(lines)


def _handle_mcp(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "loop"):
        return "No MCP info."
    bridge = getattr(ctx.loop, "mcp_bridge", None)
    if not bridge:
        return (
            "No MCP servers configured.\n"
            "Add them to .agent.json:\n"
            '{\n'
            '  "mcp_servers": {\n'
            '    "filesystem": {\n'
            '      "command": "npx",\n'
            '      "args": ["@modelcontextprotocol/server-filesystem", "/workspace"]\n'
            '    }\n'
            '  }\n'
            '}'
        )
    return bridge.manager.format_status()


def _handle_hooks(args: str, ctx) -> str:
    if not ctx or not hasattr(ctx, "loop"):
        return "No hook info."
    hooks = ctx.loop.hooks
    cfg   = hooks.config
    lines = ["Configured hooks:"]
    lines.append(f"  pre_tool_use:      {cfg.pre_tool_use or '(none)'}")
    lines.append(f"  post_tool_use:     {cfg.post_tool_use or '(none)'}")
    lines.append(f"  post_tool_failure: {cfg.post_tool_failure or '(none)'}")
    return "\n".join(lines)


def _handle_version(args: str, ctx) -> str:
    return "Agent v0.1.0 — Phase 6 build (full)"


def _handle_plan(args: str, ctx) -> str:
    """Toggle plan mode or check status."""
    if not ctx or not hasattr(ctx, "loop"):
        return "No active loop."
    loop = ctx.loop
    arg  = args.strip().lower()
    if arg == "on" or (not arg and not loop.plan_mode):
        return loop.enter_plan_mode()
    if arg == "off" or (not arg and loop.plan_mode):
        return loop.exit_plan_mode()
    status = "ON" if loop.plan_mode else "OFF"
    return (
        f"Plan mode: {status}\n"
        f"  /plan on  — Claude reads/searches only, no writes\n"
        f"  /plan off — Claude can execute changes"
    )


def _handle_run(args: str, ctx) -> str:
    """
    Run the IntelligentRunner with InteractiveConfirmation for CLI mode.
    Usage: /run <task description>
    Example: /run fix the login bug
             /run refactor auth module to use dependency injection
             /run build a REST API for user management
    """
    if not ctx or not hasattr(ctx, "loop"):
        return "No active loop."

    task = args.strip()
    if not task:
        return (
            "Usage: /run <task>\n"
            "Examples:\n"
            "  /run fix the null pointer in auth.py\n"
            "  /run build a REST API for user management\n\n"
            "Tip: you don't need /run — just type your task at the prompt. Every "
            "message runs through the single agent loop (read → tools → act). The "
            "agent sequences dependent steps itself and can spawn parallel "
            "sub-agents with the Agent tool."
        )

    return (
        "Type your task directly at the prompt — it runs through the agent loop "
        "automatically (no /run needed). For parallel/independent work the agent "
        "can call the Agent tool to spawn sub-agents."
    )


def _handle_tasks(args: str, ctx) -> str:
    from tools.tasks import get_registry
    reg = get_registry()
    if args in ("list", ""):
        return reg.format_list()
    parts = args.split(maxsplit=1)
    if parts[0] == "stop" and len(parts) > 1:
        try:
            task = reg.stop(parts[1])
        except (KeyError, ValueError) as e:
            return f"Cannot stop {parts[1]}: {e}"
        return f"Stopped: {task.task_id}"
    return reg.format_list()


def _handle_plugin(args: str, ctx) -> str:
    from tools.plugins import get_plugin_registry
    reg   = get_plugin_registry()
    parts = args.split(maxsplit=1)
    cmd   = parts[0].lower() if parts else "list"

    if cmd in ("list", ""):
        return reg.format_list()

    elif cmd == "install" and len(parts) > 1:
        try:
            return reg.install(parts[1])
        except Exception as e:
            return f"Install failed: {e}"

    elif cmd == "uninstall" and len(parts) > 1:
        return reg.uninstall(parts[1])

    elif cmd == "reload":
        reg.load_all()
        return f"Reloaded plugins. {len(reg.all_plugins())} installed."

    return (
        "Usage:\n"
        "  /plugin list\n"
        "  /plugin install <path>\n"
        "  /plugin uninstall <name>\n"
        "  /plugin reload"
    )


def _handle_stats(args: str, ctx) -> str:
    if not ctx:
        return "No stats."
    lines = []
    if hasattr(ctx, "session"):
        s = ctx.session
        lines.append(f"Messages in session:  {len(s.messages)}")
        if s.compaction:
            lines.append(f"Compactions run:      {s.compaction.count}")
    if hasattr(ctx, "tokens"):
        t = ctx.tokens
        lines.append(f"Total turns:          {t._turns}")
        lines.append(f"Total tokens in:      {t._input:,}")
        lines.append(f"Total tokens out:     {t._output:,}")
        lines.append(f"Cache reads:          {t._cache_read:,}")
    return "\n".join(lines) if lines else "No stats available."


# ---------------------------------------------------------------------------
# Build the default registry
# ---------------------------------------------------------------------------

def build_slash_commands() -> SlashCommandRegistry:
    """Build and return the default slash command registry."""
    reg = SlashCommandRegistry()

    specs = [
        ("help",        [],            "Show available slash commands",            None,                          _handle_help),
        ("status",      [],            "Show session status",                      None,                          _handle_status),
        ("cost",        [],            "Show token usage and cost",                None,                          _handle_cost),
        ("usage",       [],            "Show detailed usage stats",                None,                          _handle_usage),
        ("stats",       [],            "Show session and workspace stats",         None,                          _handle_stats),
        ("clear",       [],            "Clear session history",                    None,                          _handle_clear),
        ("compact",     [],            "Manually compact session history",         None,                          _handle_compact),
        ("model",       [],            "Show or switch active model",              "[model]",                     _handle_model),
        ("permissions", ["perm"],      "Show or change permission mode",           "[read-only|workspace-write|danger-full-access|prompt]", _handle_permissions),
        ("sandbox",     [],            "Show sandbox isolation status",            None,                          _handle_sandbox),
        ("session",     [],            "List saved sessions",                      "[list]",                      _handle_session),
        ("resume",      [],            "Resume a saved session",                   "<session-id>",                _handle_resume),
        ("config",      [],            "Show config file contents",                "[section]",                   _handle_config),
        ("diff",        [],            "Show git diff for current changes",        None,                          _handle_diff),
        ("commit",      [],            "Generate and create a git commit",         None,                          _handle_commit),
        ("export",      [],            "Export conversation to markdown file",     "[file]",                      _handle_export),
        ("memory",      [],            "Show loaded memory/instructions file",     None,                          _handle_memory),
        ("init",        [],            "Create .agent.md in current workspace",    None,                          _handle_init),
        ("doctor",      [],            "Check environment health",                 None,                          _handle_doctor),
        ("hooks",       [],            "Show configured lifecycle hooks",          None,                          _handle_hooks),
        ("mcp",         [],            "Show MCP server status and tools",         "[list|show]",                 _handle_mcp),
        ("plan",        [],            "Toggle plan mode (explore only, no writes)", "[on|off]",                    _handle_plan),
        ("run",         [],            "Run intelligent 6-phase implementation",    "<task description>",          _handle_run),
        ("tasks",       [],            "List and manage agent tasks",              "[list|stop <id>]",            _handle_tasks),
        ("plugin",      ["plugins"],   "Manage plugins",                           "[list|install|uninstall]",    _handle_plugin),
        ("version",     [],            "Show version info",                        None,                          _handle_version),
    ]

    for name, aliases, summary, hint, handler in specs:
        reg.register(SlashCommand(
            name          = name,
            aliases       = aliases,
            summary       = summary,
            argument_hint = hint,
            handler       = handler,
        ))

    return reg
