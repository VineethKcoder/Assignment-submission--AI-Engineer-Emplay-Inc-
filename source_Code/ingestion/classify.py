"""Rule-based document classification and metadata extraction.

* doc_type (bid_page / rfp / addendum / specs / affidavit / form / other)
* addendum_number
* document date (ISO) where one can be found near the top of the document
* a human-readable title guess
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from dateutil import parser as dateparser

from ingestion.models import DocType, ParsedDocument

_MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_PATTERNS = [
    re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}},?\s+\d{{4}}\b", re.I),  # July 9, 2024
    re.compile(rf"\b\d{{1,2}}[- ]{_MONTHS}[- ]\d{{2,4}}\b", re.I),  # 26-MAY-2024
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),  # 05/24/2024
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # 2024-05-24
]
_DATE_KEYWORDS = re.compile(
    r"(issue\s*date|date\s*issued|dated|publication|issued|release\s*date|(?<!due )(?<!closing )(?<!opening )\bdate\b)\s*[:\-]?",
    re.I,
)


@lru_cache(maxsize=4)
def load_rules(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def find_dates(text: str) -> list[tuple[int, str]]:
    """Return (position, ISO date) for every parseable date in `text`."""
    found: list[tuple[int, str]] = []
    for pat in DATE_PATTERNS:
        for m in pat.finditer(text):
            try:
                dt = dateparser.parse(m.group(0), fuzzy=True, dayfirst=False)
            except (ValueError, OverflowError):
                continue
            if 1990 <= dt.year <= 2100:
                found.append((m.start(), dt.date().isoformat()))
    found.sort()
    return found


def extract_doc_date(text: str) -> str | None:
    """Prefer a date that follows an 'issue date / dated' keyword, else the first date."""
    dates = find_dates(text)
    if not dates:
        return None
    for kw in _DATE_KEYWORDS.finditer(text):
        for pos, iso in dates:
            if 0 <= pos - kw.end() <= 60:
                return iso
    return None  # an un-labelled date is more often a deadline than the document date


def classify(doc: ParsedDocument, rules_path: str) -> tuple[DocType, int | None]:
    rules = load_rules(rules_path)
    name = Path(doc.file_name).stem.lower().replace("_", " ")
    head = doc.first_page_text(1200)
    head_lines = "\n".join(head.splitlines()[:15])

    if doc.file_type == "html":
        doc_type = DocType.BID_PAGE
    else:
        doc_type = None
        for rule in rules.get("rules", []):
            if any(re.search(p, name, re.I) for p in rule.get("filename", [])) or any(
                re.search(p, head_lines, re.I | re.M) for p in rule.get("first_page", [])
            ):
                doc_type = DocType(rule["doc_type"])
                break
        doc_type = doc_type or DocType(rules.get("default", "rfp"))

    addendum_number = None
    if doc_type == DocType.ADDENDUM:
        for pat in rules.get("addendum_number_patterns", []):
            m = re.search(pat, name, re.I) or re.search(pat, head_lines, re.I)
            if m:
                addendum_number = int(m.group(1))
                break
        if addendum_number is None:
            addendum_number = 1
    return doc_type, addendum_number


def guess_title(doc: ParsedDocument) -> str:
    """File names of bid documents are usually descriptive; use them as the title."""
    return re.sub(r"[_\s]+", " ", Path(doc.file_name).stem).strip()


def enrich_metadata(doc: ParsedDocument, rules_path: str) -> ParsedDocument:
    doc.doc_type, doc.addendum_number = classify(doc, rules_path)
    doc.doc_date = extract_doc_date(doc.first_page_text(2500))
    doc.title = doc.title or guess_title(doc)
    return doc
