"""PDF parsing with PyMuPDF.

Per page we produce ordered `Block`s (heading / text / table):

1. Text blocks are read in visual order (`sort=True`). Short blocks that sit on
   the same visual row (e.g. a SKU column next to a description column) are
   merged into one "a | b" line so column-aligned data stays paired.
2. Repeated running headers/footers (lines in the top/bottom margin that repeat
   on >= 50% of pages, digits normalised) and bare page numbers are removed.
3. Tables are detected with `page.find_tables()`. Layout-only tables (one
   effective column, common in exported RFP templates) are ignored; real
   tables keep their rows and the text blocks inside them are dropped to avoid
   duplication.
4. Headings are detected from font size / boldness / numbering patterns.
5. Pages with (almost) no extractable text but with images/drawings are OCR'd
   (EasyOCR or Tesseract, optional); truly blank pages are logged.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from ingestion.cleaning import clean_block_text, detect_repeated_lines, line_signature, normalize_unicode, repair_emails
from ingestion.models import Block, ParsedDocument

logger = logging.getLogger("rfp.ingestion.pdf")

_SECTION_RE = re.compile(
    r"^((section|article|part|attachment|exhibit|appendix|schedule)\s+[\w.\-]+|(\d+(\.\d+){0,3})\.?\s+[A-Z][A-Za-z])",
    re.I,
)
_PAGE_NUM_RE = re.compile(r"^(page\s*)?#(\s*(of|\|)\s*#)?$|^#$|^-\s*#\s*-$")
_MARGIN = 0.085  # fraction of page height treated as header/footer zone


@dataclass
class _RawBlock:
    page: int
    bbox: tuple[float, float, float, float]
    lines: list[str]
    size: float
    bold: bool
    chars: int
    line_y: list[tuple[float, float]] | None = None  # (y0, y1) per line


# ---------------------------------------------------------------------- OCR
class _OCR:
    _reader = None
    _failed = False

    @classmethod
    def read(cls, png_bytes: bytes, engine: str) -> str:
        if cls._failed:
            return ""
        try:
            if engine == "tesseract":
                import io

                import pytesseract
                from PIL import Image

                return pytesseract.image_to_string(Image.open(io.BytesIO(png_bytes)))
            if cls._reader is None:
                import easyocr

                try:
                    import torch

                    gpu = torch.cuda.is_available()
                except Exception:
                    gpu = False
                cls._reader = easyocr.Reader(["en"], gpu=gpu, verbose=False)
            return "\n".join(cls._reader.readtext(png_bytes, detail=0, paragraph=True))
        except Exception as exc:  # OCR is best-effort
            logger.warning("OCR unavailable (%s); continuing without OCR", exc)
            cls._failed = True
            return ""


# ---------------------------------------------------------------- helpers
def _block_from_dict(page_no: int, b: dict) -> _RawBlock | None:
    lines, sizes, bold_chars, chars, line_y = [], [], 0, 0, []
    for line in b.get("lines", []):
        spans = line.get("spans", [])
        text = "".join(s.get("text", "") for s in spans)
        if not text.strip():
            continue
        lines.append(text)
        lb = line.get("bbox", b["bbox"])
        line_y.append((lb[1], lb[3]))
        for s in spans:
            n = len(s.get("text", "").strip())
            chars += n
            sizes.extend([s.get("size", 0)] * max(n, 1))
            if s.get("flags", 0) & 16 or "bold" in s.get("font", "").lower():
                bold_chars += n
    if not lines:
        return None
    return _RawBlock(
        page=page_no,
        bbox=tuple(b["bbox"]),
        lines=lines,
        size=statistics.median(sizes) if sizes else 0,
        bold=chars > 0 and bold_chars / chars > 0.7,
        chars=chars,
        line_y=line_y,
    )


def _merge_rows(blocks: list[_RawBlock]) -> list[_RawBlock]:
    """Merge short blocks that share a visual row into one 'a | b' block."""
    out: list[_RawBlock] = []
    for b in blocks:
        short = len(b.lines) <= 2 and sum(len(l.split()) for l in b.lines) <= 18
        if out and short:
            prev = out[-1]
            prev_short = len(prev.lines) <= 2 or getattr(prev, "_merged", False)
            y0, y1 = b.bbox[1], b.bbox[3]
            p0, p1 = prev.bbox[1], prev.bbox[3]
            overlap = min(y1, p1) - max(y0, p0)
            min_h = max(1.0, min(y1 - y0, p1 - p0))
            disjoint_x = b.bbox[0] >= prev.bbox[2] - 2 or b.bbox[2] <= prev.bbox[0] + 2
            if prev_short and overlap / min_h > 0.5 and disjoint_x:
                left, right = (prev, b) if prev.bbox[0] <= b.bbox[0] else (b, prev)
                merged = _RawBlock(
                    page=b.page,
                    bbox=(min(prev.bbox[0], b.bbox[0]), min(p0, y0), max(prev.bbox[2], b.bbox[2]), max(p1, y1)),
                    lines=[" ".join(left.lines) + " | " + " ".join(right.lines)],
                    size=max(prev.size, b.size),
                    bold=prev.bold and b.bold,
                    chars=prev.chars + b.chars,
                )
                merged._merged = True  # type: ignore[attr-defined]
                out[-1] = merged
                continue
        out.append(b)
    return out


def _clean_cell(c: str | None) -> str:
    return repair_emails(re.sub(r"\s+", " ", normalize_unicode(c or "")).strip())


def _real_table_rows(raw_rows: list[list[str | None]]) -> list[list[str]] | None:
    """Compress a detected table; return None for layout-only tables."""
    rows = [[_clean_cell(c) for c in r] for r in raw_rows]
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return None
    # drop empty columns
    keep = [j for j in range(max(len(r) for r in rows)) if any(j < len(r) and r[j] for r in rows)]
    rows = [[r[j] if j < len(r) else "" for j in keep] for r in rows]
    if len(keep) < 2:
        return None
    multi = sum(1 for r in rows if sum(1 for c in r if c) >= 2)
    if multi / len(rows) < 0.4:
        return None
    # compress each row: drop empty cells and adjacent duplicates
    compressed = []
    for r in rows:
        cells: list[str] = []
        for c in r:
            if c and (not cells or cells[-1] != c):
                cells.append(c)
        compressed.append(cells)
    return compressed


def _in_margin(y: tuple[float, float], height: float) -> bool:
    return y[0] < height * _MARGIN or y[1] > height * (1 - _MARGIN)


def _inside(inner: tuple, outer: tuple, tol: float = 0.6) -> bool:
    x0, y0, x1, y1 = inner
    ox0, oy0, ox1, oy1 = outer
    ix = max(0.0, min(x1, ox1) - max(x0, ox0))
    iy = max(0.0, min(y1, oy1) - max(y0, oy0))
    area = max(1.0, (x1 - x0) * (y1 - y0))
    return ix * iy / area >= tol


def _is_heading(text: str, blk: _RawBlock, body_size: float) -> int:
    """Return heading level (1-3) or 0 when the block is body text."""
    words = text.split()
    if not words or len(words) > 16 or text.endswith((",", ";")) or text.count(".") > 2:
        return 0
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return 0
    # lines carrying data (emails, long numbers, label: value) are content, not headings
    if "@" in text or "|" in text or (re.search(r"\d{3,}", text) and not _SECTION_RE.match(text)):
        return 0
    if blk.size >= body_size + 3:
        return 1
    if blk.size >= body_size + 1.2:
        return 2
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if blk.bold and len(words) <= 12 and not text.endswith("."):
        return 2 if upper_ratio > 0.8 else 3
    if upper_ratio > 0.9 and 2 <= len(words) <= 10 and not text.endswith("."):
        return 3
    if _SECTION_RE.match(text) and len(words) <= 10 and not text.endswith("."):
        return 3
    return 0


# ------------------------------------------------------------------ main API
def parse_pdf(path: Path, bid_id: str, ocr_enabled: bool = True, ocr_engine: str = "easyocr",
              ocr_min_chars: int = 30, header_footer_ratio: float = 0.5) -> ParsedDocument:
    import fitz  # PyMuPDF

    doc = ParsedDocument(bid_id=bid_id, file_name=path.name, file_path=str(path), file_type="pdf")
    try:
        pdf = fitz.open(str(path))
    except Exception as exc:
        doc.warnings.append(f"unreadable PDF: {exc}")
        logger.error("Cannot open %s: %s", path, exc)
        return doc
    try:
        fitz.TOOLS.mupdf_display_errors(False)
    except Exception:
        pass

    if pdf.needs_pass:
        doc.warnings.append("encrypted PDF (password required) - skipped")
        return doc

    doc.num_pages = len(pdf)
    pages_blocks: list[list[_RawBlock]] = []
    pages_tables: list[list[tuple[tuple, list[list[str]]]]] = []
    margin_lines: list[list[str]] = []
    page_heights: list[float] = []
    all_sizes: list[float] = []

    for pno, page in enumerate(pdf, start=1):
        height = page.rect.height or 792
        try:
            raw = page.get_text("dict", sort=True)
        except Exception as exc:
            doc.warnings.append(f"p{pno}: text extraction failed ({exc})")
            raw = {"blocks": []}
        blocks = [rb for b in raw.get("blocks", []) if b.get("type") == 0 and (rb := _block_from_dict(pno, b))]
        text_chars = sum(b.chars for b in blocks)

        # --- OCR for scanned / image-only pages
        if text_chars < ocr_min_chars:
            has_visual = bool(page.get_images(full=True))
            if not has_visual:
                try:
                    has_visual = len(page.get_drawings()) > 20
                except Exception:
                    has_visual = False
            ocr_text = ""
            if has_visual and ocr_enabled:
                try:
                    pix = page.get_pixmap(dpi=200)
                    ocr_text = _OCR.read(pix.tobytes("png"), ocr_engine).strip()
                except Exception as exc:
                    doc.warnings.append(f"p{pno}: OCR failed ({exc})")
            if len(ocr_text) >= ocr_min_chars:
                doc.ocr_pages.append(pno)
                blocks = [_RawBlock(pno, (0, 0, page.rect.width, height), ocr_text.splitlines(), 0, False, len(ocr_text))]
            elif text_chars == 0 or not blocks:
                doc.empty_pages.append(pno)
                pages_blocks.append([])
                pages_tables.append([])
                margin_lines.append([])
                page_heights.append(height)
                continue

        # --- tables
        tables: list[tuple[tuple, list[list[str]]]] = []
        try:
            for tb in page.find_tables().tables:
                rows = _real_table_rows(tb.extract())
                if rows:
                    tables.append((tuple(tb.bbox), rows))
        except Exception as exc:
            logger.debug("table detection failed on %s p%d: %s", path.name, pno, exc)

        margin_lines.append([l for b in blocks for l, y in zip(b.lines, b.line_y or []) if _in_margin(y, height)])
        page_heights.append(height)
        for b in blocks:
            all_sizes.extend([b.size] * max(1, b.chars // 10))
        pages_blocks.append(blocks)
        pages_tables.append(tables)

    boiler = detect_repeated_lines(margin_lines, min_ratio=header_footer_ratio)
    body_size = statistics.median(all_sizes) if all_sizes else 10.0

    for page_no, (blocks, tables) in enumerate(zip(pages_blocks, pages_tables), start=1):
        # remove header/footer lines and page numbers
        height = page_heights[page_no - 1]
        filtered: list[_RawBlock] = []
        for b in blocks:
            if any(_inside(b.bbox, tb[0]) for tb in tables):
                continue  # text is represented by the table itself
            kept = []
            for l, y in zip(b.lines, b.line_y or [(b.bbox[1], b.bbox[3])] * len(b.lines)):
                sig = line_signature(l)
                if _in_margin(y, height) and (sig in boiler or _PAGE_NUM_RE.match(sig)):
                    continue  # running header / footer / page number
                kept.append(l)
            if kept:
                b.lines = kept
                filtered.append(b)
        filtered = _merge_rows(filtered)

        # interleave tables with text by vertical position
        items: list[tuple[float, str, object]] = [(b.bbox[1], "b", b) for b in filtered]
        items += [(tb[0][1], "t", tb) for tb in tables]
        items.sort(key=lambda x: x[0])
        for _, kind, obj in items:
            if kind == "t":
                bbox, rows = obj  # type: ignore[misc]
                text = "\n".join(" | ".join(r) for r in rows)
                doc.blocks.append(Block(kind="table", text=text, page=page_no, rows=rows))
                continue
            b: _RawBlock = obj  # type: ignore[assignment]
            text = clean_block_text("\n".join(b.lines))
            if not text:
                continue
            level = _is_heading(text, b, body_size)
            doc.blocks.append(Block(kind="heading" if level else "text", text=text, page=b.page, level=level))

    pdf.close()
    if doc.empty_pages:
        doc.warnings.append(f"empty/blank pages: {doc.empty_pages}")
    if doc.ocr_pages:
        doc.warnings.append(f"OCR applied to pages: {doc.ocr_pages}")
    if not doc.blocks:
        doc.warnings.append("no extractable text (scanned PDF without OCR?)")
    return doc
