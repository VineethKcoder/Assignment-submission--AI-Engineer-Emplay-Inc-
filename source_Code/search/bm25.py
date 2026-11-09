"""Keyword retrieval (BM25) with an identifier-preserving tokenizer.

Bid numbers, SKUs and model numbers (`JA-207652`, `210-BLYZ`, `WD22TB4`,
`#E20P4600040`) must match exactly, so the tokenizer keeps compound tokens
intact *and* also emits their parts (`ja-207652` -> `ja-207652`, `ja`,
`207652`). Plain words get light suffix stemming so "bonds" matches "bond".
"""

from __future__ import annotations

import re

import numpy as np
from rank_bm25 import BM25Okapi

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[-./#][a-z0-9]+)*")
STOPWORDS = frozenset(
    """a an the and or of to in on for by with at from is are was were be been being this that these those it its
    as if then than such any all each other into within without per via shall will would should may might must can
    could do does did not no nor so up out over under about above below between which who whom whose what when
    where why how there their them they we our you your he she his her i me my""".split()
)


def _stem(tok: str) -> str:
    if tok.isdigit() or len(tok) <= 4:
        return tok
    for suf in ("ies", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            if suf == "ies":
                return tok[:-3] + "y"
            if suf == "es" and tok[-3] in "sxz":
                return tok[:-2]
            if suf == "s" and not tok.endswith("ss"):
                return tok[:-1]
    return tok


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text.lower()):
        tok = m.group(0).strip(".")
        if not tok:
            continue
        if re.search(r"[-./#]", tok):
            tokens.append(tok)  # full identifier
            tokens.extend(p for p in re.split(r"[-./#]", tok) if p and p not in STOPWORDS)
        elif tok not in STOPWORDS:
            tokens.append(_stem(tok))
    return tokens


class BM25Index:
    def __init__(self, texts: list[str]):
        self.tokenized = [tokenize(t) for t in texts]
        # BM25Okapi breaks on an empty corpus; guard with a placeholder
        self.bm25 = BM25Okapi(self.tokenized if any(self.tokenized) else [["__empty__"]])
        self.size = len(texts)

    def scores(self, query: str) -> np.ndarray:
        q = tokenize(query)
        if not q or self.size == 0:
            return np.zeros(self.size, dtype=np.float32)
        return np.asarray(self.bm25.get_scores(q), dtype=np.float32)[: self.size]

    def search(self, query: str, top_k: int, allowed: np.ndarray | None = None) -> list[tuple[int, float]]:
        s = self.scores(query)
        if allowed is not None:
            mask = np.zeros_like(s, dtype=bool)
            mask[allowed] = True
            s = np.where(mask, s, -np.inf)
        order = np.argsort(-s)[:top_k]
        return [(int(i), float(s[i])) for i in order if np.isfinite(s[i]) and s[i] > 0]
