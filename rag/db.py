"""SQLite vector store. Faithful port of claw-rag-service/src/db.rs.

Vectors are stored as little-endian f32 BLOBs. Schema and serialization match
the Rust service so an index produced by either side is interchangeable.
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE(path, ordinal)
);
CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id INTEGER PRIMARY KEY,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime_ms INTEGER NOT NULL,
    indexed_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
"""


@dataclass
class ChunkRow:
    path: str
    text: str
    vec: list[float]


def f32_slice_to_blob(v: list[float]) -> bytes:
    """Little-endian 4-byte floats concatenated (db.rs f32_slice_to_blob)."""
    return struct.pack(f"<{len(v)}f", *v)


def blob_to_f32_vec(blob: bytes, dim: int) -> list[float] | None:
    """Inverse of f32_slice_to_blob; None if length mismatches (db.rs)."""
    if len(blob) != dim * 4:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(_SCHEMA)
    return con


def insert_chunk(con: sqlite3.Connection, path: str, ordinal: int, text: str) -> int:
    cur = con.execute(
        "INSERT INTO chunks (path, ordinal, text) VALUES (?, ?, ?)",
        (path, ordinal, text),
    )
    return int(cur.lastrowid)


def insert_embedding(con: sqlite3.Connection, chunk_id: int, dim: int, vec: list[float]) -> None:
    con.execute(
        "INSERT OR REPLACE INTO embeddings (chunk_id, dim, vec) VALUES (?, ?, ?)",
        (chunk_id, dim, f32_slice_to_blob(vec)),
    )


def delete_file_and_chunks(con: sqlite3.Connection, path: str) -> None:
    con.execute("DELETE FROM chunks WHERE path = ?", (path,))
    con.execute("DELETE FROM files WHERE path = ?", (path,))


def file_is_unchanged(
    con: sqlite3.Connection, path: str, content_hash: str, size_bytes: int, mtime_ms: int
) -> bool:
    row = con.execute(
        "SELECT content_hash, size_bytes, mtime_ms FROM files WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
        return False
    return row[0] == content_hash and int(row[1]) == size_bytes and int(row[2]) == mtime_ms


def upsert_file_meta(
    con: sqlite3.Connection,
    path: str,
    content_hash: str,
    size_bytes: int,
    mtime_ms: int,
    indexed_at_ms: int,
) -> None:
    con.execute(
        "INSERT OR REPLACE INTO files "
        "(path, content_hash, size_bytes, mtime_ms, indexed_at_ms) "
        "VALUES (?, ?, ?, ?, ?)",
        (path, content_hash, size_bytes, mtime_ms, indexed_at_ms),
    )


def list_all_files(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT path FROM files").fetchall()]


def chunk_count(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COUNT(*) FROM chunks").fetchone()
    return int(row[0]) if row else 0


def load_all_indexed(con: sqlite3.Connection) -> list[ChunkRow]:
    rows = con.execute(
        "SELECT c.path, c.text, e.dim, e.vec "
        "FROM chunks c JOIN embeddings e ON e.chunk_id = c.id"
    ).fetchall()
    out: list[ChunkRow] = []
    for path, text, dim, blob in rows:
        vec = blob_to_f32_vec(blob, int(dim))
        if vec is not None:
            out.append(ChunkRow(path=path, text=text, vec=vec))
    return out
