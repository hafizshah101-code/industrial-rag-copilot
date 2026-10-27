# Architecture v0.1 — CPU-first, local-first

## Goal
An educational, single-user industrial-document RAG prototype for a Windows laptop with 16 GB RAM and no discrete GPU.

## Ingestion pipeline
1. Inspect locally authorized PDFs and record checksum, equipment model and revision.
2. Extract page-aware text with PyMuPDF; compare Docling for complex layouts later.
3. Preserve chapter, section, page spans and revision in every chunk.
4. Compare fixed-size and section-aware chunking.
5. Generate compact CPU embeddings and index in local Qdrant; track metadata using SQLite.
6. Make re-ingestion idempotent; avoid mixing revisions.

## Query pipeline
1. Receive question with optional model/revision filters through FastAPI (planned).
2. Retrieve semantic top-k matches from Qdrant.
3. Later compare keyword BM25, hybrid fusion and reranking.
4. Link evidence to source page, section, model and revision.
5. Optionally synthesize grounded answers with an explicitly approved LLM; otherwise return retrieved passages.
6. Abstain when evidence is missing.

## Operational safeguards
Vendor PDFs and derived restricted material remain on the laptop and are never committed. External LLMs and telemetry are disabled by default. Treat document content as untrusted; generated maintenance steps are not operational approval.

## Evaluation
Create a manually verified 30–50 question set, including negative and revision-confusion cases. Track Recall@5, MRR, citation correctness, RAM, ingestion speed and p50/p95 query latency. Distinguish targets from measured results.

## Future experiments
Evidence-linked component graph (Neo4j optional), OCR, table extraction and multimodal figure retrieval only after baseline quality is measured.
