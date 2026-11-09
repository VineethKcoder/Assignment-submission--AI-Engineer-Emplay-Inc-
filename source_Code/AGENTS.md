# AGENTS.md — guide for coding agents (Codex, Claude Code, ...)

RFP Intelligence Platform: hybrid RAG search engine + LangGraph multi-agent system over bid folders
(HTML + PDF + addenda). The assignment spec it implements is summarised in `README.md` (sections 3-7).

## Environment

- Windows, PowerShell, Python 3.11/3.12, venv at `.venv` (`.\.venv\Scripts\Activate.ps1`).
- GPU: install `requirements.txt`, then run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\enable_gpu.ps1`.
  The helper installs matching CUDA PyTorch/torchvision wheels and verifies GPU execution before setting DEVICE=cuda.
  Exact tested application dependencies: `requirements-lock.txt`.
- Config only via `.env` / environment variables (`core/settings.py`). Never hard-code keys, model names or
  bid-specific values. Never print or commit `.env`.
- Always run commands from the repo root; entry point is `main.py`.

## Commands

| Task | Command |
|---|---|
| Unit tests (no network/GPU/key needed) | `pytest` (expect 52 passed) |
| Index bids (incremental) | `python main.py index .\data\Bid1 .\data\Bid2 [--force]` |
| Extraction (one command) | `python main.py --bid .\data\Bid1 --bid .\data\Bid2` → `outputs\<bid>.json` |
| Search | `python main.py search "<q>" --bid-id Bid1 --doc-type addendum --top-k 5 [--mode bm25\|dense\|hybrid\|hybrid_rerank]` |
| Q&A | `python main.py ask "<question>" [--bid-id Bid2] [--no-cache]` |
| Retrieval eval | `python main.py eval` → `eval\results.md` |
| Q&A log | `python main.py qa-log` → `outputs\qa_log.md` |
| Trace report | `python main.py trace-report outputs\traces\<run>.jsonl -o docs\example_trace.md` |
| API / UI | `python main.py serve` (http://127.0.0.1:8000/docs) / `streamlit run ui\app.py` |
| Everything | `powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1` |

Extraction/QA call the Groq API (free tier 8k tokens/min/model): runs take minutes and logs show
`LLM budget: waiting ...` / `429` retries — expected, handled by `core/llm.py`. Do not "fix" that by removing
the rate limiter. LLM responses are cached in `.cache/llm` (delete it to force fresh calls).

## Layout

```
ingestion/  pdf_parser, html_parser, cleaning, classify (doc types: config/doc_types.yaml), chunking, pipeline
search/     engine (hybrid RRF + filters + rerank), bm25 (identifier-aware), embeddings, reranker, store (FAISS shards), query
agents/     orchestrator (LangGraph graphs), retrieval/extraction/reconciliation/validator/qa/report agents,
            schemas (Pydantic messages), grounding (hallucination checks), fields (config/fields.yaml)
core/       settings, llm (OpenAI-compatible client, retries, model rotation, cache), tracing, trace_report
api/ ui/ eval/ tests/ config/ data/ outputs/ docs/
```

## Rules when changing code

- Keep the system generic: new fields go in `config/fields.yaml`, synonyms in `config/synonyms.yaml`,
  document-type rules in `config/doc_types.yaml` — not in Python.
- Every agent-to-agent message is a Pydantic model in `agents/schemas.py`.
- Guardrails must stay: values need ≥1 citation; ungrounded values become `null` + "Not found in documents".
- If you change parsing/chunking, bump `CHUNKING_VERSION` in `search/store.py` (forces re-index).
- Tests use fakes (`tests/conftest.py`: `HashingEmbedder`, `KeywordOverlapReranker`, `FakeLLM`) — keep tests
  offline. Run `pytest` after every change.
- If retrieval changes, re-run `python main.py eval` and update the table in README section 4.
