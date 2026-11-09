"""Pick the most relevant sentence of a passage, verbatim, so a reader can find it in the original document.

Used by the QA agent (quote that supports each cited claim) and the search UI (one-line answer per hit).
"""

from __future__ import annotations

import re

from search.bm25 import tokenize


def _answer_after(units: list[str], i: int) -> int | None:
    """Index of an 'Answer: ...' sentence that follows units[i], allowing question sentences in between."""
    for j in range(i + 1, min(i + 3, len(units))):
        if units[j].lower().startswith(("answer", "response")):
            return j
        if not units[j].rstrip().endswith("?"):
            return None
    return None


def supporting_excerpt(text: str, claim: str, max_chars: int = 320) -> str:
    """The sentence (or table row) of `text` that best matches `claim`. Identifiers and numbers count double.
    Empty if nothing overlaps."""
    units = [u.strip() for u in re.split(r"(?<=[.!?])\s+|\n+", text) if u.strip()]
    wanted = set(tokenize(claim))
    if not units or not wanted:
        return ""

    def score(unit: str) -> int:
        return sum(2 if any(ch.isdigit() for ch in t) else 1 for t in wanted & set(tokenize(unit)))

    scores = [score(u) for u in units]
    i = max(range(len(units)), key=lambda k: scores[k])
    if scores[i] == 0:
        return ""
    best = units[i]
    if len(tokenize(best)) < 4 and i > 0:  # e.g. "Answer: Yes." - keep the question it answers
        best = f"{units[i - 1]} {best}"
    elif (j := _answer_after(units, i)) is not None:  # addendum Q&A: keep the answer
        best = " ".join(units[i:j + 1])
    if len(best) > max_chars:  # long table rows: window around the first matching term
        low = best.lower()
        hits = [low.find(t) for t in wanted if low.find(t) >= 0]
        start = max(0, min(hits) - 60) if hits else 0
        if start:  # snap to word boundaries so the quote stays searchable
            start = best.find(" ", start) + 1 or start
        piece = best[start:start + max_chars]
        piece = piece[:piece.rfind(" ")] if " " in piece else piece
        best = ("..." if start else "") + piece.strip() + "..."
    return best
