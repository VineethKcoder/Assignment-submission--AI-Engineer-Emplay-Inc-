"""Unit tests for the search engine: tokenizer, fusion, filters, incremental indexing, citations."""

from __future__ import annotations

import shutil
from pathlib import Path

from search.bm25 import BM25Index, tokenize
from search.engine import SearchEngine
from search.fusion import reciprocal_rank_fusion
from search.query import QueryExpander


def test_tokenizer_keeps_identifiers_and_parts():
    toks = tokenize("Bid JA-207652, SKU 210-BLYZ and dock WD22TB4 (#E20P4600040)")
    for t in ["ja-207652", "207652", "210-blyz", "blyz", "wd22tb4", "e20p4600040"]:
        assert t in toks
    assert "and" not in toks  # stopword
    assert tokenize("bonds") == tokenize("bond")  # light stemming


def test_bm25_exact_identifier_match():
    idx = BM25Index(["laptop warranty three years", "SKU 210-BLYZ latitude base", "dock WD22TB4 thunderbolt"])
    assert idx.search("210-BLYZ", 3)[0][0] == 1
    assert idx.search("WD22TB4", 3)[0][0] == 2
    assert idx.search("WD22TB4", 3, allowed=[0, 1]) == []  # filter respected


def test_rrf_prefers_documents_ranked_high_in_both_lists():
    fused = reciprocal_rank_fusion([[1, 2, 3], [3, 1, 4]], k=60)
    assert fused[0][0] == 1  # ranks 1 & 2
    assert {d for d, _ in fused} == {1, 2, 3, 4}


def test_query_expansion(settings):
    ex = QueryExpander(str(settings.path(settings.synonyms_config)))
    q = ex.expand("What is the deadline?")
    assert "due date" in q.expansions and "closing date" in q.expansions
    assert ex.expand("unrelated text").expansions == []


def test_search_returns_citations_and_respects_filters(indexed_engine: SearchEngine):
    res = indexed_engine.search("bid bond percent", bid_id="BidZ", top_k=3)
    assert res and "5%" in res[0].text
    assert res[0].file_name.endswith(".pdf") and res[0].page_number == 2
    assert "RFP ZX-1001" in res[0].citation and "p.2" in res[0].citation

    add = indexed_engine.search("due date", bid_id="BidZ", doc_type="addendum", top_k=5)
    assert add and all(r.doc_type == "addendum" and r.addendum_number == 1 for r in add)
    assert indexed_engine.search("due date", addendum_number=7) == []
    assert indexed_engine.search("due date", bid_id="NoSuchBid") == []


def test_all_search_modes(indexed_engine: SearchEngine):
    for mode in ("bm25", "dense", "hybrid", "hybrid_rerank"):
        res = indexed_engine.search("7Z123UT#ABA part number", top_k=2, mode=mode)
        assert res, mode
    top = indexed_engine.search("7Z123UT#ABA", top_k=1, mode="bm25")[0]
    assert "7Z123UT#ABA" in top.text


def test_html_page_is_searchable(indexed_engine: SearchEngine):
    res = indexed_engine.search("issuing organization", bid_id="BidZ", doc_type="bid_page", top_k=2)
    assert res and "Springfield School District" in " ".join(r.text for r in res)


def test_incremental_indexing(engine: SearchEngine, bid_folder: Path, tmp_path: Path):
    r1 = engine.index_folder(bid_folder)
    assert r1.status == "indexed"
    r2 = engine.index_folder(bid_folder)
    assert r2.status == "up_to_date"  # unchanged -> skipped

    other = tmp_path / "BidY"
    shutil.copytree(bid_folder, other)
    before = (engine.store.root / "BidZ" / "embeddings.npy").stat().st_mtime_ns
    engine.index_folder(other)
    after = (engine.store.root / "BidZ" / "embeddings.npy").stat().st_mtime_ns
    assert before == after  # adding a bid never rewrites other shards
    assert engine.list_bids() == ["BidY", "BidZ"]

    (other / "Addendum 1 RFP ZX-1001.pdf").unlink()
    assert engine.index_folder(other).status == "indexed"  # changed folder -> re-indexed
    assert engine.delete_bid("BidY") and engine.list_bids() == ["BidZ"]


def test_catalog_lists_documents(indexed_engine: SearchEngine):
    cat = indexed_engine.catalog()
    assert cat[0]["bid_id"] == "BidZ"
    types = {d["doc_type"] for d in cat[0]["documents"]}
    assert {"rfp", "addendum", "bid_page"} <= types
