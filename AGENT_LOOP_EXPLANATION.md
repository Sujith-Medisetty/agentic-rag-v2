# Agent Loop Architecture Explanation

## Overview

The `_run_graph()` function orchestrates a **single iterative agent loop** that follows this pattern:
1. Agent (LLM) receives a task
2. Agent decides what tools to use
3. Tools execute
4. Results go back to agent
5. Loop continues until agent stops requesting tools

This is a **faithful port** of the Rust `runtime/src/conversation.rs::run_turn` logic.

---

## The Main Loop Flow

### Visual Representation

```
START
  ↓
┌─────────────────────────────────────────┐
│  _run_graph() - Setup & Configuration   │
│  • Set budgets (iterations, tokens)     │
│  • Create initial state                 │
│  • Initialize session logging           │
└─────────────────────────────────────────┘
  ↓
┌─────────────────────────────────────────┐
│  runner_graph.stream()                  │  ← Main loop driver
│  Iterates through graph events          │
└─────────────────────────────────────────┘
  ↓
  ┌──────────────────────────────────────┐
  │      ITERATION CYCLE                 │
  │                                      │
  │  ┌────────────────────────────┐     │
  │  │  1. node_agent             │     │
  │  │  • Check budgets           │     │
  │  │  • Call LLM with tools     │     │
  │  │  • Stream response         │     │
  │  │  • Return AIMessage        │     │
  │  └────────────────────────────┘     │
  │              ↓                       │
  │  ┌────────────────────────────┐     │
  │  │  2. should_continue        │     │
  │  │  Decision point:           │     │
  │  │  • Has tool_calls? → YES   │     │
  │  │  • No tool_calls?  → END   │     │
  │  └────────────────────────────┘     │
  │         ↓              ↓             │
  │    [YES]              [NO]           │
  │         ↓              ↓             │
  │  ┌──────────┐    ┌─────────┐       │
  │  │node_tools│    │   END   │       │
  │  │Execute   │    │Complete!│       │
  │  │tools     │    └─────────┘       │
  │  └──────────┘                       │
  │         ↓                            │
  │    Loop back to node_agent          │
  │                                      │
  └──────────────────────────────────────┘
```

---

## Detailed Component Breakdown

### 1. **`_run_graph()` Function** (Entry Point)

This is the **orchestrator** that sets up and runs the entire agent loop.

#### Parameters:
- **`task`**: The user's request (e.g., "Create a todo app")
- **`workspace`**: Current working directory
- **`repo`**: Git repository path
- **`mode`**: Operating mode (e.g., "cli", "auto")
- **`thread_id`**: Unique conversation identifier for resuming
- **`mcp_tools`**: Additional MCP (Model Context Protocol) tools
- **`cfg`**: Configuration object with limits

#### Key Setup Steps:

```python
# 1. BUDGET SETUP - Prevents infinite loops
reset_run_budget(
    max_iters=max_iter,              # Max iterations (default: 50)
    max_tokens=cfg.max_run_tokens,   # Token limit
    max_seconds=cfg.max_run_seconds, # Time limit
    no_progress_limit=8,             # Stall detection
)
```

```python
# 2. LANGGRAPH CONFIG
config = {
    "configurable": {"thread_id": thread_id},  # For resuming
    "recursion_limit": (max_iter * 2 + 10) if max_iter > 0 else 100_000,
}
```

```python
# 3. INITIAL STATE - What the agent starts with
initial_state = {
    "messages":        [HumanMessage(content=task)],  # User's task
    "task":            task,
    "workspace":       workspace,
    "repo":            repo,
    "project_context": _load_claude_md(workspace),    # Extra instructions
    "mode":            mode,
    "iterations":      0,
    "max_iterations":  max_iter,
}
```

---

### 2. **The Graph Structure** (`agents/graph.py`)

The graph is a **2-node cycle**:

```python
builder.add_node("node_agent", node_agent)    # LLM calls
builder.add_node("node_tools", node_tools)    # Tool execution

builder.add_edge(START, "node_agent")         # Always start with agent

builder.add_conditional_edges(
    "node_agent",
    should_continue,                          # Decision function
    {
        "node_tools": "node_tools",           # If tools requested
        "__end__":    END,                    # If no tools (done)
    },
)

builder.add_edge("node_tools", "node_agent")  # Tools → back to agent
```

**Flow:**
```
START → node_agent → should_continue?
                          ├─→ node_tools → node_agent (loop)
                          └─→ END (finished)
```

---

### 3. **`node_agent`** - The Brain

This node calls the LLM (Claude) and gets its response.

#### What it does:

1. **Budget Check** (before each call):
   ```python
   pause = _run_budget.check()
   if pause is not None:
       return {"paused": True, "pause_reason": pause}
   ```
   - Checks: iterations, tokens, time, stall detection
   - If budget exceeded → graceful pause (can resume later)

2. **Build System Prompt** (once, then cached):
   ```python
   system_prompt = state.get("system_prompt") or _build_system_prompt(state)
   ```
   - Includes: OS info, project context, available tools, instructions

3. **Prepare Messages**:
   ```python
   messages = [SystemMessage(content=system_prompt)]
   messages.extend(state.get("messages", []))  # Conversation history
   ```

4. **Call LLM with Tools**:
   ```python
   llm = _get_llm().bind_tools(_tools())  # Attach available tools
   ai = _stream_model_call(llm, messages)  # Stream response
   ```

5. **Return Updated State**:
   ```python
   return {
       "messages": [ai],           # Append AI response
       "iterations": iterations + 1,
       "system_prompt": system_prompt,
   }
   ```

---

### 4. **`should_continue`** - The Decision Point

This function determines if the loop continues or ends.

