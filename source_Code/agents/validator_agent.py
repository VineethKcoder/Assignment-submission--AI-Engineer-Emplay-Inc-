"""Validator / Critic Agent.

Checks every drafted field for:
* citation guardrail - a non-null value must cite at least one evidence passage;
* hallucination - numbers, ids, emails and dates in the value must appear in
  the cited evidence (deterministic, see `agents.grounding`);
* format - e.g. the due date must be a parseable date with a time;
* support - an LLM critic judges (batched, one call) whether the cited
  evidence actually supports each value (yes / partial / no).

Failed fields are marked retryable and sent back to the Orchestrator, which
re-runs retrieval + extraction for just those fields (bounded retries).
It also computes the final confidence score per field.
"""

from __future__ import annotations

import logging

from agents.fields import FieldConfig
from agents.grounding import has_time, parse_datetime, ungrounded_tokens
from agents.schemas import FieldValidation, FieldValue, LLMCriticResponse, ValidationReport, focus_text
from agents.services import Services
from core.llm import LLMError
from core.tracing import Span

logger = logging.getLogger("rfp.agents.validator")

CRITIC_SYSTEM = """You are the Validator / Critic Agent of an RFP extraction system.
For each field you get the extracted value and the evidence passages it cites.
Judge strictly whether the cited evidence supports the value:
- "yes": the evidence clearly states the value (paraphrase/reformatting is fine)
- "partial": partly supported, or the value adds details not in the evidence, or misses an obvious key part
- "no": the evidence does not support the value, or contradicts it
Give a short issue description for partial/no (what is wrong or missing).
Return JSON: {"results": [{"field": "<name>", "supported": "yes|partial|no", "issue": ""}]}"""


class ValidatorAgent:
    name = "validator_agent"

    def __init__(self, services: Services):
        self.s = services

    # ----------------------------------------------------------------
    def _deterministic(self, name: str, fv: FieldValue, cfg: FieldConfig) -> FieldValidation:
        spec = cfg.fields[name]
        if fv.status == "failed" and fv.value is None:
            return FieldValidation(field=name, status="failed", issues=[fv.notes or "extraction failed"], retryable=True)
        if fv.value is None:
            return FieldValidation(field=name, status="not_found")
        issues: list[str] = []
        retry = False
        if not fv.sources or not fv.cited:
            issues.append("value has no valid citation (guardrail: every value needs >= 1 source)")
            retry = True
        else:
            missing = ungrounded_tokens(fv.value, [e.text for e in fv.cited])
            n_words = len(fv.value.split())
            if missing and (n_words <= 30 or len(missing) >= 3):
                issues.append(f"value contains {missing[:5]} which do not appear in the cited evidence")
                retry = True
        if spec.is_date:
            dt = parse_datetime(fv.value)
            if dt is None:
                issues.append(f"not a parseable date (expected {spec.format})")
                retry = True
            elif not has_time(fv.value):
                issues.append("date has no time; include time and time zone if the evidence gives them")
        status = "failed" if retry else "passed"
        return FieldValidation(field=name, status=status, issues=issues, retryable=retry)

    def _critic(self, drafts: dict[str, FieldValue], names: list[str], span: Span | None) -> dict[str, tuple[str, str]]:
        if not names or not self.s.settings.validator_use_llm:
            return {}
        blocks = []
        for n in names:
            fv = drafts[n]
            ev = "\n".join(f"  ({e.file} p.{e.page}) {focus_text(e.text, f'{n} {fv.value}', 700)}"
                           for e in fv.cited[:3])
            blocks.append(f"FIELD: {n}\nVALUE: {fv.value}\nCITED EVIDENCE:\n{ev}")
        try:
            resp = self.s.llm.chat_json(CRITIC_SYSTEM, "\n\n".join(blocks), LLMCriticResponse, span=span, max_tokens=1500)
        except LLMError as exc:
            logger.warning("critic unavailable: %s", exc)
            return {}
        out = {}
        for r in resp.results:
            match = next((n for n in names if n.lower() == r.field.strip().lower()), None)
            if match:
                out[match] = (r.supported, r.issue)
        return out

    # ----------------------------------------------------------------
    def validate(self, drafts: dict[str, FieldValue], cfg: FieldConfig, span: Span | None = None,
                 only: list[str] | None = None, previous: ValidationReport | None = None) -> ValidationReport:
        names = [f.name for f in cfg.extractable if f.name in drafts and (only is None or f.name in only)]
        report = ValidationReport(results=dict(previous.results) if previous else {})
        for n in names:
            report.results[n] = self._deterministic(n, drafts[n], cfg)
        to_critic = [n for n in names if report.results[n].status == "passed"]
        verdicts = self._critic(drafts, to_critic, span)
        for n, (verdict, issue) in verdicts.items():
            r = report.results[n]
            r.critic = verdict
            if verdict == "no":
                r.status, r.retryable = "failed", True
                r.issues.append(f"critic: not supported by evidence - {issue}")
            elif verdict == "partial" and issue:
                r.issues.append(f"critic: partially supported - {issue}")
        if span:
            span.set_output({"counts": report.counts, "failed": report.failed_fields(False)})
        return report

    # ----------------------------------------------------------------
    @staticmethod
    def confidence(fv: FieldValue, v: FieldValidation | None) -> float:
        """Blend extractor self-confidence, retrieval score and validation outcome."""
        ev_score = max((e.score for e in fv.cited), default=fv.top_evidence_score)
        ev_score = max(0.0, min(1.0, ev_score))
        if fv.value is None and ("no evidence retrieved" in fv.notes or "LLM error" in fv.notes):
            return 0.3  # absence not really verified
        if fv.value is None:
            # sure it's absent when even the best retrieved passage was weak
            return round(max(0.3, min(0.95, 0.9 - 0.4 * fv.top_evidence_score)), 2)
        if v is None:
            val = 0.5
        elif v.status == "passed":
            val = 1.0 if v.critic in (None, "yes") else 0.6
        else:
            val = 0.15
        conf = 0.45 * fv.llm_confidence + 0.25 * ev_score + 0.30 * val
        return round(max(0.05, min(0.99, conf)), 2)
