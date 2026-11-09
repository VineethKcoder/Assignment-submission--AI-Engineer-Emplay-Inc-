"""Cross-encoder re-ranking."""

from __future__ import annotations

import logging
import math
import threading
from typing import Protocol

logger = logging.getLogger("rfp.search.rerank")


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


class CrossEncoderReranker:
    def __init__(self, model_name: str, device: str = "cpu", max_length: int = 512):
        from sentence_transformers import CrossEncoder

        logger.info("Loading reranker %s on %s", model_name, device)
        self.model = CrossEncoder(model_name, device=device, max_length=max_length)
        self._lock = threading.Lock()  # serialize inference from parallel agents
        self.name = model_name

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        with self._lock:
            raw = self.model.predict([(query, t) for t in texts], batch_size=32, show_progress_bar=False)
        scores = [float(s) for s in raw]
        # normalise to [0,1] so scores are comparable / usable as confidence signal
        if any(s < 0 or s > 1 for s in scores):
            scores = [_sigmoid(s) for s in scores]
        return scores


class KeywordOverlapReranker:
    """Cheap lexical reranker used in tests (no model download)."""

    name = "keyword-overlap"

    def score(self, query: str, texts: list[str]) -> list[float]:
        from search.bm25 import tokenize

        q = set(tokenize(query))
        out = []
        for t in texts:
            d = set(tokenize(t))
            out.append(len(q & d) / (len(q) or 1))
        return out
