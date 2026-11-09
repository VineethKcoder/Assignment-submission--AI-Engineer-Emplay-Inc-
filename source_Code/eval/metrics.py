"""Retrieval metrics (binary relevance)."""

from __future__ import annotations

import math


def is_relevant(result: dict, relevant: list[dict]) -> bool:
    for rel in relevant:
        if result["file_name"] == rel["file"]:
            pages = set(range(result["page_number"], result["page_end"] + 1))
            if pages & set(rel["pages"]):
                return True
    return False


def first_hit_rank(results: list[dict], relevant: list[dict]) -> int | None:
    for i, r in enumerate(results, 1):
        if is_relevant(r, relevant):
            return i
    return None


def recall_at_k(ranks: list[int | None], k: int) -> float:
    """Fraction of questions with at least one relevant passage in the top k (hit rate)."""
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def mrr(ranks: list[int | None], k: int = 10) -> float:
    return sum(1 / r for r in ranks if r is not None and r <= k) / len(ranks) if ranks else 0.0


def ndcg_at_k(rel_lists: list[list[bool]], k: int = 10) -> float:
    """Binary nDCG@k; the ideal ranking puts all retrieved relevant passages first."""
    scores = []
    for rels in rel_lists:
        rels = rels[:k]
        dcg = sum(1 / math.log2(i + 2) for i, r in enumerate(rels) if r)
        n_rel = max(1, sum(rels)) if any(rels) else 1
        idcg = sum(1 / math.log2(i + 2) for i in range(n_rel))
        scores.append(dcg / idcg if any(rels) else 0.0)
    return sum(scores) / len(scores) if scores else 0.0
