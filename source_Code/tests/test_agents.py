"""Multi-agent tests with a scripted fake LLM (no network).

Covers: LangGraph extraction workflow end-to-end, parallel group extraction,
addendum override, validator feedback loop (hallucinated value -> retry ->
fixed), guardrails, tracing, QA workflow, JSON repair utilities.
"""

from __future__ import annotations

import json
import re

import pytest

from agents.fields import load_field_config
from agents.grounding import ungrounded_tokens
from agents.orchestrator import Orchestrator
from agents.reconciliation_agent import ReconciliationAgent
from agents.schemas import AddendumChange, Evidence, FieldValue, LLMFieldExtraction, Source
from agents.services import build_services
from core.llm import extract_json, strip_reasoning
from tests.conftest import FakeLLM


def _evidence_ids(user: str, needle: str) -> list[str]:
    """Find evidence ids whose passage contains `needle`."""
    ids = []
    for m in re.finditer(r"\[(E\d+)\][^\n]*\n(.*?)(?=\n\n\[E\d+\]|\Z)", user, re.S):
        if needle.lower() in m.group(2).lower():
            ids.append(m.group(1))
    return ids


def scripted_handler(system: str, user: str) -> dict:
    if "Addendum Reconciliation Agent" in system:
        ids = _evidence_ids(user, "March 15")
        return {"changes": [{"field": "Due Date", "change_type": "modification",
                             "description": "Due date extended to March 15, 2025 2:00 PM EST",
                             "original_value": "2025-02-20 14:00 EST", "new_value": "2025-03-15 14:00 EST",
                             "evidence_ids": ids}]}
    if "Validator / Critic Agent" in system:
        fields = re.findall(r"FIELD: (.+)", user)
        return {"results": [{"field": f, "supported": "yes", "issue": ""} for f in fields]}
    if "Report Agent" in system:
        return {"summary": "Springfield School District seeks laptops. Proposals are due March 15, 2025. "
                           "A 5% bid bond is required."}
    if "rewrite search queries" in system:
        return {"queries": ["delivery within days of purchase order"]}
    if "Orchestrator/Planner" in system:
        return {"intent": "lookup", "bid_ids": ["BidZ"],
                "sub_queries": [{"query": "proposal due date extended", "bid_id": "BidZ"}]}
    if "Q&A Agent" in system:
        ids = _evidence_ids(user, "March 15")
        return {"answer": f"The due date was extended to March 15, 2025 at 2:00 PM EST [{ids[0]}].",
                "evidence_ids": ids[:1], "not_found": False, "confidence": 0.9}
    if "FIELDS TO EXTRACT" in user:
        requested = re.findall(r"^- (.+?) \[format", user, re.M)
        out = []
        for name in requested:
            item = {"field": name, "value": None, "evidence_ids": [], "confidence": 0.5, "notes": "Not found in documents"}
            if name == "Bid Bond Requirement" and (ids := _evidence_ids(user, "bid bond of 5%")):
                item.update(value="5% of the total bid amount", evidence_ids=ids[:1], confidence=0.95, notes="")
            if name == "Due Date" and (ids := _evidence_ids(user, "February 20")):
                item.update(value="2025-02-20 14:00 EST", evidence_ids=ids[:1], confidence=0.9, notes="")
            if name == "Delivery Date" and (ids := _evidence_ids(user, "within 30 days")):
                if "VALIDATOR FEEDBACK" in user:
                    item.update(value="Within 30 days of the purchase order", evidence_ids=ids[:1], confidence=0.9, notes="")
                else:  # first attempt hallucinates a number -> validator must catch it
                    item.update(value="Within 99 days of award", evidence_ids=ids[:1], confidence=0.9, notes="")
            if name == "Title" and (ids := _evidence_ids(user, "Laptop Refresh")):
                item.update(value="Laptop Refresh", evidence_ids=ids[:1], confidence=0.8, notes="")
            if name == "Part_no":  # value without any citation -> guardrail
                item.update(value="ABC-999", evidence_ids=[], confidence=0.6, notes="")
            out.append(item)
        return {"fields": out}
    raise AssertionError(f"unexpected prompt: {system[:80]}")


