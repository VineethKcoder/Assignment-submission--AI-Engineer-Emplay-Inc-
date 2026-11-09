"""Retrieval / Search Agent.

Single responsibility: turn an information need (a field spec or a QA
sub-query) into ranked, cited evidence by calling the search engine tool.
It rewrites/expands queries, applies metadata filters, relies on the
engine's hybrid search + re-ranking and returns `Evidence` objects.
On retries it broadens the search (LLM query rewrites, validator feedback,
more candidates, no doc-type filter).
"""

from __future__ import annotations

import logging
import time

from agents.fields import FieldSpec
from agents.schemas import Evidence, SubQuery
from agents.services import Services
from core.tracing import Span
from search.engine import SearchResult
from search.query import llm_rewrite

logger = logging.getLogger("rfp.agents.retrieval")


def to_evidence(r: SearchResult, query: str = "") -> Evidence:
    return Evidence(
        chunk_id=r.chunk_id, bid_id=r.bid_id, file=r.file_name, page=r.page_number, page_end=r.page_end,
        doc_type=r.doc_type, addendum_number=r.addendum_number, section=r.section, text=r.text,
        score=float(r.scores.get("rerank", r.score)), query=query,
    )


class RetrievalAgent:
    name = "retrieval_agent"

    def __init__(self, services: Services):
        self.s = services

    def _search(self, span: Span | None, query: str, **kw) -> list[SearchResult]:
        t0 = time.perf_counter()
        try:
            res = self.s.engine.search(query, **kw)
        except Exception as exc:  # empty retrieval must not crash the pipeline
            logger.exception("search failed")
            if span:
                span.tool_call("search_engine.search", {"query": query, **kw}, f"ERROR {exc}")
            return []
        if span:
            span.tool_call(
                "search_engine.search",
                {"query": query, **{k: v for k, v in kw.items() if v is not None and k != "rewrites"},
                 "n_rewrites": len(kw.get("rewrites") or [])},
                [f"{r.citation} ({r.scores.get('rerank', r.score):.3f})" for r in res],
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
        return res

    def has_addenda(self, bid_id: str) -> bool:
        return any(c.doc_type == "addendum" for c in self.s.engine.get_chunks(bid_id=bid_id, doc_type="addendum")[:1])

    # ------------------------------------------------------------ extraction
    def gather_for_field(self, bid_id: str, spec: FieldSpec, span: Span | None = None, broaden: bool = False,
                         feedback: str | None = None) -> list[Evidence]:
        k = spec.evidence_k or self.s.settings.evidence_per_field
        queries = list(spec.queries)
        rewrites = queries[1:]
        if broaden:
            k += 2
            rewrites += llm_rewrite(self.s.llm, f"{spec.name}: {spec.description}", span=span)
            if feedback:
                rewrites.append(f"{spec.name} {feedback[:150]}")
        results = self._search(span, queries[0], bid_id=bid_id, top_k=k + 2, rewrites=rewrites,
                               rerank_query=spec.rerank_query)
        focus_q = " ".join([spec.name.replace("_", " "), *queries])  # used to focus long passages
        evid: dict[str, Evidence] = {}
        for r in results:
            evid[r.chunk_id] = to_evidence(r, focus_q)
        # safety net against re-ranker misses: keep the top hybrid (pre-rerank) hits too. Keyword-heavy
        # passages such as price tables score high in BM25 but can be under-rated by small cross-encoders.
        safety = []
        if self.s.settings.default_search_mode == "hybrid_rerank":
            for r in self._search(span, queries[0], bid_id=bid_id, top_k=2, rewrites=rewrites, mode="hybrid"):
                if r.chunk_id not in evid:
                    e = to_evidence(r, focus_q)
                    e.score = 0.05  # unknown relevance - ranked after re-ranked evidence
                    evid[r.chunk_id] = e
                    safety.append(e)
        if spec.addendum_sensitive and self.has_addenda(bid_id):
            for r in self._search(span, queries[0], bid_id=bid_id, doc_type="addendum", top_k=2,
                                  rewrites=rewrites[:2], rerank_query=spec.rerank_query):
                if r.chunk_id not in evid:
                    evid[r.chunk_id] = to_evidence(r, focus_q)
        ranked = sorted(evid.values(), key=lambda e: -e.score)
        # keep the top-k plus any addendum passages (they may override the base RFP)
        reranked = [e for e in ranked if e not in safety]
        addenda = [e for e in reranked[k:] if e.doc_type == "addendum"][:2]
        # order matters: the extraction pool is filled round-robin by rank, so safety-net hits go right
        # after the two best re-ranked passages instead of at the tail
        return reranked[:2] + safety + reranked[2:k] + addenda

    # ------------------------------------------------------------ QA
    def gather_for_query(self, sq: SubQuery, span: Span | None = None, k: int | None = None,
                         broaden: bool = False) -> list[Evidence]:
        k = k or self.s.settings.qa_evidence_k
        rewrites: list[str] = []
        doc_type = sq.doc_type
        if broaden:
            rewrites = llm_rewrite(self.s.llm, sq.query, span=span)
            doc_type = None if sq.addendum_number is None else doc_type
            k += 3
        res = self._search(span, sq.query, bid_id=sq.bid_id, doc_type=doc_type,
                           addendum_number=sq.addendum_number, top_k=k, rewrites=rewrites)
        if not res and (doc_type or sq.addendum_number is not None):
            # filter too strict -> retry without doc filters
            res = self._search(span, sq.query, bid_id=sq.bid_id, top_k=k, rewrites=rewrites)
        return [to_evidence(r, sq.query) for r in res]
