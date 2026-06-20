#!/usr/bin/env python3
"""
check-feature-completeness.py — fail loudly when the agent claims a
build is "done" before the screens are actually finished.

This is the UPSTREAM gate that catches the pattern:
    agent writes 3 of 8 requested screens → marks TodoList complete →
    "Done!" → user reloads → app is half-built / routes 404 / pages
    are stubs (`return <div>TODO</div>`).

The downstream gate (check-build-freshness.py) only checks that
`dist/` matches `src/`. It does NOT check that `src/` matches what
the user actually asked for. This script fills that gap.

What it checks:
  1. Every page file under `frontend/src/pages/*.tsx` has a default
     export AND body content above a threshold (not a stub).
  2. Every default-exported page is imported somewhere (typically
     App.tsx) — flags dead files the agent wrote but never wired up.
  3. Every `<Route path="..." element={<XPage />}/>` in App.tsx
     points to a page that's actually imported + has a default export
     — flags broken routes (e.g. `<Route element={<HelpPage />} />`
     but no help.tsx).
  4. Every imported page component is also routed somewhere — flags
     pages the agent wrote but forgot to add to the router (they
     exist on disk but the user can never reach them).

Usage:
    python3 /opt/ojas/agents/scripts/check-feature-completeness.py <abs/path/frontend>

Exit codes:
    0 — all pages present, all routes resolve, no stubs detected
    1 — one or more issues; full report printed to stderr
    2 — bad usage / not a Vite project
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Pages with fewer than this many lines of body code are flagged as
# potential stubs. 15 lines is the absolute floor (a one-line return
# is clearly a placeholder); real Vite+React pages are 50+ lines.
MIN_PAGE_LINES = 15

# Common "stub" indicators — a page whose body contains one of these
# as JSX text (NOT as an HTML attribute name like `placeholder=`) is
# almost certainly not finished. Case-insensitive word-boundary match.
#
# Conservative list — only flags things that are unambiguously stub
# markers. Avoid generic words ("placeholder", "loading") that appear
# legitimately in real UIs as HTML attributes or aria text.
STUB_MARKERS = [
    "TODO", "FIXME", "XXX",
    "Not implemented", "Not Implemented",
    "Coming soon", "Under construction",
    "Stub page", "This is a stub",
    "lorem ipsum",
]


def _list_page_files(pages_dir: Path) -> dict[str, Path]:
    """Return {module_name: abs_path} for every .tsx under pages_dir.
    `welcome.tsx` -> module name `welcome` (no extension)."""
    out: dict[str, Path] = {}
    if not pages_dir.is_dir():
        return out
    for fp in sorted(pages_dir.glob("*.tsx")):
        out[fp.stem] = fp
    return out


def _has_default_export(src: str) -> bool:
    return bool(re.search(
        r"^\s*export\s+default\s+function\s+\w+|"
        r"^\s*export\s+default\s+\([^)]*\)\s*=>|"
        r"^\s*const\s+\w+\s*[:=].*=\s*\([^)]*\)\s*=>",
        src, re.M,
    )) or "export default" in src


def _page_body_lines(src: str) -> int:
    """Approximate body length — count non-blank, non-comment lines."""
    n = 0
    for line in src.splitlines():
        s = line.strip()
        if not s or s.startswith("//") or s.startswith("*"):
            continue
        n += 1
    return n


def _has_stub_marker(src: str) -> str | None:
    """Return the first stub marker found in JSX text content of the
    file body, or None. Distinguishes between:

      - Real stub: <div>TODO: implement cart</div>
      - HTML attribute: <Input placeholder="..." />  (legitimate)
      - Comment: // TODO: refactor later              (legitimate)

    Strips /* block */ and // line comments AND HTML attribute values
    before scanning, so a TODO note in a comment or a `placeholder=`
    attribute on an Input doesn't trip the check.
    """
    no_block_comments = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # Strip line comments (// ...) — preserves strings? No, but for
    # stub-marker scanning that's fine. Real JSX text never contains
    # `//` outside of comments.
    lines = []
    for ln in no_block_comments.splitlines():
        idx = ln.find("//")
        if idx >= 0:
            ln = ln[:idx]
        lines.append(ln)
    no_comments = "\n".join(lines)
    # Strip HTML/JSX attribute values: `attr="..."` and `attr='...'`
    # — keeps JSX text content between `>` and `<`.
    no_attrs = re.sub(r"""\s+\w+(?:-\w+)*\s*=\s*(?:"[^"]*"|'[^']*')""",
                      "", no_comments)
    for marker in STUB_MARKERS:
        if re.search(r"\b" + re.escape(marker) + r"\b", no_attrs,
                     flags=re.I):
            return marker
    return None