@pytest.fixture
def services(settings, indexed_engine):
    return build_services(settings=settings, engine=indexed_engine, llm=FakeLLM(scripted_handler))


def test_extraction_workflow_end_to_end(services, bid_folder):
    out = Orchestrator(services).extract("BidZ", str(bid_folder))
    rec = out["record"]
    f = rec["fields"]
    assert set(f) == set(services.fields.fields)  # all 20 fields present

    # addendum override applied with original value kept in notes
    assert f["Due Date"]["value"] == "2025-03-15 14:00 EST"
    assert "Addendum 1" in f["Due Date"]["notes"] and "2025-02-20" in f["Due Date"]["notes"]
    assert any("Addendum" in s["file"] for s in f["Due Date"]["sources"])

    # validator feedback loop fixed the hallucinated delivery value
    assert f["Delivery Date"]["value"] == "Within 30 days of the purchase order"

    # grounded value with citation and confidence
    assert f["Bid Bond Requirement"]["value"].startswith("5%")
    assert f["Bid Bond Requirement"]["sources"][0]["page"] == 2
    assert 0 < f["Bid Bond Requirement"]["confidence"] <= 1

    # guardrail: uncited value is rejected -> null + "Not found in documents"
    assert f["Part_no"]["value"] is None and "Not found in documents" in f["Part_no"]["notes"]
    assert f["Payment Terms"]["value"] is None and f["Payment Terms"]["sources"] == []

    assert f["Bid Summary"]["value"] and rec["validation"]["passed"] >= 4
    assert sum(rec["validation"].values()) == len(services.fields.fields)
    assert rec["addendum_changes"][0]["field"] == "Due Date" and rec["addendum_changes"][0]["applied"]

    # observability: trace has spans for every agent incl. a retry round
    trace = [json.loads(l) for l in open(out["trace"]["trace_file"], encoding="utf-8")]
    agents = {e["agent"] for e in trace if e["type"] == "span"}
    assert {"orchestrator", "ingestion_agent", "retrieval_agent", "addendum_reconciliation_agent",
            "validator_agent", "report_agent"} <= agents
    assert any(a.startswith("extraction_agent[") for a in agents)
    assert rec["run"]["retry_rounds"] >= 1
    assert any(tc["tool"] == "search_engine.search" for e in trace if e["type"] == "span" for tc in e["tool_calls"])


def test_extraction_survives_llm_failure(settings, indexed_engine, bid_folder):
    from core.llm import LLMError

    def broken(system, user):
        raise LLMError("timeout")

    svc = build_services(settings=settings, engine=indexed_engine, llm=FakeLLM(broken))
    rec = Orchestrator(svc).extract("BidZ", str(bid_folder))["record"]
    assert all(v["value"] is None for k, v in rec["fields"].items() if k != "Bid Summary")
    assert rec["validation"]["failed"] + rec["validation"]["not_found"] >= 19  # degraded gracefully, no crash
    assert "LLM error" in rec["fields"]["Due Date"]["notes"]


def test_qa_workflow(services):
    ans = Orchestrator(services).ask("When are proposals due now?", use_cache=False)
    assert "March 15, 2025" in ans.answer
    assert "Addendum 1 RFP ZX-1001.pdf" in ans.answer  # [E#] replaced by file/page citation
    assert ans.citations and ans.citations[0].file.startswith("Addendum")
    assert ans.validation["passed"] and not ans.not_found


def test_qa_not_found_guardrail(settings, indexed_engine):
    def handler(system, user):
        if "Orchestrator/Planner" in system:
            return {"intent": "lookup", "bid_ids": ["BidZ"], "sub_queries": [{"query": "insurance limits", "bid_id": "BidZ"}]}
        if "rewrite" in system:
            return {"queries": []}
        return {"answer": "Not found in documents.", "evidence_ids": [], "not_found": True, "confidence": 0.8}

    svc = build_services(settings=settings, engine=indexed_engine, llm=FakeLLM(handler))
    ans = Orchestrator(svc).ask("What are the insurance limits?", use_cache=False)
    assert ans.not_found and ans.answer.startswith("Not found in documents")


