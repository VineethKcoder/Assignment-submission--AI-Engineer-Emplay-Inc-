"""Regressions for summary coverage and stale answers after policy changes."""

import json

from agents.qa_agent import QAAgent, SemanticCache
from agents.schemas import Evidence, QAAnswer, QAPlan, SubQuery
from agents.services import build_services
from tests.conftest import FakeLLM


def test_summary_plan_includes_available_portal_when_planner_omits_it(settings, indexed_engine):
    def planner(system, user):
        return {"intent": "summary", "bid_ids": ["BidZ"], "sub_queries": [
            {"query": "general instructions", "bid_id": "BidZ", "doc_type": "rfp"}
        ]}
    svc = build_services(settings=settings, engine=indexed_engine, llm=FakeLLM(planner))
    plan = QAAgent(svc).plan("What do I need to do?", None)
    assert any(sq.doc_type == "bid_page" and sq.bid_id == "BidZ" for sq in plan.sub_queries)
    assert any(sq.query == "general instructions" for sq in plan.sub_queries)


def test_summary_keeps_low_scoring_topics_in_answer_context(settings, engine):
    # General instructions can outrank every warranty/product hit. They must
    # not consume the entire answer context for a broad bidding checklist.
    topics = {
        "eligibility": "Only certified small businesses may submit through the portal.",
        "deadline": "Closing date: June 10, 2024 at 2:00 PM EDT. This saved page says CLOSED.",
        "products": "Quantity: 30 laptops and 30 docking stations.",
        "warranty": "Three-year hardware warranty from delivery; certificate upon award.",
        "delivery": "Delivery within 45 days of award. Quotes valid at least 90 days after the due date.",
        "invoicing": "Submit invoices within 10 days of delivering equipment.",
    }
    plan = QAPlan(intent="summary", bid_ids=["UnseenBid"], sub_queries=[
        SubQuery(query=q, bid_id="UnseenBid") for q in topics
    ])
    evidence = [Evidence(chunk_id=f"instructions-{i}", bid_id="UnseenBid", file="RFP.pdf",
                         page=1, page_end=1, doc_type="rfp", score=0.99,
                         query="eligibility", text=topics["eligibility"])
                for i in range(15)]
    evidence += [Evidence(chunk_id=q, bid_id="UnseenBid", file="RFP.pdf", page=2, page_end=2,
                          doc_type="rfp", score=0.01, query=q, text=text)
                 for q, text in topics.items() if q != "eligibility"]

    def answer(system, user):
        for passage in topics.values():
            assert passage in user
        return {"answer": "Not found in documents.", "not_found": True}

    svc = build_services(settings=settings, engine=engine, llm=FakeLLM(answer))
    _, pool = QAAgent(svc).answer("What do I need to do?", evidence, plan)
    assert len(pool) <= 12
    assert len({e.chunk_id for e in pool}) == len(pool)


def test_policy_change_does_not_reuse_old_semantic_answers(settings, indexed_engine, monkeypatch):
    svc = build_services(settings=settings, engine=indexed_engine)
    cache = SemanticCache(svc)
    question = "What do I need to do?"
    answer = QAAnswer(question=question, answer="Original answer", confidence=0.9)
    cache.put(question, None, answer)
    assert cache.get(question, None) is not None

    from agents import qa_agent
    monkeypatch.setattr(qa_agent, "ANSWER_SYSTEM", qa_agent.ANSWER_SYSTEM + "\nUpdated policy.")
    assert cache.get(question, None) is None

    # The persisted pre-fix key has no policy fingerprint at all.
    entries = [json.loads(line) for line in cache.path.read_text(encoding="utf-8").splitlines()]
    entries[0]["key"] = entries[0]["key"].rsplit("#", 1)[0]
    cache.entries = entries
    assert cache.get(question, None) is None
