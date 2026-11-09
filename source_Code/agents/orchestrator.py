"""Orchestrator / Planner - LangGraph state machines for both modes.

EXTRACTION MODE
    START -> plan -> ingest --(fan-out: one Send per field group, run in parallel)--> extract_group
          extract_group (Retrieval Agent + specialist Extraction Agent) --> reconcile (Addendum Agent)
          reconcile --> validate (Validator/Critic)
          validate --(failed fields & retries left: Send(extract_group) for just those fields)--> extract_group
                   --(done)--> report (Report Agent) -> END

QA MODE
    START -> plan_question --(fan-out: one Send per sub-query)--> retrieve --> answer --> check
          check --(not grounded & retry left)--> retrieve_broad -> answer -> check
                --(done)--> END

The shared state (`ExtractionState` / `QAState`) holds the plan, evidence,
draft fields, validation results and final output. Parallel branches merge
through reducers. Every node runs inside a trace span (input, tool calls,
LLM tokens, latency, output, errors) and catches its own errors so one
failure never crashes the whole run.
"""

from __future__ import annotations

import logging
import operator
import time
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from agents.extraction_agent import ExtractionAgent
from agents.ingestion_agent import IngestionAgent
from agents.qa_agent import QAAgent, SemanticCache
from agents.reconciliation_agent import ReconciliationAgent
from agents.report_agent import ReportAgent
from agents.retrieval_agent import RetrievalAgent
from agents.schemas import (
    NOT_FOUND,
    AddendumChange,
    Evidence,
    ExecutionPlan,
    FieldValidation,
    FieldValue,
    PlanStep,
    QAAnswer,
    QAPlan,
    SubQuery,
    ValidationReport,
)
from agents.services import Services
from agents.validator_agent import ValidatorAgent
from core.tracing import Tracer

logger = logging.getLogger("rfp.orchestrator")