def _parse_routes_and_imports(app_tsx: Path) -> tuple[set[str], set[str]]:
    """Return (route_components, imported_components) from App.tsx.

    `route_components`  — set of PascalCase identifiers used as
        `<XPage />` in `<Route element={<XPage />}>`.
    `imported_components` — set of identifiers brought in via
        `import X from "@/pages/<file>"` (default-import name).
    """
    src = app_tsx.read_text(errors="replace")

    # Routes: `<Route ... element={<XPage />} />`. Accept optional ws
    # and allow either self-closing or paired form.
    route_components = set(re.findall(
        r"element\s*=\s*\{\s*<\s*([A-Z]\w*)\s*[^/>]*/?\s*>\s*\}",
        src,
    ))

    # Default imports of pages: `import X from "@/pages/..."`.
    imported_components = set(re.findall(
        r'import\s+(\w+)\s+from\s+["\']@/pages/[^"\']+["\']',
        src,
    ))
    # Also catch `import { X as Y }` (named import renamed) — common when
    # two files would clash on the default name.
    imported_components |= set(re.findall(
        r'import\s*\{\s*(\w+)\s+as\s+(\w+)\s*\}\s*from\s*["\']@/pages/[^"\']+["\']',
        src,
    ))

    return route_components, imported_components


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check-feature-completeness.py <abs/path/frontend>",
              file=sys.stderr)
        return 2

    frontend = Path(argv[1]).resolve()
    if not frontend.is_dir():
        print(f"ERROR: {frontend} is not a directory", file=sys.stderr)
        return 2

    pages_dir = frontend / "src" / "pages"
    app_tsx = frontend / "src" / "App.tsx"

    if not pages_dir.is_dir():
        print(f"ERROR: {pages_dir} not found.", file=sys.stderr)
        return 2
    if not app_tsx.is_file():
        print(f"ERROR: {app_tsx} not found.", file=sys.stderr)
        return 2

    page_files = _list_page_files(pages_dir)
    if not page_files:
        print(f"ERROR: no .tsx files under {pages_dir}.", file=sys.stderr)
        return 1

    route_components, imported_components = _parse_routes_and_imports(app_tsx)

    issues: list[str] = []

    # Per-file checks
    for name, fp in page_files.items():
        try:
            src = fp.read_text(errors="replace")
        except OSError as e:
            issues.append(f"CANNOT READ {fp}: {e}")
            continue

        if not _has_default_export(src):
            issues.append(
                f"NO DEFAULT EXPORT: {fp.relative_to(frontend)} — "
                "page can't be imported by App.tsx."
            )

        body_lines = _page_body_lines(src)
        if body_lines < MIN_PAGE_LINES:
            issues.append(
                f"POSSIBLE STUB ({body_lines} body lines, expected >= "
                f"{MIN_PAGE_LINES}): {fp.relative_to(frontend)} — "
                "looks like a placeholder, not a finished page."
            )

        marker = _has_stub_marker(src)
        if marker:
            issues.append(
                f"STUB MARKER \"{marker}\" in body: "
                f"{fp.relative_to(frontend)} — page is not finished."
            )

    # Cross-file checks
    # Files on disk that are imported AND routed.
    imported_page_modules = {
        mod for mod in page_files
        # Match by camelCase / kebab-case: "CartPage" -> "cart",
        # "OrderDetailPage" -> "order-detail", etc.
    }
    # The set of components that appear in routes.
    for comp in route_components:
        # Heuristic: "HomePage" -> "home", "OrderDetailPage" -> "order-detail"
        if not comp.endswith("Page") and comp != "NotFoundPage":
            issues.append(
                f"NON-PAGE COMPONENT IN ROUTE: <{comp} /> in "
                f"{app_tsx.relative_to(frontend)} doesn't end with "
                "\"Page\" — typo or wrong component?"
            )
        # The actual file name for "HomePage" is "home.tsx" — derive.
        # We don't strictly require a match because the component name
        # might not be derived from the file name (e.g. file `cart.tsx`
        # exports `Cart` not `CartPage`). The import check below is
        # the real source of truth.

    # Check 1: every route component is in the imported set.
    for comp in sorted(route_components):
        if comp not in imported_components:
            issues.append(
                "BROKEN ROUTE: <Route element={<" + comp + " />} /> in "
                + str(app_tsx.relative_to(frontend)) + " but `" + comp + "` is NOT "
                "imported from @/pages/. The browser will throw "
                "\"<comp> is not defined\" or render blank. Either "
                "add the import or remove the route."
            )

    # Check 2: every imported page is also routed somewhere — flag
    # dead imports (page written but not reachable from the UI).
    for comp in sorted(imported_components):
        if comp not in route_components:
            issues.append(
                "DEAD IMPORT: `" + comp + "` imported in "
                + str(app_tsx.relative_to(frontend)) + " but not used in any "
                "<Route> — the page exists but the user can never "
                "navigate to it. Either add a <Route> or remove the "
                "import (and the file if it's not used elsewhere)."
            )

    # Check 3: every page file on disk is imported (and ideally routed).
    # We can only match file names to import statements loosely — the
    # default-import name doesn't always equal the file name. So we
    # don't fail on missing imports; we surface it as a warning if the
    # file's default-export identifier doesn't appear in the imports.
    for name, fp in page_files.items():
        src = fp.read_text(errors="replace")
        m = re.search(r"export\s+default\s+function\s+(\w+)", src)
        if not m:
            continue
        exported = m.group(1)
        if exported not in imported_components:
            issues.append(
                "ORPHAN PAGE FILE: " + str(fp.relative_to(frontend)) + " exports "
                "`" + exported + "` but it's not imported anywhere. Either "
                "wire it into App.tsx or delete the file."
            )

    if issues:
        print(
            f"FEATURE INCOMPLETE: {len(issues)} issue(s) in "
            f"{frontend}/src/pages/ + {app_tsx.relative_to(frontend)}.\n",
            file=sys.stderr,
        )
        for line in issues:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nFix every issue above before declaring done. The build "
            "may compile (Vite doesn't catch dead imports or empty "
            "pages), but the user will see a half-built app.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK -- {len(page_files)} pages, {len(route_components)} routes, "
        f"{len(imported_components)} imports. No stubs, no dead imports, "
        f"no broken routes."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
