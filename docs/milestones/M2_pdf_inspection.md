# Milestone 2 — PDF Inspection and Page Provenance

**Issue:** [#2](https://github.com/hafizshah101-code/industrial-rag-copilot/issues/2) (stays open until review, approval, signed commit and merge)
**Branch:** `feature/issue-2-pdf-inspection` (HEAD `bb2c7ba`, no upstream, worktree holds the work below as untracked files)
**Status:** implemented and verified locally; OSJ independent source review of rev2 **PASSED** (FIX-01 and FIX-02 accepted, marker OSJ-ISSUE2-REV2-CODE-REVIEW-PASS), with the documentation correction in this revision. **Awaiting Shah's authorization to stage the seven approved files.** Nothing has been committed, pushed, merged, deployed or opened as a pull request, and Issue #2 remains open.
**Date:** 2026-11-13

## Objective

Give the pipeline a reliable first step: take one locally supplied PDF, prove what it is (fingerprint), and record where every piece of text came from (page provenance), plus honest signals about pages that need more work before they can be indexed.

## OSJ Gate B refinements and how they were implemented

| # | Refinement | Implementation | Test |
|---|---|---|---|
| 1 | Use `get_image_info()` for visible images, document the difference from `get_images()` | `image_count` counts displayed images via `get_image_info()`; `image_resource_count` keeps the resource count from `get_images(full=True)`; page warns `image_resources_not_displayed` when resources exceed displayed images; classification uses the visible set | `test_visible_images_are_distinguished_from_image_resources` |
| 2 | Sparse text plus image is not necessarily `image_only`; use `uncertain`; keep OCR flag independent | Rule 4 added: text below threshold with an image -> `uncertain`; `ocr_candidate` unchanged and independent, so such a page is `uncertain` **and** an OCR candidate | `test_sparse_text_with_image_is_uncertain_but_still_an_ocr_candidate`, `test_classification_rules_are_deterministic` |
| 3 | Guard the expensive calls so a 16 GB laptop is not overloaded | Table analysis pre-gated on drawings/visible images, per-document budget `max_table_analysis_pages=25`, `detect_tables=False`, `detect_drawings=False`, `max_pages` limit, per-page exception isolation, `table_analysis` state published per page, document warnings when a limit is hit | `test_table_analysis_budget_is_enforced`, `test_drawing_analysis_can_be_disabled`, `test_max_pages_limits_inspection_and_reports_skip`, `test_plain_text_page_skips_table_analysis_with_a_reason` |
| 4 | Encrypted PDFs: explicit refusal by default, never log or store the password | Refusal with two distinct messages ("a password is required" / "the supplied password was rejected"); the password is used only for `authenticate()`, never stored in the result, warnings or exception text | `test_encrypted_pdf_is_refused_without_password`, `test_encrypted_pdf_with_wrong_password_is_refused`, `test_password_is_never_stored_or_reported` |

## OSJ Stage D conditions (APPROVE WITH CONDITIONS) and their evidence

| Condition | Evidence |
|---|---|
| A. Provide reviewable source, not a report | Full source of `pdf_inspector.py`, `test_pdf_inspector.py`, `__init__.py`, the CLI and both docs is delivered as an attachment bundle, with a per-file SHA256 manifest in the covering message. |
| B. Validate `max_pages` and `max_table_analysis_pages`, including invalid values | `_validate_limits` runs before any file I/O: non-integers (and `bool`) raise `TypeError`, values below 1 raise `ValueError`, `None` only where "no limit" is meaningful. Tests: `test_max_pages_rejects_non_positive_values`, `test_table_budget_rejects_non_positive_values`, `test_min_text_chars_rejects_non_positive_values`, `test_limits_reject_non_integer_values`. |
| C. Skipped pages and skipped table analysis must be explicit, never reported as fully analysed | `pages_inspected` / `pages_skipped` + `page_limit_reached`; per-page `table_analysis` state (`analysed`, `disabled`, `skipped_no_graphics`, `skipped_budget`) + `table_analysis_budget_exhausted`. Tests: `test_skipped_pages_are_never_reported_as_inspected`, `test_skipped_table_analysis_is_distinguishable_from_a_negative_result`. |

## OSJ FIX-01 / FIX-02 corrections (source review round 2)

| Fix | Change | Regression tests |
|---|---|---|
| FIX-01 table analysis failure state | `TABLE_ANALYSIS_FAILED = "analysis_failed"` replaces the previous `analysed` on exception; the page warns `table_analysis_failed`; the exception text is not copied into the output; a failed analysis does not consume the per-document budget | `test_table_analysis_failure_is_not_reported_as_analysed`, `test_failed_table_analysis_does_not_consume_the_budget`, `test_successful_table_analysis_does_consume_the_budget` |
| FIX-02 password handling in the CLI | `--password` is removed and explicitly refused (value never echoed); `--ask-password` prompts with `getpass` (stdin when redirected); the value never appears in argv, JSON, warnings, exception messages or docs examples; `inspect_pdf(path, password=...)` is unchanged | `test_cli_rejects_password_as_a_command_line_argument`, `test_cli_rejects_equals_form_password_argument`, `test_cli_reads_the_password_from_a_prompt`, `test_cli_refuses_an_encrypted_pdf_without_prompt`, `test_cli_refuses_an_empty_password`, `test_cli_prints_json_for_text_pdf`, `test_cli_summary_shows_table_analysis_state`, `test_cli_missing_file_exits_2` |

FIX-03 (staging verification) is deliberately not performed: nothing is staged or committed while Shah's approval is outstanding.

## Requirements to evidence

| Requirement | Status | Evidence |
|---|---|---|
| FR-01 validation, size, SHA256, page count, safe handling of empty/invalid/encrypted/corrupt files | Done | `inspect_pdf`, exception tests (`test_zero_byte_pdf_raises_empty_file_error`, `test_invalid_pdf_raises_corrupted_error`, `test_non_pdf_extension_is_rejected`, `test_pdf_without_header_is_rejected`, the three encryption tests) |
| FR-02 page-level extraction with provenance | Done | `test_text_only_page_extracts_text`, `test_page_provenance_matches_source_pages`, `test_text_line_breaks_are_preserved` |
| FR-03 deterministic classification plus `ocr_candidate` | Done | `test_classification_rules_are_deterministic`, `test_image_only_page_is_flagged_for_ocr`, `test_mixed_page_reports_text_and_image_indicators`, `test_empty_page_is_classified_empty` |
| FR-04 image, drawing and table indicators | Done | `test_image_metadata_is_recorded`, `test_table_candidate_is_detected_on_drawn_grid`, `test_diagram_candidate_only_for_drawing_heavy_pages` |
| FR-05 JSON-serialisable structured output | Done | `test_output_is_json_serialisable_with_documented_schema`, `test_to_json_matches_to_dict`, `test_no_invented_document_metadata` |
| FR-06 incremental processing, streaming checksum, measured performance | Done | 1 MiB streaming hash chunks; per-page loop with per-page error isolation; `test_extraction_duration_and_peak_memory_are_measured`; measured table below |

## Files delivered

| File | Lines | Role |
|---|---|---|
| `app/ingestion/pdf_inspector.py` | 633 | Module: validation, streaming SHA256, page extraction, classification, indicators, resource guards, structured result. |
| `app/ingestion/__init__.py` | 27 | Package exports. |
| `tests/test_pdf_inspector.py` | 874 | 55 test functions (60 cases with parametrisation) using synthetic fixtures generated at runtime, including CLI subprocess tests. |
| `scripts/inspect_pdf.py` | 163 | CLI: JSON to stdout or file, `--summary`, `--ask-password`, `--no-table-detection`. |
| `requirements.txt` | 1 | Pins the runtime dependency that was installed but undeclared: `pymupdf==2.1`. |
| `docs/ingestion/pdf_inspection.md` | 221 | Module reference: API, schema, heuristics, warnings, performance, limitations. |
| `docs/milestones/M2_pdf_inspection.md` | this file | Milestone report. |

No existing module, test, Git history or dependency pin was modified.

## Verification

Commands run in `C:\Projects\industrial-rag-copilot` with the project venv (Python 3.12.10):

```
python -m pytest -v              -> 63 passed (3 environment + 60 pdf inspector cases)
python -m ruff check .           -> All checks passed
python -m ruff format --check .  -> 13 files already formatted
git diff --check                 -> clean (no whitespace errors)
git status --porcelain           -> only the new files below are untracked; no tracked file modified
```

CLI proof against the fixture Shah generated in the earlier hands-on session:

```
python scripts/inspect_pdf.py data/synthetic_radar_manual.pdf --summary
file=synthetic_radar_manual.pdf pages=3 size=2215B sha256=2359d803c9e33be52ed3ff267861d0c25f98bcac1c2a2788ff9a0457f8901f06
  page 1 (index 0): text  chars=82 images=0 drawings=0 tables=0 ocr_candidate=False
  page 2 (index 1): text  chars=78 images=0 drawings=0 tables=0 ocr_candidate=False
  page 3 (index 2): text  chars=92 images=0 drawings=0 tables=0 ocr_candidate=False
  duration_ms: 28
```

## Measured performance (CPU only, this laptop)

40-page synthetic manual, 33,965 bytes, four pages carrying a drawn table:

| Configuration | Wall time | Peak traced memory | Tables |
|---|---|---|---|
| table analysis on (default) | 5,244 ms / 6,463 ms (two runs) | 22.24–22.25 MiB | 4 |
| table analysis off | 99 ms / 119 ms (two runs) | 1.04 MiB | 0 (skipped) |

Individual PyMuPDF calls: `get_text` ~2.1 ms, `get_images` ~0.05 ms, `get_drawings` ~0.12 ms (simple page) / ~230 ms (4,000-drawing page), `find_tables` ~651 ms (simple page) / ~3,013 ms (4,000-drawing page). Table analysis therefore runs only on pages with drawings or visible images and is capped at 25 analysed pages per document by default.

## Documented deviations and improvements (for OSJ ruling)

1. **`non_whitespace_char_count` added to the page schema.** The brief's schema has `char_count` only, but the classification threshold compares non-whitespace characters, so the field is published to keep each decision auditable.
2. **Vector-only page classifies as `uncertain`, not `empty`.** The brief defines `empty` as "no extractable text or detected images", which would label a pure vector drawing page empty even though it carries content. The implemented rule reports `uncertain` when drawings exist and no text or images do.
3. **`detect_tables`/`detect_drawings`/`max_pages`/`max_table_analysis_pages` parameters added** with the perf evidence above, plus per-page `table_analysis` state, so a caller can see when analysis was skipped and why rather than reading a silent `false`.
4. **Document-level aggregates added:** `pages_inspected`, `pages_skipped`, `classification_counts`, `ocr_candidate_pages`, `file_size_bytes`, `extraction_duration_ms`, `inspector_version`. Additive only; no proposed field was renamed or removed.
5. **Exception taxonomy named** (`PdfNotFoundError`, `UnsupportedFileTypeError`, `PdfEmptyFileError`, `PdfCorruptedError`, `PdfEncryptedError`) so the ingestion layer can branch on a cause.
6. **`ruff format` also formats the Python block inside `docs/ingestion/pdf_inspection.md`**, so that file is part of the format gate; this is why a doc edit can make `ruff format --check` fail.

## Known limitations

- Classification is heuristic: a scanned page carrying an invisible OCR text layer reads as `text`.
- No OCR is run; `ocr_candidate` pages produce no text.
- Tables without drawn lines, and tables embedded in images, are missed.
- `diagram_candidate` cannot separate an engineering schematic from decorative vector art.
- Multi-column text may interleave because default reading order is used.
- Single-document, sequential API; no batch ingestion runner yet.
- No evaluation set exists yet for classification accuracy, so no accuracy claim is made.

## Security and privacy checklist

- No network access of any kind; no cloud OCR, no external LLM.
- No PDF, extracted text or metadata leaves the machine; nothing is written outside the caller's chosen output path.
- Passwords are never stored in the result, logged, or reported in exception messages (`test_password_is_never_stored_or_reported`).
- Only synthetic fixtures are used by the tests; fixtures are created in pytest's temporary directory and deleted with it.
- `test_repository_tracks_no_restricted_documents` asserts that no `.pdf/.docx/.xlsx/.csv/.sqlite/.db/.parquet` file is tracked in Git.
- `data/` stays git-ignored; the only PDF in the tree is Shah's git-ignored synthetic fixture.
- No credentials, keys or secrets are read or logged.
- No file outside the authorised scope was touched; no global dependency or OS setting changed.

## Next actions

1. Done: OSJ reviewed the source bundle (rev2) and accepted FIX-01 and FIX-02; this revision carries the documentation correction it asked for.
2. Next: Shah authorizes staging of the seven approved files; then `git diff --cached --check`, `git diff --cached --stat` and the staged file list are shown before any commit.
3. After commit approval: SSH-signed commit, verify the GitHub `Verified` badge, open the pull request for Issue #2, review, merge, then close the issue.
4. M3 candidates: section-aware chunking with page spans, then CPU embeddings and Qdrant indexing keyed by `document_id`.
