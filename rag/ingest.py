"""Ingest pipeline. Faithful port of claw-rag-service/src/ingest.rs.

Walks workspaces, chunks text files, embeds in batches, and stores vectors in
SQLite. Skips unchanged files via a content hash + size + mtime check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from rag import db
from rag.chunk import chunk_text
from rag.embed import EmbedConfig, embed_batch

# Constants — mirror ingest.rs
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB
CHUNK_CHARS = 900
CHUNK_OVERLAP = 120
EMBED_BATCH = 16

SKIP_DIR_NAMES = {".git", "target", "node_modules", "__pycache__", ".claw-rag"}

TEXT_EXTENSIONS = {
    "rs", "md", "toml", "txt", "json", "yaml", "yml", "js", "ts", "tsx", "jsx",
    "py", "go", "c", "h", "cpp", "hpp", "cs", "java", "kt", "swift", "rb", "php",
    "sh", "ps1", "html", "css", "sql",
}

try:  # blake3 matches Rust exactly; fall back to blake2b if unavailable
    import blake3 as _blake3

    def _content_hash(data: bytes) -> str:
        return _blake3.blake3(data).hexdigest()
except ImportError:  # pragma: no cover - fallback
    import hashlib

    def _content_hash(data: bytes) -> str:
        return hashlib.blake2b(data).hexdigest()


@dataclass
class IngestStats:
    files_indexed: int = 0
    chunks_total: int = 0
    embeddings_written: int = 0


def repo_id_for_workspace(workspace: Path) -> str:
    """`{dir_name}-{blake3(abs_path)[:8]}` — mirrors ingest.rs."""
    abs_path = str(Path(workspace).resolve())
    name = Path(abs_path).name or "workspace"
    return f"{name}-{_content_hash(abs_path.encode('utf-8'))[:8]}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iter_text_files(root: Path):
    """Walk `root`, skipping SKIP_DIR_NAMES, yielding text files <= max bytes."""
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if not path.is_file():
            continue
        ext = path.suffix.lstrip(".").lower()
        if ext not in TEXT_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > DEFAULT_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def run_ingest(
    workspaces: list[str | Path],
    db_path: str | Path,
    cfg: EmbedConfig,
) -> IngestStats:
    stats = IngestStats()
    con = db.connect(db_path)
    seen_paths: set[str] = set()

    try:
        for workspace in workspaces:
            ws = Path(workspace).resolve()
            repo_id = repo_id_for_workspace(ws)

            for file_path in _iter_text_files(ws):
                try:
                    raw = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue

                rel = file_path.relative_to(ws).as_posix()
                key = f"{repo_id}:{rel}"
                seen_paths.add(key)

                st = file_path.stat()
                content_hash = _content_hash(raw.encode("utf-8"))
                size_bytes = st.st_size
                mtime_ms = int(st.st_mtime * 1000)

                if db.file_is_unchanged(con, key, content_hash, size_bytes, mtime_ms):
                    continue

                # changed or new → reindex
                db.delete_file_and_chunks(con, key)
                chunks = chunk_text(raw, CHUNK_CHARS, CHUNK_OVERLAP)
                if not chunks:
                    db.upsert_file_meta(con, key, content_hash, size_bytes, mtime_ms, _now_ms())
                    continue

                ordinal = 0
                for batch_start in range(0, len(chunks), EMBED_BATCH):
                    batch = chunks[batch_start:batch_start + EMBED_BATCH]
                    vectors = embed_batch(cfg, batch)
                    for text, vec in zip(batch, vectors):
                        chunk_id = db.insert_chunk(con, key, ordinal, text)
                        db.insert_embedding(con, chunk_id, len(vec), vec)
                        stats.embeddings_written += 1
                        ordinal += 1

                db.upsert_file_meta(con, key, content_hash, size_bytes, mtime_ms, _now_ms())
                stats.files_indexed += 1
                stats.chunks_total += len(chunks)

        # cleanup: drop entries for files no longer present
        for existing in db.list_all_files(con):
            if existing not in seen_paths:
                db.delete_file_and_chunks(con, existing)

        con.commit()
    finally:
        con.close()

    return stats
