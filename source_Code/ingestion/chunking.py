"""Section- and table-aware chunking for RFP documents.

Strategy (justified in README):
* Chunks never cross a section heading boundary (RFP sections such as
  "Bonds", "Submission Instructions", "Warranty" are self-contained topics).
  Tiny sections (< `min_words`) are merged forward so a heading is never a
  chunk on its own.
* Body text is packed to ~`target_words` (default 180 words, ~250 tokens) with
  `overlap_words` (default 30) of sentence-aligned overlap inside a section.
  RFP facts are short (a date, an amount, a form name) so small chunks keep
  precision high, and 250 tokens + a contextual header stays well inside the
  512-token window of BGE / MiniLM encoders and cross-encoders.
* Tables are chunked separately as row groups with the header row repeated
  in every chunk so each chunk is self-describing.
* Key/value blocks (portal pages) are kept together as one chunk when small.
* Each chunk keeps its section path and page range; a contextual header
  (file, doc type, addendum number, section) is prepended at index time.
"""

from __future__ import annotations

import hashlib
import re

from ingestion.cleaning import word_count
from ingestion.models import Block, Chunk, ParsedDocument

_SENT_SPLIT = re.compile(r"(?<=[.!?;:])\s+(?=[A-Z0-9(\"'-])|\n+")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    out: list[str] = []
    for p in parts:  # hard-wrap very long "sentences" (lists without punctuation)
        words = p.split()
        while len(words) > 120:
            out.append(" ".join(words[:120]))
            words = words[120:]
        if words:
            out.append(" ".join(words))
    return out


def _chunk_id(doc: ParsedDocument, idx: int, text: str) -> str:
    h = hashlib.sha1(f"{doc.bid_id}|{doc.file_name}|{idx}|{text[:200]}".encode("utf-8")).hexdigest()[:10]
    return f"{doc.bid_id}:{h}"


