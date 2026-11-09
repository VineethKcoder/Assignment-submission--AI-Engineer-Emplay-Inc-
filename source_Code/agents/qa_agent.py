"""Q&A side of the Q&A / Report Agent, plus the question planner and answer validator.

* `plan()`     - Orchestrator/planner step for QA mode: resolves which bids a
                 question refers to (using the index catalog), the intent
                 (lookup / compare / addendum_diff / ...) and the sub-queries
                 with metadata filters for the Retrieval Agent.
* `answer()`   - writes the cited answer from evidence only.
* `validate()` - citation + grounding guardrails for the answer.
* `SemanticCache` - re-uses answers to near-identical questions (bonus).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path

import numpy as np

from agents.grounding import ungrounded_tokens
from agents.schemas import NOT_FOUND, Evidence, LLMAnswer, QAAnswer, QAPlan, Source, SubQuery
from agents.services import Services
from core.llm import LLMError
from core.tracing import Span
from search.bm25 import tokenize
from search.snippets import supporting_excerpt  # noqa: F401  (re-exported for callers/tests)

logger = logging.getLogger("rfp.agents.qa")

ANSWER_FORMAT_VERSION = "3"  # 3: sources carry section, page range and supporting quotes

_CLAIM_BREAK = re.compile(r"[.;!?](?=\s)|\n|\]")
_SENTENCE_BREAK = re.compile(r"[.!?](?=\s)|\n")


def claim_before(text: str, pos: int) -> str:
    """The clause an inline citation at `pos` supports: text back to the previous citation or clause end."""
    head = text[:pos]
    cuts = [m.end() for m in _CLAIM_BREAK.finditer(head)]
    claim = head[cuts[-1]:] if cuts else head
    if len(tokenize(claim)) < 3:  # too short to match on: use the whole sentence
        cuts = [m.end() for m in _SENTENCE_BREAK.finditer(head)]
        claim = head[cuts[-1]:] if cuts else head
    return re.sub(r"\[[^\]]*\]", " ", claim).strip(" \t,;:")



PLAN_SYSTEM = """You are the Orchestrator/Planner of an RFP question-answering system.
Given a user question and the catalog of indexed bids (bid ids, their documents and types), produce a plan:
- intent: lookup | compare | addendum_diff | list | summary | other
- bid_ids: the bids the question is about (resolve descriptions like "the Dell laptop bid" using the catalog;
  use ALL bids for comparisons or when the question does not identify a bid)
- sub_queries: 1-6 focused search queries. Each has a query string written in procurement-document wording,
  a bid_id (one bid per sub-query), optional doc_type filter (bid_page, rfp, addendum, specs, affidavit, form)
  and optional addendum_number. For comparisons create one sub-query per bid. For "what changed in addendum N"
  create a sub-query restricted to doc_type "addendum" with that addendum_number, plus one on the base rfp for
  the original values.
- For broad bid overviews or action questions ("what to do", "what does this bid mention", "how to bid"),
  use intent summary. For each bid, create focused queries covering ALL of these topics:
  (1) recorded portal status and final submission deadline including time/time zone,
  (2) eligibility, contract vehicle and submission method,
  (3) product models, quantities and essential specifications,
  (4) warranty duration, start event and required warranty documentation,
  (5) delivery window and quote validity with their reference events,
  (6) required forms, invoicing and relevant addendum changes.
  Combine related topics into at most six queries total, distributing coverage across the requested bids
  for multi-bid summaries. Do not replace these searches with
  generic "instructions" queries. Search recorded portal status/deadline with doc_type bid_page when that
  document is in the catalog; search addenda separately for deadline changes. Use other document filters
  only when they cannot exclude relevant evidence.
Return JSON: {"intent": "lookup", "bid_ids": ["..."], "sub_queries": [{"query": "...", "bid_id": "...",
"doc_type": null, "addendum_number": null}], "reasoning": "<one sentence>"}"""

ANSWER_SYSTEM = f"""You are the Q&A Agent of an RFP intelligence system. Answer the user's question using ONLY the
numbered evidence passages. Rules:
1. Cite evidence inline with its id in square brackets, e.g. "The due date is July 9, 2024 [E2]." Every factual
   sentence needs at least one citation.
