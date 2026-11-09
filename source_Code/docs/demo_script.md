# Demo video script (about 9½ minutes)

Framing: I had already built a RAG chatbot (a Telegram bot answering questions over equipment manuals). This
project starts from what worked there and adds what the assignment needs: many document types per bid,
addenda that override the RFP, structured extraction with citations and confidence, a multi-agent workflow,
retrieval evaluation, and an API/CLI/UI.

Each segment lists **Screen** (what to show) and **Say** (what to say, in your own words). Timings are targets.

---

## Before you record (checklist)

- [ ] On the personal laptop: unzip, run `scripts\setup_windows.ps1`, put the Groq key in `.env`, then
      `python main.py index .\data\Bid1 .\data\Bid2`. The deliverables in `outputs\`, `eval\` and `docs\` are already
      up to date (2026-11-09 run); the numbers below come from them.
- [ ] Run the live commands in segments 4–7 once beforehand. LLM answers are cached in `.cache\llm`, so the
      recorded run is fast and does not hit Groq rate limits. (Say so if asked; it is a real feature.)
- [ ] Recorder: Windows Game Bar (`Win+Alt+R` starts/stops) or OBS. Record at 1080p, mic on.
- [ ] Open these tabs/windows in advance:
  `README.md` (rendered, section 2 diagram) · `docs\architecture.md` · `outputs\Bid1.json` ·
  `docs\example_trace.md` · `eval\results.md` · the Bid1 Addendum 2 PDF · a PowerShell window in the repo
  with the venv active · Streamlit running (`streamlit run ui\app.py`) in a browser tab.
- [ ] Close other apps (RAM), set the terminal font large, hide `.env` (never show the API key).
- [ ] Keep the old Telegram bot's README or a screenshot of the bot ready for segment 1 (10 seconds is enough).

---

## 1. Intro: where I'm coming from (0:00–0:50)

**Screen:** your face/title slide, then a screenshot of the Telegram bot answering a question.

**Say:**
> "Hi, I'm ⟨name⟩. Before this, I built a RAG chatbot on Telegram that answers technical questions from
> equipment manuals. It used hybrid retrieval, BM25 plus vector search, a cross-encoder reranker, OCR for
> scanned pages, table-aware chunking, and an LLM on Groq to write the answer with page references.
>
> This assignment is a different problem. A bid folder has an HTML portal page, the RFP PDF, specs, forms
> and addenda that *change* earlier values. The output isn't a chat reply but a structured record, every
> value with a citation and a confidence score. So I kept the retrieval ideas that worked and built
> a multi-agent system around them. Let me show you how."

---

## 2. Architecture: what carried over and what's new (0:50–2:10)

**Screen:** `README.md` section 2 / `docs\architecture.md` diagrams. Point at each box as you talk.

**Say:**
> "Three layers. **Ingestion**: PyMuPDF for PDFs, BeautifulSoup for the portal HTML, OCR fallback, a
> classifier that tags each file as bid page, RFP, addendum, specs, affidavit or form, and picks up the
> addendum number. Header and footer removal and row-group table chunking come straight from my
> Telegram bot.
>
> **Search engine**: still hybrid, BM25 plus dense embeddings, but with three changes from the bot.
> One, I merge with Reciprocal Rank Fusion instead of a weighted score, so I don't have to tune a weight
> or normalise scores. Two, the index is FAISS, one shard per bid, rebuilt incrementally from file hashes.
> Three, every chunk has metadata, so I can filter by bid, document type or addendum number.
>
> **Agents**: built with LangGraph. An orchestrator fans out extraction specialists in parallel, a
> reconciliation agent applies addenda in order, and a validator checks every value against its cited
> text and can send a field back for a retry. Q&A has its own small graph: planner, parallel retrieval,
> answer agent, validator. Every message between agents is a Pydantic model."

**Comparison you can mention (optional on-screen table):**

| Telegram bot | This platform | Why |
|---|---|---|
| Weighted BM25 + vector merge | Reciprocal Rank Fusion | No weight tuning, scale-free |
| One pickle index | FAISS shard per bid, incremental | Add bids without rebuilding |
| One prompt answers everything | LangGraph agents + validator retry | Structured output, checked values |
| Free-text answer with "(p. 42)" | JSON: value + file/page citations + confidence | Assignment needs auditable fields |
| No metrics | Recall@k / MRR evaluation | Prove each retrieval choice helps |
| Telegram only | CLI, REST API, Streamlit UI | Different users and integrations |

---

## 3. Ingestion and indexing (2:10–2:50)

**Screen:** terminal.

```powershell
python main.py list
```

**Say:**
> "Each sub-folder is one bid; the folder name is the bid id. Here's what was indexed: per file, its type,
> page count and date. Addenda are numbered from the file name or the text. Indexing is incremental: if
> I run `index` again it says up to date, and only changed files are re-parsed."

(Optional: `python main.py index .\data\Bid1` to show "up_to_date".)

---

## 4. The search engine on its own (2:50–3:50)

**Screen:** terminal.

```powershell
python main.py search "210-BLYZ" --mode dense --top-k 3
python main.py search "210-BLYZ" --mode hybrid_rerank --top-k 3
python main.py search "new due date" --bid-id Bid1 --doc-type addendum --top-k 3
```

**Say:**
> "This is a lesson from my bot: dense embeddings are bad at exact identifiers. In dense mode, the part
> number 210-BLYZ doesn't reliably come first. With hybrid plus rerank, the Dell spec page is the top
> hit with a high rerank score. My BM25 tokenizer keeps codes like `210-BLYZ` and `JA-207652` intact
> and also indexes their parts. The third query shows metadata filters: only Bid1 addenda."

---

## 5. Multi-agent extraction (3:50–5:30)

**Screen:** terminal, then `outputs\Bid1.json`, then the Addendum 2 PDF.

```powershell
python main.py --bid .\data\Bid1
```

**Say while it runs:**
> "One command runs the extraction graph for a bid. The ingestion agent makes sure it's indexed, the
> retrieval agent gathers evidence per field group, four extraction specialists run in parallel, the
> reconciliation agent applies addenda in order, and the validator checks each value."

**Then open `outputs\Bid1.json` and point at `Due Date`:**
> "Here's the result: 20 fields, 17 passed validation, 0 failed, 3 honestly not found. Look at the due date:
> 2024-07-09 at 2 PM, taken from **Addendum 2**, with the original date kept in the notes. Every value has
> its source file and page, and a confidence score: the extractor's own confidence, the rerank score of
> the cited passage, and the validation result combined."

**Open the Addendum 2 PDF at page 1** and show the new date. Then point at a field whose value is null:
> "When something isn't in the documents, it says 'Not found in documents' instead of guessing. In my
> Telegram bot the LLM only saw a few chunks, so I never let it claim something was missing. Here the claim
> is backed by retrieval plus the validator, and the confidence says how sure it is that the value is absent."

(Optional: show `addendum_changes`, the change log mapping each addendum change to a field.)

---

## 6. Agent trace: proof the agents really interact (5:30–6:30)

**Screen:** `docs\example_trace.md`.

**Say:**
> "Every run writes a trace: each agent step, the tool calls it made, LLM calls, tokens and time. Here you
> can see the specialists running in parallel, and here the validator rejecting a value. In this run the
> model guessed a delivery date, September 1st 2024, that the documents never state. The code check caught
> it, the field went back for a retry with that feedback, twice, and then became 'not found' instead of a
> made-up date. The trace also shows cost: Bid1 took 11 LLM calls, about 22 thousand tokens, roughly half a cent."

---

## 6b. Unseen bid: no code changes (6:30–7:00)

**Screen:** `examples\unseen_bid\README.md` (the table), then `examples\unseen_bid\Riverton-Network-Switches.json`.

**Say:**
> "The brief says it will be tested on a bid it has never seen. So I made one: a different city, network
> switches instead of laptops, different file names, and an addendum that changes the due date and the
> quantity. Same command, no code changes: all 20 fields correct, and both addendum changes applied, the
> due date moved to March 21st and the quantity from 12 to 16. Everything specific lives in YAML config."

(The live run takes 2–3 minutes; show the saved JSON instead, or run it before recording.)

---

## 7. Question answering in the UI (7:00–8:20)

**Screen:** Streamlit, *Ask* tab.

Ask, one after another:
1. `Compare the warranty requirements of both bids.`
2. `What is the new due date for Bid1 after the addendums?`
3. `What is the bid bond amount for Bid2?` *(expected: not found, so rehearse it once)*

**Say:**
> "Same idea as my Telegram bot, but as agents. The planner reads the question and the catalog of bids, splits
> a comparison into one search per bid, retrieval runs in parallel, the answer agent writes only from the
> evidence, and the validator checks every number, date and email in the answer against the cited text.
>
> Citations are these small numbers. Hover one and you see the exact sentence. Below, each source shows
> the file, page, section and the quoted sentence, so I can open the PDF, press Ctrl+F and find it.
> In the bot I only gave a page number; here you can verify each fact in seconds."

**Show the "Plan & validation" expander** on one answer (the plan's sub-queries and `passed: true`).

For question 3:
> "And when the documents don't say, it says so instead of making up an amount."

(Optional 15 s: `python main.py serve` → open `http://127.0.0.1:8000/docs` to show the REST API.)

