# Architecture v0.2 — CPU-first, local-first

## Goal

An educational, single-user industrial-document RAG prototype for a Windows laptop with 16 GB RAM and no discrete GPU. Everything runs locally and offline; no document content is sent to an external service.

## Implementation state

| Stage | State | Notes |
|---|---|---|
| PDF inspection, checksum, page text, provenance | **Implemented (M2)** | `app/ingestion/pdf_inspector.py` |
| Chunking with page and section provenance | **In progress (M3)** | design reviewed; implementation not merged |
| CPU embeddings, vector index, metadata store | Planned (M4) | Qdrant and SQLite not installed |
| BM25, filters, hybrid ranking | Planned (M5) | — |
| Grounded answers, citations, abstention | Planned (M6) | no LLM is called today |
| Gold evaluation set | Planned (M7) | no accuracy claim exists yet |
| Component graph | Planned (M8) | — |
| FastAPI demo, UI, CI | Planned (M9) | CI is not configured |

## Ingestion pipeline

1. **Inspect a locally authorized PDF (implemented in M2).** Validate the file, compute a streaming SHA256 over the original bytes, and read page count. The checksum becomes `document_id` (`sha256:<hex>`). Page provenance — zero-based `page_index`, one-based `page_number`, character counts and warnings — is produced in the same step.
   **Implemented in M2:** document checksum and page provenance only. **Not implemented in M2:** equipment model and revision metadata. They are *proposed for M3* as operator-supplied input fields, recorded together with their source and defaulting to `null`; M2 neither extracts nor guesses them from document text.
2. **Extract page-aware text (implemented).** PyMuPDF returns text per page together with a zero-based `page_index`, a one-based `page_number`, character counts, a heuristic page classification, an independent OCR-candidate flag, visible-image/vector/table indicators and per-page warnings. Comparison with a layout engine such as Docling remains a later experiment.
3. **Chunk with provenance (M3, in progress).** Fixed-token-estimate baseline plus section-aware chunking; every chunk carries the document id, section path, heading, page span and deterministic id.
4. **Generate CPU embeddings and index them (planned, M4).** Compact local embedding model; Qdrant for vectors; SQLite for document and chunk metadata; re-indexing must be idempotent and must not mix document revisions.
5. **Preserve provenance end to end (partially implemented).** Page numbers already travel with the extracted text; later stages must carry them into chunks, retrieval hits and answers.

## Query pipeline

1. Receive a question with optional model/revision filters through a local interface (planned, M9).
2. Retrieve candidates from the vector index (planned, M4).
3. Compare keyword BM25, metadata filters, hybrid fusion and reranking on the same frozen query set (planned, M5).
4. Link every retrieved passage to source page, section, model and revision (planned).
5. Optionally synthesize a grounded answer with an explicitly approved LLM; otherwise return the retrieved passages (planned, M6).
6. Abstain when the evidence is missing or conflicting (planned, M6).

None of steps 1–6 of the query pipeline exists in code today.

## Operational safeguards

- Vendor PDFs and derived restricted material stay on the laptop and are never committed; `data/` is git-ignored.
- External LLMs, embedding APIs, OCR services and telemetry are disabled by default.
- Treat document content as untrusted input; extracted maintenance instructions are not operational approval.
- A supplied PDF password is used only for authentication and is never stored, logged or accepted as a command-line argument.
- Analysis calls that are expensive on a 16 GB laptop (table analysis, drawing enumeration) are bounded by explicit budgets rather than left unbounded.

## Evaluation

Create a manually verified 30–50 question set (M7), including negative and revision-confusion cases. Track Recall@5, MRR, citation correctness, RAM, ingestion speed and p50/p95 query latency. Distinguish targets from measured results; do not claim unmeasured accuracy.

## Future experiments

Evidence-linked component graph (Neo4j optional), OCR, table extraction, multi-column layout correction and multimodal figure retrieval — only after baseline retrieval quality is measured.
