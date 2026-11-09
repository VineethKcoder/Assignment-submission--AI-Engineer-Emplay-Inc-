"""Text cleaning utilities: unicode normalisation, de-hyphenation, line repair,
repeated header/footer detection and whitespace normalisation."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

_REPLACEMENTS = {
    "​": "",  # zero-width space
    "­": "",  # soft hyphen
    "﻿": "",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "•": "-",  # bullet
    "": "-",  # private-use bullet (Word exports)
    "●": "-",
    " ": " ",
    "…": "...",
}

_SENT_END = re.compile(r"[.:;!?)\]]$")
_LIST_START = re.compile(r"^(\-|\*|\d+[.)]|[a-zA-Z][.)]|\(?[ivxIVX]+\))\s")


def normalize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for src, dst in _REPLACEMENTS.items():
        text = text.replace(src, dst)
    return text


def fix_hyphenation(text: str) -> str:
    """Join words broken across lines: 'pro-\\nposal' -> 'proposal'."""
    return re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", text)


def join_broken_lines(text: str) -> str:
    """Re-flow lines that were broken mid-sentence by PDF layout.

    A newline is kept when the previous line ends a sentence/label or the next
    line starts a list item; otherwise lines are joined with a space.
    """
    lines = [ln.strip() for ln in text.split("\n")]
    out: list[str] = []
    for ln in lines:
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        if not out or out[-1] == "":
            out.append(ln)
            continue
        prev = out[-1]
        if ln in ("-", "*"):  # bare bullet glyph on its own line starts a new item
            out.append(ln)
            continue
        if prev in ("-", "*"):
            out[-1] = f"{prev} {ln}"
            continue
        if _SENT_END.search(prev) or _LIST_START.match(ln) or (prev.isupper() and len(prev.split()) <= 8):
            out.append(ln)
        else:
            out[-1] = f"{prev} {ln}"
    return "\n".join(out).strip()


_EMAIL_BREAK = re.compile(r"([\w.+-]+@[\w-]+(?:\.[\w-]+)*)(?:\s+\.|\.\s+)([a-z][\w-]*(?:\.[a-z][\w-]*)*)\b", re.I)


def repair_emails(text: str) -> str:
    """Re-join e-mail addresses split by PDF line wrapping: 'a@b.state.md .us' -> 'a@b.state.md.us'.

    Only joins when the continuation is short and lowercase-like (a domain part), so a sentence that
    simply ends with an e-mail address followed by a new sentence is left alone."""
    def _join(m: re.Match) -> str:
        tail = m.group(2)
        if len(tail) <= 10 and tail.islower():
            return f"{m.group(1)}.{tail}"
        return m.group(0)

    prev = None
    while prev != text:
        prev = text
        text = _EMAIL_BREAK.sub(_join, text)
    return text


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # dotted leader lines / underscores used as form fields
    text = re.sub(r"[.…]{6,}", " ", text)
    text = re.sub(r"[-_]{8,}", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_block_text(text: str) -> str:
    text = normalize_unicode(text)
    text = fix_hyphenation(text)
    text = join_broken_lines(text)
    return repair_emails(normalize_whitespace(text))


def line_signature(line: str) -> str:
    """Normalise a line so 'Page 3 of 40' and 'Page 4 of 40' compare equal."""
    s = normalize_unicode(line).strip().lower()
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s)


def detect_repeated_lines(pages_lines: list[list[str]], min_ratio: float = 0.5, min_pages: int = 3) -> set[str]:
    """Return signatures of lines that repeat on many pages (running headers/footers).

    `pages_lines` holds candidate lines per page (typically lines located in
    the top/bottom margin). A signature is boilerplate when it appears on at
    least `min_ratio` of pages (and at least `min_pages` pages).
    """
    n_pages = len(pages_lines)
    if n_pages < min_pages:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages_lines:
        counts.update({line_signature(l) for l in lines if l.strip()})
    threshold = max(min_pages, int(n_pages * min_ratio))
    return {sig for sig, c in counts.items() if c >= threshold and len(sig) > 2}


def word_count(text: str) -> int:
    return len(text.split())
