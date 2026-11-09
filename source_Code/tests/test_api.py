"""REST API tests (FastAPI TestClient, fake models + fake LLM)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agents.services import build_services
from tests.conftest import FakeLLM
from tests.test_agents import scripted_handler


@pytest.fixture
def client(monkeypatch, settings, indexed_engine):
    import api.server as server

    svc = build_services(settings=settings, engine=indexed_engine, llm=FakeLLM(scripted_handler))
    monkeypatch.setattr(server, "get_services", lambda: svc)
    if hasattr(server.app.state, "orch"):
        del server.app.state.orch
    return TestClient(server.app)


def test_health_and_bids(client):
    assert client.get("/health").json()["status"] == "ok"
    bids = client.get("/bids").json()
    assert bids[0]["bid_id"] == "BidZ"


def test_search_endpoint_with_filters(client):
    r = client.get("/search", params={"q": "bid bond", "bid_id": "BidZ", "top_k": 3})
    body = r.json()
    assert r.status_code == 200 and body["results"]
    first = body["results"][0]
    assert {"file_name", "page_number", "text", "citation", "scores"} <= set(first)
    r = client.get("/search", params={"q": "due date", "doc_type": "addendum"})
    assert all(x["doc_type"] == "addendum" for x in r.json()["results"])
    assert client.get("/search", params={"q": "x", "mode": "bogus"}).status_code == 422


def test_index_endpoint(client, bid_folder):
    r = client.post("/index", json={"folder": str(bid_folder)})
    assert r.json()["status"] == "up_to_date"
    assert client.post("/index", json={"folder": "/no/such/folder"}).status_code == 404


def test_ask_and_extract_endpoints(client, bid_folder):
    ans = client.post("/ask", json={"question": "When is the due date?", "use_cache": False}).json()
    assert "March 15, 2025" in ans["answer"] and ans["citations"]
    out = client.post("/extract", json={"folder": str(bid_folder)}).json()
    assert out["record"]["fields"]["Due Date"]["value"] == "2025-03-15 14:00 EST"
    rec = client.get("/records/BidZ").json()
    assert rec["bid_id"] == "BidZ"
    assert client.get("/records/Nope").status_code == 404