class _Builder:
    def __init__(self, doc: ParsedDocument, target: int, overlap: int, min_words: int, table_rows: int):
        self.doc, self.target, self.overlap, self.min_words, self.table_rows = doc, target, overlap, min_words, table_rows
        self.chunks: list[Chunk] = []
        self.buf: list[tuple[str, int]] = []  # (sentence, page)
        self.section_stack: list[tuple[int, str]] = []
        self._carry = 0  # number of buffered sentences that are only overlap carry-over

    # ----------------------------------------------------------------
    @property
    def section(self) -> str:
        return " > ".join(t for _, t in self.section_stack)[-200:]

    def _emit(self, text: str, page_start: int, page_end: int, chunk_type: str) -> None:
        text = text.strip()
        if not text:
            return
        self.chunks.append(
            Chunk(
                chunk_id=_chunk_id(self.doc, len(self.chunks), text),
                bid_id=self.doc.bid_id,
                file_name=self.doc.file_name,
                doc_type=self.doc.doc_type.value,
                addendum_number=self.doc.addendum_number,
                doc_date=self.doc.doc_date,
                page_number=page_start,
                page_end=page_end,
                section=self.section,
                chunk_type=chunk_type,  # type: ignore[arg-type]
                text=text,
            )
        )

    def _buf_words(self) -> int:
        return sum(word_count(s) for s, _ in self.buf)

    def flush(self, keep_overlap: bool = False) -> None:
        if not self.buf:
            return
        if len(self.buf) == self._carry:  # only overlap left -> already emitted
            self.buf, self._carry = [], 0
            return
        text = " ".join(s for s, _ in self.buf)
        self._emit(text, self.buf[0][1], self.buf[-1][1], "text")
        if keep_overlap and self.overlap > 0:
            carry: list[tuple[str, int]] = []
            n = 0
            for s, p in reversed(self.buf):
                if n >= self.overlap:
                    break
                carry.insert(0, (s, p))
                n += word_count(s)
            # never carry the whole buffer (would loop)
            self.buf = carry if len(carry) < len(self.buf) else []
        else:
            self.buf = []
        self._carry = len(self.buf)

    # ----------------------------------------------------------------
    def add_heading(self, block: Block) -> None:
        if self.buf and len(self.buf) == self._carry:
            self.buf, self._carry = [], 0  # no overlap across section boundaries
        if self._buf_words() >= self.min_words:
            self.flush()
        elif self.buf:
            # tiny section: keep its text, it will be merged with the next section
            pass
        level = block.level or 3
        while self.section_stack and self.section_stack[-1][0] >= level:
            self.section_stack.pop()
        self.section_stack.append((level, block.text[:120]))
        if self.buf:  # merged tiny section keeps the heading inline for context
            self.buf.append((block.text, block.page))

    def add_text(self, block: Block) -> None:
        for sent in _split_sentences(block.text):
            self.buf.append((sent, block.page))
            if self._buf_words() >= self.target:
                self.flush(keep_overlap=True)

    def add_kv(self, block: Block) -> None:
        lines = [l for l in block.text.split("\n") if l.strip()]
        group: list[str] = []
        for line in lines:
            group.append(line)
            if word_count(" ".join(group)) >= self.target:
                self._emit("\n".join(group), block.page, block.page, "kv")
                group = []
        if group:
            if word_count(" ".join(group)) < self.min_words and self.chunks and self.chunks[-1].chunk_type == "kv" \
                    and self.chunks[-1].section == self.section:
                prev = self.chunks[-1]
                prev.text += "\n" + "\n".join(group)
            else:
                self._emit("\n".join(group), block.page, block.page, "kv")

    def add_table(self, block: Block) -> None:
        self.flush()
        rows = block.rows or [r.split(" | ") for r in block.text.split("\n")]
        if not rows:
            return
        first = rows[0]
        # Repeat the first row only when it looks like a column header (form-style tables whose
        # first row is "Label: | value" are not headers).
        is_header = len(first) >= 2 and all(c and len(c.split()) <= 6 and not c.rstrip().endswith(":") for c in first)
        header_line = " | ".join(first) if is_header else ""
        body = rows[1:] if is_header else rows
        if not body:
            self._emit(header_line, block.page, block.page, "table")
            return
        prefix = header_line + "\n" if header_line else ""
        group: list[str] = []
        for row in body:
            row_text = " | ".join(row)
            if word_count(row_text) > self.target:
                # oversized cell (form tables): split it like prose, keep the row label on every piece
                if group:
                    self._emit(prefix + "\n".join(group), block.page, block.page, "table")
                    group = []
                label = row[0].rstrip(":") if len(row) > 1 and len(row[0].split()) <= 10 else ""
                content = " | ".join(row[1:]) if label else row_text
                window: list[str] = []
                emitted_any = False
                for sent in _split_sentences(content):
                    window.append(sent)
                    if word_count(" ".join(window)) >= self.target:
                        self._emit((f"{label}: " if label else "") + " ".join(window), block.page, block.page, "table")
                        emitted_any = True
                        window = window[-1:] if self.overlap else []
                if window and (not emitted_any or len(window) > 1 or not self.overlap):
                    self._emit((f"{label}: " if label else "") + " ".join(window), block.page, block.page, "table")
                continue
            if group and word_count(" ".join(group)) + word_count(row_text) > self.target:
                self._emit(prefix + "\n".join(group), block.page, block.page, "table")  # emit before overflow
                group = []
            group.append(row_text)
            if len(group) >= self.table_rows or word_count(" ".join(group)) >= self.target:
                self._emit(prefix + "\n".join(group), block.page, block.page, "table")
                group = []
        if group:
            self._emit(prefix + "\n".join(group), block.page, block.page, "table")


def chunk_document(doc: ParsedDocument, target_words: int = 180, overlap_words: int = 30,
                   min_words: int = 20, table_rows: int = 8) -> list[Chunk]:
    b = _Builder(doc, target_words, overlap_words, min_words, table_rows)
    for block in doc.blocks:
        if block.kind == "heading":
            b.add_heading(block)
        elif block.kind == "table":
            b.add_table(block)
        elif block.kind == "kv":
            b.flush()
            b.add_kv(block)
        else:
            b.add_text(block)
    b.flush()
    # drop near-empty chunks
    return [c for c in b.chunks if word_count(c.text) >= 3]