def test_uncited_answer_triggers_retry(settings, indexed_engine):
    calls = {"answer": 0}

    def handler(system, user):
        if "Orchestrator/Planner" in system:
            return {"intent": "lookup", "bid_ids": ["BidZ"], "sub_queries": [{"query": "bid bond", "bid_id": "BidZ"}]}
        if "rewrite" in system:
            return {"queries": ["bid security amount"]}
        calls["answer"] += 1
        if calls["answer"] == 1:
            return {"answer": "A bond of 10% is needed.", "evidence_ids": [], "not_found": False, "confidence": 0.9}
        ids = _evidence_ids(user, "5%")
        return {"answer": f"A 5% bid bond is required [{ids[0]}].", "evidence_ids": ids[:1], "confidence": 0.9}

    svc = build_services(settings=settings, engine=indexed_engine, llm=FakeLLM(handler))
    ans = Orchestrator(svc).ask("Is a bid bond required?", use_cache=False)
    assert calls["answer"] == 2 and "5%" in ans.answer and ans.validation["passed"]


# ------------------------------------------------------------------ unit pieces
def test_grounding_checks():
    ev = ["Proposals are due July 9, 2024 at 2:00 PM CST. Contact thawkins@treasurer.state.md .us or 410-260-7533."]
    assert ungrounded_tokens("2024-07-09 14:00 CST", ev) == []
    assert ungrounded_tokens("thawkins@treasurer.state.md.us, 410-260-7533", ev) == []
    assert "2024-07-10" in ungrounded_tokens("2024-07-10 14:00", ev)
    assert "15:00" in ungrounded_tokens("July 9, 2024 3:00 PM", ev)
    assert "555-1234" in ungrounded_tokens("call 555-1234", ev)


def test_reconciliation_apply_is_idempotent(settings):
    cfg = load_field_config(str(settings.path(settings.fields_config)))
    ev = Evidence(chunk_id="c1", bid_id="b", file="Addendum 2.pdf", page=1, page_end=1, doc_type="addendum",
                  text="The new due date for this RFP will be July 9, 2024 at 2:00 PM CST.")
    drafts = {"Due Date": FieldValue(value="2024-06-27 14:00 CST", sources=[Source(file="RFP.pdf", page=2)], status="extracted"),
              "Installation": FieldValue(value="White glove deployment", sources=[Source(file="RFP.pdf", page=3)], status="extracted")}
    changes = [
        AddendumChange(addendum_number=2, file="Addendum 2.pdf", field="Due Date", change_type="modification",
                       description="extended", new_value="2024-07-09 14:00 CST",
                       sources=[Source(file="Addendum 2.pdf", page=1)], evidence=[ev]),
        AddendumChange(addendum_number=1, file="Addendum 1.pdf", field="Installation", change_type="addition",
                       description="autopilot", new_value="Autopilot required for Windows",
                       sources=[Source(file="Addendum 1.pdf", page=4)]),
    ]
    once = ReconciliationAgent.apply(drafts, changes, cfg)
    assert once["Due Date"].value == "2024-07-09 14:00 CST"
    assert "original: 2024-06-27 14:00 CST" in once["Due Date"].notes
    assert once["Installation"].value.startswith("White glove deployment; Addendum 1:")  # append, not overwrite
    twice = ReconciliationAgent.apply(once, changes, cfg)
    assert twice["Due Date"].value == once["Due Date"].value
    assert twice["Installation"].value == once["Installation"].value


def test_llm_json_helpers():
    assert extract_json('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": "x}"}} trailing') == {"a": {"b": "x}"}}
    assert strip_reasoning("<think>secret") == ""
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_llm_field_coercion():
    item = LLMFieldExtraction.model_validate({"field": "x", "value": ["a", "b"], "evidence_ids": "E1, E2", "confidence": "0.7"})
    assert item.value == "a; b" and item.evidence_ids == ["E1", "E2"] and item.confidence == 0.7
    assert LLMFieldExtraction.model_validate({"field": "x", "value": "Not found in documents"}).value is None
