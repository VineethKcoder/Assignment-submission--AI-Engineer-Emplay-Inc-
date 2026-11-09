"""Lightweight structured tracing for agent runs.

Every agent step is recorded as a JSON event with: agent, step, input summary,
tool calls, output summary, LLM token usage, latency and errors. Events are
streamed to `outputs/traces/<run_id>.jsonl` so a run can be inspected even if
it crashes half-way. `Tracer.summary()` aggregates tokens, cost and latency
per agent (cost/latency tracking per run).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("rfp.trace")


def _short(value: Any, limit: int = 600) -> Any:
    """Make a value JSON-friendly and bounded in size for the trace."""
    try:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        text = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        text = str(value)
    if len(text) <= limit:
        try:
            return json.loads(text)
        except Exception:
            return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


@dataclass
class Span:
    tracer: "Tracer"
    agent: str
    step: str
    input: Any = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    tool_calls: list[dict] = field(default_factory=list)
    llm_calls: list[dict] = field(default_factory=list)
    output: Any = None
    error: str | None = None
    started: float = field(default_factory=time.perf_counter)

    def tool_call(self, name: str, args: Any, result: Any = None, latency_ms: float | None = None) -> None:
        self.tool_calls.append(
            {"tool": name, "args": _short(args, 300), "result": _short(result, 500), "latency_ms": latency_ms}
        )

    def llm_call(self, model: str, prompt_tokens: int, completion_tokens: int, latency_ms: float,
                 cached: bool = False, attempt: int = 1) -> None:
        self.llm_calls.append(
            {
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": round(latency_ms, 1),
                "cached": cached,
                "attempt": attempt,
            }
        )

    def set_output(self, output: Any) -> None:
        self.output = output


class Tracer:
    def __init__(self, trace_dir: Path | None = None, run_id: str | None = None, mode: str = "run",
                 price_in_per_m: float = 0.0, price_out_per_m: float = 0.0):
        self.run_id = run_id or f"{mode}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.mode = mode
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self.price_in = price_in_per_m
        self.price_out = price_out_per_m
        self.started = time.perf_counter()
        self.path: Path | None = None
        if trace_dir is not None:
            trace_dir.mkdir(parents=True, exist_ok=True)
            self.path = trace_dir / f"{self.run_id}.jsonl"

    # ------------------------------------------------------------------
    def _write(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")

    def log(self, agent: str, message: str, **data: Any) -> None:
        self._write(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "type": "log",
                "agent": agent,
                "message": message,
                "data": _short(data, 800),
            }
        )

    @contextmanager
    def span(self, agent: str, step: str, input: Any = None) -> Iterator[Span]:
        span = Span(self, agent, step, input=input)
        try:
            yield span
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            latency_ms = (time.perf_counter() - span.started) * 1000
            prompt = sum(c["prompt_tokens"] for c in span.llm_calls)
            completion = sum(c["completion_tokens"] for c in span.llm_calls)
            event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "type": "span",
                "span_id": span.span_id,
                "agent": agent,
                "step": step,
                "input": _short(span.input, 800),
                "tool_calls": span.tool_calls,
                "llm_calls": span.llm_calls,
                "tokens": {"prompt": prompt, "completion": completion, "total": prompt + completion},
                "output": _short(span.output, 1500),
                "latency_ms": round(latency_ms, 1),
                "error": span.error,
            }
            self._write(event)
            logger.info(
                "[%s] %s.%s %.0fms tokens=%d%s",
                self.run_id, agent, step, latency_ms, prompt + completion,
                f" ERROR={span.error}" if span.error else "",
            )

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        per_agent: dict[str, dict] = {}
        total_prompt = total_completion = 0
        llm_calls = 0
        for ev in self.events:
            if ev.get("type") != "span":
                continue
            a = per_agent.setdefault(ev["agent"], {"steps": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                                   "latency_ms": 0.0, "llm_calls": 0, "errors": 0})
            a["steps"] += 1
            a["prompt_tokens"] += ev["tokens"]["prompt"]
            a["completion_tokens"] += ev["tokens"]["completion"]
            a["latency_ms"] = round(a["latency_ms"] + ev["latency_ms"], 1)
            a["llm_calls"] += len(ev["llm_calls"])
            a["errors"] += 1 if ev.get("error") else 0
            total_prompt += ev["tokens"]["prompt"]
            total_completion += ev["tokens"]["completion"]
            llm_calls += len(ev["llm_calls"])
        cost = total_prompt / 1e6 * self.price_in + total_completion / 1e6 * self.price_out
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "wall_time_s": round(time.perf_counter() - self.started, 2),
            "llm_calls": llm_calls,
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "estimated_cost_usd": round(cost, 5),
            "per_agent": per_agent,
            "trace_file": str(self.path) if self.path else None,
        }


class NullTracer(Tracer):
    """Tracer that keeps events in memory only (used by tests / ad-hoc calls)."""

    def __init__(self) -> None:
        super().__init__(trace_dir=None, mode="null")
