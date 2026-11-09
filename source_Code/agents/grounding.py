"""Deterministic hallucination checks shared by the validators.

A value is *grounded* when every "hard" token it contains - numbers, ids,
emails, dates - also appears in the evidence it cites. Dates are compared by
calendar day (so '2024-07-09' matches 'July 9, 2024'); times are compared on
a 24h basis ('14:00' matches '2:00 PM').
"""

from __future__ import annotations

import re

from dateutil import parser as dateparser

from ingestion.classify import DATE_PATTERNS, find_dates

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?", re.I)
_HARD = re.compile(r"[a-z]*\d[\w\-./#]*", re.I)
_ISO_DT = re.compile(r"\b(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?")


def _squash(s: str) -> str:
    return re.sub(r"[\s,]+", "", s.lower())


def _times(text: str) -> set[str]:
    out = set()
    for h, m, ap in _TIME.findall(text):
        hh = int(h)
        if ap:
            ap = ap.lower().replace(".", "")
            if ap.startswith("p") and hh < 12:
                hh += 12
            if ap.startswith("a") and hh == 12:
                hh = 0
        if hh < 24:
            out.add(f"{hh:02d}:{m}")
    return out


def _dates(text: str) -> set[str]:
    found = {iso for _, iso in find_dates(text)}
    for m in _ISO_DT.finditer(text):
        found.add(m.group(1))
    return found


def ungrounded_tokens(value: str, evidence_texts: list[str]) -> list[str]:
    """Return hard tokens of `value` that do not appear in any evidence text."""
    if not value:
        return []
    ev = "\n".join(evidence_texts)
    ev_sq = _squash(ev)
    missing: list[str] = []

    # 1. dates by calendar day
    v_dates = _dates(value)
    ev_dates = _dates(ev)
    for d in v_dates:
        if d not in ev_dates:
            missing.append(d)
    # 2. times on 24h basis
    v_times = _times(value)
    if v_times:
        ev_times = _times(ev)
        missing.extend(t for t in v_times if t not in ev_times)
    # strip dates / times before token checks
    stripped = value
    for pat in DATE_PATTERNS:
        stripped = pat.sub(" ", stripped)
    stripped = _ISO_DT.sub(" ", stripped)
    stripped = _TIME.sub(" ", stripped)
    # 3. emails
    for em in _EMAIL.findall(stripped):
        if _squash(em) not in ev_sq:
            missing.append(em)
    stripped = _EMAIL.sub(" ", stripped)
    # 4. other hard tokens (numbers / ids)
    for tok in _HARD.findall(stripped):
        tok = tok.strip(".-/#,;:")
        if not tok or not re.search(r"\d", tok):
            continue
        if len(re.sub(r"\D", "", tok)) < 2 and not re.search(r"[a-z]", tok, re.I):
            continue  # single digits ("3 years") are too ambiguous to check strictly
        if _squash(tok) in ev_sq:
            continue
        # tolerate unit formatting: "16GB" vs "16 GB", "$1,000" vs "1000"
        digits = re.sub(r"[^\d.]", "", tok)
        if digits and len(digits) >= 2 and digits in re.sub(r"[^\d.]", " ", ev):
            continue
        missing.append(tok)
    return list(dict.fromkeys(missing))


def parse_datetime(value: str):
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # unknown tz names (EDT/CST) are fine for validation
            return dateparser.parse(value, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None


def has_time(value: str) -> bool:
    return bool(_TIME.search(value or ""))
