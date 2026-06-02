"""
Python port of the Rust `claw-rag-service` semantic search service.

Faithful reimplementation of:
  claw-rag-service/src/{chunk,embed,db,ingest,search,main,qdrant_index}.rs

Same constants and behavior:
  chunk window 900 / overlap 120, EMBED_BATCH 16, top_k default 8 (cap 64),
  480-char snippets, little-endian f32 BLOBs, CLAW_RAG_* env vars,
  deterministic 16-dim mock embeddings.
"""

from rag.chunk import chunk_text
from rag.embed import EmbedConfig, embed_batch, cosine_similarity, mock_vector_for_text
from rag.search import query_index, QueryRequest, QueryResponse, RagHit, default_top_k

__all__ = [
    "chunk_text",
    "EmbedConfig", "embed_batch", "cosine_similarity", "mock_vector_for_text",
    "query_index", "QueryRequest", "QueryResponse", "RagHit", "default_top_k",
]
