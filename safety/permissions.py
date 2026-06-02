"""
Permission system — 4 modes + per-tool authorization.
Ported from Rust: runtime/src/permissions.rs

Controls what tools Claude can run and when it needs to ask the user.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from safety.bash_validator import PermissionMode


# ---------------------------------------------------------------------------
# Outcome types (ported from Rust)
# ---------------------------------------------------------------------------

@dataclass
class PermissionOutcome:
    allowed: bool
    reason:  str = ""

    @classmethod
    def allow(cls) -> "PermissionOutcome":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "PermissionOutcome":
        return cls(allowed=False, reason=reason)


# ---------------------------------------------------------------------------
# Per-tool permission requirements
# Ported from Rust: tools/src/lib.rs required_permission field
# ---------------------------------------------------------------------------

TOOL_REQUIRED_MODES: dict[str, PermissionMode] = {
    # read-only tools
    "read_file":      PermissionMode.READ_ONLY,
    "grep_search":    PermissionMode.READ_ONLY,
    "glob_search":    PermissionMode.READ_ONLY,
    "WebFetch":       PermissionMode.READ_ONLY,
    "WebSearch":      PermissionMode.READ_ONLY,
    "Sleep":          PermissionMode.READ_ONLY,
    "AskUserQuestion":PermissionMode.READ_ONLY,
    "SendUserMessage":PermissionMode.READ_ONLY,
    "ToolSearch":     PermissionMode.READ_ONLY,
    # read-only git tools (Rust exposes these discretely; all ReadOnly)
    "GitStatus":      PermissionMode.READ_ONLY,
    "GitDiff":        PermissionMode.READ_ONLY,
    "GitLog":         PermissionMode.READ_ONLY,
    "GitShow":        PermissionMode.READ_ONLY,
    "GitBlame":       PermissionMode.READ_ONLY,
    "git_read":       PermissionMode.READ_ONLY,

    # workspace write tools
    "TodoWrite":      PermissionMode.WORKSPACE_WRITE,  # Rust: WorkspaceWrite
    "write_file":     PermissionMode.WORKSPACE_WRITE,
    "edit_file":      PermissionMode.WORKSPACE_WRITE,

    # full access tools
    "bash":           PermissionMode.FULL_ACCESS,

    # multi-agent tools (mirror Rust required_permission)
    "Agent":              PermissionMode.FULL_ACCESS,
    "AgentStatus":        PermissionMode.READ_ONLY,
    "WorkerCreate":       PermissionMode.FULL_ACCESS,
    "WorkerResolveTrust": PermissionMode.FULL_ACCESS,
    "WorkerSendPrompt":   PermissionMode.FULL_ACCESS,
    "WorkerRestart":      PermissionMode.FULL_ACCESS,
    "WorkerTerminate":    PermissionMode.FULL_ACCESS,
    "WorkerObserveCompletion": PermissionMode.FULL_ACCESS,
    "WorkerGet":          PermissionMode.READ_ONLY,
    "WorkerObserve":      PermissionMode.READ_ONLY,
    "WorkerAwaitReady":   PermissionMode.READ_ONLY,
}

MODE_RANK: dict[PermissionMode, int] = {
    # Mirrors Rust PermissionMode declaration order (derived Ord).
    PermissionMode.READ_ONLY:       0,
    PermissionMode.WORKSPACE_WRITE: 1,
    PermissionMode.FULL_ACCESS:     2,
    PermissionMode.PROMPT:          3,
    PermissionMode.ALLOW:           4,
}


# ---------------------------------------------------------------------------
# Permission rules + hook overrides (ported from Rust permissions.rs)
# ---------------------------------------------------------------------------

class PermissionOverride(Enum):
    """Hook-provided override applied before standard permission evaluation."""
    ALLOW = "allow"
    DENY  = "deny"
    ASK   = "ask"


@dataclass
class PermissionContext:
    """Extra context supplied by hooks/orchestration (Rust PermissionContext)."""
    override_decision: PermissionOverride | None = None
    override_reason:   str | None = None


@dataclass
class PermissionRequest:
    """Authorization request presented to a prompter (Rust PermissionRequest)."""
    tool_name:     str
    input:         str
    current_mode:  PermissionMode
    required_mode: PermissionMode
    reason:        str | None = None


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
    raw:       str
    tool_name: str
    kind:      str = "any"   # "any" | "exact" | "prefix"
    value:     str = ""

    @classmethod
    def parse(cls, raw: str) -> "PermissionRule":
        trimmed = raw.strip()
        open_i  = _find_first_unescaped(trimmed, "(")
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
# Ported from Rust: runtime/src/permissions.rs PermissionPolicy
# ---------------------------------------------------------------------------

class PermissionPolicy:
    """
    Evaluates whether a tool call is allowed under the current permission mode,
    plus allow/deny/ask rules, denied_tools, and hook overrides.

    Faithful port of Rust PermissionPolicy::authorize_with_context.
    """

    def __init__(
        self,
        mode: PermissionMode = PermissionMode.FULL_ACCESS,
        prompter: Callable[[str, str, str], bool] | None = None,
        allow_rules: list[str] | None = None,
        deny_rules:  list[str] | None = None,
        ask_rules:   list[str] | None = None,
        denied_tools: list[str] | None = None,
    ):
        self.mode     = mode
        self.prompter = prompter   # fn(tool_name, input_preview, reason) -> bool

        self.allow_rules  = [PermissionRule.parse(r) for r in (allow_rules or [])]
        self.deny_rules   = [PermissionRule.parse(r) for r in (deny_rules or [])]
        self.ask_rules    = [PermissionRule.parse(r) for r in (ask_rules or [])]
        self.denied_tools = list(denied_tools or [])

        # session-level approvals: tool_name → always allow
        self._always_allow: set[str] = set()

    def required_mode_for(self, tool_name: str) -> PermissionMode:
        """Required mode for a tool; defaults to full-access (Rust DangerFullAccess)."""
        return TOOL_REQUIRED_MODES.get(tool_name, PermissionMode.FULL_ACCESS)

    def authorize(
        self,
        tool_name: str,
        input_str: str,
        context: PermissionContext | None = None,
    ) -> PermissionOutcome:
        """
        Check if this tool call is allowed.

        Faithful port of Rust PermissionPolicy::authorize_with_context — evaluation
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

        current  = self.mode
        required = self.required_mode_for(tool_name)
        ask_rule   = _find_matching_rule(self.ask_rules,   tool_name, input_str)
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
        """Prompt the user if a prompter is wired; otherwise deny (Rust prompt_or_deny)."""
        if self.prompter:
            allowed = self.prompter(tool_name, input_str[:200], reason or "")
            return PermissionOutcome.allow() if allowed else PermissionOutcome.deny("User denied")
        return PermissionOutcome.deny(
            reason or f"tool '{tool_name}' requires approval to run while mode is {current.value}"
        )

    def always_allow_tool(self, tool_name: str) -> None:
        """Mark a tool as always allowed for this session (user chose 'always allow')."""
        self._always_allow.add(tool_name)


# ---------------------------------------------------------------------------
# Default interactive prompter for terminal use
# ---------------------------------------------------------------------------

def terminal_prompter(tool_name: str, input_preview: str, reason: str) -> bool:
    """
    Ask the user in the terminal whether to allow a tool call.
    Returns True = allow, False = deny.
    """
    print(f"\n\033[33m⚠️  Permission required\033[0m")
    print(f"   Tool:   {tool_name}")
    print(f"   Reason: {reason}")
    if input_preview:
        print(f"   Input:  {input_preview}")
    print()
    print("   [a] Allow once  [A] Always allow  [d] Deny")

    while True:
        try:
            choice = input("   Choice: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False

        if choice == "a":
            return True
        elif choice == "A":
            # caller can check and call always_allow_tool
            return True
        elif choice == "d":
            return False
        else:
            print("   Please enter a, A, or d")
