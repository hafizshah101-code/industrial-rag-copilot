# Learning Roadmap

Nine milestones, each with a learning objective and a demonstrable output. A milestone is only complete when its output can be reproduced locally, is covered by tests and is recorded in a milestone report.

| Milestone | Learning objective | Demonstrable output | State |
|---|---|---|---|
| **M1 — CPU-only development environment** | Reproducible local setup, dependency isolation, quality gates and signed commits | Python 3.12 venv, pinned dev tools, pytest/Ruff gates, SSH-signed commits | Completed (issue #1) |
| **M2 — PDF inspection and page provenance** | What a PDF actually contains, and why provenance matters before any retrieval | `inspect_pdf()` module + CLI: streaming SHA256, page-level text, page index/number, heuristic classification, OCR candidates, indicators | Completed (issue #2) |
| **M3 — Chunking strategies** | How chunk size, boundaries and headings change what can be retrieved later | Fixed-token-estimate baseline and section-aware chunker with deterministic chunk ids and page spans, plus a comparison report | Delivered (issue #3, PR #14) |
| **M4 — CPU embeddings and semantic retrieval** | How text becomes vectors, and how similarity search behaves on a CPU budget | Compact local embedding model, Qdrant index, idempotent re-indexing, top-k results carrying page citations | Planned (issue #4) |
| **M5 — BM25, filters and hybrid retrieval** | When lexical matching beats semantic matching, and how metadata filters prevent revision confusion | Benchmarks of lexical, semantic and hybrid retrieval on the same frozen queries, with Recall@5 and MRR | Planned (issue #5) |
| **M6 — Grounded answers, citations and abstention** | How to synthesize an answer that stays inside the retrieved evidence | Optional local or explicitly approved LLM answering over retrieved evidence, with per-claim source pages and an explicit abstain path | Planned (issue #6) |
| **M7 — Gold evaluation dataset** | How to measure a RAG system honestly, including the cases where it must fail | 30–50 human-verified questions with expected pages, revision traps and unanswerable items, plus regression tests | Planned (issue #7) |
| **M8 — Evidence-linked component graph** | How structured relationships complement flat retrieval | Minimal knowledge graph linking subsystems, components and interface dependencies, with every edge pointing at source evidence | Planned (issue #8) |
| **M9 — Portfolio delivery** | How to ship something reviewable and safe | FastAPI demo, minimal search UI, CI, privacy and secret checks, architecture decisions | Planned (issue #9) |

## How each milestone is worked

1. Read the issue's acceptance criteria and restate the objective in your own words.
2. Run a reproducible experiment and keep the commands and outputs.
3. Write down limitations and open questions **before** claiming a result.
4. Get the change reviewed, then commit it as a signed commit on a feature branch.

## Working rules

- CPU-only and offline by default; no document content leaves the machine without explicit authorization.
- Synthetic fixtures only in the public repository; vendor manuals stay in the git-ignored `data/` directory.
- Report measurements, not targets. An unmeasured number is not a result.
- Keep provenance: every chunk, hit and generated sentence must be traceable to a source page.

## Issue numbering

Milestone labels in GitHub issue titles do not necessarily match the table above yet; retitling the affected issues is a separate approval step. Issue numbers are stable and are never renumbered.
