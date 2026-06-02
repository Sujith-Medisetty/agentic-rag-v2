"""Character sliding-window chunker. Faithful port of claw-rag-service/src/chunk.rs."""

from __future__ import annotations


def chunk_text(text: str, max_chars: int, overlap: int) -> list[str]:
    """Split text into overlapping windows of `max_chars` characters.

    Mirrors Rust chunk_text exactly:
      * UTF-8 codepoint based (Python str chars == Rust chars()),
      * clamp overlap to max_chars - 1,
      * step = max(max_chars - overlap, 1),
      * skip windows that are empty after .strip(),
      * stop once the window reaches the end.
    """
    if max_chars == 0:
        return []

    chars = list(text)
    overlap = min(overlap, max_chars - 1)
    out: list[str] = []
    start = 0
    n = len(chars)

    while True:
        end = min(start + max_chars, n)
        piece = "".join(chars[start:end])
        if piece.strip():
            out.append(piece)
        if end >= n:
            break
        step = max(max_chars - overlap, 1)
        start += step

    return out
