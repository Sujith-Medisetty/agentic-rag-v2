"""
File operations — read, write, edit, grep, glob.
Ported from Rust: runtime/src/file_ops.rs

The output shapes, defaults, and semantics here mirror the Rust crate
exactly so callers see equivalent behavior across both implementations.
"""

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


# Mirrors Rust file_ops.rs MAX_READ_SIZE / MAX_WRITE_SIZE.
MAX_READ_SIZE = 10 * 1024 * 1024
MAX_WRITE_SIZE = 10 * 1024 * 1024

# Mirrors Rust GLOB_SEARCH_IGNORED_DIRS — exact set, no additions.
IGNORED_DIRS = {
    ".git",
    "node_modules",
    ".build",
    "target",
    "dist",
    "coverage",
}


# ---------------------------------------------------------------------------
# Binary detection — Rust is_binary_file()
# ---------------------------------------------------------------------------

def _is_binary_file(path: Path) -> bool:
    """Read first 8192 bytes and check for NUL bytes (Rust parity)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Structured patch hunk — Rust StructuredPatchHunk
# ---------------------------------------------------------------------------

@dataclass
class StructuredPatchHunk:
    oldStart: int
    oldLines: int
    newStart: int
    newLines: int
    lines: list[str]


def _make_patch(original: str, updated: str) -> list[StructuredPatchHunk]:
    """Mirrors Rust make_patch()."""
    lines: list[str] = []
    for line in original.splitlines():
        lines.append(f"-{line}")
    for line in updated.splitlines():
        lines.append(f"+{line}")
    return [
        StructuredPatchHunk(
            oldStart=1,
            oldLines=len(original.splitlines()),
            newStart=1,
            newLines=len(updated.splitlines()),
            lines=lines,
        )
    ]


# ---------------------------------------------------------------------------
# Path normalization — Rust normalize_path / normalize_path_allow_missing
# ---------------------------------------------------------------------------

def _normalize_path(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=True)


def _normalize_path_allow_missing(path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        return candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        parent = candidate.parent
        try:
            canonical_parent = parent.resolve(strict=True)
        except (FileNotFoundError, OSError):
            canonical_parent = parent
        return canonical_parent / candidate.name


# ---------------------------------------------------------------------------
# read_file — Rust file_ops.rs read_file()
# ---------------------------------------------------------------------------

@dataclass
class TextFilePayload:
    filePath: str
    content: str
    numLines: int
    startLine: int
    totalLines: int


@dataclass
class ReadFileOutput:
    type: str
    file: TextFilePayload


def read_file(
    path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> ReadFileOutput:
    """
    Read a text file, optionally windowed by offset/limit.
    Ported from Rust: file_ops.rs read_file().

    Validation order matches Rust: metadata (existence + size) → binary → read.
    """
    absolute_path = _normalize_path(path)

    metadata = absolute_path.stat()
    if metadata.st_size > MAX_READ_SIZE:
        raise OSError(
            f"file is too large ({metadata.st_size} bytes, max {MAX_READ_SIZE} bytes)"
        )

    if _is_binary_file(absolute_path):
        raise OSError("file appears to be binary")

    content = absolute_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    start_index = min(offset or 0, len(lines))
    if limit is None:
        end_index = len(lines)
    else:
        end_index = min(start_index + limit, len(lines))
    selected = "\n".join(lines[start_index:end_index])

    return ReadFileOutput(
        type="text",
        file=TextFilePayload(
            filePath=str(absolute_path),
            content=selected,
            numLines=end_index - start_index,
            startLine=start_index + 1,
            totalLines=len(lines),
        ),
    )


# ---------------------------------------------------------------------------
# write_file — Rust file_ops.rs write_file()
# ---------------------------------------------------------------------------

@dataclass
class WriteFileOutput:
    type: str
    filePath: str
    content: str
    structuredPatch: list[StructuredPatchHunk]
    originalFile: str | None
    gitDiff: dict | None = None


def write_file(path: str, content: str) -> WriteFileOutput:
    """
    Write a file, creating parent dirs if needed.
    Ported from Rust: file_ops.rs write_file().
    """
    if len(content) > MAX_WRITE_SIZE:
        raise OSError(
            f"content is too large ({len(content)} bytes, max {MAX_WRITE_SIZE} bytes)"
        )

    absolute_path = _normalize_path_allow_missing(path)
    original_file: str | None
    try:
        original_file = absolute_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        original_file = None

    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(content, encoding="utf-8")

    return WriteFileOutput(
        type="update" if original_file is not None else "create",
        filePath=str(absolute_path),
        content=content,
        structuredPatch=_make_patch(original_file or "", content),
        originalFile=original_file,
        gitDiff=None,
    )


# ---------------------------------------------------------------------------
# edit_file — Rust file_ops.rs edit_file()
# ---------------------------------------------------------------------------

@dataclass
class EditFileOutput:
    filePath: str
    oldString: str
    newString: str
    originalFile: str
    structuredPatch: list[StructuredPatchHunk]
    userModified: bool
    replaceAll: bool
    gitDiff: dict | None = None


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> EditFileOutput:
    """
    Replace exact text in a file.
    Ported from Rust: file_ops.rs edit_file().
    """
    absolute_path = _normalize_path(path)
    original_file = absolute_path.read_text(encoding="utf-8")

    if old_string == new_string:
        raise ValueError("old_string and new_string must differ")

    if old_string not in original_file:
        raise FileNotFoundError("old_string not found in file")

    if replace_all:
        updated = original_file.replace(old_string, new_string)
    else:
        updated = original_file.replace(old_string, new_string, 1)

    absolute_path.write_text(updated, encoding="utf-8")

    return EditFileOutput(
        filePath=str(absolute_path),
        oldString=old_string,
        newString=new_string,
        originalFile=original_file,
        structuredPatch=_make_patch(original_file, updated),
        userModified=False,
        replaceAll=replace_all,
        gitDiff=None,
    )


# ---------------------------------------------------------------------------
# grep_search — Rust file_ops.rs grep_search()
# ---------------------------------------------------------------------------

@dataclass
class GrepSearchInput:
    pattern: str
    path: str | None = None
    glob: str | None = None
    output_mode: str | None = None
    before: int | None = None             # -B
    after: int | None = None              # -A
    context_short: int | None = None      # -C
    context: int | None = None
    line_numbers: bool | None = None      # -n (default True)
    case_insensitive: bool | None = None  # -i (default False)
    file_type: str | None = None
    head_limit: int | None = None
    offset: int | None = None
    multiline: bool | None = None         # toggles dot_matches_new_line


@dataclass
class GrepSearchOutput:
    mode: str | None
    numFiles: int
    filenames: list[str]
    content: str | None = None
    numLines: int | None = None
    numMatches: int | None = None
    appliedLimit: int | None = None
    appliedOffset: int | None = None


def grep_search(input: GrepSearchInput) -> GrepSearchOutput:
    """
    Recursive regex search across files.
    Ported from Rust: file_ops.rs grep_search().

    Mirrors Rust semantics:
      - case_insensitive defaults to False (case-sensitive).
      - multiline toggles dot_matches_new_line (re.DOTALL).
      - output_mode defaults to "files_with_matches".
      - context defaults to 0.
    """
    base_path = (
        _normalize_path(input.path) if input.path is not None
        else Path.cwd().resolve()
    )

    flags = 0
    if input.case_insensitive:
        flags |= re.IGNORECASE
    if input.multiline:
        flags |= re.DOTALL

    try:
        regex = re.compile(input.pattern, flags)
    except re.error as e:
        raise ValueError(str(e))

    output_mode = input.output_mode or "files_with_matches"
    context_value = input.context if input.context is not None else input.context_short
    context_value = context_value if context_value is not None else 0
    line_numbers = input.line_numbers if input.line_numbers is not None else True

    filenames: list[str] = []
    content_lines: list[str] = []
    total_matches = 0

    for file_path in _collect_search_files(base_path):
        if not _matches_optional_filters(file_path, input.glob, input.file_type):
            continue

        try:
            file_contents = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if output_mode == "count":
            count = len(regex.findall(file_contents))
            if count > 0:
                filenames.append(str(file_path))
                total_matches += count
            continue

        lines = file_contents.splitlines()
        matched_lines: list[int] = []
        for index, line in enumerate(lines):
            if regex.search(line):
                total_matches += 1
                matched_lines.append(index)

        if not matched_lines:
            continue

        filenames.append(str(file_path))

        if output_mode == "content":
            for index in matched_lines:
                start = max(
                    0,
                    index - (input.before if input.before is not None else context_value),
                )
                end = min(
                    len(lines),
                    index + (input.after if input.after is not None else context_value) + 1,
                )
                for current in range(start, end):
                    if line_numbers:
                        prefix = f"{file_path}:{current + 1}:"
                    else:
                        prefix = f"{file_path}:"
                    content_lines.append(f"{prefix}{lines[current]}")

    filenames, applied_limit, applied_offset = _apply_limit(
        filenames, input.head_limit, input.offset
    )

    if output_mode == "content":
        lines_out, content_limit, content_offset = _apply_limit(
            content_lines, input.head_limit, input.offset
        )
        return GrepSearchOutput(
            mode=output_mode,
            numFiles=len(filenames),
            filenames=filenames,
            content="\n".join(lines_out),
            numLines=len(lines_out),
            numMatches=None,
            appliedLimit=content_limit,
            appliedOffset=content_offset,
        )

    return GrepSearchOutput(
        mode=output_mode,
        numFiles=len(filenames),
        filenames=filenames,
        content=None,
        numLines=None,
        numMatches=total_matches if output_mode == "count" else None,
        appliedLimit=applied_limit,
        appliedOffset=applied_offset,
    )


# ---------------------------------------------------------------------------
# glob_search — Rust file_ops.rs glob_search()
# ---------------------------------------------------------------------------

@dataclass
class GlobSearchOutput:
    durationMs: int
    numFiles: int
    filenames: list[str]
    truncated: bool


def glob_search(pattern: str, path: str | None = None) -> GlobSearchOutput:
    """
    Find files by glob pattern.
    Ported from Rust: file_ops.rs glob_search().
    """
    started = time.monotonic()
    base_dir = _normalize_path(path) if path is not None else Path.cwd().resolve()

    if Path(pattern).is_absolute():
        search_pattern = pattern
    else:
        search_pattern = str(base_dir / pattern)

    expanded = _expand_braces(search_pattern)

    seen: set[str] = set()
    matches: list[Path] = []
    for pat in expanded:
        walk_root = _derive_glob_walk_root(pat)
        rel = pat[len(str(walk_root)):].lstrip("/\\") or "*"
        try:
            for candidate in walk_root.rglob(rel):
                if not candidate.is_file():
                    continue
                if any(part in IGNORED_DIRS for part in candidate.parts):
                    continue
                key = str(candidate)
                if key in seen:
                    continue
                seen.add(key)
                matches.append(candidate)
        except OSError as e:
            raise ValueError(str(e))

    matches.sort(
        key=lambda p: (p.stat().st_mtime if p.exists() else 0),
        reverse=True,
    )

    truncated = len(matches) > 100
    filenames = [str(p) for p in matches[:100]]

    return GlobSearchOutput(
        durationMs=int((time.monotonic() - started) * 1000),
        numFiles=len(filenames),
        filenames=filenames,
        truncated=truncated,
    )


def _expand_braces(pattern: str) -> list[str]:
    """Mirrors Rust expand_braces()."""
    open_pos = pattern.find("{")
    if open_pos == -1:
        return [pattern]

    close_pos = pattern.find("}", open_pos)
    if close_pos == -1:
        return [pattern]

    prefix = pattern[:open_pos]
    suffix = pattern[close_pos + 1:]
    alternatives = pattern[open_pos + 1:close_pos]

    result: list[str] = []
    for alt in alternatives.split(","):
        result.extend(_expand_braces(f"{prefix}{alt}{suffix}"))
    return result


def _derive_glob_walk_root(pattern: str) -> Path:
    """Mirrors Rust derive_glob_walk_root()."""
    parts = Path(pattern).parts
    prefix: list[str] = []
    for component in parts:
        if any(c in component for c in ("*", "?", "[")):
            break
        prefix.append(component)
    if prefix:
        return Path(*prefix)
    return Path.cwd()


# ---------------------------------------------------------------------------
# Search helpers — Rust collect_search_files / matches_optional_filters / apply_limit
# ---------------------------------------------------------------------------

def _collect_search_files(base_path: Path):
    if base_path.is_file():
        yield base_path
        return
    for root, dirs, files in os.walk(base_path):
        for name in files:
            yield Path(root) / name


def _matches_optional_filters(
    path: Path,
    glob_filter: str | None,
    file_type: str | None,
) -> bool:
    if glob_filter is not None:
        from fnmatch import fnmatch
        if not (fnmatch(str(path), glob_filter) or fnmatch(path.name, glob_filter)):
            return False
    if file_type is not None:
        ext = path.suffix.lstrip(".")
        if ext != file_type:
            return False
    return True


def _apply_limit(
    items: list,
    limit: int | None,
    offset: int | None,
) -> tuple[list, int | None, int | None]:
    """Mirrors Rust apply_limit() — default limit 250, 0 means unlimited."""
    offset_value = offset or 0
    sliced = items[offset_value:]
    explicit_limit = limit if limit is not None else 250
    applied_offset = offset_value if offset_value > 0 else None

    if explicit_limit == 0:
        return sliced, None, applied_offset

    truncated = len(sliced) > explicit_limit
    sliced = sliced[:explicit_limit]
    return sliced, (explicit_limit if truncated else None), applied_offset
