> First open a terminal in **../source_Code/**. Run the commands below from that directory.

# How to run

**1. Setup (once)** — in PowerShell, inside this folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
.\.venv\Scripts\Activate.ps1
```

Put your key in `.env`: `GROQ_API_KEY=gsk_...`

**Open the app:** run `streamlit run ui\app.py`, then go to **http://localhost:8501**

**2. Run**

| What | Command |
|---|---|
| Index the bids | `python main.py index .\data\Bid1 .\data\Bid2` |
| Web UI | `streamlit run ui\app.py` → http://localhost:8501 |
| Extract a bid (JSON) | `python main.py --bid .\data\Bid1` → `outputs\Bid1.json` |
| Ask a question | `python main.py ask "Compare the warranty requirements of both bids."` |
| Search | `python main.py search "210-BLYZ"` |
| REST API | `python main.py serve` → http://127.0.0.1:8000/docs |
| All deliverables | `powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1` |
| Tests | `pytest` |

**New bid?** `python main.py --bid <folder>` — no code changes needed.

**No GPU?** Run this first in the same window:

```powershell
$env:EMBEDDING_MODEL="sentence-transformers/all-MiniLM-L6-v2"; $env:RERANKER_MODEL="cross-encoder/ms-marco-MiniLM-L-6-v2"; $env:DEVICE="cpu"; $env:INDEX_DIR="index_cpu"
```

Ask and Extract take 1–3 minutes (Groq free tier). Details: [README.md](README.md).
