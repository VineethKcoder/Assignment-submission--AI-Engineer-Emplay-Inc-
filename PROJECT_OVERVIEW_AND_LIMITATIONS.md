# RFP Intelligence Platform — Project Overview, Assumptions and Limitations

## Project description

The RFP Intelligence Platform helps users search and understand bid documents. It reads PDFs, saved HTML pages, and addenda, then combines keyword search with semantic search to find relevant information. A LangGraph workflow coordinates agents that extract bid details, apply addendum changes, validate evidence, and answer questions. Results include source citations and confidence scores. The system supports a command-line interface, REST API, and Streamlit web interface.

## Assumptions

- Each folder represents one bid and contains its related documents.
- The folder name is used as the bid identifier.
- Addendum numbers can be identified from filenames or document text. Later addenda take priority when updating the same requirement.
- Answers are based on the supplied documents. Saved portal pages represent the information available when they were downloaded.
- Information that cannot be supported by document evidence should be reported as “Not found in documents.”

## Known limitations

- Scanned pages and complicated tables may produce OCR or parsing errors.
- AI-generated answers can miss details or misinterpret evidence. Citations and validation reduce this risk but do not eliminate it.
- Confidence scores are estimates, not guarantees of correctness.
- Groq requires internet access and a valid API key. Rate limits can delay extraction and Q&A.
- CPU execution is supported but slower than GPU execution, especially during reranking.
- The retrieval evaluation uses 29 questions. It measures retrieval quality on that question set, not complete answer accuracy across all possible bids.

## Use of AI coding assistants

AI coding assistance was used to support development, troubleshooting, and documentation. Responsibility for the submitted solution remains with the author.

## Review preparation

For the review, be prepared to explain document parsing, chunking, embeddings, hybrid retrieval, reranking, agent communication, addendum handling, validation, citations, evaluation metrics, and the API/CLI flow. Only state that you understand every part after reviewing and being able to explain it yourself.
