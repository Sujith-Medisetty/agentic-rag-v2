"""
Main entry point — wires everything together.

Replaces: main.py (607) + core/loop.py (388) + core/session.py (430)
          + api/http_client.py (356) + tools/registry.py (1358)
With:     ~200 lines using LangChain/LangGraph primitives.

What's kept:   config, safety, tools, compaction, UI
What's new:    ChatAnthropic replaces HTTPClient
               LangGraph runner_graph replaces AgentLoop + IntelligentRunner
               langchain-mcp-adapters replaces mcp/ entirely
               MemorySaver/CompactingCheckpointer replaces Session + JSONL
"""

import argparse
import os
import sys
from pathlib import Path

from config.env import load_env
load_env()

from config.loader import load_config
from safety.bash_validator import PermissionMode
from safety.hooks import HookConfig, HookRunner
from safety.permissions import PermissionPolicy, terminal_prompter
from safety.sandbox import resolve_sandbox
from tools.wrappers import configure_safety, get_all_tools, get_read_tools
from mcp.client import get_mcp_tools_sync
from agents.graph import runner_graph
from ui.render import MarkdownRenderer, Spinner, format_cost_line
from ui.slash_commands import build_slash_commands


# ---------------------------------------------------------------------------
# Project instruction loading
# ---------------------------------------------------------------------------
#
# NOTE: the live system prompt is assembled by agents/nodes.py::_build_system_prompt
# via agents/prompt.py::SystemPromptBuilder (which includes the orchestration
# playbook for the top-level loop). main.py only surfaces extra project
# instructions (CLAUDE.md / .agent.md / README) for that builder to append.


def _load_claude_md(workspace: str) -> str:
    for name in ["CLAUDE.md", ".agent.md", "README.md"]:
        p = Path(workspace) / name
        if p.exists():
            try:
                return f"\n\nProject context from {name}:\n{p.read_text()[:4000]}"
            except OSError:
                pass
    return ""


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def _make_thread_id(workspace: str, task: str) -> str:
    """
    Generate a stable thread ID from workspace + task.

    Same workspace + same task = same thread ID = resumes from last checkpoint.
    Different task = new thread ID = fresh run.

    Format: <workspace_hash>-<task_hash>
    e.g. "a3f2c1d0-b4e5f6a7"
    """
    import hashlib
    ws_hash   = hashlib.md5(workspace.encode()).hexdigest()[:8]
    task_hash = hashlib.md5(task.strip().lower().encode()).hexdigest()[:8]
    return f"{ws_hash}-{task_hash}"


def _make_repl_context(workspace: str, cfg, thread_id: str):
    """
    Compatibility adapter so slash_commands.py (ported from original) works
    with the new LangGraph architecture.

    slash_commands.py expects:
      ctx.loop.permissions.mode
      ctx.loop.sandbox
      ctx.session.messages / session_id / workspace_root / model
      ctx.tokens.summary()
    """
    from safety.sandbox import resolve_sandbox
    from safety.permissions import PermissionPolicy
    from safety.bash_validator import PermissionMode

    perm_mode = cfg.permission_mode
    perm      = PermissionPolicy(mode=perm_mode)
    sandbox   = resolve_sandbox(workspace=workspace, enabled=cfg.sandbox.enabled,
                                network_isolated=cfg.sandbox.network_isolated)

    class _Tokens:
        def summary(self) -> str:
            try:
                from agents.nodes import get_token_counter
                tc = get_token_counter()
                if tc:
                    return tc.summary()
            except Exception:
                pass
            return f"Model: {cfg.model}"

    class _Session:
        session_id     = thread_id
        workspace_root = workspace
        model          = cfg.model
        messages       = []   # filled from LangGraph state when needed

    class _Loop:
        permissions = perm
        session     = _Session()

        class _SandboxProxy:
            pass

        def __init__(s):
            s.sandbox = sandbox

    class ReplCtx:
        loop     = _Loop()
        session  = _Session()
        tokens   = _Tokens()

    return ReplCtx()


