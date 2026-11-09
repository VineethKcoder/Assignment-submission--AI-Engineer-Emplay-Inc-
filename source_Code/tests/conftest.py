"""Shared fixtures. Tests never download models or call an LLM: they use a hashing
embedder, a lexical reranker and a scripted fake LLM."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.settings import Settings  # noqa: E402
from search.embeddings import HashingEmbedder  # noqa: E402
from search.engine import SearchEngine  # noqa: E402
from search.reranker import KeywordOverlapReranker  # noqa: E402

HTML_PAGE = """<html><head><title>Laptop Refresh - Bid Information | Portal</title></head><body>
<nav class="navbar"><a>Search</a><a>Dashboard</a><a>Logout</a></nav>
<div id="content">
<h3 class="content-block-title">Basic Information</h3>
<div class="mets-field"><span class="mets-field-label">Solicitation Number</span>
  <div class="mets-field-body"><p>ZX-1001</p></div></div>
<div class="mets-field"><span class="mets-field-label">Issuing Organization</span>
  <div class="mets-field-body"><p>Springfield School District</p></div></div>
<h3 class="content-block-sub-title">Dates</h3>
<div class="mets-field"><span class="mets-field-label">Closing Date</span>
  <div class="mets-field-body"><p>03/15/2025 02:00 PM EST</p></div></div>
<h3 class="content-block-sub-title">Contact Information</h3>
<div class="mets-field no-label"><span class="mets-field-label mets-field-filler"></span>
  <div class="mets-field-body"><p>Jane Buyer</p></div></div>
<div class="mets-field no-label"><span class="mets-field-label mets-field-filler"></span>
  <div class="mets-field-body"><p>jane.buyer@springfield.k12.us</p></div></div>
<table><tr><th>Item</th><th>Qty</th><th>Model</th></tr><tr><td>Laptop</td><td>40</td><td>EliteBook 840</td></tr></table>
</div>
<div class="cookie-banner">We use cookies</div>
</body></html>"""


def make_pdf(path: Path, pages: list[str]) -> Path:
    import fitz

    doc = fitz.open()
    for i, text in enumerate(pages, 1):
        page = doc.new_page()
        page.insert_text((72, 40), "ACME County Purchasing - RFP ZX-1001", fontsize=9)  # running header
        y = 90
        for line in text.split("\n"):
            size = 16 if line.startswith("#") else 11
            page.insert_text((72, y), line.lstrip("# "), fontsize=size)
            y += 22 if size > 11 else 15
        page.insert_text((280, 800), f"Page {i} of {len(pages)}", fontsize=9)  # running footer
    doc.save(str(path))
    return path


@pytest.fixture
def bid_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "BidZ"
    folder.mkdir()
    (folder / "Laptop Refresh - Bid Information.html").write_text(HTML_PAGE, encoding="utf-8")
    make_pdf(folder / "RFP ZX-1001 Laptop Refresh FINAL.pdf", [
        "# SECTION 1 GENERAL INFORMATION\nIssue Date: January 10, 2025\nProposals are due February 20, 2025 at 2:00 PM EST.\n"
        "Proposals must be submitted electronically through the eBid portal. Hard copies will not be accepted.",
        "# SECTION 2 BONDS\nA bid bond of 5% of the total bid amount is required with each proposal.\n"
        "# SECTION 3 DELIVERY\nDelivery is required within 30 days of the purchase order.",
        "# SECTION 4 SPECIFICATIONS\nModel: EliteBook 840 G10, part number 7Z123UT#ABA, 16 GB RAM, 512 GB SSD.",
    ])
    make_pdf(folder / "Addendum 1 RFP ZX-1001.pdf", [
        "# ADDENDUM No. 1\nThe due date is extended to March 15, 2025 at 2:00 PM EST.\nAll other terms remain unchanged.",
    ])
    (folder / "notes.xyz").write_text("unsupported", encoding="utf-8")
    return folder


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        llm_api_key="test",
        index_dir=str(tmp_path / "index"),
        output_dir=str(tmp_path / "out"),
        cache_dir=str(tmp_path / "cache"),
        log_dir=str(tmp_path / "logs"),
        ocr_enabled=False,
        llm_cache_enabled=False,
        llm_tpm_limit=0,
        chunk_target_words=60,
        chunk_overlap_words=10,
        chunk_min_words=5,
    )


@pytest.fixture
def engine(settings: Settings) -> SearchEngine:
    return SearchEngine(settings, embedder=HashingEmbedder(), reranker=KeywordOverlapReranker())


@pytest.fixture
def indexed_engine(engine: SearchEngine, bid_folder: Path) -> SearchEngine:
    engine.index_folder(bid_folder)
    return engine


class FakeLLM:
    """Scripted LLM: `handler(system, user) -> dict` decides the JSON returned."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, str]] = []
        self.available = True

    def chat_json(self, system, user, schema, span=None, max_tokens=None, prefer_primary=False):
        self.calls.append((system, user))
        if span is not None:
            span.llm_call("fake", len(user) // 4, 50, 1.0)
        return schema.model_validate(self.handler(system, user))

    def chat_text(self, system, user, span=None, max_tokens=None):
        return json.dumps(self.handler(system, user))
