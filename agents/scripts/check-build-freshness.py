#!/usr/bin/env python3
"""
check-build-freshness.py — fail loudly if frontend/src/ is newer than
frontend/dist/index.html.

This is the cheap, fast gate that prevents the agent from declaring an
edit "done" before running `npm run build`. Server-side enforcement
(deploy job auto-rebuilds stale dist) is the safety net, but this script
catches the bug BEFORE the user clicks Deploy — see agents/prompt.py
"Build freshness gate" rule.

Usage:
    python3 /opt/ojas/agents/scripts/check-build-freshness.py <abs/path/frontend>

Exit codes:
    0 — dist/index.html exists AND is newer than every source file
    1 — dist is stale, missing, or the dir is not a Vite project
    2 — bad usage (missing arg, path doesn't exist)

The script is read-only on disk — it never runs npm, never edits files.
It's meant to be run BEFORE npm run build, not as a substitute for it.
If src is newer than dist, the message tells the agent exactly what to
do: run `npm --prefix <frontend> run build` then re-run this check.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Extensions we treat as "source". CSS/SCSS/HTML/TSX/JSX/JS/TS — basically
# anything that affects the bundle. Images/fonts in src/ are usually imports
# and DO affect the bundle, but they're rare and the agent should rebuild
# if they change anyway. Keep this list small to stay fast.
SOURCE_EXTS = {".ts", ".tsx", ".jsx", ".js", ".mjs", ".cjs",
               ".css", ".scss", ".sass", ".less",
               ".html", ".svg"}

# Directories we never descend into. node_modules alone is the big one
# (a typical Vite project has thousands of node_modules files; scanning
# them is slow and pointless — they don't trigger a rebuild).
SKIP_DIRS = {"node_modules", ".git", ".cache", "dist", "build",
             ".next", ".nuxt", ".svelte-kit", ".vite", ".turbo",
             "coverage", ".parcel-cache"}


def _newest_source_mtime(src_dir: Path) -> float:
    """Return the max mtime across source files under src_dir. Excludes
    node_modules etc. so a reinstall doesn't trip the freshness check."""
    newest = 0.0
    # Walk src/ AND the project root for index.html (the template's
    # <title> and <meta> tags live there, not under src/). Also pick up
    # tailwind.config.* / postcss.config.* / vite.config.* at the root.
    candidates = [src_dir, src_dir.parent] if src_dir.name == "src" else [src_dir]
    for base in candidates:
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune in-place so we don't descend into skip dirs.
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in SOURCE_EXTS or fn in {"package.json"}:
                    try:
                        mt = os.path.getmtime(os.path.join(dirpath, fn))
                        if mt > newest:
                            newest = mt
                    except OSError:
                        continue
    return newest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check-build-freshness.py <abs/path/frontend>",
              file=sys.stderr)
        return 2

    frontend = Path(argv[1]).resolve()
    if not frontend.is_dir():
        print(f"ERROR: {frontend} is not a directory", file=sys.stderr)
        return 2

    pkg = frontend / "package.json"
    if not pkg.is_file():
        print(f"ERROR: {frontend}/package.json not found — "
              "this doesn't look like a Vite/React project.", file=sys.stderr)
        return 1

    src_dir = frontend / "src"
    if not src_dir.is_dir():
        print(f"ERROR: {frontend}/src not found.", file=sys.stderr)
        return 1

    dist_index = frontend / "dist" / "index.html"
    if not dist_index.is_file():
        print(
            f"STALE BUILD: {dist_index} does not exist.\n"
            f"  You edited frontend/src/ but never ran `npm run build`.\n"
            f"  Run:  npm --prefix {frontend} run build\n"
            f"  Then re-run this check before declaring done.",
            file=sys.stderr,
        )
        return 1

    newest_src = _newest_source_mtime(src_dir)
    dist_mtime = dist_index.stat().st_mtime

    if newest_src > dist_mtime:
        # Find the most recently changed source file so the agent can see
        # what's out of sync — saves a `find . -newer dist/index.html`.
        culprit = ""
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    if fp.stat().st_mtime >= newest_src - 0.001:
                        culprit = str(fp.relative_to(frontend))
                        break
                except OSError:
                    continue
            if culprit:
                break
        print(
            f"STALE BUILD: frontend/src/ is newer than frontend/dist/index.html.\n"
            f"  Most recently changed source: {culprit or '(unknown)'}\n"
            f"  You edited frontend/src/ but didn't run `npm run build`.\n"
            f"  Fix:\n"
            f"    npm --prefix {frontend} run build\n"
            f"  Then re-run this check (must exit 0) before declaring done.\n"
            f"  Reference: prompt.py → 'Build freshness gate' rule.",
            file=sys.stderr,
        )
        return 1

    print(f"OK -- dist/index.html is fresh "
          f"(newest src mtime={newest_src:.0f}, dist mtime={dist_mtime:.0f}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