def merge_dicts(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


# =========================================================== extraction
class ExtractionState(TypedDict, total=False):
    bid_id: str
    bid_folder: str | None
    force_reindex: bool
    plan: ExecutionPlan
    ingestion: dict
    drafts: Annotated[dict[str, FieldValue], merge_dicts]
    evidence: Annotated[dict[str, list[Evidence]], merge_dicts]
    addendum_changes: list[AddendumChange]
    changes_computed: bool
    validation: ValidationReport
    feedback: dict[str, str]
    retry_fields: list[str]
    retry_round: int
    record: dict
    output_files: dict
    errors: Annotated[list[str], operator.add]


class GroupTask(TypedDict):
    bid_id: str
    group: str
    fields: list[str]
    feedback: dict[str, str]
    previous: dict[str, FieldValue]
    retry: bool


class ExtractionWorkflow:
    def __init__(self, services: Services, tracer: Tracer):
        self.s = services
        self.t = tracer
        self.cfg = services.fields
        self.retrieval = RetrievalAgent(services)
        self.ingestion_agent = IngestionAgent(services)
        self.reconciler = ReconciliationAgent(services)
        self.validator = ValidatorAgent(services)
        self.reporter = ReportAgent(services)
        self.graph = self._build()

    # ------------------------------------------------------------------ nodes
    def plan(self, state: ExtractionState) -> dict:
        with self.t.span("orchestrator", "plan", {"bid_id": state["bid_id"], "folder": state.get("bid_folder")}) as sp:
            groups = {}
            for f in self.cfg.extractable:
                groups.setdefault(f.group, []).append(f.name)
            steps = [PlanStep(step=1, agent="ingestion_agent", action="parse + index bid folder (incremental)",
                              inputs={"folder": state.get("bid_folder")})]
            for i, (g, names) in enumerate(groups.items(), start=2):
                steps.append(PlanStep(step=i, agent=f"retrieval_agent + extraction_agent[{g}]",
                                      action="retrieve evidence per field, extract values (parallel)",
                                      inputs={"fields": names}))
            n = len(steps) + 1
            steps += [
                PlanStep(step=n, agent="addendum_reconciliation_agent", action="build change log, apply latest addendum values"),
                PlanStep(step=n + 1, agent="validator_agent", action="citation / grounding / format / critic checks",
                         inputs={"max_retries": self.s.settings.agent_max_retries}),
                PlanStep(step=n + 2, agent="orchestrator", action="re-run retrieval+extraction for failed fields until "
                         "they pass or retries are exhausted"),
                PlanStep(step=n + 3, agent="report_agent", action="bid summary, final JSON + markdown"),
            ]
            plan = ExecutionPlan(mode="extraction", goal=f"Extract structured record for bid {state['bid_id']}", steps=steps)
            sp.set_output(plan)
            self.t.log("orchestrator", "execution plan", plan=plan.model_dump())
        return {"plan": plan, "retry_round": 0, "changes_computed": False, "drafts": {}, "feedback": {}}

    def ingest(self, state: ExtractionState) -> dict:
        with self.t.span("ingestion_agent", "ingest", {"folder": state.get("bid_folder")}) as sp:
            try:
                info = self.ingestion_agent.run(state.get("bid_folder"), state["bid_id"], span=sp,
                                                force=state.get("force_reindex", False))
            except Exception as exc:
                logger.exception("ingestion failed")
                info = {"bid_id": state["bid_id"], "status": "failed", "chunks": 0, "documents": [],
                        "warnings": [str(exc)]}
            sp.set_output(info)
        errs = [f"ingestion: {w}" for w in info.get("warnings", [])] if info["status"] == "failed" else []
        return {"ingestion": info, "errors": errs}

    def route_after_ingest(self, state: ExtractionState):
        if state["ingestion"]["status"] == "failed":
            return "report"
        groups: dict[str, list[str]] = {}
        for f in self.cfg.extractable:
            groups.setdefault(f.group, []).append(f.name)
        return [
            Send("extract_group", GroupTask(bid_id=state["bid_id"], group=g, fields=names, feedback={}, previous={}, retry=False))
            for g, names in groups.items()
        ]

    def extract_group(self, task: GroupTask) -> dict:
        group = self.cfg.groups.get(task["group"])
        specs = [self.cfg.fields[n] for n in task["fields"]]
        agent = ExtractionAgent(self.s, group)
        evidence: dict[str, list[Evidence]] = {}
        with self.t.span("retrieval_agent", f"gather[{task['group']}]",
                         {"fields": task["fields"], "retry": task["retry"]}) as sp:
            for spec in specs:
                evidence[spec.name] = self.retrieval.gather_for_field(
                    task["bid_id"], spec, span=sp, broaden=task["retry"], feedback=task["feedback"].get(spec.name))
            sp.set_output({k: [f"{e.file} p.{e.page} ({e.score:.2f})" for e in v] for k, v in evidence.items()})
        with self.t.span(agent.name, "extract", {"fields": task["fields"], "feedback": task["feedback"]}) as sp:
            try:
                drafts = agent.run(task["bid_id"], specs, evidence, feedback=task["feedback"], span=sp,
                                   previous=task["previous"])
            except Exception as exc:  # never crash the graph
                logger.exception("extraction crashed")
                drafts = {s.name: FieldValue(status="failed", notes=f"extraction error: {exc}") for s in specs}
            sp.set_output({k: {"value": v.value, "evidence": v.evidence_ids, "status": v.status} for k, v in drafts.items()})
        return {"drafts": drafts, "evidence": evidence}

    def reconcile(self, state: ExtractionState) -> dict:
        with self.t.span(self.reconciler.name, "reconcile", {"computed": state.get("changes_computed")}) as sp:
            changes = state.get("addendum_changes") or []
            try:
                if not state.get("changes_computed"):
                    changes = self.reconciler.find_changes(state["bid_id"], state["drafts"], self.cfg, span=sp)
                drafts = self.reconciler.apply(state["drafts"], changes, self.cfg)
            except Exception as exc:
                logger.exception("reconciliation failed")
                sp.set_output({"error": str(exc)})
                return {"changes_computed": True, "addendum_changes": changes, "errors": [f"reconcile: {exc}"]}
            sp.set_output({"changes": [f"A{c.addendum_number} {c.change_type} -> {c.field}: {c.description[:80]}"
                                       for c in changes], "applied": sum(c.applied for c in changes)})
        return {"drafts": drafts, "addendum_changes": changes, "changes_computed": True}

    def validate(self, state: ExtractionState) -> dict:
        only = state.get("retry_fields") or None
        with self.t.span(self.validator.name, "validate", {"round": state.get("retry_round", 0), "only": only}) as sp:
            try:
                report = self.validator.validate(state["drafts"], self.cfg, span=sp, only=only,
                                                 previous=state.get("validation"))
            except Exception as exc:
                logger.exception("validation crashed")
                report = state.get("validation") or ValidationReport()
                return {"validation": report, "retry_fields": [], "errors": [f"validate: {exc}"]}
        rnd = state.get("retry_round", 0)
        failed = report.failed_fields()
        retry = failed if failed and rnd < self.s.settings.agent_max_retries else []
        feedback = {f: "; ".join(report.results[f].issues)[:300] for f in retry}
        self.t.log("orchestrator", "validation decision", round=rnd, counts=report.counts, retry_fields=retry)
        return {"validation": report, "retry_fields": retry, "feedback": feedback,
                "retry_round": rnd + (1 if retry else 0)}

    def route_after_validate(self, state: ExtractionState):
        retry = state.get("retry_fields") or []
        if not retry:
            return "report"
        by_group: dict[str, list[str]] = {}
        for f in retry:
            by_group.setdefault(self.cfg.fields[f].group, []).append(f)
        return [
            Send("extract_group", GroupTask(
                bid_id=state["bid_id"], group=g, fields=names,
                feedback={n: state["feedback"].get(n, "") for n in names},
                previous={n: state["drafts"][n] for n in names if n in state["drafts"]}, retry=True))
            for g, names in by_group.items()
        ]

    def report(self, state: ExtractionState) -> dict:
        with self.t.span(self.reporter.name, "report", {"bid_id": state["bid_id"]}) as sp:
            report = state.get("validation") or ValidationReport()
            drafts = state.get("drafts") or {}
            fields = self.reporter.finalize_fields(drafts, report, self.cfg)
            summary_spec = next((f for f in self.cfg.fields.values() if f.generated), None)
            if summary_spec is not None:
                summary = self.reporter.bid_summary(state["bid_id"], fields, span=sp)
                fields[summary_spec.name] = summary
                report.results[summary_spec.name] = FieldValidation(
                    field=summary_spec.name, status="passed" if summary.value else "not_found")
            run = {**self.t.summary(), "retry_rounds": state.get("retry_round", 0),
                   "llm_model": self.s.settings.llm_model, "embedding_model": self.s.engine.embedding_model_name,
                   "reranker_model": self.s.settings.reranker_model, "errors": state.get("errors", [])}
            run.pop("per_agent", None)
            record = self.reporter.build_record(state["bid_id"], fields, state.get("addendum_changes") or [], report,
                                                (state.get("ingestion") or {}).get("documents", []), run, self.cfg)
            files = self.reporter.write(record)
            sp.set_output({"validation": record.validation, "files": files})
        return {"record": record.model_dump(), "output_files": files}

    # ------------------------------------------------------------------ graph
    def _build(self):
        g = StateGraph(ExtractionState)
        g.add_node("plan", self.plan)
        g.add_node("ingest", self.ingest)
        g.add_node("extract_group", self.extract_group)
        g.add_node("reconcile", self.reconcile)
        g.add_node("validate", self.validate)
        g.add_node("report", self.report)
        g.add_edge(START, "plan")
        g.add_edge("plan", "ingest")
        g.add_conditional_edges("ingest", self.route_after_ingest, ["extract_group", "report"])
        g.add_edge("extract_group", "reconcile")
        g.add_edge("reconcile", "validate")
        g.add_conditional_edges("validate", self.route_after_validate, ["extract_group", "report"])
        g.add_edge("report", END)
        return g.compile()

    def run(self, bid_id: str, bid_folder: str | None = None, force_reindex: bool = False) -> dict:
        t0 = time.perf_counter()
        self.t.log("orchestrator", "extraction run started", bid_id=bid_id, folder=bid_folder)
        final = self.graph.invoke(
            {"bid_id": bid_id, "bid_folder": bid_folder, "force_reindex": force_reindex, "errors": []},
            config={"max_concurrency": self.s.settings.agent_max_concurrency, "recursion_limit": 80},
        )
        summary = self.t.summary()
        self.t.log("orchestrator", "extraction run finished", seconds=round(time.perf_counter() - t0, 1), summary=summary)
        return {"record": final.get("record"), "files": final.get("output_files"), "trace": summary}


# =========================================================== QA
class QAState(TypedDict, total=False):
    question: str
    bid_ids: list[str] | None
    plan: QAPlan
    evidence: Annotated[list[Evidence], operator.add]
    raw_answer: Any
    pool: list[Evidence]
    validation: dict
    attempt: int
    feedback: str
    result: QAAnswer
    errors: Annotated[list[str], operator.add]


class RetrieveTask(TypedDict):
    sub_query: SubQuery
    broaden: bool


class QAWorkflow:
    def __init__(self, services: Services, tracer: Tracer):
        self.s = services
        self.t = tracer
        self.qa = QAAgent(services)
        self.retrieval = RetrievalAgent(services)
        self.graph = self._build()

    def plan_question(self, state: QAState) -> dict:
        with self.t.span("orchestrator", "plan_question", {"question": state["question"], "bids": state.get("bid_ids")}) as sp:
            plan = self.qa.plan(state["question"], state.get("bid_ids"), span=sp)
            sp.set_output(plan)
        return {"plan": plan, "attempt": 0}

    def fan_out(self, state: QAState):
        return [Send("retrieve", RetrieveTask(sub_query=sq, broaden=False)) for sq in state["plan"].sub_queries]

    def retrieve(self, task: RetrieveTask) -> dict:
        sq = task["sub_query"]
        with self.t.span("retrieval_agent", "retrieve", sq.model_dump()) as sp:
            ev = self.retrieval.gather_for_query(sq, span=sp, broaden=task["broaden"])
            sp.set_output([f"{e.bid_id}:{e.file} p.{e.page} ({e.score:.2f})" for e in ev])
        return {"evidence": ev}

    def retrieve_broad(self, state: QAState) -> dict:
        with self.t.span("retrieval_agent", "retrieve_broad", {"feedback": state.get("feedback")}) as sp:
            ev: list[Evidence] = []
            for sq in state["plan"].sub_queries:
                ev.extend(self.retrieval.gather_for_query(sq, span=sp, broaden=True))
            sp.set_output(f"{len(ev)} passages")
        return {"evidence": ev}

    def answer(self, state: QAState) -> dict:
        with self.t.span("qa_agent", "answer", {"question": state["question"], "n_evidence": len(state.get("evidence", []))}) as sp:
            ans, pool = self.qa.answer(state["question"], state.get("evidence", []), state["plan"], span=sp,
                                       feedback=state.get("feedback"))
            sp.set_output(ans)
        return {"raw_answer": ans, "pool": pool}

    def check(self, state: QAState) -> dict:
        with self.t.span("validator_agent", "check_answer", {"attempt": state.get("attempt", 0)}) as sp:
            v = self.qa.validate(state["raw_answer"], state.get("pool", []), state["plan"].bid_ids)
            sp.set_output(v)
        attempt = state.get("attempt", 0)
        if not v["passed"] and attempt < 1:
            return {"validation": v, "attempt": attempt + 1, "feedback": "; ".join(v["issues"])}
        result = self.qa.format(state["question"], state["raw_answer"], state.get("pool", []), state["plan"], v)
        result.run_id = self.t.run_id
        return {"validation": v, "result": result}

    def route_check(self, state: QAState):
        return END if state.get("result") is not None else "retrieve_broad"

    def _build(self):
        g = StateGraph(QAState)
        g.add_node("plan_question", self.plan_question)
        g.add_node("retrieve", self.retrieve)
        g.add_node("retrieve_broad", self.retrieve_broad)
        g.add_node("answer", self.answer)
        g.add_node("check", self.check)
        g.add_edge(START, "plan_question")
        g.add_conditional_edges("plan_question", self.fan_out, ["retrieve"])
        g.add_edge("retrieve", "answer")
        g.add_edge("retrieve_broad", "answer")
        g.add_edge("answer", "check")
        g.add_conditional_edges("check", self.route_check, ["retrieve_broad", END])
        return g.compile()

    def run(self, question: str, bid_ids: list[str] | None = None) -> QAAnswer:
        final = self.graph.invoke(
            {"question": question, "bid_ids": bid_ids, "evidence": [], "errors": []},
            config={"max_concurrency": self.s.settings.agent_max_concurrency, "recursion_limit": 40},
        )
        return final["result"]


# =========================================================== facade
class Orchestrator:
    """Entry point used by the CLI, API and UI."""

    def __init__(self, services: Services):
        self.s = services
        self._cache: SemanticCache | None = None

    def extract(self, bid_id: str, bid_folder: str | None = None, force_reindex: bool = False) -> dict:
        self.s.engine.warmup()
        tracer = self.s.new_tracer(f"extract-{bid_id}")
        return ExtractionWorkflow(self.s, tracer).run(bid_id, bid_folder, force_reindex)

    def ask(self, question: str, bid_ids: list[str] | None = None, use_cache: bool = True) -> QAAnswer:
        if use_cache:
            if self._cache is None:
                self._cache = SemanticCache(self.s)
            self.s.engine.warmup()
            hit = self._cache.get(question, bid_ids)
            if hit is not None:
                return hit
        tracer = self.s.new_tracer("qa")
        try:
            ans = QAWorkflow(self.s, tracer).run(question, bid_ids)
        except Exception as exc:  # last-resort guard
            logger.exception("QA workflow failed")
            ans = QAAnswer(question=question, answer=f"{NOT_FOUND}. (internal error: {exc})", not_found=True)
        ans.validation["trace"] = tracer.summary()
        if use_cache and self._cache is not None:
            self._cache.put(question, bid_ids, ans)
        return ans

    @staticmethod
    def graph_mermaid(services: Services) -> dict[str, str]:
        t = Tracer(None, mode="diagram")
        return {
            "extraction": ExtractionWorkflow(services, t).graph.get_graph().draw_mermaid(),
            "qa": QAWorkflow(services, t).graph.get_graph().draw_mermaid(),
        }
