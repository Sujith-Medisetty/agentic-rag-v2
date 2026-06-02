"""Search. Faithful port of claw-rag-service/src/search.rs.

Embeds the query, scores every indexed chunk by cosine similarity, returns the
top-k (capped at 64) with 480-char snippets. Reports a `phase` string matching
the Rust service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rag import db
from rag.embed import EmbedConfig, embed_batch, cosine_similarity

TOP_K_CAP = 64
SNIPPET_CHARS = 480


def default_top_k() -> int:
    return 8


@dataclass
class RagHit:
    path: str
    snippet: str
    score: float | None = None

    def to_dict(self) -> dict:
        d = {"path": self.path, "snippet": self.snippet}
        if self.score is not None:
            d["score"] = self.score
        return d


@dataclass
class QueryRequest:
    query: str
    top_k: int = field(default_factory=default_top_k)


@dataclass
class QueryResponse:
    hits: list[RagHit]
    phase: str

    def to_dict(self) -> dict:
        return {"hits": [h.to_dict() for h in self.hits], "phase": self.phase}


def truncate_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Mirror search.rs truncate_snippet: cut to `limit` chars + '…' ellipsis."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def query_index(db_path: str | Path, cfg: EmbedConfig, req: QueryRequest) -> QueryResponse:
    db_path = Path(db_path)
    if not db_path.is_file():
        return QueryResponse(hits=[], phase="1-sqlite-no-db")

    qvecs = embed_batch(cfg, [req.query])
    if not qvecs:
        return QueryResponse(hits=[], phase="1-sqlite-no-db")
    q = qvecs[0]

    # Optional Qdrant backend (CLAW_RAG_QDRANT_URL + qdrant-client). Returns None
    # to fall back to the SQLite linear scan, mirroring search.rs.
    try:
        from rag.qdrant_index import query_qdrant
        qresp = query_qdrant(q, req.top_k)
        if qresp is not None:
            return qresp
    except Exception:
        pass

    con = db.connect(db_path)
    try:
        rows = db.load_all_indexed(con)
    finally:
        con.close()

    if not rows:
        return QueryResponse(hits=[], phase="1-sqlite-empty")

    scored = sorted(
        ((cosine_similarity(q, row.vec), i) for i, row in enumerate(rows)),
        key=lambda t: t[0],
        reverse=True,
    )

    top = min(req.top_k, TOP_K_CAP)
    hits = [
        RagHit(
            path=rows[i].path,
            snippet=truncate_snippet(rows[i].text, SNIPPET_CHARS),
            score=score,
        )
        for score, i in scored[:top]
    ]
    return QueryResponse(hits=hits, phase="1-sqlite")
