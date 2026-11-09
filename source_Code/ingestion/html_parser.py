"""HTML bid-page parsing with BeautifulSoup.

Portal pages (BidNet, BidSync, eMMA, ...) wrap a few facts in a lot of
navigation chrome. The parser:

1. drops scripts/styles/nav/header/footer/cookie banners and hidden elements;
2. converts label/value widgets into "Label: Value" lines - covers <dl>,
   two-column <table> rows and the common `*label*` + sibling `*body|value*`
   div pattern;
3. keeps real tables as table blocks;
4. emits headings (h1-h4) so chunking stays section-aware;
5. removes menu noise: very short lines that repeat on the page.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from ingestion.cleaning import normalize_unicode, normalize_whitespace
from ingestion.models import Block, ParsedDocument

logger = logging.getLogger("rfp.ingestion.html")

_NOISE_ATTR = re.compile(
    r"(^|[\s_-])(nav|navbar|menu|breadcrumb|cookie|banner|modal|popup|share|social|sidebar|toolbar|skip|"
    r"login|logout|avatar|footer|site-header|tooltip|accessibility-hidden|print-options)([\s_-]|$)",
    re.I,
)
_LABEL_CLS = re.compile(r"(^|[-_])(label|field-?name|key|term|caption)$", re.I)
_VALUE_CLS = re.compile(r"(^|[-_])(body|value|field-?value|data|desc|description)$", re.I)


def _is_label(el: Tag) -> bool:
    cls = el.get("class") or []
    return any(_LABEL_CLS.search(c) and not c.lower().startswith("no-") for c in cls)


def _is_value(el: Tag) -> bool:
    cls = el.get("class") or []
    return any(_VALUE_CLS.search(c) for c in cls)
_BLOCK_TAGS = {"p", "div", "li", "section", "article", "td", "th", "dd", "dt", "span", "a", "label"}


def _txt(el: Tag | NavigableString) -> str:
    s = el.get_text(" ", strip=True) if isinstance(el, Tag) else str(el)
    return normalize_whitespace(normalize_unicode(s))


def _attrs(el: Tag) -> str:
    cls = el.get("class") or []
    return " ".join(cls if isinstance(cls, list) else [cls]) + " " + (el.get("id") or "")


def _strip_noise(soup: BeautifulSoup) -> None:
    for t in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer", "button", "input", "select",
                   "option", "form-control", "img", "meta", "link"]):
        t.decompose()
    for t in list(soup.find_all(True)):
        if not isinstance(t, Tag) or t.attrs is None:
            continue
        style = (t.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or t.get("aria-hidden") == "true" or t.get("hidden") is not None:
            t.decompose()
            continue
        if t.name in ("header",) or _NOISE_ATTR.search(_attrs(t)):
            # never drop big content containers by accident
            if len(_txt(t)) < 400:
                t.decompose()


def _convert_label_values(root: Tag, soup: BeautifulSoup) -> None:
    """Replace label/value widgets by a single <p>Label: Value</p>."""
    # <dl><dt>label</dt><dd>value</dd>
    for dl in root.find_all("dl"):
        lines = []
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                lines.append(f"{_txt(dt).rstrip(':')}: {_txt(dd)}")
        if lines:
            p = soup.new_tag("p")
            p.string = "\n".join(lines)
            p["data-kv"] = "1"
            dl.replace_with(p)
    # label element followed by value sibling
    for lab in root.find_all(_is_label):
        if not isinstance(lab, Tag) or lab.parent is None:
            continue
        val = lab.find_next_sibling(_is_value) or lab.find_next_sibling()
        label, value = _txt(lab).rstrip(":"), _txt(val) if val else ""
        if not label or not value or len(label) > 80:
            continue
        p = soup.new_tag("p")
        p.string = f"{label}: {value}"
        p["data-kv"] = "1"
        if val:
            val.decompose()
        lab.replace_with(p)


def _table_rows(table: Tag) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [_txt(c) for c in tr.find_all(["td", "th"])]
        if any(cells):
            rows.append(cells)
    return rows


def parse_html(path: Path, bid_id: str) -> ParsedDocument:
    doc = ParsedDocument(bid_id=bid_id, file_name=path.name, file_path=str(path), file_type="html", num_pages=1)
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        doc.warnings.append(f"unreadable HTML: {exc}")
        return doc
    soup = BeautifulSoup(raw, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else None
    doc.title = normalize_unicode(title).split("|")[0].strip() if title else None

    _strip_noise(soup)
    root = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
    _convert_label_values(root, soup)

    blocks: list[Block] = []
    kv_buffer: list[str] = []

    def flush_kv() -> None:
        if kv_buffer:
            blocks.append(Block(kind="kv", text="\n".join(kv_buffer), page=1))
            kv_buffer.clear()

    consumed: set[int] = set()  # elements whose text was already emitted
    for el in root.descendants:
        if not isinstance(el, Tag):
            continue
        if any(id(p) in consumed for p in el.parents):
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            consumed.add(id(el))
            text = _txt(el)
            if text:
                flush_kv()
                blocks.append(Block(kind="heading", text=text, page=1, level=int(el.name[1])))
        elif el.name == "table":
            consumed.add(id(el))
            rows = _table_rows(el)
            if not rows:
                continue
            if max(len(r) for r in rows) == 2:
                kv_buffer.extend(f"{r[0].rstrip(':')}: {r[1]}" for r in rows if len(r) == 2 and r[0] and r[1])
            else:
                flush_kv()
                blocks.append(Block(kind="table", text="\n".join(" | ".join(r) for r in rows), page=1, rows=rows))
        elif el.name == "p" and el.get("data-kv"):
            consumed.add(id(el))
            kv_buffer.extend(el.get_text().split("\n"))
        elif el.name in _BLOCK_TAGS:
            # leaf-ish text containers only (no block children)
            if el.find(["p", "div", "li", "table", "h1", "h2", "h3", "h4", "ul", "ol", "dl"]):
                continue
            consumed.add(id(el))
            text = _txt(el)
            if text:
                flush_kv()
                blocks.append(Block(kind="text", text=text, page=1))
    flush_kv()

    # drop menu noise: short text lines repeated on the page, and exact duplicates
    counts = Counter(b.text for b in blocks if b.kind == "text")
    cleaned: list[Block] = []
    seen: set[str] = set()
    for b in blocks:
        if b.kind == "text":
            if len(b.text.split()) <= 3 and counts[b.text] > 1:
                continue
            if b.text in seen:
                continue
            seen.add(b.text)
        cleaned.append(b)
    # drop dangling headings with no content until next heading
    final: list[Block] = []
    for i, b in enumerate(cleaned):
        if b.kind == "heading":
            nxt = cleaned[i + 1] if i + 1 < len(cleaned) else None
            if nxt is None or nxt.kind == "heading" and nxt.level <= b.level:
                continue
        final.append(b)
    doc.blocks = final
    if not final:
        doc.warnings.append("no content extracted from HTML")
    return doc
