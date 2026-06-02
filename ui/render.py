"""
Terminal markdown renderer + spinner.
Ported from Rust: rusty-claude-cli/src/render.rs

Converts markdown to ANSI-colored terminal output.
Uses only stdlib + ANSI escape codes — no external dependencies.
"""

import re
import sys
import threading
import time


# ---------------------------------------------------------------------------
# ANSI color codes
# ---------------------------------------------------------------------------

class Color:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    ITALIC  = "\033[3m"

    # foreground
    CYAN    = "\033[36m"
    MAGENTA = "\033[35m"
    YELLOW  = "\033[33m"
    GREEN   = "\033[32m"
    BLUE    = "\033[34m"
    RED     = "\033[31m"
    WHITE   = "\033[37m"
    GREY    = "\033[90m"

    # bright
    BRIGHT_CYAN   = "\033[96m"
    BRIGHT_GREEN  = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_WHITE  = "\033[97m"


def _c(color: str, text: str) -> str:
    """Wrap text in color codes."""
    return f"{color}{text}{Color.RESET}"


# ---------------------------------------------------------------------------
# Markdown renderer
# Ported from Rust: render.rs TerminalRenderer::render_markdown()
# ---------------------------------------------------------------------------

class MarkdownRenderer:
    """
    Converts markdown to ANSI terminal output.
    Handles: headings, bold, italic, code, code blocks,
             lists, blockquotes, links, horizontal rules, tables.
    """

    def render(self, text: str) -> str:
        """Render markdown string to ANSI string."""
        lines   = text.split("\n")
        output  = []
        i       = 0
        in_code = False
        code_lang   = ""
        code_lines  = []

        while i < len(lines):
            line = lines[i]

            # fenced code block start
            if not in_code and (line.startswith("```") or line.startswith("~~~")):
                in_code   = True
                code_lang = line[3:].strip()
                code_lines = []
                i += 1
                continue

            # fenced code block end
            if in_code and (line.startswith("```") or line.startswith("~~~")):
                in_code = False
                output.append(self._render_code_block(code_lines, code_lang))
                i += 1
                continue

            if in_code:
                code_lines.append(line)
                i += 1
                continue

            # headings
            if line.startswith("######"):
                output.append(_c(Color.CYAN, line[6:].strip()))
            elif line.startswith("#####"):
                output.append(_c(Color.CYAN, line[5:].strip()))
            elif line.startswith("####"):
                output.append(_c(Color.CYAN + Color.BOLD, line[4:].strip()))
            elif line.startswith("###"):
                output.append(_c(Color.BRIGHT_CYAN, "  " + line[3:].strip()))
            elif line.startswith("##"):
                output.append(_c(Color.BRIGHT_CYAN + Color.BOLD, line[2:].strip()))
            elif line.startswith("# "):
                output.append(_c(Color.BRIGHT_CYAN + Color.BOLD, "\n" + line[2:].strip() + "\n"))

            # blockquote
            elif line.startswith("> "):
                output.append(_c(Color.GREY, "│ " + self._inline(line[2:])))

            # horizontal rule
            elif re.match(r"^[-*_]{3,}$", line.strip()):
                output.append(_c(Color.GREY, "─" * 60))

            # unordered list
            elif re.match(r"^(\s*)[-*+] ", line):
                indent = len(line) - len(line.lstrip())
                bullet = "  " * (indent // 2) + _c(Color.CYAN, "•") + " "
                content = re.sub(r"^\s*[-*+] ", "", line)
                output.append(bullet + self._inline(content))

            # ordered list
            elif re.match(r"^(\s*)\d+\. ", line):
                indent = len(line) - len(line.lstrip())
                num    = re.match(r"^\s*(\d+)\. ", line).group(1)
                prefix = "  " * (indent // 2) + _c(Color.CYAN, f"{num}.") + " "
                content = re.sub(r"^\s*\d+\. ", "", line)
                output.append(prefix + self._inline(content))

            # table row
            elif "|" in line and line.strip().startswith("|"):
                output.append(self._render_table_row(line))

            # empty line
            elif not line.strip():
                output.append("")

            # regular paragraph
            else:
                output.append(self._inline(line))

            i += 1

        return "\n".join(output)

    def _inline(self, text: str) -> str:
        """Apply inline formatting: bold, italic, code, links."""
        # inline code
        text = re.sub(
            r"`([^`]+)`",
            lambda m: _c(Color.GREEN, f"`{m.group(1)}`"),
            text
        )
        # bold+italic
        text = re.sub(
            r"\*\*\*(.+?)\*\*\*",
            lambda m: _c(Color.YELLOW + Color.BOLD + Color.ITALIC, m.group(1)),
            text
        )
        # bold
        text = re.sub(
            r"\*\*(.+?)\*\*",
            lambda m: _c(Color.BOLD, m.group(1)),
            text
        )
        # italic
        text = re.sub(
            r"\*(.+?)\*",
            lambda m: _c(Color.ITALIC, m.group(1)),
            text
        )
        # links [text](url)
        text = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: _c(Color.BLUE, m.group(1)) + _c(Color.GREY, f" ({m.group(2)})"),
            text
        )
        return text

    def _render_code_block(self, lines: list[str], lang: str) -> str:
        """Render a fenced code block with border."""
        border = _c(Color.GREY, "─" * 50)
        lang_label = _c(Color.GREY, f" {lang}") if lang else ""
        header = _c(Color.GREY, "┌") + lang_label + _c(Color.GREY, "─" * (49 - len(lang)))
        footer = _c(Color.GREY, "└" + "─" * 50)
        body   = "\n".join(_c(Color.GREY, "│ ") + line for line in lines)
        return f"{header}\n{body}\n{footer}"

    def _render_table_row(self, line: str) -> str:
        """Render a markdown table row."""
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # separator row (---)
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            return _c(Color.GREY, "├" + "┼".join("─" * 12 for _ in cells) + "┤")
        rendered = _c(Color.GREY, "│")
        for cell in cells:
            rendered += " " + self._inline(cell).ljust(10) + " " + _c(Color.GREY, "│")
        return rendered


# ---------------------------------------------------------------------------
# Spinner
# Ported from Rust: render.rs Spinner
# ---------------------------------------------------------------------------

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class Spinner:
    """
    Animated terminal spinner for long-running operations.
    Runs in a background thread.
    Ported from Rust: render.rs Spinner
    """

    def __init__(self, label: str = "Thinking..."):
        self.label    = label
        self._stop    = threading.Event()
        self._thread  = None
        self._frame   = 0

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self, success: bool = True) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        # clear spinner line
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()

    def _spin(self) -> None:
        while not self._stop.is_set():
            frame = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
            sys.stdout.write(
                f"\r{Color.BLUE}{frame}{Color.RESET} {Color.DIM}{self.label}{Color.RESET}"
            )
            sys.stdout.flush()
            self._frame += 1
            time.sleep(0.08)


# ---------------------------------------------------------------------------
# Cost display
# ---------------------------------------------------------------------------

def format_cost_line(summary: str) -> str:
    """Format token/cost summary for display."""
    return _c(Color.DIM, summary)


def format_tool_start(tool_name: str, preview: str) -> str:
    """Format tool call start line."""
    return f"\n{_c(Color.CYAN, '⚡ ' + tool_name)}{_c(Color.GREY, ': ' + preview)}"


def format_tool_end(tool_name: str, output_preview: str, is_error: bool) -> str:
    """Format tool call result line."""
    if is_error:
        return f"  {_c(Color.RED, '✗ error')}: {_c(Color.DIM, output_preview[:80])}"
    return f"  {_c(Color.GREEN, '✓')} {_c(Color.DIM, output_preview[:80])}"


def format_safety_block(tool_name: str, reason: str) -> str:
    """Format safety block line."""
    return f"\n{_c(Color.RED, '🚫 Blocked')}: {tool_name} — {reason}"


def format_thinking(chunk: str) -> str:
    """Format thinking text in dim style."""
    return _c(Color.DIM, chunk)
