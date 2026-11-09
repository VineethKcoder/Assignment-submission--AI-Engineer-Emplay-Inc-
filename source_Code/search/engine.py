"""Hybrid RAG search engine.

    query --> QueryExpander (synonyms [+ LLM rewrites])
          --> dense search (FAISS, one list per dense query)   \
          --> BM25 keyword search (identifier-aware tokens)     >-- RRF fusion
          --> metadata filter (bid_id / doc_type / addendum / file) applied inside both retrievers
          --> cross-encoder re-ranking of the top fused candidates
          --> SearchResult list with file, page, section, text and all scores (citations)

Usable standalone (CLI / REST API) and as the tool every agent calls.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field

from core.settings import Settings
from ingestion.models import Chunk, IngestionReport
from ingestion.pipeline import bid_id_from_folder, discover_files, file_sha1, ingest_folder
from search.bm25 import BM25Index
from search.fusion import reciprocal_rank_fusion
from search.query import QueryExpander
from search.store import IndexStore

logger = logging.getLogger("rfp.search")

SearchMode = Literal["bm25", "dense", "hybrid", "hybrid_rerank"]


class SearchResult(BaseModel):
    chunk_id: str
    bid_id: str
    file_name: str
    doc_type: str
    addendum_number: int | None = None
    page_number: int
    page_end: int
    section: str = ""
    chunk_type: str = "text"
    text: str
    score: float
    scores: dict[str, float] = Field(default_factory=dict)

    @property
    def citation(self) -> str:
        pages = f"p.{self.page_number}" if self.page_end == self.page_number else f"pp.{self.page_number}-{self.page_end}"
        return f"{self.file_name}, {pages}"


class SearchEngine:
    def __init__(self, settings: Settings, embedder=None, reranker=None):
        self.settings = settings
        self._embedder = embedder
        self._reranker = reranker
        self.store = IndexStore(settings.path(settings.index_dir))
        self.expander = QueryExpander(str(settings.path(settings.synonyms_config)))
        self._lock = threading.RLock()
        self._model_lock = threading.Lock()  # models are lazy-loaded once, even from parallel agents
        self.chunks: list[Chunk] = []
        self.embeddings = np.zeros((0, 1), dtype=np.float32)
        self.manifests: dict[str, dict] = {}
        self._faiss = None
        self._bm25: BM25Index | None = None
        self._loaded = False

    # ------------------------------------------------------------ models
    @property
    def embedder(self):
        if self._embedder is None:
            with self._model_lock:
                if self._embedder is None:
                    from search.embeddings import SentenceTransformerEmbedder

                    s = self.settings
                    self._embedder = SentenceTransformerEmbedder(
                        s.embedding_model, device=s.resolved_device, query_prefix=s.query_prefix,
                        batch_size=s.embedding_batch_size,
                    )
        return self._embedder

    @property
    def reranker(self):
        if self._reranker is None:
            with self._model_lock:
                if self._reranker is None:
                    from search.reranker import CrossEncoderReranker

                    self._reranker = CrossEncoderReranker(self.settings.reranker_model, device=self.settings.resolved_device)
        return self._reranker

    def warmup(self) -> None:
        """Load models + index up-front (avoids first-query latency inside parallel agents)."""
        self._ensure_loaded()
        _ = self.embedder
        if self.settings.default_search_mode == "hybrid_rerank":
            _ = self.reranker

    @property
    def embedding_model_name(self) -> str:
        return self._embedder.name if self._embedder is not None else self.settings.embedding_model

    # ------------------------------------------------------------ indexing
    def index_folder(self, folder: str | Path, force: bool = False, bid_id: str | None = None) -> IngestionReport:
        """Ingest + embed one bid folder. Incremental: unchanged bids are skipped and
        other bids' shards are never touched."""
        folder = Path(folder)
        bid_id = bid_id or bid_id_from_folder(folder)
        files, _ = discover_files(folder) if folder.is_dir() else ([], [])
        hashes = {f.name: file_sha1(f) for f in files}
        if not force and hashes and self.store.is_up_to_date(bid_id, hashes, self.embedding_model_name):
            logger.info("Bid %s is up to date - skipping re-index", bid_id)
            m = self.store.manifest(bid_id) or {}
            rep = IngestionReport.model_validate(m.get("ingestion_report", {"bid_id": bid_id, "folder": str(folder)}))
            rep.status = "up_to_date"
            self._ensure_loaded()
            return rep

        t0 = time.perf_counter()
        docs, chunks, report = ingest_folder(folder, self.settings, bid_id=bid_id)
        if not chunks:
            return report
        texts = [c.index_text() for c in chunks]
        embeddings = self.embedder.encode_documents(texts)
        report.seconds = round(time.perf_counter() - t0, 2)
        self.store.save(
            bid_id,
            chunks,
            embeddings,
            {
                "folder": str(folder.resolve()),
                "file_hashes": hashes,
                "embedding_model": self.embedding_model_name,
                "documents": report.documents,
                "ingestion_report": report.model_dump(),
            },
        )
        self.refresh()
        return report

    def delete_bid(self, bid_id: str) -> bool:
        ok = self.store.delete(bid_id)
        self.refresh()
        return ok

    def refresh(self) -> None:
        with self._lock:
            self.chunks, self.embeddings, self.manifests = self.store.load_all(self.embedding_model_name)
            self._bm25 = BM25Index([c.index_text() for c in self.chunks]) if self.chunks else None
            self._faiss = None
            if self.chunks:
                try:
                    import faiss

                    index = faiss.IndexFlatIP(self.embeddings.shape[1])
                    index.add(np.ascontiguousarray(self.embeddings, dtype=np.float32))
                    self._faiss = index
                except Exception as exc:  # numpy fallback
                    logger.warning("FAISS unavailable (%s); using numpy search", exc)
            self._meta_bid = np.array([c.bid_id for c in self.chunks])
            self._meta_type = np.array([c.doc_type for c in self.chunks])
            self._meta_add = np.array([c.addendum_number if c.addendum_number is not None else -1 for c in self.chunks])
            self._meta_file = np.array([c.file_name for c in self.chunks])
            self._loaded = True
            logger.info("Search index ready: %d chunks across %d bids", len(self.chunks), len(self.manifests))

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.refresh()

    # ------------------------------------------------------------ catalog
    def list_bids(self) -> list[str]:
        self._ensure_loaded()
        return sorted(self.manifests)

    def catalog(self) -> list[dict]:
        """Bid -> documents overview (used by the planner to resolve 'the Dell bid' -> Bid2)."""
        self._ensure_loaded()
        out = []
        for bid, m in sorted(self.manifests.items()):
            docs = [
                {k: d.get(k) for k in ("file", "doc_type", "addendum_number", "doc_date", "pages", "title")}
                for d in m.get("documents", [])
            ]
            out.append({"bid_id": bid, "documents": docs, "chunks": m.get("chunk_count", 0)})
        return out

    # ------------------------------------------------------------ filtering
    def _allowed(self, bid_id=None, doc_type=None, addendum_number=None, file_name=None) -> np.ndarray | None:
        if not any(v is not None for v in (bid_id, doc_type, addendum_number, file_name)):
            return None
        mask = np.ones(len(self.chunks), dtype=bool)

        def _in(arr, value):
            values = value if isinstance(value, (list, tuple, set)) else [value]
            return np.isin(arr, list(values))

        if bid_id is not None:
            mask &= _in(self._meta_bid, bid_id)
        if doc_type is not None:
            mask &= _in(self._meta_type, doc_type)
        if addendum_number is not None:
            mask &= _in(self._meta_add, addendum_number)
        if file_name is not None:
            mask &= _in(self._meta_file, file_name)
        return np.nonzero(mask)[0]

    def get_chunks(self, bid_id=None, doc_type=None, addendum_number=None, file_name=None) -> list[Chunk]:
        self._ensure_loaded()
        allowed = self._allowed(bid_id, doc_type, addendum_number, file_name)
        idx = range(len(self.chunks)) if allowed is None else allowed
        return [self.chunks[i] for i in idx]

    # ------------------------------------------------------------ retrieval
    def _dense(self, queries: list[str], k: int, allowed: np.ndarray | None) -> list[list[int]]:
        if not self.chunks or not queries:
            return []
        q = self.embedder.encode_queries(queries)
        rankings: list[list[int]] = []
        if allowed is not None and len(allowed) == 0:
            return [[] for _ in queries]
        if self._faiss is not None:
            try:
                import faiss

                params = None
                if allowed is not None:
                    params = faiss.SearchParameters()
                    params.sel = faiss.IDSelectorBatch(np.asarray(allowed, dtype=np.int64))
                kk = min(k, len(self.chunks) if allowed is None else len(allowed))
                _, ids = self._faiss.search(np.ascontiguousarray(q, dtype=np.float32), kk, params=params)
                return [[int(i) for i in row if i >= 0] for row in ids]
            except Exception as exc:
                logger.debug("faiss filtered search failed (%s); numpy fallback", exc)
        sims = q @ self.embeddings.T
        if allowed is not None:
            mask = np.full(sims.shape[1], -np.inf)
            mask[allowed] = 0
            sims = sims + mask
        for row in sims:
            order = np.argsort(-row)[:k]
            rankings.append([int(i) for i in order if np.isfinite(row[i])])
        return rankings

    def search(
        self,
        query: str,
        bid_id: str | list[str] | None = None,
        doc_type: str | list[str] | None = None,
        addendum_number: int | list[int] | None = None,
        file_name: str | None = None,
        top_k: int | None = None,
        mode: SearchMode | None = None,
        expand: bool = True,
        rewrites: list[str] | None = None,
        rerank_query: str | None = None,
    ) -> list[SearchResult]:
        """Hybrid search. `rewrites` are extra queries (multi-query retrieval);
        `rerank_query` lets agents re-rank with a natural-language description."""
        self._ensure_loaded()
        if not self.chunks or not query.strip():
            return []
        s = self.settings
        top_k = top_k or s.top_k
        mode = mode or s.default_search_mode  # type: ignore[assignment]
        allowed = self._allowed(bid_id, doc_type, addendum_number, file_name)
        if allowed is not None and len(allowed) == 0:
            return []
        eq = self.expander.expand(query, rewrites) if expand else self.expander.expand("", rewrites)
        if not expand:
            eq.original = query
        n_cand = s.retrieve_candidates
        scores: dict[int, dict[str, float]] = {}

        bm25_rank: list[int] = []
        if mode in ("bm25", "hybrid", "hybrid_rerank") and self._bm25 is not None:
            hits = self._bm25.search(eq.keyword_query if expand else query, n_cand, allowed)
            bm25_rank = [i for i, _ in hits]
            for i, sc in hits:
                scores.setdefault(i, {})["bm25"] = round(sc, 4)

        dense_ranks: list[list[int]] = []
        if mode in ("dense", "hybrid", "hybrid_rerank"):
            dq = eq.dense_queries if expand else [query, *(rewrites or [])]
            dense_ranks = self._dense(dq, n_cand, allowed)
            for r in dense_ranks:
                for rank, i in enumerate(r):
                    scores.setdefault(i, {}).setdefault("dense_rank", rank + 1)

        if mode == "bm25":
            fused = [(i, scores[i]["bm25"]) for i in bm25_rank]
        elif mode == "dense":
            fused = reciprocal_rank_fusion(dense_ranks, k=s.rrf_k)
        else:
            weights = [1.0 / max(1, len(dense_ranks))] * len(dense_ranks) + [1.0]
            fused = reciprocal_rank_fusion(dense_ranks + [bm25_rank], k=s.rrf_k, weights=weights)
        for i, sc in fused:
            scores.setdefault(i, {})["fused"] = round(float(sc), 6)

        if mode == "hybrid_rerank" and fused:
            cand = [i for i, _ in fused[: s.rerank_candidates]]
            rr = self.reranker.score(rerank_query or query, [self.chunks[i].index_text()[:2000] for i in cand])
            for i, sc in zip(cand, rr):
                scores[i]["rerank"] = round(float(sc), 4)
            ranked = sorted(zip(cand, rr), key=lambda x: -x[1])
        else:
            ranked = fused

        results = []
        for i, sc in ranked[:top_k]:
            c = self.chunks[i]
            results.append(
                SearchResult(
                    chunk_id=c.chunk_id, bid_id=c.bid_id, file_name=c.file_name, doc_type=c.doc_type,
                    addendum_number=c.addendum_number, page_number=c.page_number, page_end=c.page_end,
                    section=c.section, chunk_type=c.chunk_type, text=c.text, score=round(float(sc), 4),
                    scores=scores.get(i, {}),
                )
            )
        return results
