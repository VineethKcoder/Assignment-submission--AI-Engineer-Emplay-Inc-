"""Presentation helpers for the Streamlit chat.

The QA agent returns answers with full inline citations, e.g.
"... 45 days of award [PORFP_-_Dell_Laptop_Final.pdf, p.2; Addendum 1.pdf, pp.1-2]."
That format stays unchanged for the API, CLI and QA log. For reading in the UI, each
citation group becomes a small numbered superscript (hover shows the file and page) and
the files are listed once, numbered, in a muted footer.
"""

from __future__ import annotations

import html
import re
from typing import Iterable

_REF = r"[^\[\]]+?,\s*pp?\.\s*\d+(?:\s*-\s*\d+)?"
CITATION_GROUP = re.compile(rf"\s*\[({_REF}(?:\s*;\s*{_REF})*)\]")
REF_PARTS = re.compile(r"(?P<file>.+?),\s*(?P<page>pp?\.\s*\d+(?:\s*-\s*\d+)?)$")

SUP_STYLE = "font-size:0.68em;opacity:0.6;margin-left:1px;cursor:help"
FOOTER_STYLE = "font-size:0.78rem;margin:0.75rem 0 0.6rem;line-height:1.5"
MUTED_STYLE = "opacity:0.62"
SOURCE_STYLE = "margin-top:0.4rem;opacity:0.62"
SECTION_STYLE = "font-style:italic"
QUOTE_STYLE = "margin:0.1rem 0 0 1.25rem;padding-left:0.5rem;border-left:2px solid rgba(128,128,128,0.5);opacity:0.95"


def _page(page: str) -> str:
    return re.sub(r"\s+", "", page)


def _location(file: str, page: str, doc_type: str | None = None, start: int | None = None,
              end: int | None = None) -> str:
    """Human location inside the file: 'web page', 'p.4' or 'pp.4–5'."""
    if doc_type == "bid_page" or file.lower().endswith((".html", ".htm")):
        return "web page"
    if start and end and end > start:
        return f"pp.{start}–{end}"
    return page


def _src_location(file: str, page: str, src) -> str:
    return _location(file, page, getattr(src, "doc_type", None), getattr(src, "page", None),
                     getattr(src, "page_end", None))


def _source_line(n: int, file: str, page: str, src) -> str:
    """One numbered source: file · location · section, then the exact supporting sentence(s)."""
    location = _src_location(file, page, src)
    head = f"{html.escape(file)} · {location}"
    # A web page's "section" is just its title, which repeats the file name.
    section = "" if location == "web page" else " ".join((getattr(src, "section", None) or "").split())
    if section:
        section = section if len(section) <= 90 else section[:87] + "..."
        head += f" · <span style=\"{SECTION_STYLE}\">§ {html.escape(section)}</span>"
    quotes = "".join(f'<div style="{QUOTE_STYLE}">“{html.escape(" ".join(q.split()))}”</div>'
                     for q in (getattr(src, "quotes", None) or []))
    return f'<div style="{SOURCE_STYLE}">{n}&nbsp;&nbsp;{head}{quotes}</div>'


def render_answer(answer: str, citations: Iterable = (), confidence: float | None = None,
                  cached: bool = False) -> str:
    """Return Markdown+HTML for st.markdown(..., unsafe_allow_html=True)."""
    numbers: dict[tuple[str, str], int] = {}

    def number(file: str, page: str) -> int:
        key = (file.strip(), _page(page))
        if key not in numbers:
            numbers[key] = len(numbers) + 1
        return numbers[key]

    details = {}
    for c in citations:
        details.setdefault((c.file.strip(), f"p.{c.page}"), c)

    def hover(file: str, page: str) -> str:
        src = details.get((file, page))
        quote = (getattr(src, "quotes", None) or [""])[0]
        return f"{file}, {_src_location(file, page, src)}" + (f" — “{' '.join(quote.split())}”" if quote else "")

    parts, pos = [], 0
    for m in CITATION_GROUP.finditer(answer):
        parts.append(html.escape(answer[pos:m.start()], quote=False))
        refs: list[tuple[int, str]] = []
        for raw in m.group(1).split(";"):
            r = REF_PARTS.match(raw.strip())
            if not r:
                continue
            n = number(r["file"], r["page"])
            if all(n != existing for existing, _ in refs):
                refs.append((n, hover(r["file"].strip(), _page(r["page"]))))
        if refs:
            label = ",".join(str(n) for n, _ in refs)
            title = html.escape(" | ".join(t for _, t in refs), quote=True)
            parts.append(f'<sup title="{title}" style="{SUP_STYLE}">{label}</sup>')
        pos = m.end()
    parts.append(html.escape(answer[pos:], quote=False))

    # Sources the validator recorded but the text did not cite inline still get listed.
    for c in citations:
        number(c.file, f"p.{c.page}")

    lines = [_source_line(n, f, p, details.get((f, p))) for (f, p), n in numbers.items()]
    meta = []
    if confidence is not None:
        meta.append(f"confidence {confidence}")
    if cached:
        meta.append("cached")
    footer = f'<div style="{MUTED_STYLE}"><b>Sources</b></div>' + \
             ("".join(lines) if lines else f'<div style="{MUTED_STYLE}">none</div>')
    if meta:
        footer += f'<div style="{MUTED_STYLE};margin-top:0.4rem">{" · ".join(meta)}</div>'
    # Kept on one line: a blank line would end the HTML block in Markdown.
    return "".join(parts).strip() + f'\n\n<div style="{FOOTER_STYLE}">{footer}</div>'
