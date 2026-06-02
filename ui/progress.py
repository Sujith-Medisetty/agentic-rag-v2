"""
Progress reporting.
  CliReporter: prints formatted progress to the terminal.

Nodes and tools call this shared API to surface tool activity.
"""

import time
from abc import ABC, abstractmethod
from typing import Optional


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------

class ProgressReporter(ABC):

    @abstractmethod
    def phase(self, name: str, detail: str = "") -> None:
        """Phase transition: Phase 1: Understanding..."""

    @abstractmethod
    def tool_start(self, tool: str, target: str = "") -> None:
        """Tool call starting: → edit_file: auth.py"""

    @abstractmethod
    def tool_done(self, tool: str, preview: str = "", error: bool = False) -> None:
        """Tool call finished: ✓ Edited: auth.py  or  ✗ Error: ..."""

    @abstractmethod
    def wave(self, wave_num: int, total_waves: int, tasks: list[str]) -> None:
        """Wave starting: Wave 2/3 — Backend API (1 worker)"""

    @abstractmethod
    def wave_done(self, wave_num: int, files_touched: int = 0) -> None:
        """Wave complete: Wave 2/3 complete — 4 files changed"""

    @abstractmethod
    def worker(self, idx: int, total: int, task: str, done: bool = False) -> None:
        """Worker status: Worker 2/3 [frontend]: Building React components..."""

    @abstractmethod
    def message(self, text: str) -> None:
        """Generic message."""


# ---------------------------------------------------------------------------
# CLI implementation — ANSI terminal output
# ---------------------------------------------------------------------------

class CliReporter(ProgressReporter):

    CYAN    = "\033[36m"
    GREEN   = "\033[32m"
    RED     = "\033[31m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    DIM     = "\033[2m"
    BOLD    = "\033[1m"
    RESET   = "\033[0m"

    def phase(self, name: str, detail: str = "") -> None:
        detail_str = f" — {detail}" if detail else ""
        print(f"\n{self.BOLD}{self.BLUE}[{name}]{self.RESET}{self.DIM}{detail_str}{self.RESET}",
              flush=True)

    def tool_start(self, tool: str, target: str = "") -> None:
        target_str = f": {self.DIM}{target[:70]}{self.RESET}" if target else ""
        print(f"  {self.CYAN}→ {tool}{self.RESET}{target_str}", flush=True)

    def tool_done(self, tool: str, preview: str = "", error: bool = False) -> None:
        if error:
            symbol = f"{self.RED}✗{self.RESET}"
        else:
            symbol = f"{self.GREEN}✓{self.RESET}"
        preview_str = f" {self.DIM}{preview[:80]}{self.RESET}" if preview else ""
        print(f"  {symbol}{preview_str}", flush=True)

    def wave(self, wave_num: int, total_waves: int, tasks: list[str]) -> None:
        tasks_str = " + ".join(t[:30] for t in tasks)
        print(
            f"\n{self.BOLD}Wave {wave_num}/{total_waves}{self.RESET}"
            f"{self.DIM} — {tasks_str}{self.RESET}",
            flush=True,
        )

    def wave_done(self, wave_num: int, files_touched: int = 0) -> None:
        files_str = f" ({files_touched} files)" if files_touched else ""
        print(
            f"  {self.GREEN}✓ Wave {wave_num} complete{files_str}{self.RESET}",
            flush=True,
        )

    def worker(self, idx: int, total: int, task: str, done: bool = False) -> None:
        status = f"{self.GREEN}✓ done{self.RESET}" if done else f"{self.DIM}running...{self.RESET}"
        print(
            f"  {self.DIM}Worker {idx}/{total}{self.RESET} "
            f"[{task[:40]}] {status}",
            flush=True,
        )

    def message(self, text: str) -> None:
        print(f"  {self.DIM}{text}{self.RESET}", flush=True)


# ---------------------------------------------------------------------------
# Global reporter — set once at startup, used everywhere
# ---------------------------------------------------------------------------

_reporter: ProgressReporter = CliReporter()   # default


def set_reporter(reporter: ProgressReporter) -> None:
    global _reporter
    _reporter = reporter


def get_reporter() -> ProgressReporter:
    return _reporter
