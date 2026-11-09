"""REST API (FastAPI).

    GET  /health
    GET  /bids                                   indexed bids + documents
    POST /index      {"folder": "...", "force": false}
    GET  /search?q=...&bid_id=...&doc_type=...&addendum_number=...&top_k=5&mode=hybrid_rerank
    POST /ask        {"question": "...", "bid_ids": ["Bid1"]}
    POST /extract    {"folder": "..."}  or {"bid_id": "Bid1"}
    GET  /records/{bid_id}                       last extraction JSON

Run:  python main.py serve   (or: uvicorn api.server:app)
Docs: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from agents.orchestrator import Orchestrator
from agents.services import get_services
from core.logging_setup import setup_logging
from ingestion.pipeline import bid_id_from_folder

setup_logging()
logger = logging.getLogger("rfp.api")
app = FastAPI(title="RFP Intelligence Platform", version="1.0.0",
              description="Hybrid RAG search engine + multi-agent extraction / QA over bid documents.")


class IndexRequest(BaseModel):
    folder: str
    force: bool = False


class AskRequest(BaseModel):
    question: str
    bid_ids: list[str] | None = None
    use_cache: bool = True


class ExtractRequest(BaseModel):
    folder: str | None = None
    bid_id: str | None = None
    force: bool = False


def _orch() -> Orchestrator:
    if not hasattr(app.state, "orch"):
        app.state.orch = Orchestrator(get_services())
    return app.state.orch


@app.get("/health")
def health() -> dict:
    s = get_services().settings
    return {"status": "ok", "llm_model": s.llm_model, "embedding_model": s.embedding_model,
            "reranker_model": s.reranker_model, "device": s.resolved_device}


@app.get("/bids")
def bids() -> list[dict]:
    return get_services().engine.catalog()


@app.post("/index")
def index(req: IndexRequest) -> dict:
    if not Path(req.folder).is_dir():
        raise HTTPException(404, f"folder not found: {req.folder}")
    rep = get_services().engine.index_folder(req.folder, force=req.force)
    return rep.model_dump()


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    bid_id: list[str] | None = Query(None),
    doc_type: list[str] | None = Query(None),
    addendum_number: int | None = None,
    file_name: str | None = None,
    top_k: int = Query(5, ge=1, le=50),
    mode: str = Query("hybrid_rerank", pattern="^(bm25|dense|hybrid|hybrid_rerank)$"),
    expand: bool = True,
) -> dict:
    res = get_services().engine.search(q, bid_id=bid_id, doc_type=doc_type, addendum_number=addendum_number,
                                       file_name=file_name, top_k=top_k, mode=mode, expand=expand)  # type: ignore[arg-type]
    return {"query": q, "results": [{**r.model_dump(), "citation": r.citation} for r in res]}


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    return _orch().ask(req.question, req.bid_ids, use_cache=req.use_cache).model_dump()


@app.post("/extract")
def extract(req: ExtractRequest) -> dict:
    if not req.folder and not req.bid_id:
        raise HTTPException(400, "give 'folder' or 'bid_id'")
    if req.folder and not Path(req.folder).is_dir():
        raise HTTPException(404, f"folder not found: {req.folder}")
    bid = req.bid_id or bid_id_from_folder(Path(req.folder))  # type: ignore[arg-type]
    return _orch().extract(bid, req.folder, force_reindex=req.force)


@app.get("/records/{bid_id}")
def record(bid_id: str) -> dict:
    s = get_services().settings
    f = s.path(s.output_dir) / f"{bid_id}.json"
    if not f.exists():
        raise HTTPException(404, f"no extraction record for {bid_id}; POST /extract first")
    return json.loads(f.read_text(encoding="utf-8"))
