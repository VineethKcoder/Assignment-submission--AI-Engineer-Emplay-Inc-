"""Embedding models.

`SentenceTransformerEmbedder` wraps any sentence-transformers model (default
BAAI/bge-base-en-v1.5 on CUDA when available). `HashingEmbedder` is a tiny,
dependency-free fallback used by unit tests and CI.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Protocol

import numpy as np

logger = logging.getLogger("rfp.search.embed")


class Embedder(Protocol):
    name: str
    dim: int

    def encode_documents(self, texts: list[str]) -> np.ndarray: ...

    def encode_queries(self, texts: list[str]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, device: str = "cpu", query_prefix: str = "", batch_size: int = 32):
        from sentence_transformers import SentenceTransformer

        logger.info("Loading embedding model %s on %s", model_name, device)
        self.model = SentenceTransformer(model_name, device=device)
        self._lock = threading.Lock()  # one shared model, many parallel agents -> serialize inference
        self.name = model_name
        self.query_prefix = query_prefix
        self.batch_size = batch_size
        get_dim = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self.dim = int(get_dim())

    def _encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        with self._lock:
            emb = self.model.encode(
                texts, batch_size=self.batch_size, convert_to_numpy=True, normalize_embeddings=True,
                show_progress_bar=len(texts) > 64,
            )
        return emb.astype(np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode([self.query_prefix + t for t in texts])


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder (no model download).

    Not semantic, but good enough to exercise the full pipeline in tests.
    """

    def __init__(self, dim: int = 256):
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self.encode_documents(texts)
