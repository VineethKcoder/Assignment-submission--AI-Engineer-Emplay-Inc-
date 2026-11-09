"""Query understanding: synonym expansion (config-driven) and optional LLM rewriting."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger("rfp.search.query")


@dataclass
class ExpandedQuery:
    original: str
    expansions: list[str] = field(default_factory=list)
    rewrites: list[str] = field(default_factory=list)

    @property
    def keyword_query(self) -> str:
        return " ".join([self.original, *self.expansions, *self.rewrites])

    @property
    def dense_queries(self) -> list[str]:
        out = [self.original, *self.rewrites]
        if self.expansions:
            out.append(f"{self.original} ({', '.join(self.expansions[:6])})")
        return out


@lru_cache(maxsize=4)
def _load_groups(path: str) -> list[list[str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return [[t.lower() for t in g] for g in data.get("groups", [])]
    except FileNotFoundError:
        logger.warning("synonyms file %s not found; expansion disabled", path)
        return []


class QueryExpander:
    def __init__(self, synonyms_path: str, max_terms: int = 10):
        self.groups = _load_groups(synonyms_path)
        self.max_terms = max_terms

    def expand(self, query: str, rewrites: list[str] | None = None) -> ExpandedQuery:
        q = query.lower()
        extra: list[str] = []
        for group in self.groups:
            if any(re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", q) for t in group):
                extra.extend(t for t in group if t not in q and t not in extra)
        return ExpandedQuery(original=query, expansions=extra[: self.max_terms], rewrites=list(rewrites or []))


class QueryRewrites(BaseModel):
    queries: list[str] = Field(default_factory=list, description="2-3 alternative search queries")


REWRITE_SYSTEM = (
    "You rewrite search queries for a retrieval engine over RFP / bid documents. Produce 2-3 short alternative "
    "queries that use the vocabulary procurement documents typically use (e.g. 'deadline' -> 'proposal due date and "
    "time'). Do not answer the question. Output JSON: {\"queries\": [..]}"
)


def llm_rewrite(llm, query: str, span=None) -> list[str]:
    """LLM-based query rewriting (used by the Retrieval Agent on retries)."""
    try:
        out = llm.chat_json(REWRITE_SYSTEM, f"Query: {query}", QueryRewrites, span=span, max_tokens=400)
        return [q for q in out.queries if q and q.strip()][:3]
    except Exception as exc:  # rewriting is optional
        logger.warning("query rewrite failed: %s", exc)
        return []
