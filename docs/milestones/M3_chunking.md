# Milestone 3 — Chunking with Page and Section Provenance

**Issue:** [#3](https://github.com/hafizshah101-code/industrial-rag-copilot/issues/3) (stays open until review, approval, signed commit and merge)
**Branch:** `feature/issue-3-chunking` (HEAD `6d5befe` = B1 documentation alignment; the code below is untracked work on that branch)
**Status:** implemented and verified locally. OSJ Stage D source review of the rev3 bundle **PASSED** (`OSJ-M3-STAGE-D-SOURCE-PASS-REV3`); D-01 through D-05 are closed. **Awaiting Shah's separate authorization to stage the approved files.** Nothing has been staged, committed, pushed, merged or opened as a pull request, and Issue #3 remains open.
**Source version:** `CHUNKER_VERSION = "0.2.0"`
**Date:** 2026-11-15

## Objective

Turn the page-aware text produced by M2 into retrievable units that keep their
provenance: every chunk must state which document, section and pages it came
from, and must have an id that is reproducible from the bytes plus the
configuration. M3 delivers two comparable strategies — a size baseline and a
structure-aware chunker — so later milestones can measure which one retrieves
better instead of assuming it.

## Implementation scope

| File | Lines | Purpose |
|---|---|---|
| `app/chunking/errors.py` | 31 | controlled exception hierarchy for the layer boundary |
| `app/chunking/tokenizer.py` | 53 | `whitespace_split_v1` token estimate and token spans |
| `app/chunking/headings.py` | 744 | layout probe, heading heuristics, running furniture, M2 offset mapping |
| `app/chunking/chunker.py` | 1,406 | result contract, both strategies, provenance, statistics, comparison |
| `app/chunking/__init__.py` | 91 | public exports |
| `scripts/chunk_pdf.py` | 301 | CLI: strategy, sizes, metadata, `--summary`, `--output`, `--compare`, `--ask-password` |
| `tests/test_chunking.py` | 1,260 | 63 tests: contract, strategies, provenance, identity, buckets, CLI |
| `tests/test_headings.py` | 516 | 28 tests: body size, candidacy, furniture, mapping, reader |
| `docs/ingestion/chunking.md` | 225 | module reference: schema, policies, limitations |

No new runtime dependency (`requirements.txt` unchanged), no LLM call, no OCR
execution, no embeddings, no network access. The frozen M2 module
(`app/ingestion/pdf_inspector.py`) is untouched: M3 consumes its output and only
re-reads the PDF for line layout.

## The two strategies

| Aspect | `fixed_token` (baseline) | `section_aware` (default) |
|---|---|---|
| Unit | consecutive token windows | heading-delimited section |
| Size | `target_tokens`, hard cap `max_tokens` | whole section if it fits, otherwise split at `target_tokens` under `max_tokens` |
| Overlap | 64 tokens between consecutive windows | 32 tokens, only between split parts of the same section |
| Page boundaries | may span pages, exact span reported | may span pages inside one section |
| Sections | none: `section_path = ["untitled"]` | from verified heading offsets |
| Split category | fixed-window partition (`origin = "split"`) | `origin = "section"` or `"split"` |

Boundary preference for both: a sentence end between 80 % of the target and the
hard maximum, then a line end read from the M2 character ranges, then a hard cut
at the maximum (`ends_at = "hard_cut"` plus a chunk warning). A section that
fits stays whole; a trailing fragment under 40 tokens is merged backwards only
when the merge still fits `max_tokens`, so no chunk ever exceeds the hard cap.

The `fixed_token` baseline overlaps more because it has no heading to anchor a
boundary on; it is the control condition for the comparison, not a fallback that
pretends to understand structure.

## M2 provenance and the deterministic identity contract

M2 character offsets are the only coordinate system. Page texts are concatenated
in page order, each page's `[start, end)` recorded once, and every chunk boundary
is computed in that space.

| Invariant | Meaning |
|---|---|
| I1 | every chunk `text` is a contiguous substring of the M2 concatenation |
| I2 | boundaries and page spans come from M2 character offsets, never from layout geometry |
| I3 | each page is in exactly one bucket: `covered`, `needs_ocr`, `skipped(reason)` |
| I4 | same bytes + same configuration + same operator metadata ⇒ identical `chunk_id` list |
| I5 | anything that can change chunk content or identity changes `config_fingerprint`, hence every id |
| I6 | one document per result; a chunk never mixes documents |

```
config_fingerprint = "sha256:" + sha256(canonical_json({
  chunker_version, strategy, target_tokens, max_tokens, overlap_tokens,
  min_text_chars, token_estimate_method,
  heading_rules{size_delta_pt, max_chars, accept_bold, accept_upper,
                accept_numbering, header_band_pct, footer_band_pct,
                repeat_threshold_pct, mapping_ratio_threshold,
                per_heading_validation},
  metadata{equipment_model{value,source}, revision{value,source}}
}))
chunk_id = "sha256:" + sha256(f"{document_id}|{strategy}|{config_fingerprint}|{chunk_index}|{text}")
```

Canonical JSON is sorted keys, compact separators, UTF-8. `duration_ms` is
runtime telemetry and is excluded from `to_deterministic_json()`, which is what
makes two runs of the same input byte-identical there.

`ChunkingResult` publishes: `document_id`, `filename`, `strategy`,
`chunker_version`, `config_fingerprint`, `chunks`, `needs_ocr`, `skipped_pages`,
`warnings`, `stats`, `duration_ms`, plus the diagnostic blocks required by the
Stage D rulings: `layout_exclusions[]`, `heading_diagnostics[]`,
`page_mapping[]`, `ocr_diagnostics[]`. Every chunk carries `boundary`
(`starts_at`, `ends_at`, `overlap_token_estimate`, `origin`), `source_pages[]`
with per-page character shares, `metadata`, `section_path`, `heading`,
`section_inferred`, `page_index_start/end` and `page_start/end`.

## Stage D findings and their resolutions

| ID | Finding | Resolution | Regression tests |
|---|---|---|---|
| D-01 | eligible-text coverage was unproven | tests reconstruct each chunk's absolute interval and assert every **non-whitespace** character of every covered page is in at least one chunk, for both strategies, with punctuation, repeated identical lines, a page gap, heading content and a trailing line | `test_every_non_whitespace_character_of_a_covered_page_is_in_a_chunk`, `test_coverage_holds_for_heading_and_trailing_fragment_chunks` |
| D-02 | OCR candidates discarded useful extracted text | buckets are decided text first: text at or above `min_text_chars` is `covered` even when M2 flagged `ocr_candidate` or found images; the signal is published in `ocr_diagnostics[]` with `extracted_char_count` and a reason (`useful_text_kept`, `sparse_text_with_visuals`, `no_text_layer`) | `test_page_with_useful_text_and_ocr_signal_is_chunked`, `test_ocr_page_with_sparse_text_publishes_what_was_left_out`, `test_ocr_diagnostics_make_omission_explicit_not_silent` |
| D-03 | the line-end fallback was unreachable: `"\n" in token` can never be true for `\S+` tokens | `_line_breaks()` reads the gap after each token from the M2 character ranges; `_choose_end()` uses it for both strategies, so a line-ended cut is `ends_at = "line_end"` instead of a hard cut | `test_line_end_fallback_cuts_at_m2_line_breaks`, `test_line_end_fallback_keeps_deterministic_ids` |
| D-04 | the layout pass opened an encrypted PDF without the password that M2 used | `read_layout()` accepts and verifies the password, `chunk_document()` forwards it, the CLI prompts once and reuses the value; without a usable password it raises `PdfEncryptedError` instead of returning an empty layout; the value never reaches argv, JSON, logs, warnings or exception text | `test_encrypted_pdf_layout_pass_uses_the_same_password`, `test_encrypted_pdf_without_password_fails_explicitly`, `test_encrypted_pdf_with_wrong_password_fails_explicitly`, `test_cli_prompts_once_and_chunks_an_encrypted_pdf` |
| D-05 | `--min-text-chars` never reached the `--compare` path, so a comparison table could silently use 32 while the operator asked for another threshold | `compare_strategies()` takes `min_text_chars`, forwards it to both strategies and publishes it with `chunker_version`; the CLI passes the flag through | `test_compare_strategies_forwards_min_text_chars`, `test_cli_compare_honours_min_text_chars` |

D-02 and D-03 can change chunk content, so `CHUNKER_VERSION` was bumped from
`0.1.0` to `0.2.0`: ids produced by the earlier code must not be mistaken for
current ones by a later index.

## Stage C measurements (verified rev2 data)

Synthetic fixtures, CPU only, one process, `target_tokens=320`,
`max_tokens=400`, overlap 32 (section) / 64 (baseline). OSJ independently
recomputed the file digest and both reconciliation identities (6/6 records).

| Fixture | Strategy | Chunks | Token min/median/max | Pages covered/OCR/skip | Hard cuts | Sections | Splits |
|---|---|---:|---|---|---|---:|---:|
| radar_manual (7 p) | section_aware | 10 | 2 / 187 / 317 | 6 / 1 / 0 | 0 | 5 | 3 |
| radar_manual (7 p) | fixed_token | 7 | 164 / 291 / 316 | 6 / 1 / 0 | 0 | 0 | 0 |
| flat_report (2 p) | section_aware | 2 | 260 / 260 / 302 | 2 / 0 / 0 | 0 | 0 | 1 |
| flat_report (2 p) | fixed_token | 3 | 72 / 284 / 302 | 2 / 0 / 0 | 0 | 0 | 0 |
| scanned_like (3 p) | section_aware | 1 | 125 / 125 / 125 | 1 / 2 / 0 | 0 | 0 | 0 |
| scanned_like (3 p) | fixed_token | 1 | 125 / 125 / 125 | 1 / 2 / 0 | 0 | 0 | 0 |

Honest reading: the section-aware strategy produces more chunks on the structured
manual because it preserves section boundaries, and it produces one 2-token chunk
where a parent heading is immediately followed by a subheading. More chunks is
not evidence of better retrieval — that needs M4/M5 and the gold set. The
`scanned_like` fixture shows both strategies processing one page of three, with
two in `needs_ocr`; the token estimate is `whitespace_split_v1`, not a model
tokenizer.

## Verification (this snapshot)

```
$ ./.venv/Scripts/python.exe -m pytest -q
154 passed          # 91 of them M3 (63 chunking + 28 headings); 63 are M1/M2
$ ./.venv/Scripts/python.exe -m ruff check .
All checks passed!
$ ./.venv/Scripts/python.exe -m ruff format --check .
22 files already formatted
$ git diff --check
(no output)
$ git status --porcelain
?? app/chunking/
?? docs/ingestion/chunking.md
?? docs/milestones/M3_chunking.md
?? scripts/chunk_pdf.py
?? tests/test_chunking.py
?? tests/test_headings.py
```

These counts are reported by this machine's own run; OSJ verified the source and
the measurement file, not the execution.

## Reproduction

Run these from the repository root (Windows paths, one command per line):

```
cd C:\Projects\industrial-rag-copilot
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

The test suite needs **no external document**: every fixture is synthetic. The
unit tests build inspection and layout objects in memory, the end-to-end tests
generate small PDFs in pytest's `tmp_path`, and no vendor document is stored or
committed.

The CLI examples below need a PDF **that you supply** — the repository ships no
sample manual, and `data/` is git-ignored. Replace the placeholder path with a
local file:

```
.\.venv\Scripts\python.exe scripts\chunk_pdf.py "C:\path\to\your-manual.pdf" --summary
.\.venv\Scripts\python.exe scripts\chunk_pdf.py "C:\path\to\your-manual.pdf" --strategy fixed_token --summary
.\.venv\Scripts\python.exe scripts\chunk_pdf.py "C:\path\to\your-manual.pdf" --target-tokens 320 --max-tokens 400 --output chunks.json
.\.venv\Scripts\python.exe scripts\chunk_pdf.py "C:\path\to\your-manual.pdf" --compare
.\.venv\Scripts\python.exe scripts\chunk_pdf.py "C:\path\to\an-encrypted-manual.pdf" --ask-password
```

A password is read from a prompt (or from stdin when it is redirected) and is
never accepted on the command line. JSON goes to stdout unless `--output` names
a file.

## Known limitations

- **Heading-only chunks.** A parent heading immediately followed by a subheading
  becomes a very small chunk (2 estimated tokens in the measurement fixture). No
  text is dropped, but such a chunk is a poor retrieval candidate. A minimum
  section size or heading-merge policy is a later, configurable change.
- **OCR dependency.** Pages without a usable text layer produce no chunk and are
  listed in `needs_ocr[]`; the character count that was left out is published in
  `ocr_diagnostics[]`. M3 runs no OCR.
- **Synthetic fixture scope.** The measurements and heading heuristics are
  validated on generated documents. Real manuals bring ligatures, hyphenation,
  custom font encodings and outlined text, which is exactly why the layout
  mapping ratio is published per page and why a page below the ratio falls back
  to fixed windows.
- **Whitespace at boundaries.** Chunk text is a contiguous M2 substring, so the
  whitespace between two chunks belongs to neither. The coverage guarantee is
  therefore stated over non-whitespace characters.
- **Heading heuristics.** A false heading mislabels a section without deleting
  text; a missed heading merges two sections. Neither loses content.
- **Layout skills.** Multi-column pages raise `possible_multi_column_layout` and
  keep M2 reading order; running headers stay inside the chunk text because
  provenance must not break.
- **Token estimate.** `whitespace_split_v1` is a documented estimate; M4 must add
  the model tokenizer beside it rather than redefine `token_estimate`.
- **No quality claim.** M3 makes no accuracy or retrieval claim; that is M4/M5
  plus the gold evaluation set (M7).

## Portfolio learning takeaways

1. **Provenance is a design constraint, not a field.** Keeping every boundary in
   M2 character space made "is this chunk faithful?" a testable property
   (substring, page shares, coverage) instead of a review opinion.
2. **Deterministic identity is what makes re-indexing safe.** Hashing document
   id, strategy, configuration fingerprint, index and text means a changed
   setting cannot silently reuse stale vectors later.
3. **A fallback must be observable.** The layout-mapping ratio, the partial-page
   flag and the unmapped-heading diagnostics exist so a degraded page is visible
   rather than quietly chunked badly.
4. **Findings from review are cheaper than bugs in production.** Five Stage D
   findings (coverage proof, OCR bucket policy, dead line-end fallback, encrypted
   layout, threshold propagation) were all found by reading source and data, and
   each became a regression test rather than a paragraph of intent.
5. **Say what the metric means.** Distinguishing a section split point from a
   fixed-window partition, and a token estimate from a model token count, kept
   the milestone from overstating what it measured.

## Reviewed evidence (rev3 snapshot)

| Artefact | Size (bytes) | SHA256 |
|---|---:|---|
| `M3_SOURCE_BUNDLE_20261115_rev3.txt` (nine files + manifest header) | 170,827 | `9f407c3219e76c0725b9862fae8254b9e553e7c5e298cea9d62837a27bb4508b` |
| `M3_SOURCE_BUNDLE_MANIFEST_20261115_rev3.json` | 2,777 | `eeef470be53443cc71c25a5358ee4a6984f580f7d11430e2d40ed1b0b568baa3` |
| `M3_STAGE_D_fix_summary_rev3_20261115.txt` | 5,042 | `14e63d6086ede749a2847d0685d6f7638c2edc16c43a3759079d884d9002ef59` |
| `M3_STAGE_C_measurements_20261115_rev2.json` (baseline) | 6,091 | `10199cb3829b557025d17dfa7e56042185250f1d8f40118ee1076b0555e4f252` |
| `M3_STAGE_D_review_package_20261115.txt` (design + first evidence round) | 15,050 | `e54ada81d96e0e6749546c18b61ab287c1f44dfe39c8bd51e2f8b840180e4f78` |

Per-file digests of the reviewed source:

| File | Lines | Bytes | SHA256 |
|---|---:|---:|---|
| `app/chunking/errors.py` | 31 | 1,114 | `20a1e0b442d31430cd7e0f92c6f569503be632ecee0a23a1c80bdc4be4842419` |
| `app/chunking/tokenizer.py` | 53 | 1,661 | `94d64bddb2cf957d137e677a666bf43b8fc7ab7e1284fd9725756b2de28d9af6` |
| `app/chunking/headings.py` | 744 | 26,054 | `410d74a2a12111734be73b6a2e0f4154ba775a4267d90ead02b07e52e364bf9b` |
| `app/chunking/chunker.py` | 1,406 | 51,780 | `4b357e771969fb7eefe30a4794aa23fc6f23df13e504444cb6b4aa9cdc2b161f` |
| `app/chunking/__init__.py` | 91 | 2,218 | `4e14db1d28912a22218c9e620d10e443d1836c176024b3e35cfa10ea978a1f83` |
| `scripts/chunk_pdf.py` | 301 | 10,041 | `e8d4412902cd9f11d6133bb2f7ff12928fae6cba19285070e7a738e13ad11e7f` |
| `tests/test_chunking.py` | 1,260 | 45,193 | `63fda91b328624ad84898ff3a7efe7640453d40dd20061597e1a05597b2a79f5` |
| `tests/test_headings.py` | 516 | 17,657 | `0443b522eb389ea66dddd8c9635c34814ac3340bb500044d251c883d01d2216f` |
| `docs/ingestion/chunking.md` | 225 | 11,619 | `973bce64a7f3c0866626796968681e6ca78bf7c41919e4a6eb13bf8fa4013d5a` |

## Not done, and the next gate

Nothing has been staged, committed, pushed, merged or deployed; no GitHub issue
was edited; no new dependency was added; the frozen M2 module is unchanged. The
next decision belongs to Shah: a separate approval to `git add` and commit these
files on `feature/issue-3-chunking`.
