# Ojas

A personal coding agent. You type a task in plain English; the agent plans, edits
files, runs commands, and ships the change — autonomously.

```
  ❯ build me a snake and ladder board game
  ⏺ the agent reads, edits, runs tests, deploys, and reports back
```

## Quick start

```bash
git clone <repo> /opt/ojas
cd /opt/ojas
sudo bash deploy/install.sh     # one-shot: deps, venv, build, systemd, caddy
# edit .env to set Ojas admin email + password
sudo bash deploy/update.sh      # pull + rebuild + restart (run anytime)
```

The installer creates a Linux user `ojas`, builds the frontend, and registers a
systemd unit (`ojas-backend`) and a Caddy config. Open `http://<vm>:8765` (or the
domain you wired into `.env`) and sign in.

To update after a `git pull`:

```bash
sudo bash deploy/update.sh
```

## What's in the box

| Path | What it is |
|---|---|
| `agents/` | LangGraph agent loop — node_agent, system prompt, graph wiring |
| `tools/` | Tool surface the LLM can call — bash, file edits, git, web, tasks, sub-agents |
| `memory/` | Compaction-aware SQLite checkpointer + per-session LLM call trace |
| `safety/` | Bash validator, sandbox, hook runner, permission policy |
| `server/` | FastAPI app, session runner, DB layer, auth, deploy pipeline |
| `web/` | Vite + React + Tailwind UI |
| `deploy/` | `install.sh` (one-shot) + `update.sh` (pull + rebuild + restart) |

## Documentation

- **`PROJECT_GUIDE.md`** — full beginner-friendly walkthrough of every moving part
- **`AGENT_LOOP_EXPLANATION.md`** — the iterative agent loop in detail
- **`LANGCHAIN_LANGGRAPH_BEGINNER_GUIDE.md`** — the framework concepts in plain English

For wire-level debugging while the agent is running, hit `⌥ llm` in the chat
header to see the last 50 LLM call request/response pairs.

## Status

Personal project, single author. The recent cleanup pass removed ~1000 lines of
dead/duplicate code. Known follow-ups before this is "production-grade":

- **No test suite yet.** The bash validator, DB migrations, and deploy pipeline
  have no automated coverage.
- Two god-modules (`server/app.py` ~5600 lines, `web/src/pages/ChatPage.tsx`
  ~3900 lines) would benefit from splitting.
- A handful of `as any` casts in `LLMTracePanel.tsx` should be replaced with
  proper discriminated unions.
- The `EnterPlanMode` / `ExitPlanMode` tools are documented as blocking writes
  but only check an advisory flag — they need a real `ContextVar` to enforce
  the read-only mode.

## License

Personal project. All rights reserved by the author.
