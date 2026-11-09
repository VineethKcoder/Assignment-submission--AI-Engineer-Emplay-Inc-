"""Extraction Agents (one specialist per field group, run in parallel).

Each specialist receives ONLY retrieved evidence (never whole documents),
fills its fields with values + evidence ids, and must answer
"Not found in documents" instead of guessing.
"""

from __future__ import annotations

import logging

from agents.fields import FieldSpec, GroupSpec
from agents.schemas import NOT_FOUND, Evidence, FieldValue, LLMExtractionResponse, Source
from agents.services import Services
from core.llm import LLMError
from core.tracing import Span

logger = logging.getLogger("rfp.agents.extraction")

SYSTEM_TEMPLATE = """You are the {agent} in a multi-agent RFP intelligence system.
Focus: {focus}

Rules (strict):
1. Use ONLY the numbered evidence passages provided. Never use outside knowledge and never guess.
2. If the evidence does not contain a field's information, return value null with notes "{not_found}".
3. Every non-null value MUST cite at least one evidence id (e.g. ["E3"]) whose text directly supports it.
4. Addendum documents (type=addendum) override the original RFP. When an addendum changes a value, use the
   value from the highest-numbered addendum and mention the original value in notes.
5. Copy identifiers, numbers, dates, emails and phone numbers exactly as written (you may reformat dates
   into the requested format, keeping the same day and time).
6. Respect each field's format hint. Keep values concise (<= 60 words; specifications <= 120 words).
7. confidence (0-1) = how directly the cited evidence states the value.

Return JSON: {{"fields": [{{"field": "<field name>", "value": "<string or null>", "evidence_ids": ["E1"],
"confidence": 0.0, "notes": "<short note>"}}]}} with exactly one entry per requested field."""


def build_evidence_pool(fields: list[FieldSpec], evidence_by_field: dict[str, list[Evidence]],
                        pool_max: int) -> list[Evidence]:
    """Round-robin merge of per-field evidence so every field keeps its best passages."""
    pool: list[Evidence] = []
    seen: set[str] = set()
    lists = [list(evidence_by_field.get(f.name, [])) for f in fields]
    depth = max((len(l) for l in lists), default=0)
    for i in range(depth):
        for lst in lists:
            if i < len(lst) and lst[i].chunk_id not in seen and len(pool) < pool_max:
                seen.add(lst[i].chunk_id)
                pool.append(lst[i].model_copy())
    for n, e in enumerate(pool, start=1):
        e.evidence_id = f"E{n}"
    return pool


class ExtractionAgent:
    def __init__(self, services: Services, group: GroupSpec):
        self.s = services
        self.group = group
        self.name = f"extraction_agent[{group.key}]"

    def run(self, bid_id: str, fields: list[FieldSpec], evidence_by_field: dict[str, list[Evidence]],
            feedback: dict[str, str] | None = None, span: Span | None = None,
            previous: dict[str, FieldValue] | None = None) -> dict[str, FieldValue]:
        st = self.s.settings
        feedback = feedback or {}
        pool = build_evidence_pool(fields, evidence_by_field, st.evidence_pool_max)
        by_id = {e.evidence_id: e for e in pool}
        top_score = {f.name: max((e.score for e in evidence_by_field.get(f.name, [])), default=0.0) for f in fields}

        if not pool:
            return {
                f.name: FieldValue(status="not_found", notes=f"{NOT_FOUND} (no evidence retrieved)",
                                   attempts=(previous or {}).get(f.name, FieldValue()).attempts + 1)
                for f in fields
            }

        field_lines = []
        for f in fields:
            line = f"- {f.name} [format: {f.format}]: {f.description}"
            if f.name in feedback:
                prev = (previous or {}).get(f.name)
                prev_val = f' Previous value: "{prev.value}".' if prev and prev.value else ""
                line += f"\n  VALIDATOR FEEDBACK on the previous attempt:{prev_val} Problem: {feedback[f.name]}"
            field_lines.append(line)
        evidence_txt = "\n\n".join(e.render(st.evidence_max_chars) for e in pool)
        user = (
            f"Bid: {bid_id}\n\nFIELDS TO EXTRACT:\n" + "\n".join(field_lines) + f"\n\nEVIDENCE:\n{evidence_txt}\n"
        )
        system = SYSTEM_TEMPLATE.format(agent=self.group.agent, focus=self.group.focus, not_found=NOT_FOUND)

        try:
            resp = self.s.llm.chat_json(system, user, LLMExtractionResponse, span=span, prefer_primary=True)
        except LLMError as exc:
            logger.error("%s failed: %s", self.name, exc)
            return {
                f.name: FieldValue(status="failed", notes=f"LLM error: {str(exc)[:150]}",
                                   attempts=(previous or {}).get(f.name, FieldValue()).attempts + 1,
                                   top_evidence_score=top_score[f.name])
                for f in fields
            }

        by_field = {}
        for item in resp.fields:
            key = item.field.strip()
            match = next((f.name for f in fields if f.name.lower() == key.lower()), None)
            if match:
                by_field[match] = item

        out: dict[str, FieldValue] = {}
        for f in fields:
            attempts = (previous or {}).get(f.name, FieldValue()).attempts + 1
            item = by_field.get(f.name)
            if item is None:
                out[f.name] = FieldValue(status="failed", notes="extractor returned no entry for this field",
                                         attempts=attempts, top_evidence_score=top_score[f.name])
                continue
            cited = [by_id[i] for i in item.evidence_ids if i in by_id]
            if item.value is None:
                out[f.name] = FieldValue(
                    value=None, status="not_found", notes=item.notes or NOT_FOUND, attempts=attempts,
                    llm_confidence=item.confidence, top_evidence_score=top_score[f.name],
                )
                continue
            out[f.name] = FieldValue(
                value=item.value,
                sources=[Source(file=e.file, page=e.page, chunk_id=e.chunk_id) for e in cited],
                status="extracted",
                notes=item.notes if item.notes and item.notes.lower() != NOT_FOUND.lower() else "",
                attempts=attempts,
                evidence_ids=item.evidence_ids,
                llm_confidence=item.confidence,
                cited=cited,
                top_evidence_score=top_score[f.name],
            )
        return out
