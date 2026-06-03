"""
Hooks system — pre/post tool lifecycle scripts.

Users configure shell scripts in.agent.json:
 {
 "hooks": {
 "pre_tool_use": "scripts/pre.sh",
 "post_tool_use": "scripts/post.sh",
 "post_tool_failure": "scripts/on_error.sh"
 }
 }

Pre-hook can: allow / deny / modify the tool input
Post-hook can: log / alert / append feedback to result
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum

class HookEvent(Enum):
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_FAILURE = "PostToolUseFailure"

@dataclass
class HookResult:
    """
    Result returned by running a hook script.
    """
    allowed: bool = True
    denied: bool = False
    failed: bool = False
    messages: list[str] = field(default_factory=list)
    updated_input: str | None = None  # hook can modify tool input
    permission_override: str | None = None  # "allow" or "deny"

    @classmethod
    def allow(cls, messages: list[str] | None = None) -> "HookResult":
        return cls(allowed=True, messages=messages or [])

    @classmethod
    def deny(cls, reason: str) -> "HookResult":
        return cls(allowed=False, denied=True, messages=[reason])

    @classmethod
    def fail(cls, reason: str) -> "HookResult":
        return cls(allowed=False, failed=True, messages=[reason])

@dataclass
class HookConfig:
    """Hook script paths from.agent.json config."""
    pre_tool_use: str | None = None
    post_tool_use: str | None = None
    post_tool_failure: str | None = None

class HookRunner:
    """
    Runs hook scripts at tool lifecycle points.
    """

    HOOK_TIMEOUT = 30  # seconds

    def __init__(self, config: HookConfig | None = None):
        self.config = config or HookConfig()

    def pre_tool_use(self, tool_name: str, input_str: str) -> HookResult:
        """
        Run pre-tool-use hook.
        Hook receives JSON via stdin: {tool_name, input}
        Hook can return JSON: {allow: bool, deny_reason: str, updated_input: str}
        Exit code 0 = allow, exit code 2 = deny.

        """
        script = self.config.pre_tool_use
        if not script:
            return HookResult.allow()

        return self._run_hook(
            script=script,
            event=HookEvent.PRE_TOOL_USE,
            tool_name=tool_name,
            payload={"tool_name": tool_name, "input": input_str},
        )

    def post_tool_use(
        self,
        tool_name: str,
        input_str: str,
        output: str,
    ) -> HookResult:
        """
        Run post-tool-use hook.
        Hook receives: {tool_name, input, output}
        Can append messages to the tool result.

        """
        script = self.config.post_tool_use
        if not script:
            return HookResult.allow()

        return self._run_hook(
            script=script,
            event=HookEvent.POST_TOOL_USE,
            tool_name=tool_name,
            payload={
                "tool_name": tool_name,
                "input": input_str,
                "output": output[:2000],  # cap to avoid huge payloads
            },
        )

    def post_tool_failure(
        self,
        tool_name: str,
        input_str: str,
        error: str,
    ) -> HookResult:
        """
        Run post-tool-failure hook.
        Called when a tool returns is_error=True.

        """
        script = self.config.post_tool_failure
        if not script:
            return HookResult.allow()

        return self._run_hook(
            script=script,
            event=HookEvent.POST_TOOL_FAILURE,
            tool_name=tool_name,
            payload={
                "tool_name": tool_name,
                "input": input_str,
                "error": error[:2000],
            },
        )

    def _run_hook(
        self,
        script: str,
        event: HookEvent,
        tool_name: str,
        payload: dict,
    ) -> HookResult:
        """
        Run a hook script, passing payload as JSON on stdin.
        Interpret exit code and stdout as HookResult.

        """
        if not os.path.exists(script):
            return HookResult.allow()

        stdin_data = json.dumps(payload)

        try:
            result = subprocess.run(
                ["sh", script],
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=self.HOOK_TIMEOUT,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            return HookResult.fail(
                f"Hook '{script}' timed out after {self.HOOK_TIMEOUT}s"
            )
        except Exception as e:
            return HookResult.fail(f"Hook '{script}' failed to run: {e}")

        # exit code 2 = deny
        if result.returncode == 2:
            reason = result.stdout.strip() or result.stderr.strip() or \
                f"Hook '{script}' denied tool '{tool_name}'"
            return HookResult.deny(reason)

        # non-zero but not 2 = failure
        if result.returncode != 0:
            reason = result.stderr.strip() or \
                f"Hook '{script}' exited with code {result.returncode}"
            return HookResult.fail(reason)

        # exit code 0 = allow
        # try to parse JSON response for updated_input or messages
        messages = []
        updated_input = None

        stdout = result.stdout.strip()
        if stdout:
            try:
                response = json.loads(stdout)
                if isinstance(response, dict):
                    updated_input = response.get("updated_input")
                    if msg := response.get("message"):
                        messages.append(str(msg))
            except json.JSONDecodeError:
                # plain text stdout → treat as message
                if stdout:
                    messages.append(stdout)

        return HookResult(
            allowed=True,
            messages=messages,
            updated_input=updated_input,
        )