2. Addenda override the original RFP: report the latest value and mention what it replaced.
3. If the evidence does not contain the answer, set not_found=true and answer exactly "{NOT_FOUND}." optionally
   followed by one sentence on what related information was found.
4. For comparisons, or when the question names no bid and several bids are in scope, address each bid
   separately (say "not found" for a bid without evidence), then summarise the difference.
5. Distinguish explicit requirements from generic or conditional boilerplate: a clause such as "must comply
   with any bond requirements noted elsewhere" does NOT establish that a bond is required - say what is and
   is not stated.
6. Keep each value attached to the exact item/tier the evidence gives it for; do not merge tiers.
7. Be concise (<= 180 words for focused questions; <= 300 words for a summary/checklist), factual,
   no speculation. Copy numbers, dates and identifiers exactly.
8. For summary/action questions, give a practical checklist covering the recorded status and final deadline
   (date, time, timezone), eligibility/submission, products AND quantities, warranty AND its documentation,
   delivery, quote validity, required forms and invoicing. Include these when supported by the evidence;
   mark missing checklist details "Not found in documents" rather than silently dropping them or guessing.
9. Preserve material qualifiers: "within N days OF AWARD", "at least N days AFTER THE DUE DATE", and warranty
   start/end events. Do not confuse invoice submission deadlines with payment terms or line-item due dates
   with delivery windows. A supplied form alone does not establish when it must be submitted.
10. Portal pages are saved documents, not live status checks. Attribute OPEN/CLOSED status to the supplied
    portal snapshot. If it records CLOSED, describe the original bid requirements in the past tense; do not
    tell the user that submissions are currently accepted or assert that the bid has since reopened.
