"""Retrieval evaluation: compare search configurations on eval/eval_set.json.

    python main.py eval                       # all configurations
    python -m eval.run_eval --configs dense hybrid_rerank

Writes eval/results.md and eval/results.json.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from eval.metrics import first_hit_rank, is_relevant, mrr, ndcg_at_k, recall_at_k

EVAL_DIR = Path(__file__).resolve().parent

CONFIGS = {
    "bm25": {"mode": "bm25", "expand": False},
    "dense": {"mode": "dense", "expand": False},
    "hybrid (RRF)": {"mode": "hybrid", "expand": False},
    "hybrid + expansion": {"mode": "hybrid", "expand": True},
    "hybrid + rerank": {"mode": "hybrid_rerank", "expand": False},
    "hybrid + expansion + rerank": {"mode": "hybrid_rerank", "expand": True},
}


def run_eval(configs: list[str] | None = None, ks: list[int] | None = None, engine=None, use_bid_filter: bool = True,
             write: bool = True) -> dict:
    from agents.services import get_services

    ks = ks or [1, 3, 5, 10]
    engine = engine or get_services().engine
    data = json.loads((EVAL_DIR / "eval_set.json").read_text(encoding="utf-8"))
    questions = data["questions"]
    names = [c for c in CONFIGS if not configs or c in configs or CONFIGS[c]["mode"] in configs]
    engine.warmup()
    kmax = max(ks)
    results: dict[str, dict] = {}
    per_question: dict[str, list] = {}
    for name in names:
        cfg = CONFIGS[name]
        ranks, rel_lists, latencies, rows = [], [], [], []
        for q in questions:
            t0 = time.perf_counter()
            res = engine.search(q["question"], bid_id=q["bid_id"] if use_bid_filter else None, top_k=kmax,
                                mode=cfg["mode"], expand=cfg["expand"])
            latencies.append((time.perf_counter() - t0) * 1000)
            dicts = [r.model_dump() for r in res]
            rank = first_hit_rank(dicts, q["relevant"])
            ranks.append(rank)
            rel_lists.append([is_relevant(d, q["relevant"]) for d in dicts])
            rows.append({"id": q["id"], "rank": rank, "top1": f"{dicts[0]['file_name']} p.{dicts[0]['page_number']}" if dicts else None})
        metrics = {f"Recall@{k}": round(recall_at_k(ranks, k), 3) for k in ks}
        metrics[f"MRR@{kmax}"] = round(mrr(ranks, kmax), 3)
        metrics[f"nDCG@{kmax}"] = round(ndcg_at_k(rel_lists, kmax), 3)
        metrics["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1)
        results[name] = metrics
        per_question[name] = rows
        print(f"{name:30s} {metrics}")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_questions": len(questions),
        "bid_filter": use_bid_filter,
        "embedding_model": engine.embedding_model_name,
        "reranker_model": engine.settings.reranker_model,
        "chunks_indexed": len(engine.chunks),
        "results": results,
        "per_question": per_question,
    }
    if write:
        (EVAL_DIR / "results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        (EVAL_DIR / "results.md").write_text(to_markdown(out, questions), encoding="utf-8")
        print(f"Wrote {EVAL_DIR / 'results.md'}")
    return out


def to_markdown(out: dict, questions: list[dict]) -> str:
    names = list(out["results"])
    cols = list(next(iter(out["results"].values())).keys())
    lines = [
        "# Retrieval evaluation", "",
        f"- Questions: {out['n_questions']} (Bid1: {sum(q['bid_id'] == 'Bid1' for q in questions)}, "
        f"Bid2: {sum(q['bid_id'] == 'Bid2' for q in questions)}), bid filter: {out['bid_filter']}",
        f"- Embedding model: `{out['embedding_model']}`; reranker: `{out['reranker_model']}`; chunks indexed: {out['chunks_indexed']}",
        "- Relevance: a retrieved chunk is relevant if it comes from an expected file and its page range overlaps an expected page.",
        "- Recall@k = share of questions with at least one relevant passage in the top k.", "",
        "| Configuration | " + " | ".join(cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for n in names:
        lines.append(f"| {n} | " + " | ".join(str(out["results"][n][c]) for c in cols) + " |")
    lines += ["", "## Per-question rank of first relevant passage", "",
              "| id | question | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    by_id = {n: {r["id"]: r for r in out["per_question"][n]} for n in names}
    for q in questions:
        cells = [str(by_id[n][q["id"]]["rank"] or "-") for n in names]
        lines.append(f"| {q['id']} | {q['question'][:70]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(EVAL_DIR.parent))
    from core.logging_setup import setup_logging

    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="*")
    ap.add_argument("--no-bid-filter", action="store_true")
    a = ap.parse_args()
    run_eval(a.configs, use_bid_filter=not a.no_bid_filter)
