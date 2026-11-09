"""UI startup and indexing must work without downloading models."""

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from agents.services import build_services


APP = Path(__file__).resolve().parents[1] / "ui" / "app.py"


def test_ui_starts_without_models_and_warns_about_old_indexes(settings, engine, monkeypatch):
    svc = build_services(settings=settings, engine=engine)
    monkeypatch.setattr("agents.services.get_services", lambda: svc)
    monkeypatch.setattr(engine.store, "list_bids", lambda: ["OldBid"])
    monkeypatch.setattr(engine.store, "manifest", lambda _: {"embedding_model": "old-model"})
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(APP)).run(timeout=20)
        assert not app.exception
        assert app.title[0].value == "RFP Intelligence Platform"
        assert "OldBid" in app.warning[0].value
        assert app.chat_input[0].disabled
    finally:
        st.cache_resource.clear()


def test_index_button_refreshes_bid_choices(settings, engine, bid_folder, monkeypatch):
    svc = build_services(settings=settings, engine=engine)
    monkeypatch.setattr("agents.services.get_services", lambda: svc)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(APP)).run(timeout=20)
        app.sidebar.text_input[0].set_value(str(bid_folder))
        app.sidebar.button[0].click().run(timeout=20)
        assert not app.exception
        assert "BidZ" in app.selectbox(key="extract_bid").options
        assert not app.chat_input[0].disabled
    finally:
        st.cache_resource.clear()
