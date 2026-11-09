"""Pydantic models for every agent-to-agent message and for LLM structured outputs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

NOT_FOUND = "Not found in documents"


# ----------------------------------------------------------------- evidence
class Evidence(BaseModel):
    """A retrieved passage handed from the Retrieval Agent to other agents."""

    evidence_id: str = ""  # E1, E2 ... assigned per prompt
    chunk_id: str
    bid_id: str
    file: str
    page: int
    page_end: int
    doc_type: str
    addendum_number: int | None = None
    section: str = ""
    text: str
    score: float = 0.0  # rerank (0-1) or fused score
    query: str = ""

    def render(self, max_chars: int = 900) -> str:
        meta = f'file="{self.file}" type={self.doc_type}'
        if self.addendum_number is not None:
            meta += f" addendum={self.addendum_number}"
        meta += f" page={self.page}"
        if self.section:
            meta += f' section="{self.section[:90]}"'
        text = self.text if len(self.text) <= max_chars else focus_text(self.text, self.query, max_chars)
        return f"[{self.evidence_id}] {meta}\n{text}"


def focus_text(text: str, query: str, max_chars: int) -> str:
    """Return the window of consecutive sentences (<= max_chars) that best matches the query terms."""
    import re

    from search.bm25 import tokenize

    sents = [s for s in re.split(r"(?<=[.;:!?])\s+|\n+", text) if s.strip()]
    if not sents:
        return text[:max_chars]
    q = set(tokenize(query or ""))
    scores = [len(q & set(tokenize(s))) for s in sents]
    best, best_i, best_j = -1, 0, 0
    for i in range(len(sents)):
        length, score, j = 0, 0, i
        while j < len(sents) and length + len(sents[j]) + 1 <= max_chars:
            length += len(sents[j]) + 1
            score += scores[j]
            j += 1
        if j == i:  # a single very long sentence
            j, score = i + 1, scores[i]
        if score > best:
            best, best_i, best_j = score, i, j
    window = " ".join(sents[best_i:best_j])[:max_chars]
    return ("... " if best_i > 0 else "") + window + (" ..." if best_j < len(sents) else "")


class Source(BaseModel):
    file: str
    page: int
    chunk_id: str | None = None
    quote: str | None = None
    # Pinpointing details (Q&A answers): where in the file and the exact supporting sentence(s).
    page_end: int | None = None
    section: str | None = None
    doc_type: str | None = None
    quotes: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- fields
class FieldValue(BaseModel):
    value: str | None = None
    sources: list[Source] = Field(default_factory=list)
    confidence: float = 0.0
    notes: str = ""
    status: Literal["extracted", "not_found", "failed", "generated"] = "not_found"
    attempts: int = 0
    evidence_ids: list[str] = Field(default_factory=list, exclude=True)  # internal
    llm_confidence: float = Field(default=0.0, exclude=True)
    cited: list[Evidence] = Field(default_factory=list, exclude=True)  # evidence backing the value
    top_evidence_score: float = Field(default=0.0, exclude=True)  # best retrieval score seen

    def public(self) -> dict:
        return {
            "value": self.value,
            "sources": [s.model_dump(exclude_none=True) for s in self.sources],
            "confidence": round(self.confidence, 2),
            "notes": self.notes,
        }


# ----------------------------------------------------------------- LLM outputs
class LLMFieldExtraction(BaseModel):
    field: str
    value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    notes: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> str | None:
        if v is None:
            return None
        if isinstance(v, list):
            v = "; ".join(str(x) for x in v if x not in (None, ""))
        elif isinstance(v, dict):
            v = "; ".join(f"{k}: {val}" for k, val in v.items())
        v = str(v).strip()
        if not v or v.lower() in {"null", "none", "n/a", "not found", NOT_FOUND.lower(), "not found in documents."}:
            return None
        return v

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_conf(cls, v: Any) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.5

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _coerce_ids(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [x.strip() for x in v.replace(";", ",").split(",")]
        return [str(x).strip().strip("[]") for x in v if str(x).strip()]


class LLMExtractionResponse(BaseModel):
    fields: list[LLMFieldExtraction] = Field(default_factory=list)


class LLMChange(BaseModel):
    field: str | None = None  # schema field name, or null if the change maps to no field
    change_type: Literal["modification", "clarification", "addition", "deletion"] = "clarification"
    description: str
    original_value: str | None = None
    new_value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return LLMFieldExtraction._coerce_ids(v)

    @field_validator("change_type", mode="before")
    @classmethod
    def _ct(cls, v: Any) -> str:
        v = str(v or "clarification").lower()
        return v if v in {"modification", "clarification", "addition", "deletion"} else "clarification"


class LLMReconciliationResponse(BaseModel):
    changes: list[LLMChange] = Field(default_factory=list)


class LLMCriticItem(BaseModel):
    field: str
    supported: Literal["yes", "partial", "no"] = "yes"
    issue: str = ""

    @field_validator("supported", mode="before")
    @classmethod
    def _sup(cls, v: Any) -> str:
        v = str(v).lower().strip()
        if v in {"true", "supported", "y"}:
            return "yes"
        if v in {"false", "unsupported", "n"}:
            return "no"
        return v if v in {"yes", "partial", "no"} else "partial"


class LLMCriticResponse(BaseModel):
    results: list[LLMCriticItem] = Field(default_factory=list)


class LLMSummary(BaseModel):
    summary: str


# ----------------------------------------------------------------- reconciliation / validation
class AddendumChange(BaseModel):
    addendum_number: int | None = None
    file: str
    field: str | None = None
    change_type: str
    description: str
    original_value: str | None = None
    new_value: str | None = None
    applied: bool = False
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list, exclude=True)  # addendum passages (internal)


class FieldValidation(BaseModel):
    field: str
    status: Literal["passed", "failed", "not_found"]
    issues: list[str] = Field(default_factory=list)
    retryable: bool = False
    critic: str | None = None  # yes / partial / no


class ValidationReport(BaseModel):
    results: dict[str, FieldValidation] = Field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        c = {"passed": 0, "failed": 0, "not_found": 0}
        for r in self.results.values():
            c[r.status] += 1
        return c

    def failed_fields(self, retryable_only: bool = True) -> list[str]:
        return [f for f, r in self.results.items() if r.status == "failed" and (r.retryable or not retryable_only)]


# ----------------------------------------------------------------- planning
class PlanStep(BaseModel):
    step: int
    agent: str
    action: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    mode: Literal["extraction", "qa"]
    goal: str
    steps: list[PlanStep] = Field(default_factory=list)


class SubQuery(BaseModel):
    query: str
    bid_id: str | None = None
    doc_type: str | None = None
    addendum_number: int | None = None

    @field_validator("doc_type", mode="before")
    @classmethod
    def _dt(cls, v: Any) -> str | None:
        allowed = {"bid_page", "rfp", "addendum", "specs", "affidavit", "form", "other"}
        v = (str(v).lower().strip() if v else None)
        return v if v in allowed else None

    @field_validator("addendum_number", mode="before")
    @classmethod
    def _an(cls, v: Any) -> int | None:
        try:
            return int(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None


class QAPlan(BaseModel):
    intent: Literal["lookup", "compare", "addendum_diff", "list", "summary", "other"] = "lookup"
    bid_ids: list[str] = Field(default_factory=list)
    sub_queries: list[SubQuery] = Field(default_factory=list)
    reasoning: str = ""

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v: Any) -> str:
        v = str(v or "lookup").lower()
        return v if v in {"lookup", "compare", "addendum_diff", "list", "summary", "other"} else "other"


class LLMAnswer(BaseModel):
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)
    not_found: bool = False
    confidence: float = 0.5

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return LLMFieldExtraction._coerce_ids(v)


class QAAnswer(BaseModel):
    question: str
    answer: str
    citations: list[Source] = Field(default_factory=list)
    not_found: bool = False
    confidence: float = 0.0
    plan: QAPlan | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    run_id: str = ""
    cached: bool = False


class BidRecord(BaseModel):
    bid_id: str
    fields: dict[str, dict]
    addendum_changes: list[dict] = Field(default_factory=list)
    validation: dict[str, int] = Field(default_factory=dict)
    documents: list[dict] = Field(default_factory=list)
    run: dict[str, Any] = Field(default_factory=dict)
