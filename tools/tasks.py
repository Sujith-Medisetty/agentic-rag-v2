"""
Task registry — in-memory task tracking for the agent.

Claude uses these tools to break complex work into tracked tasks,
monitor progress, and coordinate multi-step operations.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
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
    return int(time.time())


@dataclass
class TaskMessage:
    role: str
    content: str
    timestamp: int = field(default_factory=_now_secs)


@dataclass
class Task:
    """One tracked task."""
    task_id: str
    prompt: str
    description: str | None
    status: TaskStatus
    created_at: int
    updated_at: int
    messages: list[TaskMessage] = field(default_factory=list)


class TaskRegistry:
    """In-memory task registry."""

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._counter: int = 0
        self._lock = threading.Lock()

    def _next_id(self) -> str:
        """Format: task_<8hex_seconds>_<counter>."""
        with self._lock:
            self._counter += 1
            counter = self._counter
        return f"task_{_now_secs():08x}_{counter}"

    def create(self, prompt: str, description: str | None = None) -> Task:
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
        """Exact lookup only."""
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
        """Append a message to a task's log."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.updated_at = _now_secs()
        task.messages.append(TaskMessage(role="agent", content=message))
        return task

    def set_status(self, task_id: str, status: TaskStatus) -> Task | None:
        """Update status."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.status = status
        task.updated_at = _now_secs()
        return task

    def stop(self, task_id: str) -> Task:
        """Stop a running task."""
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
