# Architecture

## System overview

```mermaid
flowchart TB
    subgraph Ingestion["Ingestion (Ingestion Agent)"]
        F[Bid folder<br/>HTML + PDF + addenda] --> D{file type}
        D -->|.html| H[BeautifulSoup parser<br/>label/value widgets, tables,<br/>nav-noise removal]
        D -->|.pdf| P[PyMuPDF parser<br/>sorted blocks, row merge,<br/>real-table detection, OCR fallback]
        H --> C[Cleaning<br/>unicode, de-hyphenation,<br/>line repair, header/footer removal]
        P --> C
        C --> M[Classifier<br/>doc_type, addendum_number,<br/>doc date, title]
        M --> K[Section/table-aware chunker<br/>~180 words, 30 overlap,<br/>header rows repeated]
    end

    subgraph Search["RAG Search Engine"]
        K --> E[Embeddings<br/>bge-base-en-v1.5]
        E --> V[(FAISS IP index<br/>per-bid shards,<br/>incremental)]
        K --> B[(BM25<br/>identifier-aware tokens)]
        Q[query] --> X[Query understanding<br/>synonym expansion + LLM rewrites]
        X --> V & B
        V & B --> R[RRF fusion + metadata filters<br/>bid / doc_type / addendum / file]
        R --> RR[Cross-encoder rerank<br/>bge-reranker-base]
        RR --> S[Cited results<br/>file, page, section, text, scores]
    end

    subgraph Agents["Multi-agent system (LangGraph)"]
        O[Orchestrator / Planner] --> IA[Ingestion Agent]
        O --> RA[Retrieval Agent]
        RA -->|search tool| S
        O --> EA[Extraction specialists x4<br/>parallel]
        EA --> AR[Addendum Reconciliation Agent]
        AR --> VA[Validator / Critic Agent]
        VA -->|failed fields + feedback<br/>max 2 retries| RA
        VA --> RP[Q&A / Report Agent]
    end

    API[FastAPI REST] --> O
    CLI[CLI main.py] --> O
    UI[Streamlit UI] --> O
    API --> S
    RP --> OUT[outputs/BidN.json<br/>summary.md, traces/*.jsonl]
```

## Extraction graph (LangGraph)

```mermaid
flowchart LR
    START((start)) --> plan[plan<br/>Orchestrator]
    plan --> ingest[ingest<br/>Ingestion Agent]
    ingest -- "Send x4 (parallel)" --> eg[extract_group<br/>Retrieval Agent +<br/>specialist Extraction Agent]
    ingest -- ingestion failed --> report
    eg --> reconcile[reconcile<br/>Addendum Agent]
    reconcile --> validate[validate<br/>Validator/Critic]
    validate -- "failed fields & retries left:<br/>Send(extract_group, feedback)" --> eg
    validate -- done --> report[report<br/>Report Agent]
    report --> END((end))
```

Shared state (`ExtractionState`): plan, ingestion report, `drafts` (field -> value/sources/confidence),
`evidence` (field -> passages), addendum change log, validation report, per-field validator feedback,
retry round, final record, errors. Parallel branches merge through dict reducers.

## QA graph (LangGraph)

```mermaid
flowchart LR
    START((start)) --> pq[plan_question<br/>Planner: intent, bids,<br/>filtered sub-queries]
    pq -- "Send per sub-query (parallel)" --> rt[retrieve<br/>Retrieval Agent]
    rt --> ans[answer<br/>Q&A Agent]
    ans --> chk[check<br/>citations + grounding]
    chk -- not grounded, 1 retry --> rb[retrieve_broad<br/>LLM rewrites, relaxed filters] --> ans
    chk -- ok --> END((end))
```

## Agent responsibilities and messages

| Agent | Responsibility | Input message | Output message |
|---|---|---|---|
| Orchestrator / Planner | builds the execution plan, fans out work, decides retries and when the job is done | user goal | `ExecutionPlan` / `QAPlan` |
| Ingestion Agent | parse, clean, classify, chunk, index (incremental) | folder path | ingestion report (docs, chunks, warnings) |
| Retrieval Agent | query expansion/rewriting, filtered hybrid search + rerank | `FieldSpec` / `SubQuery` | `list[Evidence]` |
| Extraction specialists (identity, dates & logistics, commercial & legal, product & specs) | fill fields from evidence only | fields + evidence + validator feedback | `dict[field, FieldValue]` (`LLMExtractionResponse`) |
| Addendum Reconciliation Agent | change log per addendum, latest-wins application | drafts + addendum passages | `list[AddendumChange]` + updated drafts |
| Validator / Critic Agent | citation, grounding, format, LLM support check, confidence | drafts | `ValidationReport` |
| Q&A / Report Agent | cited answers; final JSON, Bid Summary, Markdown | evidence / validated fields | `QAAnswer` / `BidRecord` |

All messages are Pydantic models (`agents/schemas.py`); LLM outputs are validated against them with one
automatic repair round-trip on malformed JSON.
