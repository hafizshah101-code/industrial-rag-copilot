# Learning Roadmap

**M1 — Foundation:** CPU-only setup, input validation, PyMuPDF extraction, checksum and page provenance.

**M2 — Semantic RAG:** Tokenization, section-aware vs fixed-size chunking, CPU embeddings, vector search and page citations.

**M3 — Hybrid Retrieval:** BM25, document/revision filters, fusion and reranking comparison using the same evaluation questions.

**M4 — Grounded Generation:** Optional LLM, prompt-injection defenses, verifiable citations and explicit abstention.

**M5 — Evaluation:** 30–50 human-reviewed questions, including unanswerable examples. Report Recall@5, MRR, citation validity and performance measurements.

**M6 — Graph Experiment:** Small component knowledge graph with evidence-backed edges, compared against baseline retrieval.

**M7 — Portfolio Delivery:** FastAPI, minimal UI, automated tests, CI, threat model, architecture decisions and reproducible setup.

For each milestone: explain the concept in your own words, run a reproducible experiment, document limitations and open a reviewed pull request.
