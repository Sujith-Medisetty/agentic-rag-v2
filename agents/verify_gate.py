"""
Done-gate: an Ojas app may not be declared finished until its staged
verifier (`npm run verify` → scripts/verify.mjs) has gone GREEN for the
CURRENT code.

The verifier writes a sentinel `<app>/.ojas/verify-pass.json` on a full
pass and deletes it at the start of every run. "Green for the current
code" therefore means: the sentinel exists AND no source file under the
app is newer than the sentinel. That's the same mtime-freshness idea as
scripts/check-build-freshness.py, lifted to the whole app.

This module is pure filesystem inspection — it never runs the verifier.
The agent loop uses it to decide whether to let a turn END or to nudge
the model to run verify and fix the failure first. Bounded by
OJAS_VERIFY_GATE_BUDGET_SECS / OJAS_VERIFY_GATE_MAX_NUDGES so it can
never loop forever.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

# Source extensions whose mtime counts as "the app changed".
_SOURCE_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".css", ".scss", ".sass", ".less", ".html", ".svg",
    ".py", ".json",
}
# Directories we never descend into when looking for "newest source".
_SKIP_DIRS = {"node_modules", ".git", ".cache", "dist", "build", "__pycache__", ".ojas"}
# Files that don't count as source even with a source extension.
_SKIP_FILE_NAMES = {"package-lock.json", "verify-report.json"}

SENTINEL_REL = Path(".ojas") / "verify-pass.json"


def gate_enabled() -> bool:
    return os.getenv("OJAS_VERIFY_GATE", "on").strip().lower() not in ("0", "off", "false", "no")


def _budget_secs() -> float:
    try:
        return float(os.getenv("OJAS_VERIFY_GATE_BUDGET_SECS", "1800"))
    except ValueError:
        return 1800.0


def _max_nudges() -> int:
    try:
        return int(os.getenv("OJAS_VERIFY_GATE_MAX_NUDGES", "30"))
    except ValueError:
        return 30


def _is_app_dir(d: Path) -> bool:
    """An Ojas app has a Vite frontend with its own package.json."""
    return (d / "frontend" / "package.json").is_file()


def _find_app_dirs(workspace: Path) -> list[Path]:
    apps: list[Path] = []
    if _is_app_dir(workspace):
        apps.append(workspace)
    try:
        for child in sorted(workspace.iterdir()):
            if child.is_dir() and child.name not in _SKIP_DIRS and _is_app_dir(child):
                apps.append(child)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        pass
    # De-dupe while preserving order.
    seen: set = set()
    out: list[Path] = []
    for a in apps:
        r = a.resolve()
        if r not in seen:
            seen.add(r)
            out.append(a)
    return out


def _newest_source(app: Path) -> tuple[float, Path | None]:
    """Newest mtime of any source file under the app (frontend + backend
    + the manifest), skipping build output and deps."""
    newest = 0.0
    culprit: Path | None = None
    roots = [app / "frontend" / "src", app / "backend"]
    extra = [
        app / "frontend" / "index.html",
        app / "verify.manifest.json",
        app / "frontend" / "src" / "index.css",
    ]
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if name in _SKIP_FILE_NAMES:
                    continue
                if Path(name).suffix.lower() not in _SOURCE_EXTS:
                    continue
                fp = Path(dirpath) / name
                try:
                    m = fp.stat().st_mtime
                except OSError:
                    continue
                if m > newest:
                    newest, culprit = m, fp
    for fp in extra:
        try:
            m = fp.stat().st_mtime
        except OSError:
            continue
        if m > newest:
            newest, culprit = m, fp
    return newest, culprit


def find_unverified_apps(workspace: str | os.PathLike) -> list[tuple[Path, str]]:
    """Return (app_dir, reason) for every app in the workspace that is NOT
    green-for-current-code. Empty list ⇒ nothing blocks finishing."""
    ws = Path(workspace)
    out: list[tuple[Path, str]] = []
    for app in _find_app_dirs(ws):
        sentinel = app / SENTINEL_REL
        if not sentinel.is_file():
            out.append((app, "`npm run verify` has never gone green here (no .ojas/verify-pass.json)."))
            continue
        try:
            sentinel_m = sentinel.stat().st_mtime
        except OSError:
            out.append((app, "verify sentinel is unreadable — re-run `npm run verify`."))
            continue
        newest, culprit = _newest_source(app)
        if newest > sentinel_m:
            rel = culprit.relative_to(app) if culprit else "(source)"
            out.append((app, f"code changed since the last green verify (newest: {rel}) — re-run `npm run verify`."))
    return out


def build_force_message(unverified: list[tuple[Path, str]], workspace: str | os.PathLike) -> str:
    ws = Path(workspace)
    lines = [
        "⛔ NOT DONE — the staged verifier has not gone green for the current code.",
        "An Ojas app ships only after `npm run verify` prints `✅ verify GREEN` "
        "(stages: preflight → auth → db → api → browser → smoke → cleanup, stopping "
        "at the first failure with the exact fix).",
        "",
    ]
    for app, reason in unverified:
        try:
            rel = app.resolve().relative_to(ws.resolve())
        except ValueError:
            rel = app
        rel_str = "." if str(rel) == "." else str(rel)
        lines.append(f"• {rel_str}: {reason}")
    lines += [
        "",
        "Do this now: `cd <app>/frontend && npm run verify`. If a stage fails, fix "
        "the ROOT CAUSE it names (the check is right — the app is wrong), then re-run "
        "until green. Only then end your turn.",
    ]
    return "\n".join(lines)


def budget_exhausted(started_at: float | None, nudges: int) -> str | None:
    """Return a reason string if the gate should STOP forcing (and let the
    turn end with a warning), else None."""
    if nudges >= _max_nudges():
        return f"verify gate gave up after {nudges} attempts"
    if started_at is not None and (time.time() - started_at) > _budget_secs():
        return f"verify gate budget ({int(_budget_secs())}s) elapsed"
    return None
