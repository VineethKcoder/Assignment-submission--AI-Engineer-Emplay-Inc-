"""Bid-folder ingestion: discover files -> parse -> classify -> clean -> chunk."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path

from core.settings import Settings
from ingestion.chunking import chunk_document
from ingestion.classify import enrich_metadata
from ingestion.html_parser import parse_html
from ingestion.models import Block, Chunk, IngestionReport, ParsedDocument
from ingestion.pdf_parser import parse_pdf

logger = logging.getLogger("rfp.ingestion")

SUPPORTED = {".pdf": "pdf", ".html": "html", ".htm": "html", ".txt": "txt", ".md": "txt"}


def bid_id_from_folder(folder: Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", folder.resolve().name).strip("_") or "bid"


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def discover_files(folder: Path) -> tuple[list[Path], list[dict]]:
    files, skipped = [], []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name.startswith((".", "~$")):
            continue
        if p.suffix.lower() in SUPPORTED:
            files.append(p)
        else:
            skipped.append({"file": p.name, "reason": f"unsupported type {p.suffix}"})
    return files, skipped


def _parse_txt(path: Path, bid_id: str) -> ParsedDocument:
    doc = ParsedDocument(bid_id=bid_id, file_name=path.name, file_path=str(path), file_type="txt", num_pages=1)
    text = path.read_text(encoding="utf-8", errors="ignore")
    for para in re.split(r"\n\s*\n", text):
        if para.strip():
            doc.blocks.append(Block(kind="text", text=para.strip(), page=1))
    return doc


def parse_file(path: Path, bid_id: str, settings: Settings) -> ParsedDocument:
    kind = SUPPORTED[path.suffix.lower()]
    try:
        if kind == "pdf":
            doc = parse_pdf(path, bid_id, ocr_enabled=settings.ocr_enabled, ocr_engine=settings.ocr_engine,
                            ocr_min_chars=settings.ocr_min_chars, header_footer_ratio=settings.header_footer_min_ratio)
        elif kind == "html":
            doc = parse_html(path, bid_id)
        else:
            doc = _parse_txt(path, bid_id)
    except Exception as exc:  # never let one bad file kill the bid
        logger.exception("Failed to parse %s", path)
        doc = ParsedDocument(bid_id=bid_id, file_name=path.name, file_path=str(path), file_type=kind)  # type: ignore[arg-type]
        doc.warnings.append(f"parse error: {type(exc).__name__}: {exc}")
        return doc
    return enrich_metadata(doc, str(settings.path(settings.doc_types_config)))


def ingest_folder(folder: Path, settings: Settings, bid_id: str | None = None) -> tuple[list[ParsedDocument], list[Chunk], IngestionReport]:
    t0 = time.perf_counter()
    folder = Path(folder)
    bid_id = bid_id or bid_id_from_folder(folder)
    report = IngestionReport(bid_id=bid_id, folder=str(folder))
    if not folder.is_dir():
        report.status = "failed"
        report.warnings.append(f"folder not found: {folder}")
        return [], [], report

    files, skipped = discover_files(folder)
    report.skipped_files = skipped
    docs: list[ParsedDocument] = []
    chunks: list[Chunk] = []
    for path in files:
        doc = parse_file(path, bid_id, settings)
        doc_chunks = chunk_document(
            doc,
            target_words=settings.chunk_target_words,
            overlap_words=settings.chunk_overlap_words,
            min_words=settings.chunk_min_words,
            table_rows=settings.table_rows_per_chunk,
        )
        docs.append(doc)
        chunks.extend(doc_chunks)
        report.documents.append(
            {
                "file": doc.file_name,
                "file_type": doc.file_type,
                "doc_type": doc.doc_type.value,
                "addendum_number": doc.addendum_number,
                "doc_date": doc.doc_date,
                "title": doc.title,
                "pages": doc.num_pages,
                "blocks": len(doc.blocks),
                "chunks": len(doc_chunks),
                "empty_pages": doc.empty_pages,
                "ocr_pages": doc.ocr_pages,
                "warnings": doc.warnings,
                "sha1": file_sha1(path),
            }
        )
        for w in doc.warnings:
            report.warnings.append(f"{doc.file_name}: {w}")
        logger.info("Parsed %s -> %s, %d blocks, %d chunks", doc.file_name, doc.doc_type.value, len(doc.blocks), len(doc_chunks))
    report.chunks = len(chunks)
    report.seconds = round(time.perf_counter() - t0, 2)
    if not chunks:
        report.status = "failed"
        report.warnings.append("no chunks produced for this bid")
    return docs, chunks, report
