"""Unit tests for ingestion: cleaning, PDF/HTML parsing, classification, chunking."""

from __future__ import annotations

from pathlib import Path

from core.settings import Settings
from ingestion.chunking import chunk_document
from ingestion.classify import classify, extract_doc_date, find_dates
from ingestion.cleaning import (
    clean_block_text,
    detect_repeated_lines,
    fix_hyphenation,
    join_broken_lines,
    line_signature,
    normalize_unicode,
)
from ingestion.html_parser import parse_html
from ingestion.models import Block, DocType, ParsedDocument
from ingestion.pdf_parser import _real_table_rows, parse_pdf
from ingestion.pipeline import ingest_folder


# ------------------------------------------------------------------ cleaning
def test_fix_hyphenation_joins_broken_words():
    assert fix_hyphenation("pro-\nposal due") == "proposal due"
    assert fix_hyphenation("JA-\n207652") == "JA-\n207652"  # not a lowercase word break


def test_join_broken_lines_reflows_sentences_but_keeps_lists():
    text = "Proposals must be\nsubmitted by the\ndue date.\n1. First item\n2. Second item"
    out = join_broken_lines(text)
    assert "Proposals must be submitted by the due date." in out
    assert "\n1. First item\n2. Second item" in out


def test_bullet_glyph_lines_are_merged_with_their_item():
    out = clean_block_text("•\nMemory: 16GB\n•\nStorage: 256 GB")
    assert out.splitlines() == ["- Memory: 16GB", "- Storage: 256 GB"]


def test_normalize_unicode_and_signature():
    assert normalize_unicode("“quoted”​") == '"quoted"'
    assert line_signature("Page 3 of 40") == line_signature("Page 12 of 40")


def test_detect_repeated_lines():
    pages = [["Dallas ISD rev 2.0", f"Page {i} of 9"] for i in range(1, 10)]
    pages[0].append("unique line")
    sigs = detect_repeated_lines(pages)
    assert line_signature("Dallas ISD rev 2.0") in sigs
    assert line_signature("Page 1 of 9") in sigs
    assert line_signature("unique line") not in sigs


# ------------------------------------------------------------------ dates / classification
def test_find_dates_formats():
    isos = {iso for _, iso in find_dates("due July 9, 2024; opened 26-MAY-2024 and 05/24/2024")}
    assert {"2024-07-09", "2024-05-26", "2024-05-24"} <= isos


def test_doc_date_requires_label():
    assert extract_doc_date("Issue Date: 05/24/2024 Proposal due 06/10/2024") == "2024-05-24"
    assert extract_doc_date("The new due date will be July 9, 2024") is None


def _doc(name: str, first: str, ftype: str = "pdf") -> ParsedDocument:
    return ParsedDocument(bid_id="b", file_name=name, file_path=name, file_type=ftype,
                          blocks=[Block(kind="text", text=first, page=1)])


def test_classification_rules(settings: Settings):
    rules = str(settings.path(settings.doc_types_config))
    assert classify(_doc("Addendum 2 RFP X.pdf", "ADDENDUM No. 2"), rules) == (DocType.ADDENDUM, 2)
    assert classify(_doc("notice.pdf", "AMENDMENT NO. 3\nchanges"), rules) == (DocType.ADDENDUM, 3)
    assert classify(_doc("Mercury_Affidavit.pdf", "MERCURY AFFIDAVIT"), rules)[0] == DocType.AFFIDAVIT
    assert classify(_doc("Dell_Laptop_Specs.pdf", "SKU"), rules)[0] == DocType.SPECS
    assert classify(_doc("PORFP_-_Dell_Laptop_Final.pdf", "Purchase Order Request for Proposals"), rules)[0] == DocType.RFP
    assert classify(_doc("page.html", "x", "html"), rules)[0] == DocType.BID_PAGE


# ------------------------------------------------------------------ PDF
def test_pdf_parser_removes_headers_and_detects_headings(bid_folder: Path):
    doc = parse_pdf(bid_folder / "RFP ZX-1001 Laptop Refresh FINAL.pdf", "BidZ", ocr_enabled=False)
    texts = " ".join(b.text for b in doc.blocks)
    assert "ACME County Purchasing" not in texts  # running header removed
    assert "Page 1 of 3" not in texts  # page footer removed
    headings = [b.text for b in doc.blocks if b.kind == "heading"]
    assert any("BONDS" in h for h in headings)
    assert {b.page for b in doc.blocks} == {1, 2, 3}