```python
def should_continue(state: RunnerState) -> str:
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    
    # Check if last message has tool calls
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        return "node_tools"  # Continue: execute tools
    return "__end__"         # Stop: agent is done
```

**Logic:**
- **Has `tool_calls`?** → Route to `node_tools`
- **No `tool_calls`?** → Route to `END` (task complete)

---

### 5. **`node_tools`** - The Executor

This node executes the tools the agent requested.

```python
def node_tools(state: RunnerState) -> dict:
    reporter = get_reporter()
    
    # Execute all requested tools
    result = ToolNode(_tools()).invoke(state)
    
    # Report results (for UI feedback)
    for msg in result.get("messages", []):
        content = msg.content
        name = getattr(msg, "name", "")
        reporter.tool_done(name, preview, error=is_error)
    
    return result  # Tool results appended to messages
```

**What happens:**
1. Executes each tool call from the agent
2. Collects results (success/error)
3. Appends `ToolMessage` results to conversation
4. Returns to `node_agent` for next iteration

---

## The Complete Iteration Cycle

### Example: "Create a todo app"

**Iteration 1:**
```
node_agent:
  Input:  [HumanMessage("Create a todo app")]
  LLM:    "I'll create the files..."
  Output: AIMessage(tool_calls=[
            {name: "write_to_file", args: {path: "todo.html", content: "..."}}
          ])

should_continue: Has tool_calls → "node_tools"

node_tools:
  Execute: write_to_file("todo.html", "...")
  Output:  [ToolMessage("File created successfully")]
```

**Iteration 2:**
```
node_agent:
  Input:  [HumanMessage(...), AIMessage(...), ToolMessage("File created")]
  LLM:    "Now I'll create the CSS..."
  Output: AIMessage(tool_calls=[
            {name: "write_to_file", args: {path: "style.css", content: "..."}}
          ])

should_continue: Has tool_calls → "node_tools"

node_tools:
  Execute: write_to_file("style.css", "...")
  Output:  [ToolMessage("File created successfully")]
```

**Iteration 3:**
```
node_agent:
  Input:  [... all previous messages ...]
  LLM:    "I've created the todo app with HTML and CSS files."
  Output: AIMessage(content="...", tool_calls=[])  # No tools!

should_continue: No tool_calls → "__end__"

LOOP ENDS
```

---

## Budget System (Safety Limits)

The `_RunBudget` class prevents infinite loops:

### 1. **Iteration Limit**
```python
if self.iters >= self.max_iters:
    return {"reason": "iterations", "detail": "reached cap"}
```

### 2. **Token Limit**
```python
if tokens_used >= self.max_tokens:
    return {"reason": "tokens", "detail": "reached budget"}
```

### 3. **Time Limit**
```python
if elapsed >= self.max_seconds:
    return {"reason": "time", "detail": "reached wall-clock budget"}
```

### 4. **Stall Detection** (No Progress)
```python
if self._repeat_streak >= self.no_progress_limit:
    return {"reason": "no_progress", "detail": "likely stalled"}
```

Detects when agent keeps calling the same tools with same arguments (stuck in a loop).

---

## Session Management & Resumability

### Checkpointing
```python
checkpointer = CompactingCheckpointer()  # SQLite-based
runner_graph = builder.compile(checkpointer=checkpointer)
```

- **Saves state** after each iteration
- **Can resume** from any checkpoint using same `thread_id`
- **Graceful pauses** when budgets hit (not crashes)

### Session Logging
```python
store = SessionStore(sess_path, thread_id)
store.write_meta(model=cfg.model, workspace_root=workspace)
store.append_prompt(task)
# ... later ...
store.append_text_message("assistant", text)
```

- Logs conversation to JSONL file
- Separate from checkpointing (for analysis/debugging)

---

## The Stream Loop

```python
for _event in runner_graph.stream(initial_state, config=config, stream_mode="updates"):
    pass  # Events are handled inside nodes (streaming output)
```

**What happens:**
- `stream()` yields events as graph executes
- `node_agent` streams LLM output in real-time (prints to console)
- `node_tools` reports tool execution progress
- Loop continues until graph reaches `END`

---

## Final State Handling

```python
final = runner_graph.get_state(config)
iters = final.values.get("iterations", 0)

if final.values.get("paused"):
    reason = final.values.get("pause_reason", {}).get("detail", "...")
    print(f"[paused after {iters} iterations: {reason}]")
else:
    print(f"[done in {iters} iterations]")
```

**Two outcomes:**
1. **Paused**: Budget hit → can resume with same `thread_id`
2. **Done**: Agent finished (no more tool calls)

---

## Key Design Principles

### 1. **Faithful to Rust Implementation**
- Mirrors `runtime/src/conversation.rs::run_turn`
- Same iteration counting, budget checks, terminal conditions

### 2. **Graceful Degradation**
- Budgets pause (not crash)
- Checkpoints allow resuming
- Stall detection prevents infinite loops

### 3. **Streaming UX**
- Real-time LLM output
- Tool execution feedback
- Progress reporting

### 4. **Stateless Nodes**
- Budget state is **per-invocation** (not persisted)
- Fresh budget on resume
- Clean separation of concerns

---

## Summary

**The agent loop is:**
1. **Simple**: Just 2 nodes (agent ↔ tools)
2. **Iterative**: Continues while agent requests tools
3. **Safe**: Multiple budget limits prevent runaway
4. **Resumable**: Checkpoints + graceful pauses
5. **Transparent**: Streaming output + progress reporting

**The flow:**
```
User Task → node_agent (LLM) → tool_calls? 
              ↓ yes              ↓ no
         node_tools (execute) → END
              ↓
         back to node_agent
```

This continues until the agent decides it's done (returns no tool calls) or a budget limit is reached.