---

## 8. Evaluation (8:20–8:55)

**Screen:** `eval\results.md`.

**Say:**
> "I didn't want to judge retrieval by feel, so I wrote 29 questions with the expected file and page,
> and measured Recall@1, 3, 5 and MRR for six configurations. Dense search alone gets 0.79 Recall@1 and
> struggles with identifiers; BM25 gets 0.83; hybrid with query expansion and reranking gets 0.86
> Recall@1 and 0.91 MRR, and every hybrid setup finds the right passage in the top 5. It's a small set,
> so I use it as a regression check; CI runs it with the tests."

---

## 9. Limitations and close (8:55–9:40)

**Screen:** README section 8, or back to your face.

**Say:**
> "Known limitations: long list fields can be incomplete. For example, extraction lists one of Bid1's two
> purchasing cooperatives, although Q&A finds both. Groq's free tier
> rate-limits, which the client handles with budgeting and model rotation, but it makes runs take a few minutes.
> The system is generic: fields, synonyms and document-type rules live in YAML config, not in code,
> and 52 offline unit tests cover parsing, search, agents and the API.
>
> To sum up: I took the hybrid RAG pipeline from my Telegram bot, rebuilt the search layer to handle
> multiple bids with filters and evaluation, and put agents on top that extract, reconcile addenda,
> and validate every value with a citation. Thanks for watching."

---

## Likely questions (prepare short answers)

- **Why LangGraph, not CrewAI/AutoGen?** Explicit graph with parallel fan-out, state reducers and a bounded retry
  loop; no hidden role-play prompts, so every prompt is mine, testable with a fake LLM, and traceable.
- **Why RRF?** BM25 and cosine scores are on different scales; RRF uses ranks only, needs no tuning.
- **Chunking?** Section-aware, about 180 words with 30 overlap; tables as row groups with their header;
  section heading stored on every chunk.
- **How do you stop hallucinations?** Evidence-only prompts, at least one citation per value, a code check that
  numbers/dates/emails/times appear in the cited text, an LLM critic, retries, then "Not found in documents".
- **What changes for a new bid?** Drop the folder in `data\`, run `index`, run extraction. No code changes.
