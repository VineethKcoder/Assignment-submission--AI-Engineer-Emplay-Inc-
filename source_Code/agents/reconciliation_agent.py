"""Addendum Reconciliation Agent.

Single responsibility: compare each addendum with the base RFP and apply the
latest valid change to each structured field.

1. For each addendum (ascending number) the agent reads the addendum's chunks
   (addenda are short; long ones are truncated) together with the current draft
   field values and asks the LLM for a structured change log.
2. Changes are applied deterministically, latest addendum wins:
   * `replace` fields (due date, pre-bid meeting, delivery, term, submission)
     take the new value; the original is kept in `notes`;
   * `append` fields (documents, installation, specs ...) get the addendum
     detail appended so earlier information is never lost;
   * clarifications only add a note + citation.
Re-running `apply` is idempotent (used after validator-triggered retries).
"""

from __future__ import annotations

import logging

from agents.fields import FieldConfig
from agents.schemas import AddendumChange, Evidence, FieldValue, LLMReconciliationResponse, Source
from agents.services import Services
from core.llm import LLMError
from core.tracing import Span

logger = logging.getLogger("rfp.agents.reconcile")

SYSTEM = """You are the Addendum Reconciliation Agent of an RFP intelligence system.
You receive ONE addendum (numbered evidence passages) and the current values of the bid's structured fields,
which were extracted mainly from the original RFP.
List every change the addendum makes or clarifies. For each change:
- field: the exact field name from the CURRENT FIELD VALUES list that the change affects. Map generously:
  deadlines -> "Due Date"; pre-bid/pre-proposal -> "Pre Bid Meeting"; warranty, hardware/port/spec
  requirements -> "Product Specification"; imaging, software installation, etching, asset tagging,
  deployment -> "Installation"; forms, affidavits, certificates, attachments -> "Any Additional Documentation
  Required"; quantities/products -> "Product"; delivery timing -> "Delivery Date"; contract length ->
  "Term of Bid"; payment/invoicing -> "Payment Terms". Use null only if no field is related.
- change_type: "modification" (a value is changed/extended/replaced), "addition" (new requirement added),
  "deletion" (requirement removed) or "clarification" (Q&A answer that clarifies without changing)
- description: one sentence describing the change
- original_value: the previous value if stated or known from the current values, else null
- new_value: the new value (for dates use YYYY-MM-DD HH:MM TZ), else null
- evidence_ids: ids of the passages that state it
Use ONLY the addendum text. Do not invent changes. Prefer fewer, accurate entries (max 15).
Return JSON: {"changes": [{"field": null, "change_type": "clarification", "description": "", "original_value": null,
"new_value": null, "evidence_ids": ["E1"]}]}"""


class ReconciliationAgent:
    name = "addendum_reconciliation_agent"

    def __init__(self, services: Services):
        self.s = services

    def addenda(self, bid_id: str) -> list[tuple[int, str]]:
        seen: dict[str, int] = {}
        for c in self.s.engine.get_chunks(bid_id=bid_id, doc_type="addendum"):
            seen[c.file_name] = c.addendum_number or 0
        return sorted(((n, f) for f, n in seen.items()), key=lambda x: (x[0], x[1]))

    def find_changes(self, bid_id: str, drafts: dict[str, FieldValue], cfg: FieldConfig,
                     span: Span | None = None) -> list[AddendumChange]:
        changes: list[AddendumChange] = []
        field_names = [f.name for f in cfg.extractable]
        current = "\n".join(
            f"- {n}: {(drafts.get(n).value or 'null')[:160] if drafts.get(n) else 'null'}" for n in field_names
        )
        for number, file_name in self.addenda(bid_id):
            chunks = self.s.engine.get_chunks(bid_id=bid_id, file_name=file_name)
            chunks.sort(key=lambda c: (c.page_number, c.chunk_id))
            pool: list[Evidence] = []
            budget = self.s.settings.addendum_max_chars
            for c in chunks:
                if budget <= 0:
                    break
                text = c.text[:budget]
                budget -= len(text)
                pool.append(Evidence(evidence_id=f"E{len(pool) + 1}", chunk_id=c.chunk_id, bid_id=bid_id,
                                     file=c.file_name, page=c.page_number, page_end=c.page_end,
                                     doc_type=c.doc_type, addendum_number=c.addendum_number, text=text))
            if span:
                span.tool_call("search_engine.get_chunks", {"bid_id": bid_id, "file_name": file_name},
                               f"{len(pool)} passages")
            user = (
                f"Bid: {bid_id}\nAddendum number: {number}  (file: {file_name})\n\nCURRENT FIELD VALUES:\n{current}\n\n"
                f"ADDENDUM PASSAGES:\n" + "\n\n".join(e.render(2000) for e in pool)
            )
            try:
                resp = self.s.llm.chat_json(SYSTEM, user, LLMReconciliationResponse, span=span, prefer_primary=True)
            except LLMError as exc:
                logger.error("reconciliation failed for %s: %s", file_name, exc)
                changes.append(AddendumChange(addendum_number=number, file=file_name, change_type="error",
                                              description=f"could not analyse addendum: {str(exc)[:120]}"))
                continue
            by_id = {e.evidence_id: e for e in pool}
            for ch in resp.changes:
                fld = next((n for n in field_names if ch.field and n.lower() == ch.field.strip().lower()), None)
                cited = [by_id[i] for i in ch.evidence_ids if i in by_id] or pool[:1]
                changes.append(
                    AddendumChange(
                        addendum_number=number, file=file_name, field=fld, change_type=ch.change_type,
                        description=ch.description, original_value=ch.original_value, new_value=ch.new_value,
                        sources=[Source(file=e.file, page=e.page, chunk_id=e.chunk_id) for e in cited],
                        evidence=cited,
                    )
                )
        return changes

    @staticmethod
    def apply(drafts: dict[str, FieldValue], changes: list[AddendumChange], cfg: FieldConfig) -> dict[str, FieldValue]:
        out = {k: v.model_copy(deep=True) for k, v in drafts.items()}
        for ch in sorted(changes, key=lambda c: (c.addendum_number or 0)):
            if not ch.field or ch.field not in out:
                continue
            spec = cfg.fields[ch.field]
            d = out[ch.field]
            tag = f"Addendum {ch.addendum_number}"
            already = any(s.file == ch.file for s in d.sources)
            if ch.change_type in ("modification", "addition", "deletion") and ch.new_value:
                if already:
                    # extractor already used the addendum; just document the change
                    if tag not in d.notes:
                        orig = f" (original: {ch.original_value})" if ch.original_value else ""
                        d.notes = (d.notes + " " if d.notes else "") + f"Reflects {tag}{orig}."
                elif spec.on_addendum == "replace" or d.value is None:
                    orig = d.value or ch.original_value
                    d.value = ch.new_value
                    d.notes = f"Updated by {tag}" + (f" (original: {orig})" if orig else "") + "."
                    d.sources = ch.sources + d.sources
                    d.status = "extracted"
                    d.llm_confidence = max(d.llm_confidence, 0.8)
                else:
                    d.value = f"{d.value}; {tag}: {ch.new_value}"
                    d.notes = (d.notes + " " if d.notes else "") + f"Amended by {tag}."
                    d.sources = d.sources + ch.sources
                d.cited = d.cited + ch.evidence
                ch.applied = True
            elif ch.change_type == "clarification" and d.value is not None and tag not in d.notes:
                d.notes = (d.notes + " " if d.notes else "") + f"Clarified by {tag}: {ch.description[:160]}"
                if not already:
                    d.sources = d.sources + ch.sources[:1]
                ch.applied = True
        return out
