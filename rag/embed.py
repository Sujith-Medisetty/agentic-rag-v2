"""Embeddings + cosine similarity. Faithful port of claw-rag-service/src/embed.rs.

Supports an OpenAI-compatible /embeddings endpoint and a deterministic mock
provider (CLAW_RAG_MOCK_PROVIDERS=1) producing 16-dim unit vectors.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass

_MOCK_DIM = 16
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "text-embedding-3-small"


@dataclass
class EmbedConfig:
    api_key: str
    base_url: str
    model: str
    mock: bool = False

    @classmethod
    def from_env(cls) -> "EmbedConfig":
        """Mirror EmbedConfig::mock_from_env then ::from_env."""
        if os.getenv("CLAW_RAG_MOCK_PROVIDERS") == "1":
            return cls(api_key="mock", base_url="mock://", model="mock-embedding", mock=True)
        api_key = os.getenv("CLAW_RAG_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        base_url = os.getenv("CLAW_RAG_EMBEDDING_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
        model = os.getenv("CLAW_RAG_EMBEDDING_MODEL", _DEFAULT_MODEL)
        return cls(api_key=api_key, base_url=base_url, model=model, mock=False)


def mock_vector_for_text(s: str) -> list[float]:
    """Deterministic 16-dim unit vector. Mirrors embed.rs mock_vector_for_text:
    accumulate first 64 bytes into v[i % 16] += b/255, then L2-normalize."""
    v = [0.0] * _MOCK_DIM
    for i, b in enumerate(s.encode("utf-8")):
        if i >= _MOCK_DIM * 4:  # take(DIM * 4) == first 64 bytes
            break
        v[i % _MOCK_DIM] += b / 255.0
    norm = math.sqrt(sum(x * x for x in v))
    if norm > 0.0:
        v = [x / norm for x in v]
    return v


def embed_batch(cfg: EmbedConfig, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one vector per input, in order.

    Mirrors embed.rs embed_batch (POST {base}/embeddings, {model, input:[...]},
    bearer auth, error on count mismatch).
    """
    if not texts:
        return []

    if cfg.mock:
        return [mock_vector_for_text(t) for t in texts]

    payload = json.dumps({"model": cfg.model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.base_url}/embeddings",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    data = body.get("data", [])
    vectors = [item.get("embedding", []) for item in data]
    if len(vectors) != len(texts):
        raise RuntimeError(
            f"embedding count mismatch: got {len(vectors)} for {len(texts)} inputs"
        )
    return vectors


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """dot(a,b) / (||a|| * ||b||); 0.0 on empty / length-mismatch / zero-norm.

    Mirrors embed.rs cosine_similarity exactly.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
