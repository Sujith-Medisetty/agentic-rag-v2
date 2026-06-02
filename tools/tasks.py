"""
Task registry — in-memory task tracking for the agent.
Ported from Rust: runtime/src/task_registry.rs.

Claude uses these tools to break complex work into tracked tasks,
monitor progress, and coordinate multi-step operations.
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    """Mirrors Rust task_registry.rs TaskStatus."""
    CREATED = "created"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"


_TERMINAL_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.STOPPED,
}


def _now_secs() -> int:
    """Mirrors Rust now_secs() — seconds since the UNIX epoch."""
    return int(time.time())


@dataclass
class TaskMessage:
    role: str
    content: str
    timestamp: int = field(default_factory=_now_secs)


@dataclass
class Task:
    """One tracked task. Mirrors Rust task_registry.rs Task."""
    task_id: str
    prompt: str
    description: str | None
    status: TaskStatus
    created_at: int
    updated_at: int
    messages: list[TaskMessage] = field(default_factory=list)
    output: str = ""


class TaskRegistry:
    """
    In-memory task registry.
    Ported from Rust: runtime/src/task_registry.rs TaskRegistry.
    """

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._counter: int = 0
        self._lock = threading.Lock()

    def _next_id(self) -> str:
        """Format: task_<8hex_seconds>_<counter>. Mirrors Rust ID format."""
        with self._lock:
            self._counter += 1
            counter = self._counter
        return f"task_{_now_secs():08x}_{counter}"

    def create(self, prompt: str, description: str | None = None) -> Task:
        """Mirrors Rust TaskRegistry::create()."""
        now = _now_secs()
        task = Task(
            task_id=self._next_id(),
            prompt=prompt,
            description=description,
            status=TaskStatus.CREATED,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        """Exact lookup only (Rust TaskRegistry::get does not prefix-match)."""
        return self._tasks.get(task_id)

    def list(self, status: str | None = None) -> list[Task]:
        tasks = list(self._tasks.values())
        if status:
            try:
                target = TaskStatus(status)
                tasks = [t for t in tasks if t.status == target]
            except ValueError:
                pass
        return sorted(tasks, key=lambda t: t.created_at)

    def update(self, task_id: str, message: str) -> Task | None:
        """
        Append a message to a task's log.
        Mirrors Rust TaskRegistry::update(task_id, message).
        """
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.updated_at = _now_secs()
        task.messages.append(TaskMessage(role="agent", content=message))
        return task

    def set_status(self, task_id: str, status: TaskStatus) -> Task | None:
        """Update status (Rust exposes this via internal helpers)."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = status
        task.updated_at = _now_secs()
        return task

    def set_output(self, task_id: str, output: str) -> Task | None:
        """Update output buffer."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.output = output
        task.updated_at = _now_secs()
        return task

    def stop(self, task_id: str) -> Task:
        """
        Stop a running task.
        Mirrors Rust TaskRegistry::stop() — rejects already-terminal tasks.
        """
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"task not found: {task_id}")
        if task.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"task {task_id} is already in terminal status {task.status.value}"
            )
        task.status = TaskStatus.STOPPED
        task.updated_at = _now_secs()
        return task

    def output(self, task_id: str) -> str:
        """
        Return the raw output buffer for a task.
        Mirrors Rust TaskRegistry::output() which returns just the string.
        """
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"task not found: {task_id}")
        return task.output

    def format_list(self) -> str:
        """Python-side convenience: formatted human-readable task list."""
        tasks = self.list()
        if not tasks:
            return "No tasks."
        icons = {
            TaskStatus.CREATED: "[ ]",
            TaskStatus.RUNNING: "[~]",
            TaskStatus.BLOCKED: "[!]",
            TaskStatus.COMPLETED: "[x]",
            TaskStatus.FAILED: "[X]",
            TaskStatus.STOPPED: "[-]",
        }
        lines = [f"Tasks ({len(tasks)}):"]
        for t in tasks:
            icon = icons.get(t.status, "[?]")
            lines.append(f"  {icon} {t.task_id} {t.prompt[:60]}")
        return "\n".join(lines)


# Global registry — shared across the session.
_global_registry: TaskRegistry | None = None


def get_registry() -> TaskRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = TaskRegistry()
    return _global_registry


def reset_registry() -> None:
    global _global_registry
    _global_registry = TaskRegistry()