def test_pdf_parser_handles_unreadable_file(tmp_path: Path):
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    doc = parse_pdf(bad, "b", ocr_enabled=False)
    assert doc.warnings and not doc.blocks


def test_pdf_blank_page_is_logged(tmp_path: Path):
    import fitz

    p = tmp_path / "blank.pdf"
    d = fitz.open()
    d.new_page()
    d.save(str(p))
    doc = parse_pdf(p, "b", ocr_enabled=False)
    assert doc.empty_pages == [1]


def test_layout_tables_are_ignored_and_real_tables_compressed():
    layout = [["SECTION", ""], ["long paragraph", ""], ["more", None]]
    assert _real_table_rows(layout) is None
    real = [["", "Item", "", "Qty"], ["", "Laptop", "", "30"], ["", "Dock", "", "30"]]
    assert _real_table_rows(real) == [["Item", "Qty"], ["Laptop", "30"], ["Dock", "30"]]


# ------------------------------------------------------------------ HTML
def test_html_parser_extracts_label_values_and_drops_nav(tmp_path: Path):
    from tests.conftest import HTML_PAGE

    f = tmp_path / "p.html"
    f.write_text(HTML_PAGE, encoding="utf-8")
    doc = parse_html(f, "BidZ")
    text = "\n".join(b.text for b in doc.blocks)
    assert "Solicitation Number: ZX-1001" in text
    assert "Issuing Organization: Springfield School District" in text
    assert "jane.buyer@springfield.k12.us" in text
    assert "Dashboard" not in text and "cookies" not in text
    assert any(b.kind == "table" for b in doc.blocks)
    assert doc.title == "Laptop Refresh - Bid Information"


# ------------------------------------------------------------------ chunking + pipeline
def test_chunking_respects_sections_and_tables():
    doc = ParsedDocument(bid_id="b", file_name="f.pdf", file_path="f.pdf", file_type="pdf", blocks=[
        Block(kind="heading", text="BONDS", page=1, level=2),
        Block(kind="text", text="A bid bond of five percent is required with each proposal submitted. " * 3, page=1),
        Block(kind="heading", text="DELIVERY", page=2, level=2),
        Block(kind="text", text="Delivery within 30 days of award to all district locations listed. " * 3, page=2),
        Block(kind="table", text="", page=2, rows=[["Item", "Qty"], ["Laptop", "30"], ["Dock", "30"]]),
    ])
    chunks = chunk_document(doc, target_words=200, overlap_words=10, min_words=5)
    sections = [c.section for c in chunks]
    assert "BONDS" in sections[0] and "DELIVERY" in sections[1]
    assert not any("bond" in c.text.lower() and "delivery" in c.text.lower() for c in chunks)
    table = [c for c in chunks if c.chunk_type == "table"][0]
    assert table.text.startswith("Item | Qty") and table.page_number == 2


def test_long_sections_are_split_with_overlap():
    words = " ".join(f"Sentence number {i} is here." for i in range(100))
    doc = ParsedDocument(bid_id="b", file_name="f.pdf", file_path="f.pdf", file_type="pdf",
                         blocks=[Block(kind="text", text=words, page=1)])
    chunks = chunk_document(doc, target_words=100, overlap_words=20, min_words=5)
    assert len(chunks) >= 4
    assert all(len(c.text.split()) <= 125 for c in chunks)
    first_tail = chunks[0].text.split()[-5:]
    assert any(w in chunks[1].text.split()[:30] for w in first_tail)  # overlap


def test_ingest_folder_end_to_end(bid_folder: Path, settings: Settings):
    docs, chunks, report = ingest_folder(bid_folder, settings)
    types = {d["file"]: (d["doc_type"], d["addendum_number"]) for d in report.documents}
    assert types["Addendum 1 RFP ZX-1001.pdf"] == ("addendum", 1)
    assert types["Laptop Refresh - Bid Information.html"][0] == "bid_page"
    assert types["RFP ZX-1001 Laptop Refresh FINAL.pdf"][0] == "rfp"
    assert report.skipped_files and report.skipped_files[0]["file"] == "notes.xyz"
    assert all(c.bid_id == "BidZ" for c in chunks)
    rfp = next(d for d in docs if d.doc_type == DocType.RFP)
    assert rfp.doc_date == "2025-01-10"
