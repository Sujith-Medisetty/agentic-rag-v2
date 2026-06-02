"""
JSONL session store. Faithful port of Rust runtime/src/session.rs persistence.

An append-only newline-delimited JSON log of session records:
  session_meta | message | compaction | prompt_history

Matches the Rust behavior:
  * SESSION_VERSION = 1
  * rotate the log once it exceeds 256 KB, keeping the 3 most recent rotated files
  * per-field truncation at 16 KB with a marker
  * secret redaction (API keys / bearer tokens) before writing

NOTE on roles (resolved): the SQLite `CompactingCheckpointer`
(memory/checkpointer.py) is the SINGLE SOURCE OF TRUTH for conversation state and
resume. This JSONL store is AUDIT-ONLY: a human-readable transcript (session meta,
prompts, final assistant text) plus the backing for `/session` listing and
`/resume` inspection. It is never read back to reconstruct loop state, and must
not become the primary checkpointer — that role belongs to the checkpointer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

SESSION_VERSION = 1
ROTATE_AFTER_BYTES = 256 * 1024
MAX_ROTATED_FILES = 3
MAX_JSONL_FIELD_CHARS = 16 * 1024
JSONL_TRUNCATION_MARKER = "… [truncated for session JSONL]"
JSONL_REDACTION_MARKER = "[redacted]"

_SECRET_MARKERS = (
    "ANTHROPIC_API_KEY=",
    "ANTHROPIC_AUTH_TOKEN=",
    "OPENAI_API_KEY=",
    "DASHSCOPE_API_KEY=",
    "XAI_API_KEY=",
    "Authorization: Bearer ",
    "authorization: Bearer ",
    "Bearer sk-",
    "sk-ant-",
)
_SECRET_TERMINATORS = set(" \t\n\r'\",}]")


def _redact_after_marker(value: str, marker: str) -> str:
    """Replace the secret following `marker` with the redaction marker.
    Faithful port of session.rs redact_after_marker."""
    out: list[str] = []
    rest = value
    while True:
        idx = rest.find(marker)
        if idx == -1:
            break
        out.append(rest[:idx])
        out.append(marker)
        out.append(JSONL_REDACTION_MARKER)
        after_marker = rest[idx + len(marker):]
        end = len(after_marker)
        for i, ch in enumerate(after_marker):
            if ch in _SECRET_TERMINATORS:
                end = i
                break
        rest = after_marker[end:]
    out.append(rest)
    return "".join(out)


def redact_jsonl_secrets(value: str) -> str:
    redacted = value
    for marker in _SECRET_MARKERS:
        redacted = _redact_after_marker(redacted, marker)
    return redacted


def truncate_jsonl_field(value: str) -> str:
    """Char-based truncation at MAX_JSONL_FIELD_CHARS (session.rs)."""
    if len(value) <= MAX_JSONL_FIELD_CHARS:
        return value
    keep = max(0, MAX_JSONL_FIELD_CHARS - len(JSONL_TRUNCATION_MARKER))
    return value[:keep] + JSONL_TRUNCATION_MARKER


def sanitize_field(value: str) -> str:
    """redact then truncate — order matches session.rs (truncate(redact(value)))."""
    return truncate_jsonl_field(redact_jsonl_secrets(value))


def _now_ms() -> int:
    return int(time.time() * 1000)


class SessionStore:
    """Append-only JSONL session log with rotation."""

    def __init__(self, path: str | Path, session_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id

    # -- writing -----------------------------------------------------------

    def _append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._maybe_rotate()

    def write_meta(self, model: str | None = None, workspace_root: str | None = None) -> None:
        self._append({
            "type": "session_meta",
            "version": SESSION_VERSION,
            "session_id": self.session_id,
            "created_at_ms": _now_ms(),
            "model": model,
            "workspace_root": workspace_root,
        })

    def append_message(self, role: str, blocks: list[dict]) -> None:
        clean_blocks = []
        for b in blocks:
            cb = dict(b)
            for k, v in cb.items():
                if isinstance(v, str):
                    cb[k] = sanitize_field(v)
            clean_blocks.append(cb)
        self._append({
            "type": "message",
            "session_id": self.session_id,
            "ts_ms": _now_ms(),
            "role": role,
            "blocks": clean_blocks,
        })

    def append_text_message(self, role: str, text: str) -> None:
        self.append_message(role, [{"type": "text", "text": text}])

    def append_prompt(self, text: str, ts_ms: int | None = None) -> None:
        self._append({
            "type": "prompt_history",
            "session_id": self.session_id,
            "ts_ms": ts_ms if ts_ms is not None else _now_ms(),
            "text": sanitize_field(text),
        })

    def append_compaction(self, count: int, removed_message_count: int, summary: str) -> None:
        self._append({
            "type": "compaction",
            "session_id": self.session_id,
            "ts_ms": _now_ms(),
            "count": count,
            "removed_message_count": removed_message_count,
            "summary": sanitize_field(summary),
        })

    # -- rotation ----------------------------------------------------------

    def _maybe_rotate(self) -> None:
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= ROTATE_AFTER_BYTES:
            return
        rotated = self.path.with_name(f"{self.path.name}.rot-{_now_ms()}")
        self.path.rename(rotated)
        self._cleanup_rotated()

    def _cleanup_rotated(self) -> None:
        rotated = sorted(
            self.path.parent.glob(f"{self.path.name}.rot-*"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in rotated[MAX_ROTATED_FILES:]:
            try:
                old.unlink()
            except OSError:
                pass

    # -- reading -----------------------------------------------------------

    def load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records
