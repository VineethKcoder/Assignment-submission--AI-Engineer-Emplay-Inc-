"""Rank fusion utilities."""

from __future__ import annotations

from collections import defaultdict


def reciprocal_rank_fusion(rankings: list[list[int]], k: int = 60, weights: list[float] | None = None) -> list[tuple[int, float]]:
    """Fuse several ranked lists of doc ids with RRF: score = sum w / (k + rank).

    RRF is scale-free (no need to normalise BM25 vs cosine scores) and robust
    when one retriever returns nothing.
    """
    weights = weights or [1.0] * len(rankings)
    scores: dict[int, float] = defaultdict(float)
    for ranking, w in zip(rankings, weights):
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] += w / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])
