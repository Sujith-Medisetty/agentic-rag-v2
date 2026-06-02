"""Optional Qdrant backend. Faithful port of claw-rag-service/src/qdrant_index.rs.

Enabled only when CLAW_RAG_QDRANT_URL is set AND the qdrant-client package is
installed. query_qdrant() returns None to signal "fall back to SQLite", matching
the Rust feature-gated behavior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_COLLECTION = "claw_rag_chunks"
TOP_K_CAP = 64
SNIPPET_CHARS = 480


@dataclass
class QdrantConfig:
    url: str
    api_key: str | None
    collection: str

    @classmethod
    def from_env(cls) -> "QdrantConfig | None":
        url = os.getenv("CLAW_RAG_QDRANT_URL")
        if not url:
            return None
        return cls(
            url=url,
            api_key=os.getenv("CLAW_RAG_QDRANT_API_KEY"),
            collection=os.getenv("CLAW_RAG_QDRANT_COLLECTION", DEFAULT_COLLECTION),
        )


def query_qdrant(query_vec: list[float], top_k: int):
    """Return a QueryResponse(phase="2-qdrant") or None to fall back to SQLite."""
    cfg = QdrantConfig.from_env()
    if cfg is None:
        return None
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        return None

    from rag.search import QueryResponse, RagHit, truncate_snippet

    client = QdrantClient(url=cfg.url, api_key=cfg.api_key)
    try:
        if not client.collection_exists(cfg.collection):
            return None
        result = client.query_points(
            collection_name=cfg.collection,
            query=query_vec,
            limit=min(top_k, TOP_K_CAP),
            with_payload=True,
        )
    except Exception:
        return None

    hits = []
    for point in getattr(result, "points", []) or []:
        payload = point.payload or {}
        hits.append(RagHit(
            path=str(payload.get("path", "")),
            snippet=truncate_snippet(str(payload.get("text", "")), SNIPPET_CHARS),
            score=getattr(point, "score", None),
        ))
    return QueryResponse(hits=hits, phase="2-qdrant")
