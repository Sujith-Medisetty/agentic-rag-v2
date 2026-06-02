# LangChain & LangGraph Beginner's Guide

## 🎯 You're New to LangChain/LangGraph? Start Here!

This guide explains **exactly** what's happening in the agent loop, step by step, with no assumptions about prior knowledge.

---

## 📚 Table of Contents

1. [What is LangChain?](#what-is-langchain)
2. [What is LangGraph?](#what-is-langgraph)
3. [Core Concepts](#core-concepts)
4. [How Messages Work](#how-messages-work)
5. [How the Graph Executes](#how-the-graph-executes)
6. [Complete Walkthrough with Examples](#complete-walkthrough)
7. [What Gets Called When](#what-gets-called-when)

---

## What is LangChain?

**LangChain** is a Python framework for building applications with Large Language Models (LLMs like Claude, GPT-4, etc.).

### Key Components Used in This Project:

#### 1. **Messages** - The Conversation Format

LangChain uses different message types to structure conversations:

```python
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage

# User's input
HumanMessage(content="Create a todo app")

# AI's response
AIMessage(content="I'll create the files for you...")

# System instructions (invisible to user)
SystemMessage(content="You are a helpful coding assistant...")

# Tool execution results
ToolMessage(content="File created successfully", name="write_to_file")
```

**Think of it like a chat history:**
```
[SystemMessage]  "You are a helpful assistant"
[HumanMessage]   "Create a todo app"
[AIMessage]      "I'll create index.html..."
[ToolMessage]    "File created successfully"
[AIMessage]      "Done! I've created your app."
```

#### 2. **ChatAnthropic** - The LLM Interface

```python
from langchain_anthropic import ChatAnthropic

llm = ChatAnthropic(model="claude-opus-4-6", streaming=True)
```

This is a **wrapper** around Claude's API that:
- Sends messages to Claude
- Receives responses
- Handles streaming (word-by-word output)
- Manages tool calling

#### 3. **Tools** - Functions the AI Can Use

```python
from langchain_core.tools import tool

@tool
def write_to_file(path: str, content: str) -> str:
    """Write content to a file."""
    with open(path, 'w') as f:
        f.write(content)
    return f"File {path} created successfully"
```

When you "bind tools" to the LLM:
```python
llm_with_tools = llm.bind_tools([write_to_file, read_file, execute_command])
```

The AI can now **request** to use these tools in its response.

---

## What is LangGraph?

**LangGraph** is a library for building **stateful, multi-step workflows** with LLMs.

### Why Use LangGraph?

**Without LangGraph** (simple approach):
```python
# One-shot: ask AI, get response, done
response = llm.invoke("Create a todo app")
print(response)
# Problem: AI can't use tools, can't iterate, can't fix mistakes
```

**With LangGraph** (iterative approach):
```python
# Multi-step: AI can use tools, see results, continue working
for step in graph.stream(initial_state):
    # AI calls tools, sees results, decides next action
    pass
# AI keeps working until task is complete
```

### Core LangGraph Concepts

#### 1. **State** - The Shared Memory

State is a dictionary that gets passed between nodes and updated over time.

```python
from typing_extensions import TypedDict
from typing import Annotated
from langgraph.graph.message import add_messages

class RunnerState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # Conversation history
    iterations: int                                        # How many loops
    workspace: str                                         # Working directory
    # ... more fields
```

**Key Point:** `Annotated[list[BaseMessage], add_messages]`
- `add_messages` is a **reducer** - it appends new messages instead of replacing
- Without it, each update would overwrite the entire message list

**Example:**
```python
# Initial state
state = {"messages": [HumanMessage("Hello")], "iterations": 0}

# Node 1 returns
{"messages": [AIMessage("Hi!")], "iterations": 1}

# State becomes (messages are APPENDED):
{
    "messages": [HumanMessage("Hello"), AIMessage("Hi!")],
    "iterations": 1  # This was REPLACED (no reducer)
}
```

#### 2. **Nodes** - The Processing Steps

A node is just a **Python function** that:
- Takes `state` as input
- Does some work
- Returns a dictionary to update the state

```python
def node_agent(state: RunnerState) -> dict:
    """Call the AI model."""
    messages = state["messages"]
    
    # Call Claude
    response = llm.invoke(messages)
    
    # Return update to state
    return {"messages": [response], "iterations": state["iterations"] + 1}
```

#### 3. **Edges** - The Flow Control

Edges define **what happens next** after a node runs.

**Simple Edge** (always go to next node):
```python
builder.add_edge("node_a", "node_b")  # A always goes to B
```

**Conditional Edge** (decide based on state):
```python
def should_continue(state):
    if state["done"]:
        return "end"
    else:
        return "continue"

builder.add_conditional_edges(
    "node_a",
    should_continue,  # Decision function
    {
        "continue": "node_b",  # If returns "continue", go to node_b
        "end": END             # If returns "end", stop
    }
)
```

#### 4. **The Graph** - Putting It All Together

```python
from langgraph.graph import StateGraph, START, END

# 1. Create graph with state schema
builder = StateGraph(RunnerState)

# 2. Add nodes (functions)
builder.add_node("agent", node_agent)
builder.add_node("tools", node_tools)

# 3. Define flow
builder.add_edge(START, "agent")              # Start → agent
builder.add_conditional_edges(
    "agent",
    should_continue,
    {"tools": "tools", "end": END}
)
builder.add_edge("tools", "agent")            # tools → agent (loop!)

# 4. Compile
graph = builder.compile()
```

**Visual:**
```
START → agent → should_continue?
                    ├─→ tools → agent (loop back)
                    └─→ END (done)
```

---

## How Messages Work

### The Conversation Grows Over Time

```python
# Iteration 1
messages = [
    SystemMessage("You are a helpful assistant"),
    HumanMessage("Create a todo app")
]
# AI responds with tool call
ai_msg = AIMessage(
    content="I'll create the HTML file",
    tool_calls=[{"name": "write_to_file", "args": {"path": "todo.html", ...}}]
)
messages.append(ai_msg)

# Tool executes
tool_msg = ToolMessage(content="File created", name="write_to_file")
messages.append(tool_msg)

# Iteration 2 - AI sees the tool result
messages = [
    SystemMessage("..."),
    HumanMessage("Create a todo app"),
    AIMessage("I'll create the HTML file", tool_calls=[...]),
    ToolMessage("File created"),
]
# AI can now decide what to do next based on the result
```

### Tool Calls in AIMessage

When the AI wants to use a tool, it returns an `AIMessage` with `tool_calls`:

```python
AIMessage(
    content="I'll create the file now",
    tool_calls=[
        {
            "name": "write_to_file",
            "args": {
                "path": "todo.html",
                "content": "<html>...</html>"
            },
            "id": "call_123"
        }
    ]
)
```

**This is NOT executing the tool yet!** It's just the AI **requesting** to use it.

---

## How the Graph Executes

### The `.stream()` Method

```python
for event in graph.stream(initial_state, config=config):
    # Events are yielded as the graph executes
    pass
```

**What happens:**
1. Graph starts with `initial_state`
2. Executes first node (`node_agent`)
3. Node returns state updates
4. Graph merges updates into state
5. Checks edges to find next node
6. Executes next node
7. Repeats until reaching `END`

### Stream Modes

```python
# stream_mode="updates" - yields state updates from each node
for event in graph.stream(state, stream_mode="updates"):
    print(event)  # {"node_agent": {"messages": [...], "iterations": 1}}

# stream_mode="values" - yields full state after each node
for event in graph.stream(state, stream_mode="values"):
    print(event)  # {"messages": [...], "iterations": 1, "workspace": "..."}
```

### Checkpointing (Saving Progress)

```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()  # Or SQLite-based
graph = builder.compile(checkpointer=checkpointer)

# Run with thread_id
config = {"configurable": {"thread_id": "conversation-123"}}
graph.stream(initial_state, config=config)
```

**What this does:**
- Saves state after each node execution
- Can resume from any point using same `thread_id`
- Survives crashes/interruptions

---

## Complete Walkthrough

Let's trace **exactly** what happens when you run the agent.

### Initial Setup

```python
# main.py calls _run_graph()
_run_graph(
    task="Create a todo app",
    workspace="/path/to/project",
    repo="/path/to/repo",
    mode="cli",
    thread_id="session-123",
    mcp_tools=[],
    cfg=config_object
)
```

### Step-by-Step Execution

#### **SETUP PHASE**

```python
# 1. Reset budgets (safety limits)
reset_run_budget(max_iters=50, max_tokens=0, max_seconds=0, no_progress_limit=8)
# This creates a fresh _run_budget object to track limits

# 2. Create LangGraph config
config = {
    "configurable": {"thread_id": "session-123"},
    "recursion_limit": 110  # (50 * 2 + 10)
}

# 3. Create initial state
initial_state = {
    "messages": [HumanMessage(content="Create a todo app")],
    "task": "Create a todo app",
    "workspace": "/path/to/project",
    "repo": "/path/to/repo",
    "project_context": "...",  # From CLAUDE.md
    "mode": "cli",
    "iterations": 0,
    "max_iterations": 50,
}
```

#### **ITERATION 1**

```python
# Graph starts: START → node_agent

# ===== node_agent() is called =====
def node_agent(state: RunnerState) -> dict:
    # 1. Check budgets
    pause = _run_budget.check()  # Returns None (no limits hit yet)
    
    # 2. Increment iteration counter
    iterations = 0 + 1  # = 1
    
    # 3. Build system prompt (first time only)
    system_prompt = _build_system_prompt(state)
    # Returns: "You are Roo, a helpful assistant. You have these tools: ..."
    
    # 4. Prepare messages for Claude
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content="Create a todo app")
    ]
    
    # 5. Get LLM with tools bound
    llm = ChatAnthropic(model="claude-opus-4-6", streaming=True)
    llm_with_tools = llm.bind_tools([write_to_file, read_file, ...])
    
    # 6. Call Claude (streaming)
    ai = _stream_model_call(llm_with_tools, messages)
    # Claude responds: "I'll create a todo app with HTML, CSS, and JS..."
    # And requests tool: write_to_file(path="todo.html", content="...")
    
    # ai = AIMessage(
    #     content="I'll create a todo app...",
    #     tool_calls=[{
    #         "name": "write_to_file",
    #         "args": {"path": "todo.html", "content": "<html>..."}
    #     }]
    # )
    
    # 7. Record in budget tracker
    _run_budget.record(ai)  # iters=1, check for stalls
    
    # 8. Return state updates
    return {
        "messages": [ai],           # Appended to state.messages
        "iterations": 1,            # Replaces state.iterations
        "system_prompt": system_prompt  # Cached for next iteration
    }

# State is now:
# {
#     "messages": [HumanMessage("..."), AIMessage("...", tool_calls=[...])],
#     "iterations": 1,
#     "system_prompt": "...",
#     ...
# }

# ===== should_continue() is called =====
def should_continue(state: RunnerState) -> str:
    messages = state["messages"]
    last = messages[-1]  # AIMessage with tool_calls
    
    if isinstance(last, AIMessage) and last.tool_calls:
        return "node_tools"  # ← Returns this
    return "__end__"

# Graph routes to: node_tools

# ===== node_tools() is called =====
def node_tools(state: RunnerState) -> dict:
    # 1. Get the tool node (LangGraph's built-in)
    tool_node = ToolNode([write_to_file, read_file, ...])
    
    # 2. Execute tools
    result = tool_node.invoke(state)
    # This looks at the last AIMessage, finds tool_calls,
    # executes each tool, and creates ToolMessages
    
    # result = {
    #     "messages": [
    #         ToolMessage(
    #             content="File todo.html created successfully",
    #             name="write_to_file"
    #         )
    #     ]
    # }
    
    # 3. Report to UI
    reporter.tool_done("write_to_file", "File created", error=False)
    
    # 4. Return state updates
    return result

# State is now:
# {
#     "messages": [
#         HumanMessage("Create a todo app"),
#         AIMessage("I'll create...", tool_calls=[...]),
#         ToolMessage("File created", name="write_to_file")
#     ],
#     "iterations": 1,
#     ...
# }

# Graph routes to: node_agent (loop back!)
```

#### **ITERATION 2**

```python
# ===== node_agent() is called again =====
def node_agent(state: RunnerState) -> dict:
    # 1. Check budgets
    pause = _run_budget.check()  # Still None
    
    # 2. Increment iteration
    iterations = 1 + 1  # = 2
    
    # 3. System prompt already cached
    system_prompt = state["system_prompt"]  # Reuse!
    
    # 4. Prepare messages (now includes tool result!)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content="Create a todo app"),
        AIMessage("I'll create...", tool_calls=[...]),
        ToolMessage("File created", name="write_to_file")
    ]
    
    # 5. Call Claude again
    ai = _stream_model_call(llm_with_tools, messages)
    # Claude sees the tool result and decides next action
    # Maybe: "Now I'll create the CSS file..."
    # tool_calls=[{"name": "write_to_file", "args": {"path": "style.css", ...}}]
    
    # 6. Return updates
    return {
        "messages": [ai],
        "iterations": 2,
        "system_prompt": system_prompt
    }

# ===== should_continue() =====
# Last message has tool_calls → return "node_tools"

# ===== node_tools() =====
# Executes write_to_file for style.css
# Returns ToolMessage("File created")

# Loop continues...
```

#### **FINAL ITERATION**

```python
# After several iterations...

# ===== node_agent() =====
messages = [
    SystemMessage("..."),
    HumanMessage("Create a todo app"),
    AIMessage("I'll create HTML", tool_calls=[...]),
    ToolMessage("File created"),
    AIMessage("Now CSS", tool_calls=[...]),
    ToolMessage("File created"),
    AIMessage("Now JS", tool_calls=[...]),
    ToolMessage("File created"),
]

ai = _stream_model_call(llm_with_tools, messages)
# Claude: "I've created a complete todo app with HTML, CSS, and JS files."
# tool_calls=[]  ← NO TOOL CALLS!

return {"messages": [ai], "iterations": 4}

# ===== should_continue() =====
last = messages[-1]  # AIMessage with NO tool_calls
if last.tool_calls:  # False!
    return "node_tools"
return "__end__"  # ← Returns this

# Graph routes to: END
# Loop terminates!
```

### After the Loop

```python
# Back in _run_graph()
for event in runner_graph.stream(initial_state, config=config):
    pass  # Loop finished

# Get final state
final = runner_graph.get_state(config)
iters = final.values["iterations"]  # 4

if final.values.get("paused"):
    print("[paused - budget hit]")
else:
    print(f"[done in {iters} iterations]")  # ← This prints
```

---

## What Gets Called When

### Complete Call Stack

```
main.py
  └─ _run_graph()
      ├─ reset_run_budget()              # Setup budgets
      ├─ SessionStore()                  # Setup logging
      └─ runner_graph.stream()           # START THE LOOP
          │
          ├─ [Iteration 1]
          │   ├─ node_agent()
          │   │   ├─ _run_budget.check()           # Check limits
          │   │   ├─ _build_system_prompt()        # Build prompt
          │   │   ├─ _get_llm()                    # Get ChatAnthropic
          │   │   ├─ llm.bind_tools()              # Attach tools
          │   │   ├─ _stream_model_call()          # Call Claude
          │   │   │   └─ llm.stream()              # Stream response
          │   │   └─ _run_budget.record()          # Track usage
          │   │
          │   ├─ should_continue()                 # Check tool_calls
          │   │   └─ return "node_tools"
          │   │
          │   └─ node_tools()
          │       ├─ ToolNode().invoke()           # Execute tools
          │       │   └─ write_to_file()           # Actual tool
          │       └─ reporter.tool_done()          # UI feedback
          │
          ├─ [Iteration 2]
          │   ├─ node_agent()
          │   ├─ should_continue()
          │   └─ node_tools()
          │
          ├─ [Iteration 3]
          │   ├─ node_agent()
          │   ├─ should_continue()
          │   └─ node_tools()
          │
          └─ [Final Iteration]
              ├─ node_agent()
              ├─ should_continue()
              │   └─ return "__end__"              # NO TOOLS!
              └─ END
```

### Function Call Order (Detailed)

```
1. _run_graph() starts
2. reset_run_budget() - initialize limits
3. SessionStore() - setup logging
4. runner_graph.stream() - begin graph execution

   LOOP STARTS:
   
   5. node_agent() called
      6. _run_budget.check() - verify we can continue
      7. _build_system_prompt() - create instructions (once)
      8. _get_llm() - get ChatAnthropic instance
      9. llm.bind_tools() - attach available tools
      10. _stream_model_call() - call Claude
          11. llm.stream() - actual API call
          12. Print streamed text to console
          13. reporter.tool_start() - announce tool calls
      14. _run_budget.record() - track iteration
      15. Return {"messages": [ai], "iterations": N}
   
   16. LangGraph merges updates into state
   
   17. should_continue() called
       18. Check last message for tool_calls
       19. Return "node_tools" or "__end__"
   
   IF "node_tools":
       20. node_tools() called
           21. ToolNode().invoke() - execute tools
               22. write_to_file() / read_file() / etc.
           23. reporter.tool_done() - report results
           24. Return {"messages": [tool_msg]}
       
       25. LangGraph merges updates into state
       26. Edge routes back to node_agent (step 5)
       
   IF "__end__":
       27. Graph terminates
       28. Exit loop

29. runner_graph.get_state() - get final state
30. Print completion message
```

---

## Key Takeaways

### 1. **Messages Are the Memory**
Every interaction is stored in the `messages` list. The AI sees the entire conversation history each time.

### 2. **Nodes Are Just Functions**
`node_agent` and `node_tools` are regular Python functions that take state and return updates.

### 3. **The Graph Manages Flow**
LangGraph handles:
- Calling nodes in order
- Merging state updates
- Routing based on conditions
- Saving checkpoints

### 4. **Tool Calls Are Requests**
When AI returns `tool_calls`, it's **requesting** to use tools. The `node_tools` actually executes them.

### 5. **The Loop Continues Until AI Stops**
The loop only ends when the AI returns an `AIMessage` with **no tool_calls**.

### 6. **Budgets Prevent Infinite Loops**
Multiple safety limits (iterations, tokens, time, stalls) ensure the loop doesn't run forever.

---

## Debugging Tips

### See What's Happening

```python
# Add prints in nodes
def node_agent(state: RunnerState) -> dict:
    print(f"🤖 Agent iteration {state['iterations'] + 1}")
    print(f"📝 Message count: {len(state['messages'])}")
    # ... rest of function
```

### Inspect State

```python
# After graph runs
final = runner_graph.get_state(config)
print(f"Final iterations: {final.values['iterations']}")
print(f"Message history: {len(final.values['messages'])}")
for msg in final.values['messages']:
    print(f"  {type(msg).__name__}: {msg.content[:50]}...")
```

### Check Tool Calls

```python
# In should_continue
def should_continue(state: RunnerState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage):
        tools = getattr(last, "tool_calls", None)
        print(f"🔧 Tool calls: {[t['name'] for t in tools] if tools else 'None'}")
    # ... rest of function
```

---

## Summary

**LangChain** provides:
- Message types (HumanMessage, AIMessage, etc.)
- LLM wrappers (ChatAnthropic)
- Tool definitions

**LangGraph** provides:
- State management (shared memory)
- Node execution (calling functions)
- Flow control (edges, conditions)
- Checkpointing (saving progress)

**The Loop**:
1. Agent calls Claude with conversation history
2. Claude responds (maybe with tool requests)
3. If tools requested → execute them → loop back to step 1
4. If no tools → done!

**That's it!** The complexity comes from the details, but the core concept is simple: keep calling the AI and executing tools until the AI says it's done.
