"""RFP Intelligence Platform - command line interface.

One-command extraction (index if needed + multi-agent extraction -> outputs/<bid>.json):
    python main.py --bid ./data/Bid1 [--bid ./data/Bid2]

Other commands:
    python main.py index ./data/Bid1 ./data/Bid2 [--force]
    python main.py search "bid bond amount" --bid-id Bid1 [--doc-type rfp] [--addendum 2] [--top-k 5] [--mode hybrid_rerank]
    python main.py ask "What is the submission deadline for Bid1 after all addendums?"
    python main.py extract ./data/Bid2            # same as --bid
    python main.py qa-log                         # run the sample questions -> outputs/qa_log.md
    python main.py eval                           # retrieval evaluation -> eval/results.md
    python main.py list                           # indexed bids
    python main.py serve [--port 8000]            # REST API (FastAPI)
    python main.py graph                          # print the LangGraph diagrams (mermaid)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.logging_setup import setup_logging  # noqa: E402

SAMPLE_QUESTIONS = [
    "What is the submission deadline for Bid1 after all addendums?",
    "Which affidavits are required for the Dell laptop bid?",
    "Compare the warranty requirements of both bids.",
    "Is a bid bond required, and if so, how much?",
    "What changed in Addendum 2 compared to the original RFP?",
    "Who is the procurement contact for each bid and how can they be reached?",
    "How must proposals be submitted for the Dell laptop bid?",
    "What is the contract term and renewal options for Bid1?",
    "What delivery timeframe is required after award for the Dell laptops?",
    "What processor, memory and storage does the quoted Dell Latitude 5550 have?",
    "Was a pre-proposal meeting held for Bid1 and was attendance mandatory?",
    "What imaging or software installation is required for Windows devices and Chromebooks in Bid1?",
    "Which cooperative purchasing agreements are referenced in Bid1?",
    "What are the payment or invoicing terms for the Dell laptop bid?",
]


def _bid_id(folder: str) -> str:
    from ingestion.pipeline import bid_id_from_folder

    return bid_id_from_folder(Path(folder))


def cmd_extract(folders: list[str], force: bool) -> int:
    from agents.orchestrator import Orchestrator
    from agents.services import get_services

    services = get_services()
    orch = Orchestrator(services)
    rc = 0
    for folder in folders:
        if not Path(folder).is_dir():
            print(f"Folder not found: {folder}", file=sys.stderr)
            rc = 1
            continue
        bid = _bid_id(folder)
        print(f"\n=== Extracting {bid} from {folder} ===")
        out = orch.extract(bid, folder, force_reindex=force)
        rec = out.get("record") or {}
        print(json.dumps({"bid_id": bid, "validation": rec.get("validation"), "files": out.get("files"),
                          "llm_calls": out["trace"]["llm_calls"],
                          "tokens": out["trace"]["prompt_tokens"] + out["trace"]["completion_tokens"],
                          "wall_time_s": out["trace"]["wall_time_s"], "trace": out["trace"]["trace_file"]}, indent=2))
        for name, fv in (rec.get("fields") or {}).items():
            val = fv.get("value")
            shown = (val[:110] + "...") if val and len(val) > 110 else val
            print(f"  {name:38s} {fv.get('confidence', 0):.2f}  {shown if shown else '— ' + fv.get('notes', '')[:80]}")
    return rc


def cmd_index(folders: list[str], force: bool) -> int:
    from agents.services import get_services

    engine = get_services().engine
    for folder in folders:
        rep = engine.index_folder(folder, force=force)
        print(json.dumps({"bid_id": rep.bid_id, "status": rep.status, "chunks": rep.chunks, "seconds": rep.seconds,
                          "documents": [(d["file"], d["doc_type"], d.get("addendum_number")) for d in rep.documents],
                          "warnings": rep.warnings[:10]}, indent=2))
    return 0


def cmd_search(args) -> int:
    from agents.services import get_services

    engine = get_services().engine
    res = engine.search(args.query, bid_id=args.bid_id, doc_type=args.doc_type, addendum_number=args.addendum,
                        top_k=args.top_k, mode=args.mode, expand=not args.no_expand)
    if args.json:
        print(json.dumps([r.model_dump() for r in res], indent=2, ensure_ascii=False))
        return 0
    for i, r in enumerate(res, 1):
        print(f"\n[{i}] {r.citation}  ({r.bid_id}, {r.doc_type}{' #' + str(r.addendum_number) if r.addendum_number else ''})"
              f"  score={r.score}  {r.scores}")
        if r.section:
            print(f"    section: {r.section}")
        print("    " + r.text[:600].replace("\n", "\n    "))
    if not res:
        print("No results.")
    return 0


def cmd_ask(question: str, bid_ids: list[str] | None, no_cache: bool) -> int:
    from agents.orchestrator import Orchestrator
    from agents.services import get_services

    ans = Orchestrator(get_services()).ask(question, bid_ids, use_cache=not no_cache)
    print(f"\nQ: {ans.question}\n\nA: {ans.answer}\n")
    for c in ans.citations:
        print(f"   - {c.file}, p.{c.page}" + (f"-{c.page_end}" if c.page_end else ""))
        for q in c.quotes:
            print(f"       \"{q}\"")
    print(f"\nconfidence={ans.confidence} not_found={ans.not_found} cached={ans.cached} "
          f"validation={ {k: v for k, v in ans.validation.items() if k != 'trace'} }")
    return 0


def cmd_qa_log(questions_file: str | None) -> int:
    from agents.orchestrator import Orchestrator
    from agents.services import get_services

    services = get_services()
    qs = SAMPLE_QUESTIONS
    if questions_file:
        qs = [l.strip() for l in Path(questions_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    orch = Orchestrator(services)
    out = services.settings.path(services.settings.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["# Sample Q&A log", "", "Answers produced by the multi-agent QA workflow (planner -> parallel retrieval -> "
             "QA agent -> answer validator). Citations are `file, page`; each source lists the exact supporting "
             "sentence(s) so it can be found in the original document.", ""]
    records = []
    for i, q in enumerate(qs, 1):
        print(f"[{i}/{len(qs)}] {q}")
        ans = orch.ask(q, use_cache=False)
        tr = ans.validation.get("trace", {})
        lines += [f"## Q{i}. {q}", "", ans.answer, "",
                  "**Citations:** " + ("; ".join(f"{c.file} p.{c.page}" for c in ans.citations) or "none"), "",
                  *[f"- {c.file}, p.{c.page}" + (f"-{c.page_end}" if c.page_end else "") +
                    (f" (§ {' '.join(c.section.split())[:80]})" if c.section else "") + ": " +
                    " / ".join(f"\"{' '.join(q.split())}\"" for q in c.quotes)
                    for c in ans.citations if c.quotes],
                  "",
                  f"*intent: {ans.plan.intent if ans.plan else '-'}; bids: {', '.join(ans.plan.bid_ids) if ans.plan else '-'}; "
                  f"confidence: {ans.confidence}; validation passed: {ans.validation.get('passed')}; "
                  f"LLM calls: {tr.get('llm_calls')}; tokens: {tr.get('prompt_tokens', 0) + tr.get('completion_tokens', 0)}; "
                  f"time: {tr.get('wall_time_s')}s; trace: `{Path(tr.get('trace_file') or '').name}`*", ""]
        records.append(ans.model_dump())
    (out / "qa_log.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "qa_log.json").write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"Wrote {out / 'qa_log.md'}")
    return 0


def cmd_list() -> int:
    from agents.services import get_services

    for c in get_services().engine.catalog():
        print(f"{c['bid_id']}: {c['chunks']} chunks")
        for d in c["documents"]:
            print(f"   - {d['file']} [{d['doc_type']}{' #' + str(d['addendum_number']) if d.get('addendum_number') else ''}]"
                  f" pages={d.get('pages')} date={d.get('doc_date')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RFP Intelligence Platform", formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__)
    parser.add_argument("--bid", action="append", help="bid folder to index + extract (repeatable)")
    parser.add_argument("--force", action="store_true", help="force re-indexing")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("index", help="index bid folders (incremental)")
    p.add_argument("folders", nargs="+")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("extract", help="multi-agent extraction for bid folders")
    p.add_argument("folders", nargs="+")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("search", help="hybrid search")
    p.add_argument("query")
    p.add_argument("--bid-id", action="append")
    p.add_argument("--doc-type", action="append")
    p.add_argument("--addendum", type=int)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--mode", choices=["bm25", "dense", "hybrid", "hybrid_rerank"])
    p.add_argument("--no-expand", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ask", help="question answering")
    p.add_argument("question")
    p.add_argument("--bid-id", action="append")
    p.add_argument("--no-cache", action="store_true")

    p = sub.add_parser("qa-log", help="run sample questions and write outputs/qa_log.md")
    p.add_argument("--questions", help="text file with one question per line")

    p = sub.add_parser("eval", help="retrieval evaluation")
    p.add_argument("--configs", nargs="*")
    p.add_argument("--k", type=int, nargs="*", default=[1, 3, 5, 10])

    sub.add_parser("list", help="list indexed bids")
    p = sub.add_parser("serve", help="start the REST API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    sub.add_parser("graph", help="print LangGraph mermaid diagrams")
    p = sub.add_parser("trace-report", help="render a JSONL trace as markdown")
    p.add_argument("trace")
    p.add_argument("-o", "--output")

    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    if args.bid and not args.cmd:
        return cmd_extract(args.bid, args.force)
    if args.cmd == "extract":
        return cmd_extract(args.folders, args.force)
    if args.cmd == "index":
        return cmd_index(args.folders, args.force)
    if args.cmd == "search":
        return cmd_search(args)
    if args.cmd == "ask":
        return cmd_ask(args.question, args.bid_id, args.no_cache)
    if args.cmd == "qa-log":
        return cmd_qa_log(args.questions)
    if args.cmd == "eval":
        from eval.run_eval import run_eval

        run_eval(configs=args.configs, ks=args.k)
        return 0
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("api.server:app", host=args.host, port=args.port, reload=False)
        return 0
    if args.cmd == "graph":
        from agents.orchestrator import Orchestrator
        from agents.services import get_services

        for name, mm in Orchestrator.graph_mermaid(get_services()).items():
            print(f"\n%% {name}\n{mm}")
        return 0
    if args.cmd == "trace-report":
        from core.trace_report import render

        md = render(Path(args.trace))
        if args.output:
            Path(args.output).write_text(md, encoding="utf-8")
            print(f"Wrote {args.output}")
        else:
            print(md)
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
