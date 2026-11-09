"""Ingestion Agent.

Single responsibility: make sure a bid folder is parsed, cleaned, classified,
chunked and indexed (incrementally) before other agents query it. Wraps the
deterministic ingestion pipeline + `SearchEngine.index_folder` - no LLM is
needed for this step, which keeps it fast, cheap and reproducible.
"""

from __future__ import annotations

import logging
from pathlib import Path

from agents.services import Services
from core.tracing import Span
from ingestion.models import IngestionReport

logger = logging.getLogger("rfp.agents.ingestion")


class IngestionAgent:
    name = "ingestion_agent"

    def __init__(self, services: Services):
        self.s = services

    def run(self, bid_folder: str | None, bid_id: str, span: Span | None = None, force: bool = False) -> dict:
        engine = self.s.engine
        if bid_folder and not Path(bid_folder).is_dir() and bid_id in engine.list_bids():
            # e.g. project moved to another machine: the stored folder path no longer exists,
            # but the bid is already indexed -> use the index instead of failing.
            if span:
                span.tool_call("filesystem.check", {"folder": bid_folder}, "missing - using existing index")
            bid_folder = None
        if bid_folder:
            report: IngestionReport = engine.index_folder(Path(bid_folder), force=force, bid_id=bid_id)
            if span:
                span.tool_call("search_engine.index_folder", {"folder": bid_folder, "force": force},
                               {"status": report.status, "chunks": report.chunks})
            docs = report.documents
            status = report.status
            warnings = report.warnings
        else:
            status = "up_to_date" if bid_id in engine.list_bids() else "failed"
            docs = next((c["documents"] for c in engine.catalog() if c["bid_id"] == bid_id), [])
            warnings = [] if status != "failed" else [f"bid {bid_id} is not indexed and no folder was given"]
        n_chunks = len(engine.get_chunks(bid_id=bid_id))
        return {
            "bid_id": bid_id,
            "status": status if n_chunks else "failed",
            "chunks": n_chunks,
            "documents": [
                {k: d.get(k) for k in ("file", "doc_type", "addendum_number", "doc_date", "pages", "chunks",
                                       "empty_pages", "ocr_pages")}
                for d in docs
            ],
            "warnings": warnings,
        }
