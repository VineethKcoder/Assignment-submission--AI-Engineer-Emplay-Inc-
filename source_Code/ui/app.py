"""Streamlit UI: search, chat (Q&A) and extraction over indexed bids.

    streamlit run ui/app.py
"""

from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from agents.orchestrator import Orchestrator  # noqa: E402
from agents.services import get_services  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from ui.formatting import render_answer  # noqa: E402

setup_logging()
st.set_page_config(page_title="RFP Intelligence", layout="wide")


@st.cache_resource(show_spinner="Loading index ...")
def services():
    # SearchEngine loads models on demand. Warming up here blocks the entire
    # page on large downloads, even when the user only wants saved results.
    return get_services()


@contextmanager
def operation(message):
    try:
        with st.spinner(message):
            yield
    except Exception:
        logging.getLogger("rfp.ui").exception("UI operation failed: %s", message)
        st.error("This operation could not finish. Check logs/rfp.log for details. "
                 "Model downloads require internet access; Q&A and extraction also "
                 "require a valid LLM API key in .env.")
        st.stop()


@st.cache_resource
def orchestrator():
    return Orchestrator(services())


st.title("RFP Intelligence Platform")
svc = services()
bids = svc.engine.list_bids()
st.caption(f"{len(bids)} bids indexed · LLM `{svc.settings.llm_model}` · embeddings `{svc.engine.embedding_model_name}`")
unavailable = sorted(set(svc.engine.store.list_bids()) - set(bids))
if unavailable:
    st.warning("Saved indexes need rebuilding: " + ", ".join(unavailable) +
               ". They could not be loaded with the current embedding model. "
               "Use the sidebar to index each bid folder again.")
if not bids:
    st.info("Index a bid folder in the sidebar to enable search, Q&A and extraction. "
            "The first indexing or semantic search may download AI models.")

with st.sidebar:
    st.header("Index a bid folder")
    folder = st.text_input("Folder path", value=str(ROOT / "data" / "Bid1"))
    force = st.checkbox("Force re-index")
    if st.button("Index"):
        with operation("Parsing, chunking and embedding (first use may download a model) ..."):
            rep = svc.engine.index_folder(folder, force=force)
        if rep.status == "failed":
            st.error(f"Could not index {folder}. Check the folder and its supported documents.")
        else:
            st.rerun()
    st.divider()
    for c in svc.engine.catalog():
        with st.expander(f"{c['bid_id']} ({c['chunks']} chunks)"):
            for d in c["documents"]:
                st.write(f"- {d['file']} · `{d['doc_type']}`" + (f" #{d['addendum_number']}" if d.get("addendum_number") else ""))

tab_search, tab_chat, tab_extract = st.tabs(["Search", "Ask (multi-agent Q&A)", "Extract (multi-agent)"])

with tab_search:
    c1, c2, c3, c4 = st.columns([4, 2, 2, 2])
    q = c1.text_input("Query", placeholder="bid bond amount, 210-BLYZ, due date ...", disabled=not bids)
    bid_f = c2.multiselect("Bid", bids)
    type_f = c3.multiselect("Doc type", ["bid_page", "rfp", "addendum", "specs", "affidavit", "form"])
    mode = c4.selectbox("Mode", ["hybrid_rerank", "hybrid", "dense", "bm25"])
    if q:
        with operation("Searching (first use may download AI models) ..."):
            res = svc.engine.search(q, bid_id=bid_f or None, doc_type=type_f or None, top_k=8, mode=mode)
        for r in res:
            with st.container(border=True):
                st.markdown(f"**{r.citation}** · `{r.bid_id}` · `{r.doc_type}` · score {r.score}")
                if r.section:
                    st.caption(r.section)
                st.text(r.text[:1200])

with tab_chat:
    if "history" not in st.session_state:
        st.session_state.history = []
    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"], unsafe_allow_html=turn.get("html", False))
    question = st.chat_input("Ask about the bids, e.g. 'Compare the warranty requirements of both bids'", disabled=not bids)
    if question:
        st.session_state.history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"), operation("Planner → retrieval → QA agent → validator ..."):
            ans = orchestrator().ask(question)
            body = render_answer(ans.answer, ans.citations, ans.confidence, ans.cached)
            st.markdown(body, unsafe_allow_html=True)
            with st.expander("Plan & validation"):
                st.json({"plan": ans.plan.model_dump() if ans.plan else None,
                         "validation": {k: v for k, v in ans.validation.items()}})
        st.session_state.history.append({"role": "assistant", "content": body, "html": True})

with tab_extract:
    target = st.selectbox("Bid", bids, key="extract_bid")
    if st.button("Run extraction", disabled=not target):
        with operation("Running the multi-agent extraction graph ..."):
            out = orchestrator().extract(target, svc.engine.manifests.get(target, {}).get("folder"))
        st.success(f"Validation: {out['record']['validation']} · {out['trace']['llm_calls']} LLM calls · "
                   f"{out['trace']['wall_time_s']}s")
    rec_file = svc.settings.path(svc.settings.output_dir) / f"{target}.json" if target else None
    if rec_file and rec_file.exists():
        rec = json.loads(rec_file.read_text(encoding="utf-8"))
        rows = [{"field": k, "value": v["value"], "confidence": v["confidence"],
                 "sources": "; ".join(f"{s['file']} p.{s['page']}" for s in v["sources"]), "notes": v["notes"]}
                for k, v in rec["fields"].items()]
        st.dataframe(rows, hide_index=True)
        if rec.get("addendum_changes"):
            st.subheader("Addendum changes")
            st.dataframe(rec["addendum_changes"], hide_index=True)
        st.download_button("Download JSON", rec_file.read_text(encoding="utf-8"), file_name=rec_file.name)
