# Project Guide — `agentic-rag-v2`

A complete, beginner-friendly walkthrough of this project: what it is, how it's
built, every moving part, and the LangChain/LangGraph concepts behind it. Read this
top to bottom and you'll understand the whole codebase.

---

## 0. TL;DR — what is this?

`agentic-rag-v2` is a **Python re-implementation of a Rust project called
`claw-code`**. `claw-code` is an "agentic coding assistant" — like a command-line
Claude Code: you give it a task in plain English, and an LLM (Claude) reads your
files, runs commands, edits code, and reports back, using **tools**.

The goal of this project: **keep the Python + LangChain/LangGraph stack, but match
the *logic* of the Rust project exactly.** Rust `claw-code` is the "source of truth."

It has **two independent parts**:

1. **The agent** (`main.py` + `agents/` + `tools/` + `safety/` + `memory/`) — the
   interactive coding assistant.
2. **The RAG service** (`rag/`) — a standalone semantic-search server over your code
   (embeddings + cosine similarity). Separate process, like Rust's `claw-rag-service`.

---

## 1. The 30-second mental model

```
You type a task ──▶ main.py ──▶ the AGENT LOOP ──▶ Claude decides ──▶ calls TOOLS
                                      ▲                                    │
                                      └──────── tool results ◀────────────┘
                          (repeat until Claude stops asking for tools)
```

- **Claude** = the brain (the LLM). It can't touch your computer directly.
- **Tools** = the hands (`read_file`, `bash`, `edit_file`, `grep_search`, …). Claude
  *requests* a tool; our code *runs* it and feeds the result back.
- **The loop** = keep going: brain → hands → brain → hands … until the brain says
  "done" (i.e. it replies with text and no tool request).
- **Safety** = before any tool runs, a permission check + validation gate decides if
  it's allowed.
- **Memory** = the whole conversation is saved so it can resume; old messages get
  summarized when they grow too big ("compaction").

That loop is the heart of every coding agent (including Claude Code itself). The rest
of the project is plumbing around it.

---

## 2. LangChain & LangGraph — the concepts you need (beginner primer)

You said you're new to these. Here's the minimum, mapped to this project.

### 2.1 LangChain basics

**LangChain** is a library that standardizes "talk to an LLM and use tools."

- **Messages** — a conversation is a list of message objects:
  - `HumanMessage` — what the user said.
  - `AIMessage` — what the model replied (text, and/or *tool calls*).
  - `ToolMessage` — the result of running a tool, fed back to the model.
  - `SystemMessage` — instructions that set the model's behavior (the "system prompt").
  - In this project: `from langchain_core.messages import ...`.

- **`ChatAnthropic`** — the LangChain wrapper around Claude. You give it messages, it
  returns an `AIMessage`. We create it in `agents/nodes.py::_get_llm()`:
  ```python
  ChatAnthropic(model="claude-opus-4-6", streaming=True)
  ```

- **Tools (`StructuredTool`)** — a tool is just a Python function + a description +
  an input schema (a Pydantic model). The LLM reads the description/schema and decides
  when to call it. We build these in `tools/wrappers.py`:
  ```python
  StructuredTool.from_function(func=_read_file, name="read_file",
      args_schema=ReadFileInput, description="Read a text file ...")
  ```

- **`llm.bind_tools(tools)`** — tells Claude "these tools exist." Now when you call the
  model, its reply may include **tool calls** (`AIMessage.tool_calls`), e.g.
  `read_file(path="main.py")`.

- **Streaming** — instead of waiting for the full reply, we receive it in chunks so
  text appears live in the terminal (`llm.stream(...)`).

### 2.2 LangGraph basics

**LangGraph** sits on top of LangChain. It lets you build the agent as a **graph**: a
set of **nodes** (functions) connected by **edges** (what runs next). It manages the
shared **state**, loops, and saving/resuming.

Key ideas, all used in `agents/graph.py`:

- **State** — a dictionary that flows through the graph. Ours is `RunnerState`
  (`agents/state.py`): it holds `messages`, `iterations`, `workspace`, etc.