def run_repl(workspace: str, repo: str, cfg, mcp_tools: list) -> None:
    slash_commands = build_slash_commands()
    renderer       = MarkdownRenderer()

    ctx = _make_repl_context(workspace, cfg, "repl")
    slash_commands.set_context(ctx)

    print("\033[1mAgent ready.\033[0m  Type your message.\n")

    # One continuous session for the whole REPL — like the Rust agent, every
    # message runs through the SAME single run_turn loop and appends to one
    # session/thread (no complex/simple routing split).
    repl_thread = _make_thread_id(workspace, "repl-session")

    while True:
        try:
            user_input = input("\033[1m>\033[0m ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "/exit", "/quit"):
            print("Bye.")
            break
        if user_input.startswith("/"):
            result = slash_commands.handle(user_input)
            if result:
                print(result)
            continue

        print()
        _run_graph(
            task      = user_input,
            workspace = workspace,
            repo      = repo,
            mode      = "cli",
            thread_id = repl_thread,
            mcp_tools = mcp_tools,
            cfg       = cfg,
        )


def _run_graph(
    task: str,
    workspace: str,
    repo: str,
    mode: str,
    thread_id: str,
    mcp_tools: list,
    cfg,
) -> None:
    """Invoke the single run_turn-style loop (agent ↔ tools until no tool uses)."""
    from langchain_core.messages import HumanMessage
    from agents.nodes import reset_run_budget

    max_iter = getattr(cfg, "max_iterations", 50) or 0
    # Per-invocation budgets — graceful pause when hit (see agents.nodes._RunBudget).
    reset_run_budget(
        max_iters=max_iter,
        max_tokens=getattr(cfg, "max_run_tokens", 0) or 0,
        max_seconds=getattr(cfg, "max_run_seconds", 0) or 0,
        no_progress_limit=getattr(cfg, "no_progress_limit", 8),
    )
    config = {
        "configurable": {"thread_id": thread_id},
        # Per-invocation superstep ceiling (agent + tools per iteration). With a
        # finite iteration cap, size it to that; unbounded ⇒ a high last-resort
        # backstop (token/time/no-progress budgets are the real guards).
        "recursion_limit": (max_iter * 2 + 10) if max_iter > 0 else 100_000,
    }

    initial_state = {
        "messages":        [HumanMessage(content=task)],
        "task":            task,
        "workspace":       workspace,
        "repo":            repo,
        "project_context": _load_claude_md(workspace),  # extra instructions
        "mode":            mode,
        "iterations":      0,
        "max_iterations":  max_iter,
    }

    try:
        # JSONL session transcript (Rust-faithful logic; see memory/session.py).
        # Resume itself is still handled by the SQLite checkpointer.
        try:
            from memory.session import SessionStore
            sess_path = Path.home() / ".agent" / "sessions" / f"{thread_id}.jsonl"
            store = SessionStore(sess_path, thread_id)
            if not sess_path.exists():
                store.write_meta(model=getattr(cfg, "model", None), workspace_root=workspace)
            store.append_prompt(task)
        except Exception:
            store = None

        # Drive the loop. node_agent streams assistant text live; node_tools
        # reports tool results via the progress reporter.
        for _event in runner_graph.stream(initial_state, config=config, stream_mode="updates"):
            pass

        final = runner_graph.get_state(config)
        iters = final.values.get("iterations", 0)

        # log the final assistant text to the session transcript
        if store is not None:
            try:
                from langchain_core.messages import AIMessage
                for msg in reversed(final.values.get("messages", [])):
                    if isinstance(msg, AIMessage) and msg.content:
                        text = msg.content if isinstance(msg.content, str) else str(msg.content)
                        store.append_text_message("assistant", text)
                        break
            except Exception:
                pass

        if final.values.get("paused"):
            reason = (final.values.get("pause_reason") or {}).get("detail", "run budget reached")
            print(
                f"\n\033[33m[paused after {iters} iteration(s): {reason} — "
                f"run again (or send 'continue') to resume from the last checkpoint]\033[0m\n"
            )
        else:
            print(f"\n\033[2m[done in {iters} iteration(s) — review with: git diff HEAD]\033[0m\n")

    except KeyboardInterrupt:
        print("\n[interrupted — run again to resume from last checkpoint]")
    except Exception as e:
        print(f"\033[31mError: {e}\033[0m")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Autonomous coding agent (LangGraph)")
    parser.add_argument("--model",     "-m", default=None)
    parser.add_argument("--workspace", "-w", default=None)
    parser.add_argument("--provider",  "-p", default=None)
    parser.add_argument("--thinking",        action="store_true")
    parser.add_argument("--auto",            action="store_true", help="Auto mode (no prompts)")
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    args = parser.parse_args()

    workspace   = str(Path(args.workspace or os.getenv("AGENT_WORKSPACE", ".")).resolve())
    cfg         = load_config(workspace=workspace, cli_model=args.model)
    repo        = os.getenv("GITHUB_REPO", "")
    thinking_on = args.thinking or cfg.thinking

    # resolve provider + model (supports anthropic, openai, ollama, groq, etc.)
    try:
        from api.providers import resolve_provider_and_model
        provider, model = resolve_provider_and_model(
            args.provider or os.getenv("AGENT_PROVIDER"),
            args.model    or os.getenv("AGENT_MODEL"),
        )
        cfg.model    = model
        cfg.provider = provider
    except Exception:
        pass   # use defaults from config

    # ── safety layer ──────────────────────────────────────────────────────────
    perm_mode = cfg.permission_mode
    prompter  = terminal_prompter if perm_mode == PermissionMode.PROMPT else None
    perm      = PermissionPolicy(mode=perm_mode, prompter=prompter)
    sandbox   = resolve_sandbox(workspace=workspace, enabled=cfg.sandbox.enabled,
                                network_isolated=cfg.sandbox.network_isolated)
    hooks     = HookRunner(config=HookConfig(
        pre_tool_use      = cfg.hooks.pre_tool_use,
        post_tool_use     = cfg.hooks.post_tool_use,
        post_tool_failure = cfg.hooks.post_tool_failure,
    ))

    # inject safety into all tool wrappers
    configure_safety(
        permission_policy = perm,
        hook_runner       = hooks,
        sandbox           = sandbox,
        workspace         = workspace,
        permission_mode   = perm_mode,
    )

    # diff display: only in CLI mode (not auto — too noisy)
    from tools.wrappers import configure_display
    configure_display(show_diffs=(not args.auto))

    # progress reporter
    from ui.progress import CliReporter, set_reporter
    set_reporter(CliReporter())

    # configure model for all graph nodes
    from agents.nodes import configure_model
    configure_model(
        model=cfg.model,
        thinking=thinking_on,
        thinking_budget=getattr(cfg, "thinking_budget", 10000),
    )


    # ── MCP tools (replaces all of mcp/ from original) ────────────────────────
    mcp_tools = []
    if cfg.mcp_servers:
        print(f"\033[2mConnecting to {len(cfg.mcp_servers)} MCP server(s)...\033[0m")
        mcp_tools = get_mcp_tools_sync(cfg.mcp_servers)

    # make MCP tools available to the agent loop's tool set
    from agents.nodes import configure_tools
    configure_tools(mcp_tools)

    # ── status line ───────────────────────────────────────────────────────────
    sandbox_label = f"sandbox:{sandbox.mode.value}" if sandbox.active else "no-sandbox"
    print(
        f"\033[2mModel: {cfg.model} | Mode: {perm_mode.value} | "
        f"{sandbox_label} | MCP tools: {len(mcp_tools)}\033[0m"
    )

    # ── one-shot or REPL ──────────────────────────────────────────────────────
    if args.prompt:
        prompt_text = " ".join(args.prompt)
        mode        = "auto" if args.auto else "cli"
        thread_id   = _make_thread_id(workspace, prompt_text)
        print(f"\n\033[1m>\033[0m {prompt_text}\n")
        _run_graph(
            task=prompt_text, workspace=workspace, repo=repo,
            mode=mode, thread_id=thread_id, mcp_tools=mcp_tools, cfg=cfg,
        )
    else:
        run_repl(workspace=workspace, repo=repo, cfg=cfg, mcp_tools=mcp_tools)


if __name__ == "__main__":
    main()
