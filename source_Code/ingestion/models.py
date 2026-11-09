"""Data models for parsed documents and index chunks."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class DocType(str, Enum):
    BID_PAGE = "bid_page"
    RFP = "rfp"
    ADDENDUM = "addendum"
    SPECS = "specs"
    AFFIDAVIT = "affidavit"
    FORM = "form"
    OTHER = "other"


class Block(BaseModel):
    """A structural unit of a document page: heading, paragraph, key-value group or table."""

    kind: Literal["heading", "text", "kv", "table"]
    text: str
    page: int
    level: int = 0  # heading level (1 = top)
    # table only: list of rows (first row = header)
    rows: list[list[str]] | None = None


class ParsedDocument(BaseModel):
    bid_id: str
    file_name: str
    file_path: str
    file_type: Literal["pdf", "html", "txt"]
    doc_type: DocType = DocType.OTHER
    addendum_number: int | None = None
    doc_date: str | None = None  # ISO date when available
    title: str | None = None
    num_pages: int = 0
    blocks: list[Block] = Field(default_factory=list)
    empty_pages: list[int] = Field(default_factory=list)
    ocr_pages: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def first_page_text(self, max_chars: int = 3000) -> str:
        out, n = [], 0
        for b in self.blocks:
            if b.page > 2:
                break
            out.append(b.text)
            n += len(b.text)
            if n > max_chars:
                break
        return "\n".join(out)[:max_chars]


class Chunk(BaseModel):
    chunk_id: str
    bid_id: str
    file_name: str
    doc_type: str
    addendum_number: int | None = None
    doc_date: str | None = None
    page_number: int
    page_end: int
    section: str = ""
    chunk_type: Literal["text", "kv", "table"] = "text"
    text: str

    @property
    def doc_label(self) -> str:
        label = self.doc_type
        if self.addendum_number is not None:
            label += f" #{self.addendum_number}"
        return label

    def context_header(self) -> str:
        """Contextual header prepended for embedding / BM25 (not shown as evidence)."""
        parts = [f"Document: {self.file_name} ({self.doc_label})"]
        if self.section:
            parts.append(f"Section: {self.section}")
        return " | ".join(parts)

    def index_text(self) -> str:
        return f"{self.context_header()}\n{self.text}"

    def citation(self) -> str:
        pages = f"p.{self.page_number}" if self.page_end == self.page_number else f"pp.{self.page_number}-{self.page_end}"
        return f"{self.file_name}, {pages}"


class IngestionReport(BaseModel):
    bid_id: str
    folder: str
    documents: list[dict] = Field(default_factory=list)
    chunks: int = 0
    skipped_files: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    status: Literal["indexed", "up_to_date", "failed"] = "indexed"
    seconds: float = 0.0
