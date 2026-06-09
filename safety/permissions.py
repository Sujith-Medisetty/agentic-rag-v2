"""
Permission system — 4 modes + per-tool authorization.

Controls what tools Claude can run and when it needs to ask the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from safety.bash_validator import PermissionMode

# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------

@dataclass
class PermissionOutcome:
    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> "PermissionOutcome":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "PermissionOutcome":
        return cls(allowed=False, reason=reason)

# ---------------------------------------------------------------------------
# Per-tool permission requirements
# ---------------------------------------------------------------------------

TOOL_REQUIRED_MODES: dict[str, PermissionMode] = {
    # read-only tools
    "read_file": PermissionMode.READ_ONLY,
    "WebFetch": PermissionMode.READ_ONLY,
    "WebSearch": PermissionMode.READ_ONLY,
    "Sleep": PermissionMode.READ_ONLY,
    "AskUserQuestion":PermissionMode.READ_ONLY,
    "SendUserMessage":PermissionMode.READ_ONLY,
    "ToolSearch": PermissionMode.READ_ONLY,
    # read-only git tools
    "GitStatus": PermissionMode.READ_ONLY,
    "GitDiff": PermissionMode.READ_ONLY,
    "GitLog": PermissionMode.READ_ONLY,
    "GitShow": PermissionMode.READ_ONLY,
    "GitBlame": PermissionMode.READ_ONLY,

    # workspace write tools
    "TodoWrite": PermissionMode.WORKSPACE_WRITE, # Rust: WorkspaceWrite
    "write_file": PermissionMode.WORKSPACE_WRITE,
    "edit_file": PermissionMode.WORKSPACE_WRITE,

    # full access tools
    "bash": PermissionMode.FULL_ACCESS,

    # multi-agent tools
    "Agent": PermissionMode.FULL_ACCESS,
    "AgentStatus": PermissionMode.READ_ONLY,
    "WorkerCreate": PermissionMode.FULL_ACCESS,
    "WorkerResolveTrust": PermissionMode.FULL_ACCESS,
    "WorkerSendPrompt": PermissionMode.FULL_ACCESS,
    "WorkerRestart": PermissionMode.FULL_ACCESS,
    "WorkerTerminate": PermissionMode.FULL_ACCESS,
    "WorkerObserveCompletion": PermissionMode.FULL_ACCESS,
    "WorkerGet": PermissionMode.READ_ONLY,
    "WorkerObserve": PermissionMode.READ_ONLY,
    "WorkerAwaitReady": PermissionMode.READ_ONLY,
}

MODE_RANK: dict[PermissionMode, int] = {
    PermissionMode.READ_ONLY: 0,
    PermissionMode.WORKSPACE_WRITE: 1,
    PermissionMode.FULL_ACCESS: 2,
    PermissionMode.PROMPT: 3,
    PermissionMode.ALLOW: 4,
}

# ---------------------------------------------------------------------------
# Permission rules + hook overrides
# ---------------------------------------------------------------------------

class PermissionOverride(Enum):
    """Hook-provided override applied before standard permission evaluation."""
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"

@dataclass
class PermissionContext:
    """Extra context supplied by hooks/orchestration."""
    override_decision: PermissionOverride | None = None
    override_reason: str | None = None

@dataclass
class PermissionRequest:
    """Authorization request presented to a prompter."""
    tool_name: str
    input: str
    current_mode: PermissionMode
    required_mode: PermissionMode
    reason: str | None = None

_SUBJECT_KEYS = (
    "command", "path", "file_path", "filePath", "notebook_path",
    "notebookPath", "url", "pattern", "code", "message",
)

def _find_first_unescaped(value: str, needle: str) -> int | None:
    escaped = False
    for idx, ch in enumerate(value):
        if ch == "\\":
            escaped = not escaped
            continue
        if ch == needle and not escaped:
            return idx
        escaped = False
    return None

def _find_last_unescaped(value: str, needle: str) -> int | None:
    for pos in range(len(value) - 1, -1, -1):
        if value[pos] != needle:
            continue
        backslashes = 0
        j = pos - 1
        while j >= 0 and value[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 0:
            return pos
    return None

def _unescape_rule_content(content: str) -> str:
    return content.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")

def _extract_permission_subject(input_str: str) -> str | None:
    import json
    try:
        parsed = json.loads(input_str)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in _SUBJECT_KEYS:
            v = parsed.get(key)
            if isinstance(v, str):
                return v
    return input_str if input_str.strip() else None

@dataclass
class PermissionRule:
    raw: str
    tool_name: str
    kind: str = "any" # "any" | "exact" | "prefix"
    value: str = ""

    @classmethod
    def parse(cls, raw: str) -> "PermissionRule":
        trimmed = raw.strip()
        open_i = _find_first_unescaped(trimmed, "(")
        close_i = _find_last_unescaped(trimmed, ")")
        if (
            open_i is not None and close_i is not None
            and close_i == len(trimmed) - 1 and open_i < close_i
        ):
            tool = trimmed[:open_i].strip()
            content = trimmed[open_i + 1:close_i]
            if tool:
                unescaped = _unescape_rule_content(content.strip())
                if unescaped == "" or unescaped == "*":
                    return cls(trimmed, tool, "any")
                if unescaped.endswith(":*"):
                    return cls(trimmed, tool, "prefix", unescaped[:-2])
                return cls(trimmed, tool, "exact", unescaped)
        return cls(trimmed, trimmed, "any")

    def matches(self, tool_name: str, input_str: str) -> bool:
        if self.tool_name != tool_name:
            return False
        if self.kind == "any":
            return True
        subject = _extract_permission_subject(input_str)
        if subject is None:
            return False
        if self.kind == "exact":
            return subject == self.value
        if self.kind == "prefix":
            return subject.startswith(self.value)
        return False

def _find_matching_rule(
    rules: list[PermissionRule], tool_name: str, input_str: str
) -> PermissionRule | None:
    for rule in rules:
        if rule.matches(tool_name, input_str):
            return rule
    return None

def _rank(mode: PermissionMode) -> int:
    return MODE_RANK.get(mode, 0)

# ---------------------------------------------------------------------------
# Permission policy
# ---------------------------------------------------------------------------

class PermissionPolicy:
    """
    Evaluates whether a tool call is allowed under the current permission mode,
    plus allow/deny/ask rules, denied_tools, and hook overrides.

    """

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.FULL_ACCESS,
        prompter: Callable[[str, str, str], bool] | None = None,
        allow_rules: list[str] | None = None,
        deny_rules: list[str] | None = None,
        ask_rules: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ):
        self.mode = mode
        self.prompter = prompter # fn(tool_name, input_preview, reason) -> bool

        self.allow_rules = [PermissionRule.parse(r) for r in (allow_rules or [])]
        self.deny_rules = [PermissionRule.parse(r) for r in (deny_rules or [])]
        self.ask_rules = [PermissionRule.parse(r) for r in (ask_rules or [])]
        self.denied_tools = list(denied_tools or [])

        # session-level approvals: tool_name → always allow
        self._always_allow: set[str] = set()

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        """Required mode for a tool; defaults to full-access."""
        return TOOL_REQUIRED_MODES.get(tool_name, PermissionMode.FULL_ACCESS)

    def authorize(
        self,
        tool_name: str,
        input_str: str,
        context: PermissionContext | None = None,
    ) -> PermissionOutcome:
        """
        Check if this tool call is allowed.

        order: denied_tools → deny_rules → hook override → ask_rules → allow grant
        (allow_rule / Allow mode / rank ≥ required) → escalation prompt → deny.
        """
        ctx = context or PermissionContext()

        # session-level always-allow overrides everything
        if tool_name in self._always_allow:
            return PermissionOutcome.allow()

        # #159: unconditional tool-name denials, before rule evaluation
        if tool_name in self.denied_tools:
            return PermissionOutcome.deny(
                f"tool '{tool_name}' has been denied by denied_tools configuration"
            )

        deny_rule = _find_matching_rule(self.deny_rules, tool_name, input_str)
        if deny_rule:
            return PermissionOutcome.deny(
                f"Permission to use {tool_name} has been denied by rule '{deny_rule.raw}'"
            )

        current = self.mode
        required = self.required_mode_for(tool_name)
        ask_rule = _find_matching_rule(self.ask_rules, tool_name, input_str)
        allow_rule = _find_matching_rule(self.allow_rules, tool_name, input_str)

        def _granted() -> bool:
            return (
                allow_rule is not None
                or current == PermissionMode.ALLOW
                or _rank(current) >= _rank(required)
            )

        od = ctx.override_decision
        if od == PermissionOverride.DENY:
            return PermissionOutcome.deny(
                ctx.override_reason or f"tool '{tool_name}' denied by hook"
            )
        if od == PermissionOverride.ASK:
            reason = ctx.override_reason or (
                f"tool '{tool_name}' requires approval due to hook guidance"
            )
            return self._prompt_or_deny(tool_name, input_str, current, required, reason)
        if od == PermissionOverride.ALLOW:
            if ask_rule:
                reason = f"tool '{tool_name}' requires approval due to ask rule '{ask_rule.raw}'"
                return self._prompt_or_deny(tool_name, input_str, current, required, reason)
            if _granted():
                return PermissionOutcome.allow()
            # else fall through to common evaluation

        # common path (override None, or Allow-override that did not grant)
        if ask_rule:
            reason = f"tool '{tool_name}' requires approval due to ask rule '{ask_rule.raw}'"
            return self._prompt_or_deny(tool_name, input_str, current, required, reason)

        if _granted():
            return PermissionOutcome.allow()

        if current == PermissionMode.PROMPT or (
            current == PermissionMode.WORKSPACE_WRITE
            and required == PermissionMode.FULL_ACCESS
        ):
            reason = (
                f"tool '{tool_name}' requires approval to escalate from "
                f"{current.value} to {required.value}"
            )
            return self._prompt_or_deny(tool_name, input_str, current, required, reason)

        return PermissionOutcome.deny(
            f"tool '{tool_name}' requires {required.value} permission; "
            f"current mode is {current.value}"
        )

    def _prompt_or_deny(
        self,
        tool_name: str,
        input_str: str,
        current: PermissionMode,
        required: PermissionMode,
        reason: str | None,
    ) -> PermissionOutcome:
        """Prompt the user if a prompter is wired; otherwise deny."""
        if self.prompter:
            allowed = self.prompter(tool_name, input_str[:200], reason or "")
            return PermissionOutcome.allow() if allowed else PermissionOutcome.deny("User denied")
        return PermissionOutcome.deny(
            reason or f"tool '{tool_name}' requires approval to run while mode is {current.value}"
        )

    def always_allow_tool(self, tool_name: str) -> None:
        """Mark a tool as always allowed for this session (user chose 'always allow')."""
        self._always_allow.add(tool_name)

# Note: the old terminal_prompter() helper was removed when the CLI was
# retired. Web-mode permission prompts will route through a UI modal in a
# later phase; today, configure PermissionPolicy without a prompter and
# either run in FULL_ACCESS or rely on the bash validator + sandbox.
