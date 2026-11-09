"""Chat answers render citations as small numbered superscripts, not inline file names."""

import re

from agents.schemas import Source
from ui.formatting import render_answer

ANSWER = (
    "The main task in Bid 2 is to supply 30 Dell Latitude 5550 laptops "
    "[PORFP_-_Dell_Laptop_Final.pdf, p.3]. The bid is closed "
    "[Dell Laptops w_Extended Warranty - Bid Information - {3} _ BidNet Direct.html, p.1]. "
    "Delivery within 45 days of award [PORFP_-_Dell_Laptop_Final.pdf, p.2]; warranty "
    "[PORFP_-_Dell_Laptop_Final.pdf, p.3; Addendum 1 RFP.pdf, pp.1-2]. A < B & C."
)


def body_and_footer(rendered):
    body, footer = rendered.split('\n\n<div style="', 1)
    return body, footer


def test_inline_file_names_become_numbered_superscripts():
    body, footer = body_and_footer(render_answer(ANSWER, confidence=1.0))
    visible = re.sub(r"<sup[^>]*>.*?</sup>", "", body)
    assert ".pdf" not in visible and ".html" not in visible and "[" not in visible
    # repeated sources reuse their number, multiple refs share one superscript
    assert re.findall(r">([\d,]+)</sup>", body) == ["1", "2", "3", "1,4"]
    assert "laptops<sup" in body  # no dangling space before the marker
    assert 'title="PORFP_-_Dell_Laptop_Final.pdf, p.3"' in body
    assert "A &lt; B &amp; C." in body


def test_footer_lists_each_source_once_with_confidence():
    _, footer = body_and_footer(render_answer(ANSWER, confidence=0.9, cached=True))
    assert footer.count("PORFP_-_Dell_Laptop_Final.pdf · p.3") == 1
    assert "4&nbsp;&nbsp;Addendum 1 RFP.pdf · pp.1-2" in footer
    assert "confidence 0.9 · cached" in footer
    assert "\n\n" not in footer  # a blank line would break the HTML block


def test_sources_pinpoint_section_page_range_and_quote():
    cites = [
        Source(file="PORFP_-_Dell_Laptop_Final.pdf", page=3, page_end=4, section="SCOPE OF\nWORK",
               doc_type="rfp", quotes=["The Contractor shall deliver within 45 days of award."]),
        Source(file="Portal - Bid Information.html", page=1, doc_type="bid_page", quotes=["Status: CLOSED"]),
    ]
    rendered = render_answer("Delivery within 45 days [PORFP_-_Dell_Laptop_Final.pdf, p.3]. "
                             "Closed [Portal - Bid Information.html, p.1].", cites)
    body, footer = body_and_footer(rendered)
    assert "PORFP_-_Dell_Laptop_Final.pdf · pp.3–4" in footer
    assert "§ SCOPE OF WORK" in footer
    assert "“The Contractor shall deliver within 45 days of award.”" in footer
    assert "Portal - Bid Information.html · web page" in footer and "· p.1" not in footer
    assert "“Status: CLOSED”" in footer
    assert "within 45 days of award" in body.split("</sup>")[0]  # quote also shown on hover


def test_validator_sources_without_inline_marker_are_still_listed():
    rendered = render_answer("Not found in documents.", [Source(file="RFP.pdf", page=7)])
    assert "Not found in documents." in rendered
    assert "1&nbsp;&nbsp;RFP.pdf · p.7" in rendered
