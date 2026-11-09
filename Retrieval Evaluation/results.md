# Retrieval evaluation

- Questions: 29 (Bid1: 15, Bid2: 14), bid filter: True
- Embedding model: `BAAI/bge-base-en-v1.5`; reranker: `BAAI/bge-reranker-base`; chunks indexed: 309
- Relevance: a retrieved chunk is relevant if it comes from an expected file and its page range overlaps an expected page.
- Recall@k = share of questions with at least one relevant passage in the top k.

| Configuration | Recall@1 | Recall@3 | Recall@5 | Recall@10 | MRR@10 | nDCG@10 | avg_latency_ms |
|---|---|---|---|---|---|---|---|
| bm25 | 0.828 | 0.966 | 1.0 | 1.0 | 0.899 | 0.877 | 0.7 |
| dense | 0.793 | 0.966 | 0.966 | 0.966 | 0.874 | 0.855 | 30.1 |
| hybrid (RRF) | 0.793 | 0.966 | 1.0 | 1.0 | 0.882 | 0.87 | 26.7 |
| hybrid + expansion | 0.828 | 1.0 | 1.0 | 1.0 | 0.902 | 0.878 | 50.4 |
| hybrid + rerank | 0.862 | 0.931 | 1.0 | 1.0 | 0.914 | 0.901 | 7576.1 |
| hybrid + expansion + rerank | 0.862 | 0.931 | 1.0 | 1.0 | 0.914 | 0.902 | 8183.4 |

## Per-question rank of first relevant passage

| id | question | bm25 | dense | hybrid (RRF) | hybrid + expansion | hybrid + rerank | hybrid + expansion + rerank |
|---|---|---|---|---|---|---|---|
| b1-01 | What is the solicitation number of the student and staff computing dev | 1 | 2 | 2 | 2 | 1 | 1 |
| b1-02 | When was the solicitation originally due? | 4 | 1 | 1 | 1 | 4 | 4 |
| b1-03 | What is the new due date after the addendum extension? | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-04 | When and how will the pre-proposal meeting be held? | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-05 | What is the length of the contract and the renewal options? | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-06 | How can vendors submit their proposals, electronically or by mail? | 2 | 2 | 1 | 1 | 2 | 2 |
| b1-07 | What deployment services such as asset tagging and etching are require | 1 | 2 | 2 | 1 | 1 | 1 |
| b1-08 | What are the minimum requirements for the Tier 3 student Windows lapto | 3 | 1 | 2 | 2 | 1 | 1 |
| b1-09 | Must Form 1295 Certificate of Interested Parties be submitted with the | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-10 | Who is the buyer for this RFP and what is the buyer's email? | 1 | - | 1 | 3 | 1 | 1 |
| b1-11 | Which purchasing cooperative interlocal agreement can other entities u | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-12 | What minimum warranty is required for student Chromebooks and Windows  | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-13 | When must the certificate of insurance be provided? | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-14 | Is Windows Autopilot or Google management enrollment required for imag | 1 | 1 | 1 | 1 | 1 | 1 |
| b1-15 | Do resellers need factory-authorized repair certifications? | 1 | 1 | 3 | 3 | 1 | 1 |
| b2-01 | What is the PORFP number? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-02 | What is the proposal due date for the Dell laptops? | 2 | 3 | 4 | 2 | 4 | 4 |
| b2-03 | How must bids be submitted, and is email accepted? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-04 | How many days after award must delivery happen? | 1 | 2 | 2 | 1 | 1 | 1 |
| b2-05 | Is a Mercury Affidavit required? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-06 | Where and when should invoices be submitted? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-07 | What is the phone number of the agency point of contact? | 2 | 1 | 1 | 1 | 2 | 2 |
| b2-08 | How long must the extended hardware warranty last? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-09 | What processor and memory does the Latitude 5550 configuration have? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-10 | 210-BLYZ | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-11 | Which master contract must vendors hold to be eligible? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-12 | Must the contractor be an authorized Dell reseller? | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-13 | registration with the State Department of Assessments and Taxation aff | 1 | 1 | 1 | 1 | 1 | 1 |
| b2-14 | What quantity of Thunderbolt docks is requested? | 1 | 1 | 1 | 1 | 1 | 1 |