- **Reducer** — a rule for *how* a state field updates. Our `messages` field uses
  `add_messages`:
  ```python
  messages: Annotated[list[BaseMessage], add_messages]
  ```
  This means "when a node returns `{"messages": [new_msg]}`, **append** it to the list"
  (not overwrite). That's how the conversation accumulates.

- **Node** — a Python function `state -> dict`. It reads state, does work, returns the
  fields it wants to update. Ours: `node_agent` and `node_tools`.

- **Edge** — "after node A, go to node B." `builder.add_edge("node_tools", "node_agent")`.

- **Conditional edge** — "after node A, look at the state and decide where to go."
  Ours: `should_continue` decides "tools or finish?".

- **`ToolNode`** — a built-in LangGraph node that takes the last `AIMessage`, runs the
  tools it requested, and appends `ToolMessage` results. We use it inside `node_tools`.

- **Checkpointer** — LangGraph can save the state after every step to a database. If
  the program crashes or you re-run, it **resumes** from the last checkpoint. We use a
  custom one (`memory/checkpointer.py`) backed by SQLite.

- **`thread_id`** — the "save slot" id. Same `thread_id` = same conversation that
  resumes. Different `thread_id` = fresh conversation.

- **`recursion_limit`** — a safety cap on how many node-steps a single run can take
  (so an infinite loop can't run forever).

That's it. With those ~10 terms, the whole agent makes sense.

---

## 3. Directory map

```
agentic-rag-v2/
├── main.py              ← entry point: startup, REPL, drives the loop
├── agents/              ← the agent loop (LangGraph)
│   ├── graph.py         ← the 2-node graph (node_agent ↔ node_tools)
│   ├── nodes.py         ← the node functions + model/tool config + streaming
│   ├── state.py         ← RunnerState (the shared dict) + OrchestratorState
│   └── prompt.py        ← SystemPromptBuilder (assembles the system prompt)
├── tools/               ← all tools the agent can call
│   ├── wrappers.py      ← wraps each tool as a StructuredTool + safety gate
│   ├── file_ops.py      ← read_file/write_file/edit_file/grep_search/glob_search
│   ├── bash.py          ← execute_bash
│   ├── git.py           ← git operations
│   ├── web.py           ← web_fetch / web_search
│   ├── github.py        ← GitHub API (PRs, issues)
│   ├── tasks.py         ← background task registry (TaskCreate/…)
│   ├── multi_agent.py   ← Agent / Team / Worker multi-agent tools
│   └── utils.py         ← TodoWrite, Sleep, AskUserQuestion, etc.
├── safety/              ← the security layer
│   ├── permissions.py   ← permission modes + rules + per-tool requirements
│   ├── bash_validator.py← blocks destructive commands; classifies intent
│   ├── sandbox.py       ← Linux `unshare` namespace isolation for bash
│   └── hooks.py         ← optional pre/post tool-use shell hooks
├── memory/              ← context + persistence
│   ├── checkpointer.py  ← SQLite checkpointer + auto-compaction
│   ├── session.py       ← JSONL session transcript (rotation/redaction)
│   └── token_counter.py ← token usage + cost tracking
├── config/              ← configuration
│   ├── loader.py        ← merges config files + env + CLI flags
│   └── env.py           ← loads .env
├── rag/                 ← the standalone semantic-search service
│   ├── chunk.py         ← split files into overlapping chunks
│   ├── embed.py         ← embeddings + cosine similarity (+ mock mode)
│   ├── db.py            ← SQLite vector store
│   ├── ingest.py        ← walk files → chunk → embed → store
│   ├── search.py        ← query: embed → score → top-k
│   ├── server.py        ← HTTP API (/v1/query, /v1/stats, /health)
│   └── __main__.py      ← CLI: `python -m rag serve | ingest`
├── mcp/                 ← Model Context Protocol client (external tools)
│   └── client.py
├── ui/                  ← terminal rendering
│   ├── progress.py      ← live "tool started / tool done" feed
│   ├── render.py        ← markdown rendering, spinner
│   └── slash_commands.py← /help, /status, /permissions, /compact, …
└── api/                 ← provider config + shared types
    ├── providers.py     ← Anthropic/OpenAI/Ollama/… model registry
    └── types.py         ← TokenUsage and other dataclasses
```

---

## 4. The agent loop — the core (in detail)

This is the single most important part. It is a **faithful port of Rust's
`runtime/src/conversation.rs::run_turn`**: one LLM, one loop, calling tools until it
stops. (The project used to have a 6-phase pipeline; that was removed because Rust
doesn't have it.)

### 4.1 The graph (`agents/graph.py`)

```
START ──▶ node_agent ──(should_continue?)──▶ node_tools ──▶ node_agent ──▶ …
                              │
                              └──▶ END   (model asked for no tools → done)
```

Built with LangGraph:
```python
builder = StateGraph(RunnerState)
builder.add_node("node_agent", node_agent)
builder.add_node("node_tools", node_tools)
builder.add_edge(START, "node_agent")
builder.add_conditional_edges("node_agent", should_continue,
                              {"node_tools": "node_tools", "__end__": END})
builder.add_edge("node_tools", "node_agent")
runner_graph = builder.compile(checkpointer=CompactingCheckpointer())
```

### 4.2 The three functions (`agents/nodes.py`)

**`node_agent(state)`** — one "turn" of thinking = one LLM call:
1. Increment `iterations`; if it exceeds `max_iterations`, raise (mirrors Rust's hard
   cap). Default cap 50; Rust's is effectively unbounded but configurable.
2. Build the **system prompt** once and reuse it (via `SystemPromptBuilder`, see §5).
3. `llm = _get_llm().bind_tools(tools)` then **stream** the reply, printing text live
   and announcing tool calls to the progress UI.
4. Return `{"messages": [ai_message], "iterations": n, "system_prompt": ...}`. The
   `add_messages` reducer appends the AI message to the conversation.

**`should_continue(state)`** — the loop's brain:
```python
last = state["messages"][-1]
return "node_tools" if last.tool_calls else "__end__"
```
If the model asked for tools → run them. If it just replied with text → the turn is
done (this is exactly Rust's "no pending tool uses → break").

**`node_tools(state)`** — the hands:
- Uses LangGraph's `ToolNode(get_all_tools())` to execute every tool the model
  requested and append `ToolMessage` results.
- Reports each result (`✓`/`✗`) to the progress UI.
- Control returns to `node_agent`, which now sees the tool results and continues.

> **Why a graph instead of a `while` loop?** Because the checkpointer saves state
> after every node, so the conversation survives crashes/restarts and can resume. The
> graph also makes streaming and the tool cycle clean. Behaviorally it *is* a while
> loop — the graph just adds persistence + structure.

### 4.3 Parallel tool calls
In one turn the model can request several tools at once (e.g. read 3 files). `ToolNode`
runs them together before the next thinking step — so independent reads/fetches happen
in one go.

---

## 5. The system prompt (`agents/prompt.py`)

The **system prompt** is the instruction block sent to Claude before the conversation.
`SystemPromptBuilder` is a faithful port of Rust's `runtime/src/prompt.rs`. It assembles
ONE prompt from ordered sections:

```
[static] intro → system rules → "doing tasks" rules → "acting with care"
__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__          ← marker dividing static vs dynamic
[dynamic] environment (cwd, date, OS) → project context (git status/diff,
          recent commits) → instruction files (CLAUDE.md, .claw/instructions.md)
```

- **Instruction files**: it walks from the repo root down to the cwd collecting
  `CLAUDE.md`, `CLAUDE.local.md`, `.claw/CLAUDE.md`, `.claw/instructions.md`. These are
  *your* project rules; the agent follows them.
- **Limits** (match Rust): 4 000 chars per instruction file, 12 000 total, 50 000 for
  the git diff — so the prompt can't blow up.
- Built once per run and reused each iteration (cheap + consistent).

---

## 6. Tools — the agent's hands (`tools/`)

### 6.1 How a tool is wired (`tools/wrappers.py`)
Every tool follows the same pattern:
1. A real implementation function (e.g. `file_ops.read_file`).
2. A small wrapper that runs the **safety gate** (`_safe_tool` / `_check_permission`):
   pre-hook → permission check → run → post-hook → return (errors become `"Error: …"`).
3. Wrapped as a `StructuredTool` with a name + Pydantic input schema + description.
4. Collected into `get_all_tools()`, which the agent binds.

So **every tool call automatically passes through permission + hooks** — there's no way
for the model to bypass it.

### 6.2 The core toolset
| Tool | What it does |
|---|---|
| `read_file` | read a file, optionally a line window |
| `write_file` | create/overwrite a file |
| `edit_file` | exact find-and-replace in a file |
| `grep_search` | regex search across files (the agent's main "find code") |
| `glob_search` | find files by pattern (`**/*.py`) |
| `bash` | run a shell command (through validation + sandbox) |
| `git` / `git_read` | git operations |
| `WebFetch` / `WebSearch` | read a URL / search the web |
| `github` | GitHub API (PRs, issues) |
| `TodoWrite`, `Sleep`, `AskUserQuestion`, `ToolSearch`, … | utilities |

> **Important — how the agent finds context:** by **keyword search** (`grep_search` /
> `glob_search` / `read_file`), exactly like Rust. The *semantic* RAG search (§9) is a
> **separate service**, not wired into the agent by default (Rust keeps it separate too).

### 6.3 Multi-agent tools (`tools/multi_agent.py`)
This is how the agent does **parallel / independent work** — by spawning sub-agents on
demand. Faithful port of Rust's `Agent` / `Team` / `Worker` tools.

- **`Agent`** — spawns a **real background sub-agent**: a separate thread running its
  own mini agent-loop with a *restricted* tool set and a role-specific prompt
  (`subagent_type` ∈ general-purpose / Explore / Plan / Verification / claw-guide /
  statusline-setup). Returns immediately as `"running"` and writes its result to
  `.clawd-agents/<id>.md`. **Call it multiple times → multiple sub-agents run in
  parallel.** (Sub-agents cannot spawn further sub-agents.)
  - The main agent orders dependent work itself (e.g. "create the DB schema, then the
    backend that uses it") by sequencing tool calls — same as Rust.
- **`TeamCreate` / `TeamDelete`** — an in-memory registry to *group/track* a set of
  tasks (metadata only; it doesn't itself parallelize — matches Rust).
- **`Worker*`** (9 tools) — an in-memory **state machine** that tracks an external
  coding session's boot lifecycle from terminal snapshots: `WorkerCreate`,
  `WorkerObserve` (feed screen text → detects trust gate / ready / running /
  misdelivery), `WorkerResolveTrust`, `WorkerAwaitReady`, `WorkerSendPrompt`,
  `WorkerRestart`, `WorkerTerminate`, `WorkerObserveCompletion`, `WorkerGet`. It does
  **not** spawn processes; it's a control-plane for an external worker — faithful to
  Rust's `worker_boot.rs`.

### 6.4 MCP tools (`mcp/client.py`)
**MCP** (Model Context Protocol) lets you plug in *external* tool servers (defined in
config under `mcp_servers`). At startup we connect and add their tools to the agent's
toolset via `langchain-mcp-adapters`.

---

## 7. Safety (`safety/`)

Three layers, all faithful to Rust:

### 7.1 Permission modes (`permissions.py`)
Each tool declares a required mode in `TOOL_REQUIRED_MODES`. The active mode must be
high enough or the call is denied/prompted.

| Mode | Meaning |
|---|---|
| `read-only` | only read tools (read/grep/glob/web) |
| `workspace-write` | + write/edit files |
| `danger-full-access` | + bash and everything (**this is the default**, matching Rust) |
| `prompt` | (Rust quirk: actually allows everything — kept faithful) |
| `allow` | allow everything |

It also supports **rules** ported from Rust: `deny_rules`, `ask_rules`, `allow_rules`
(e.g. `bash(rm:*)` to deny `rm …`), unconditional `denied_tools`, and hook
`PermissionOverride` (allow/deny/ask). Evaluation order: denied_tools → deny rules →
hook override → ask rules → allow grant → escalation prompt → deny.

> ⚠️ **Default is `danger-full-access`** (faithful to Rust). That means bash runs
> unrestricted unless you set a stricter mode via `AGENT_PERMISSION_MODE` env or config.

### 7.2 Bash validation (`bash_validator.py`)
Before any shell command runs, it's checked against Rust-identical lists: destructive
patterns (`rm -rf /`, fork bombs, `dd if=`, …) are blocked; commands are classified
(read-only vs write vs network vs git-read, etc.) to enforce the permission mode.

### 7.3 Sandbox (`sandbox.py`)
Faithful to Rust: **Linux `unshare` user namespaces only**. On Linux with working
`unshare`, bash runs inside an isolated namespace (own mount/pid/uts/ipc, optional no
network). On macOS/Windows or without `unshare`, the sandbox is inactive and bash runs
directly (still gated by permissions + validation). No Docker.

### 7.4 Hooks (`hooks.py`)
Optional user shell scripts that run **before** and **after** every tool. A pre-hook can
block or redirect a tool call (acts like user feedback); configured under `hooks` in
config.

---

## 8. Memory & persistence (`memory/`)

### 8.1 Checkpointer + resume (`checkpointer.py`)
`CompactingCheckpointer` extends LangGraph's `SqliteSaver`. After every node it saves
the state to `~/.agent/checkpoints.db`, keyed by `thread_id`. Re-running the same task
in the same workspace resumes from where it left off.

### 8.2 Compaction (auto-summarize old context)
LLMs have a context limit, and long conversations get expensive. When the estimated
tokens cross **100 000** (env `CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS`, matching Rust),
the checkpointer:
1. keeps the most recent 4 messages verbatim,
2. summarizes everything older into one `SystemMessage`,
3. continues. Token estimate = `len/4 + 1` per block (matches Rust `compact.rs`).

### 8.3 JSONL session transcript (`session.py`)
A faithful port of Rust's `session.rs`: an append-only newline-delimited JSON log of
the conversation (`session_meta` / `message` / `prompt_history` / `compaction` records)
under `~/.agent/sessions/<thread_id>.jsonl`, with **rotation** at 256 KB (keep 3),
per-field truncation at 16 KB, and **secret redaction** (API keys / bearer tokens are
replaced with `[redacted]`). `main.py` logs the prompt + final reply here each run.

### 8.4 Token counter (`token_counter.py`)
Tracks cumulative input/output/cache tokens and computes cost from a per-model price
table. Surfaced via `/cost` and the status line.

---

## 9. The RAG service (`rag/`) — semantic search

This is a **separate program** (not the agent). It indexes your code and answers
"find the chunks most similar to this query" using embeddings. Faithful port of Rust's
`claw-rag-service`.

### Pipeline
```
ingest:  walk files → chunk (900 chars, 120 overlap) → embed (batches of 16)
         → store vectors in SQLite (.claw-rag/index.sqlite)
query:   embed the query → cosine-similarity vs every chunk → sort → top-k (max 64)
         → return 480-char snippets
```

### Files
- `chunk.py` — sliding-window splitter (window 900, overlap 120 → step 780).
- `embed.py` — calls an OpenAI-compatible `/embeddings` endpoint. **Mock mode**
  (`CLAW_RAG_MOCK_PROVIDERS=1`) produces deterministic 16-dim vectors with no network —
  great for testing. Also `cosine_similarity`.
- `db.py` — SQLite schema (`chunks`, `embeddings`, `files`); vectors stored as
  little-endian f32 BLOBs.
- `ingest.py` — walks workspaces (skips `.git/target/node_modules/…`), hashes each file
  (blake3) to skip unchanged ones, chunks + embeds + stores.
- `search.py` — `query_index`: returns hits + a `phase` string
  (`1-sqlite-no-db` / `1-sqlite-empty` / `1-sqlite` / `2-qdrant`).
- `server.py` — HTTP API: `GET /health`, `GET /v1/stats`, `POST /v1/query`.
- `qdrant_index.py` — optional Qdrant backend (only if `CLAW_RAG_QDRANT_URL` set +
  `qdrant-client` installed; otherwise falls back to SQLite).

### Run it
```bash
export CLAW_RAG_MOCK_PROVIDERS=1            # or set a real OPENAI_API_KEY
python -m rag ingest --workspace . --db .claw-rag/index.sqlite
python -m rag serve                          # http://127.0.0.1:8787
curl -XPOST localhost:8787/v1/query -d '{"query":"user authentication","top_k":8}'
```

---

## 10. Configuration (`config/`)

Settings merge from lowest → highest priority (later wins), faithful to Rust's
discovery order, plus `.agent.*` names for back-compat:
```
legacy ~/.claw.json  <  ~/.claw/settings.json / ~/.agent/settings.json
  <  ./.claw.json  <  ./.claw/settings.json / ./.agent.json
  <  ./.claw/settings.local.json  <  environment variables  <  CLI flags
```
Key settings (`AgentConfig`): `provider`, `model` (default `claude-opus-4-6`),
`permission_mode` (default `danger-full-access`), `thinking` + `thinking_budget`,
`max_iterations` (50), `sandbox`, `hooks`, `mcp_servers`.
Env vars: `AGENT_PROVIDER`, `AGENT_MODEL`, `AGENT_PERMISSION_MODE` (or
`RUSTY_CLAUDE_PERMISSION_MODE`), `AGENT_MAX_ITERATIONS`, `AGENT_WORKSPACE`, plus the
provider's API key (`ANTHROPIC_API_KEY` / `MINIMAX_API_KEY` / `AGENT_API_KEY`).

### 10.1 Switching the model (Anthropic / MiniMax / other OpenAI-compatible)

The agent supports three provider styles. Pick one with `AGENT_PROVIDER`:

**Anthropic (default).** No extra setup — just keep using Claude.
```bash
export ANTHROPIC_API_KEY=sk-ant-...
# AGENT_PROVIDER=anthropic  (default, can omit)
# AGENT_MODEL=claude-opus-4-6  (default)
```

**MiniMax** (M1 / M2 / etc. — OpenAI-compatible under the hood).
```bash
export AGENT_PROVIDER=minimax
export AGENT_MODEL=MiniMax-M2        # whatever model your key allows
export MINIMAX_API_KEY=your-key-here
# Optional override (default is the international endpoint):
# export MINIMAX_BASE_URL=https://api.minimax.io/v1
```

**Any other OpenAI-compatible endpoint** (DeepSeek, Together, Groq, local
vLLM/llama.cpp, etc.):
```bash
export AGENT_PROVIDER=openai-compatible
export AGENT_MODEL=deepseek-chat
export AGENT_API_KEY=...
export AGENT_BASE_URL=https://api.deepseek.com/v1
```

You can also put these in `.claw/settings.json` instead of env vars:
```json
{ "provider": "minimax", "model": "MiniMax-M2" }
```
(The API key still comes from the env var — never put secrets in the JSON.)

Restart the backend after changing the provider. The agent leans heavily on tool
calling; Claude models are the gold standard for that, but MiniMax M2 is
specifically tuned for agentic / coding workloads and works well here too.

---

## 11. Startup & end-to-end flow (`main.py`)

What happens when you run `python main.py "fix the failing test"`:

1. **Load env + config** (`config/`): resolves model, permission mode, sandbox, hooks,
   MCP servers.
2. **Wire safety** (`configure_safety`): injects the permission policy + hooks + sandbox
   into every tool wrapper.
3. **Configure the model + tools** (`configure_model`, `configure_tools`): tells the
   loop which model, max-iterations, and MCP tools to use.
4. **Run**:
   - one-shot prompt → `_run_graph(...)` once.
   - no prompt → `run_repl(...)`: a prompt loop. Slash commands (`/help`, `/status`,
     `/permissions …`) are handled locally; everything else goes through the loop. The
     whole REPL is **one continuous session** (one `thread_id`), like a Rust session.
5. **`_run_graph`** builds the initial state (`messages=[HumanMessage(task)]`,
   `iterations=0`), sets a `recursion_limit`, opens a JSONL session log, then
   `runner_graph.stream(...)` drives the loop — `node_agent` streams Claude's text and
   `node_tools` runs the tools — until it ends. Final assistant text is logged.

### One concrete request, start to finish
```
You: "add a /health route to the API"
 └▶ node_agent: Claude thinks, calls grep_search("def create_app")    [tool call]
     └▶ node_tools: runs grep_search → ToolMessage with matches
 └▶ node_agent: Claude calls read_file("app.py", offset=…)            [tool call]
     └▶ node_tools: returns the file window
 └▶ node_agent: Claude calls edit_file("app.py", old=…, new=…)        [tool call]
     └▶ node_tools: permission check (workspace-write) ✓ → edit applied, diff shown
 └▶ node_agent: Claude calls bash("pytest -q")                        [tool call]
     └▶ node_tools: validation ✓ → runs tests → ToolMessage with output
 └▶ node_agent: Claude replies "Added the route and tests pass."  (no tool call)
 └▶ should_continue → __end__   ✅ done
```

---

## 12. How everything connects (one diagram)

```
                       ┌──────────────── main.py ────────────────┐
   config/ ──load──▶   │  load config → wire safety → set model   │
   .env                │  → run REPL / one-shot                    │
                       └───────────────┬──────────────────────────┘
                                       │ runner_graph.stream(state)
                          ┌────────────▼─────────────┐
                          │   LangGraph runner_graph  │   agents/graph.py
                          │  ┌──────────┐             │
   agents/prompt.py ─────▶│  │node_agent│──tool? yes─▶│──┐
   (system prompt)        │  └──────────┘             │  │ agents/nodes.py
                          │        ▲      no→END       │  │
                          │        │                   │  ▼
   tools/wrappers.py ◀────┼────────┴── node_tools ◀────┼─ ToolNode runs tools
   (StructuredTools)      └───────────────────────────┘   (read/edit/bash/Agent/…)
        │                         │  each tool call
        ▼                         ▼
   safety/ (permissions,     memory/checkpointer.py  ──▶ ~/.agent/checkpoints.db
   bash_validator, sandbox)  (save state + compaction)    memory/session.py → JSONL

   rag/  ── standalone service (python -m rag serve) ── not in the agent loop
```

---

## 13. Relationship to the Rust project (source of truth)

This project is a **logic-faithful port** of `claw-code` (Rust). What matches:
single iterative agent loop, keyword search tools, permission rules/modes, bash
validation lists, compaction params, JSONL sessions, the RAG service, and the full
multi-agent suite (Agent/Team/Worker).

**Known deviations / notes:**
- The loop runs on **LangGraph** (Rust hand-writes it). Behaviorally equivalent;
  compaction fires on checkpoint-write rather than precisely mid-iteration.
- **Primary persistence** is the SQLite checkpointer (for resume); the JSONL session is
  a parallel transcript (Rust's JSONL is its primary store).
- Some niche Rust tools (`NotebookEdit`, `REPL`, `Skill`, `LSP`, `Cron*`) are **not**
  ported.
- `prompt` permission mode is faithfully "allows everything" (a Rust quirk).
- Multi-agent `Agent` execution uses LangChain under the hood; the Worker tools are a
  control-plane state machine (no process spawning), exactly like Rust.

---

## 14. Quick command reference

```bash
# install deps (needed to actually run the agent — LangChain/LangGraph)
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# run the agent
python main.py                      # interactive REPL
python main.py "refactor auth.py"   # one-shot task
python main.py -m claude-sonnet-4-6 --workspace ./myproj "add tests"

# restrict permissions (default is danger-full-access)
AGENT_PERMISSION_MODE=workspace-write python main.py "..."

# RAG service (separate)
CLAW_RAG_MOCK_PROVIDERS=1 python -m rag ingest --workspace .
python -m rag serve

# inside the REPL
/help          /status        /cost         /permissions read-only
/compact       /session list  /diff         /tasks list
```

---

## 15. Where to look when you want to change X

| I want to… | Edit |
|---|---|
| change the loop behavior | `agents/nodes.py`, `agents/graph.py` |
| change what Claude is told | `agents/prompt.py` |
| add a new tool | `tools/wrappers.py` (+ an impl in `tools/`) |
| change who can run what | `safety/permissions.py` |
| block/allow shell commands | `safety/bash_validator.py` |
| change sandboxing | `safety/sandbox.py` |
| change resume/compaction | `memory/checkpointer.py` |
| change the session log | `memory/session.py` |
| change semantic search | `rag/` |
| change defaults / config | `config/loader.py` |
| change CLI startup / REPL | `main.py` |

---

*This guide describes the project as ported to match Rust `claw-code`. Framework-
dependent modules require `langchain`/`langgraph` installed to run; the pure-logic
modules (rag, prompt, permissions, sessions, multi-agent registries) are independently
runnable and tested.*
