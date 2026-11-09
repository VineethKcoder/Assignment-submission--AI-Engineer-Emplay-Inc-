"""Q&A sources pinpoint the exact supporting sentence, section and page range."""

from agents.qa_agent import QAAgent, claim_before, supporting_excerpt
from agents.schemas import Evidence, LLMAnswer, QAPlan

PASSAGE = (
    "SECTION 4 DELIVERY. All equipment must be delivered to the district warehouse. "
    "Delivery must occur within 45 days of award. Late delivery may result in cancellation.\n"
    "210-BLYZ | Intel Core Ultra 5 125U processor | 1"
)


def test_claim_before_stops_at_previous_citation():
    text = "OEM warranties were required [E5], local locations were not mandatory [E5]."
    assert claim_before(text, text.rindex("[E5]")) == "local locations were not mandatory"
    assert claim_before(text, text.index("[E5]")) == "OEM warranties were required"


def test_supporting_excerpt_picks_matching_sentence_and_table_row():
    assert supporting_excerpt(PASSAGE, "delivery within 45 days of award") == \
        "Delivery must occur within 45 days of award."
    assert supporting_excerpt(PASSAGE, "The 210-BLYZ processor") == \
        "210-BLYZ | Intel Core Ultra 5 125U processor | 1"
    assert supporting_excerpt(PASSAGE, "quantum entanglement") == ""


def test_supporting_excerpt_keeps_addendum_answer_with_matched_question():
    qa = ("Question: Is the awarded vendor required to have a local location in Dallas? "
          "Answer: No, a local location is not required.")
    assert supporting_excerpt(qa, "a local location in Dallas was not mandatory") == qa
    two_part = ("16. On page 7, question #4 asks for the address of their local location. "
                "Is a local location a requirement? Answer: No, a local location is not required. "
                "17. During the pre-bid call OEM warranty was mentioned.")
    assert supporting_excerpt(two_part, "local locations were not mandatory").endswith(
        "Answer: No, a local location is not required.")


def test_supporting_excerpt_keeps_semicolon_lists_in_one_sentence():
    text = ("Although Offeror must sign, no enforceable contract will be formed unless (i) the Director formally "
            "accepts the Offer; and (ii) the Board of Trustees has granted all approvals. "
            "Once an enforceable Contract has been formed, the District will issue a formal Purchase Order.")
    assert "Board of Trustees" in supporting_excerpt(
        text, "No enforceable contract formed until formal acceptance and Board approval")


def test_format_attaches_quotes_section_and_page_range():
    ev = Evidence(evidence_id="E1", chunk_id="c1", bid_id="B", file="RFP.pdf", page=4, page_end=5,
                  doc_type="rfp", section="SECTION 4 DELIVERY", text=PASSAGE)
    ans = LLMAnswer(answer="Delivery is due within 45 days of award [E1].", evidence_ids=["E1"], confidence=0.9)
    out = QAAgent.format("When is delivery due?", ans, [ev], QAPlan(intent="lookup", bid_ids=["B"]),
                         {"passed": True, "cited": ["E1"]})
    assert out.answer == "Delivery is due within 45 days of award [RFP.pdf, p.4]."
    src = out.citations[0]
    assert src.quotes == ["Delivery must occur within 45 days of award."]
    assert src.quote == src.quotes[0]
    assert (src.page, src.page_end, src.section, src.doc_type) == (4, 5, "SECTION 4 DELIVERY", "rfp")
