"""Render a JSONL agent trace as a readable Markdown report.

    python main.py trace-report outputs/traces/<run>.jsonl [-o docs/example_trace.md]
"""

from __future__ import annotations

import json
from pathlib import Path


def render(trace_path: Path) -> str:
    events = [json.loads(l) for l in trace_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    spans = [e for e in events if e["type"] == "span"]
    run_id = events[0]["run_id"] if events else trace_path.stem
    tot_p = sum(s["tokens"]["prompt"] for s in spans)
    tot_c = sum(s["tokens"]["completion"] for s in spans)
    lines = [f"# Agent trace - `{run_id}`", "",
             f"{len(spans)} agent steps, {sum(len(s['llm_calls']) for s in spans)} LLM calls, "
             f"{tot_p} prompt + {tot_c} completion tokens.", "",
             "| # | time (UTC) | agent | step | tool calls | LLM calls | tokens | latency ms | error |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(spans, 1):
        lines.append(f"| {i} | {s['ts'][11:19]} | {s['agent']} | {s['step']} | {len(s['tool_calls'])} | "
                     f"{len(s['llm_calls'])} | {s['tokens']['total']} | {s['latency_ms']:.0f} | {s.get('error') or ''} |")
    lines += ["", "## Step details", ""]
    for i, s in enumerate(spans, 1):
        lines += [f"### {i}. {s['agent']} - {s['step']}", "",
                  f"- latency: {s['latency_ms']:.0f} ms; tokens: {s['tokens']}",
                  f"- input: `{json.dumps(s['input'], ensure_ascii=False)[:400]}`"]
        for tc in s["tool_calls"][:8]:
            res = tc["result"]
            res = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)
            lines.append(f"- tool `{tc['tool']}` args `{json.dumps(tc['args'], ensure_ascii=False)[:220]}` -> {res[:300]}")
        if len(s["tool_calls"]) > 8:
            lines.append(f"- ... {len(s['tool_calls']) - 8} more tool calls")
        for c in s["llm_calls"]:
            lines.append(f"- LLM `{c['model']}` prompt={c['prompt_tokens']} completion={c['completion_tokens']} "
                         f"latency={c['latency_ms']}ms cached={c['cached']}")
        out = s["output"] if isinstance(s["output"], str) else json.dumps(s["output"], ensure_ascii=False)
        lines += [f"- output: `{out[:900]}`", ""]
    logs = [e for e in events if e["type"] == "log"]
    if logs:
        lines += ["## Orchestrator log events", ""]
        for e in logs:
            lines.append(f"- {e['ts'][11:19]} **{e['agent']}**: {e['message']} `{json.dumps(e['data'], ensure_ascii=False)[:500]}`")
    return "\n".join(lines) + "\n"