Return JSON: {{"answer": "...", "evidence_ids": ["E1"], "not_found": false, "confidence": 0.0}}"""


class QAAgent:
    name = "qa_agent"

    def __init__(self, services: Services):
        self.s = services

    # ------------------------------------------------------------ planning
    def plan(self, question: str, bid_ids: list[str] | None, span: Span | None = None) -> QAPlan:
        catalog = self.s.engine.catalog()
        known = [c["bid_id"] for c in catalog]
        if bid_ids:
            catalog = [c for c in catalog if c["bid_id"] in bid_ids]
        cat_txt = "\n".join(
            f"- {c['bid_id']}: " + "; ".join(
                f"{d.get('file')} ({d.get('doc_type')}{' #' + str(d['addendum_number']) if d.get('addendum_number') else ''})"
                for d in c["documents"]
            )
            for c in catalog
        )
        try:
            plan = self.s.llm.chat_json(PLAN_SYSTEM, f"CATALOG:\n{cat_txt}\n\nQUESTION: {question}", QAPlan,
                                        span=span, max_tokens=900)
        except LLMError as exc:
            logger.warning("planner failed (%s); using default plan", exc)
            plan = QAPlan(intent="other", reasoning="fallback plan (planner unavailable)")
        allowed = set(bid_ids or known)
        plan.bid_ids = [b for b in plan.bid_ids if b in allowed] or sorted(allowed)
        subs = [sq for sq in plan.sub_queries if sq.query.strip()]
        for sq in subs:
            if sq.bid_id not in allowed:
                sq.bid_id = None if len(plan.bid_ids) > 1 else plan.bid_ids[0]
        if not subs:
            subs = [SubQuery(query=question, bid_id=b) for b in plan.bid_ids]
        if plan.intent == "summary":
            # A broad cross-document search can bury short portal status/date
            # passages under RFP instructions. Guarantee a portal search when
            # that source exists, without assuming any bid-specific contents.
            portal_bids = {c["bid_id"] for c in catalog
                           if any(d.get("doc_type") == "bid_page" for d in c["documents"])}
            portal_queries = [SubQuery(query="solicitation status closing date deadline time timezone",
                                       bid_id=b, doc_type="bid_page")
                              for b in plan.bid_ids if b in portal_bids and not any(
                                  sq.bid_id == b and sq.doc_type == "bid_page" for sq in subs)]
            subs = portal_queries + subs
        # Broad summary wording adds noise; retain the focused coverage queries instead.
        if plan.intent != "summary" and not any(sq.query.strip().lower() == question.strip().lower() for sq in subs):
            subs.append(SubQuery(query=question, bid_id=plan.bid_ids[0] if len(plan.bid_ids) == 1 else None))
        plan.sub_queries = subs[:7]
        return plan

    # ------------------------------------------------------------ answering
    def answer(self, question: str, evidence: list[Evidence], plan: QAPlan, span: Span | None = None,
               feedback: str | None = None) -> tuple[LLMAnswer, list[Evidence]]:
        # dedupe + cap evidence, keep ordering by score but ensure every bid in plan is represented
        uniq: dict[str, Evidence] = {}
        for e in sorted(evidence, key=lambda e: -e.score):
            uniq.setdefault(e.chunk_id, e)
        ranked = list(uniq.values())
        cap = 12
        pool: list[Evidence] = []
        if plan.intent == "summary":
            # Scores from different queries are not directly comparable. Reserve
            # space for each planned topic before filling with high-scoring hits.
            # Build from the original evidence so a shared chunk can still be
            # represented by the query that focuses its relevant text window.
            groups = [sorted([e for e in evidence if e.query == sq.query and
                              (sq.bid_id is None or e.bid_id == sq.bid_id)], key=lambda e: -e.score)
                      for sq in plan.sub_queries]
            seen: set[str] = set()
            while len(pool) < cap:
                added = False
                for group in groups:
                    while group and group[0].chunk_id in seen:
                        group.pop(0)
                    if group and len(pool) < cap:
                        e = group.pop(0)
                        pool.append(e)
                        seen.add(e.chunk_id)
                        added = True
                if not added:
                    break
        for b in plan.bid_ids:  # round-robin across bids for comparisons
            if plan.intent != "summary":
                pool.extend([e for e in ranked if e.bid_id == b][: max(3, cap // max(1, len(plan.bid_ids)))])
        for e in ranked:
            if len(pool) >= cap:
                break
            if not any(p.chunk_id == e.chunk_id for p in pool):
                pool.append(e)
        pool = [e.model_copy() for e in pool[:cap]]
        for i, e in enumerate(pool, 1):
            e.evidence_id = f"E{i}"
        if not pool:
            return LLMAnswer(answer=f"{NOT_FOUND}.", not_found=True, confidence=0.8), []
        ev_txt = "\n\n".join(f"(bid {e.bid_id}) " + e.render(self.s.settings.evidence_max_chars) for e in pool)
        user = f"QUESTION: {question}\nINTENT: {plan.intent}\nBIDS: {', '.join(plan.bid_ids)}\n\nEVIDENCE:\n{ev_txt}"
        if feedback:
            user += f"\n\nVALIDATOR FEEDBACK ON PREVIOUS ANSWER: {feedback}"
        try:
            ans = self.s.llm.chat_json(ANSWER_SYSTEM, user, LLMAnswer, span=span, max_tokens=1200,
                                       prefer_primary=True)
        except LLMError as exc:
            logger.error("QA answer failed: %s", exc)
            ans = LLMAnswer(answer=f"{NOT_FOUND}. (The language model was unavailable: {str(exc)[:80]})",
                            not_found=True, confidence=0.0)
        return ans, pool

    # ------------------------------------------------------------ validation / formatting
    @staticmethod
    def cited_ids(ans: LLMAnswer) -> list[str]:
        inline = re.findall(r"\[(E\d+)\]", ans.answer)
        inline += [x for grp in re.findall(r"\[((?:E\d+\s*,\s*)+E\d+)\]", ans.answer) for x in re.split(r"\s*,\s*", grp)]
        return list(dict.fromkeys(inline + ans.evidence_ids))

    def validate(self, ans: LLMAnswer, pool: list[Evidence], bid_ids: list[str] | None = None) -> dict:
        by_id = {e.evidence_id: e for e in pool}
        ids = [i for i in self.cited_ids(ans) if i in by_id]
        issues = []
        if not ans.not_found and not ids:
            issues.append("answer has no valid citations")
        if not ans.not_found and ids:
            text = re.sub(r"\[[^\]]*\]", " ", ans.answer)  # drop citation markers
            for b in set((bid_ids or []) + [e.bid_id for e in pool]):
                text = re.sub(rf"\b{re.escape(b)}\b", " ", text)  # bid ids are labels, not facts
            missing = ungrounded_tokens(text, [by_id[i].text for i in ids])
            if len(missing) > max(1, len(text.split()) // 40):
                issues.append(f"answer contains values not in cited evidence: {missing[:5]}")
        return {"passed": not issues, "issues": issues, "cited": ids}

    @staticmethod
    def format(question: str, ans: LLMAnswer, pool: list[Evidence], plan: QAPlan, validation: dict) -> QAAnswer:
        by_id = {e.evidence_id: e for e in pool}
        quotes: dict[tuple[str, int], list[str]] = {}

        def repl(m: re.Match) -> str:
            claim = claim_before(m.string, m.start())
            refs = []
            for i in re.split(r"\s*,\s*", m.group(1)):
                e = by_id.get(i.strip())
                if e:
                    refs.append(f"{e.file}, p.{e.page}")
                    found = quotes.setdefault((e.file, e.page), [])
                    q = supporting_excerpt(e.text, claim)
                    if q and q not in found and len(found) < 3:
                        found.append(q)
            return f"[{'; '.join(dict.fromkeys(refs))}]" if refs else ""

        text = re.sub(r"\[((?:E\d+\s*,\s*)*E\d+)\]", repl, ans.answer)
        cites, seen = [], set()
        for i in validation.get("cited", []):
            e = by_id[i]
            if (e.file, e.page) not in seen:
                seen.add((e.file, e.page))
                found = quotes.get((e.file, e.page)) or [q for q in [supporting_excerpt(e.text, question)] if q]
                cites.append(Source(file=e.file, page=e.page, chunk_id=e.chunk_id,
                                    quote=found[0] if found else e.text[:200], quotes=found,
                                    page_end=e.page_end if e.page_end > e.page else None,
                                    section=e.section or None, doc_type=e.doc_type))
        conf = ans.confidence if validation.get("passed") else min(ans.confidence, 0.4)
        return QAAnswer(question=question, answer=text, citations=cites, not_found=ans.not_found,
                        confidence=round(conf, 2), plan=plan, validation=validation)


class SemanticCache:
    """Embedding-similarity cache of answered questions (file-backed)."""

    def __init__(self, services: Services):
        self.s = services
        self.path = services.settings.path(services.settings.cache_dir) / "qa_semantic_cache.jsonl"
        self._lock = threading.Lock()
        self.entries: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self.entries.append(json.loads(line))
                except Exception:
                    pass

    def _key(self, bid_ids: list[str] | None) -> str:
        bids = ",".join(sorted(bid_ids or []))
        index_version = "|".join(f"{b}:{m.get('indexed_at', '')}" for b, m in sorted(self.s.engine.manifests.items()))
        # Prompt changes must not return answers created under an older policy.
        # ANSWER_FORMAT_VERSION: bump when the stored answer shape changes (e.g. source quotes added).
        policy = hashlib.sha256((PLAN_SYSTEM + ANSWER_SYSTEM + ANSWER_FORMAT_VERSION).encode()).hexdigest()[:16]
        return f"{bids}#{index_version}#{self.s.settings.llm_model}#{policy}"

    def get(self, question: str, bid_ids: list[str] | None) -> QAAnswer | None:
        if not self.entries:
            return None
        key = self._key(bid_ids)
        cands = [e for e in self.entries if e["key"] == key]
        if not cands:
            return None
        q = self.s.engine.embedder.encode_queries([question])[0]
        sims = [float(np.dot(q, np.asarray(e["emb"], dtype=np.float32))) for e in cands]
        best = int(np.argmax(sims))
        if sims[best] >= self.s.settings.semantic_cache_threshold:
            ans = QAAnswer.model_validate(cands[best]["answer"])
            ans.cached = True
            return ans
        return None

    def put(self, question: str, bid_ids: list[str] | None, answer: QAAnswer) -> None:
        if answer.not_found and answer.confidence < 0.3:
            return  # don't cache failures
        q = self.s.engine.embedder.encode_queries([question])[0]
        entry = {"key": self._key(bid_ids), "question": question, "emb": [round(float(x), 5) for x in q],
                 "answer": answer.model_dump()}
        with self._lock:
            self.entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
