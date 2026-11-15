# Chunking with page and section provenance (M3)

`app.chunking` turns the page-aware text produced by
`app.ingestion.pdf_inspector` into retrievable chunks. It never re-extracts
text, never calls an LLM, never runs OCR, generates no embeddings and touches no
network service.

## Public API

```text
chunk_document(inspection, *, strategy, source_path=None, layout=None,
               target_tokens=320, max_tokens=400, overlap_tokens=None,
               min_text_chars=32, mapping_ratio_threshold=0.90,
               per_heading_validation=True, metadata=None,
               chunker_version="0.2.0") -> ChunkingResult

compare_strategies(inspection, *, source_path=None, layout=None,
                   target_tokens=320, max_tokens=400) -> dict
```

`ChunkingResult.as_dict()` / `to_dict()` are JSON-serialisable;
`to_json(indent=2)` serialises them. `deterministic_payload()` and
`to_deterministic_json()` drop `duration_ms`, which is runtime telemetry only.

Command line:

```text
python scripts/chunk_pdf.py data/manual.pdf --summary
python scripts/chunk_pdf.py data/manual.pdf --strategy fixed_token --summary
python scripts/chunk_pdf.py data/manual.pdf --target-tokens 320 --max-tokens 400
python scripts/chunk_pdf.py data/manual.pdf --output data/chunks.json
python scripts/chunk_pdf.py data/manual.pdf --compare
python scripts/chunk_pdf.py data/locked.pdf --ask-password
```

`--min-text-chars` applies to every path: the single-strategy run and `--compare`
alike, and the comparison output records the value it used (`min_text_chars`,
`chunker_version`) so a table can never be mistaken for one produced with
different bucket thresholds.

A password is never accepted as a command-line argument.

## Strategies

| Strategy | Unit | Size target | Overlap | Sections |
|---|---|---|---|---|
| `section_aware` (default) | heading-delimited section | `target_tokens`, hard cap `max_tokens` when split | 32 tokens, only between split parts of the same section | from verified heading offsets |
| `fixed_token` | consecutive windows | `target_tokens` | 64 tokens between consecutive windows | none: every chunk carries `section_path = ["untitled"]` |

Both strategies prefer a sentence end between 80 % of the target and the hard
maximum, then a line end, and only then cut at the maximum (recorded as
`boundary.ends_at = "hard_cut"` with a warning). A line end is detected from the
**M2 character ranges**: whitespace tokens never contain a newline, so the gap
between two consecutive tokens is inspected for `\n`/`\r`. A section that fits
`max_tokens` stays a single chunk; a trailing split fragment under 40 tokens is
merged back into the previous part when the merge still fits `max_tokens`.

The fixed-token baseline may span page boundaries; every chunk still reports the
exact page span and per-page character shares.

## Chunk schema

```text
chunk_id              "sha256:" + sha256(document_id|strategy|config_fingerprint|chunk_index|text)
document_id           "sha256:<pdf digest>" from M2
filename, strategy, chunker_version, config_fingerprint
chunk_index           0-based, contiguous within the result
text, char_count, token_estimate, token_estimate_method
page_index_start/end   0-based, mirroring M2
page_start/end         1-based
section_path          e.g. ["2", "2.2"]; ["untitled"] when there is no heading
heading, section_inferred
boundary              {starts_at, ends_at, overlap_token_estimate, origin}
metadata              {equipment_model: {value, source}, revision: {value, source}}
source_pages          [{page_index, page_number, char_count}]
warnings
```

`boundary.origin` is `section` for a chunk that is exactly one section span and
`split` for a chunk produced by splitting. For the `fixed_token` baseline every
chunk is `split`, and that is a **fixed-window partition**, not a section split:
the baseline has no section concept, so its windows are technical partitions of
one untitled span. `stats.section_splits` counts only real section split points
and therefore stays 0 for `fixed_token`. The two categories are mutually
exclusive and reconcile with
`stats.chunks_by_section + stats.chunks_by_split == stats.chunks`.

`config_fingerprint` is a SHA256 over the canonical JSON (sorted keys, compact
separators, UTF-8) of the chunker version, strategy, size settings, token
estimate method, heading rules, mapping threshold and operator metadata. Any
change that can alter chunk content or identity therefore invalidates every
`chunk_id`, which is what lets M4 keep an index free of stale entries.

Operator metadata is never parsed from document text. Absent values serialise as
`{"value": null, "source": "unknown"}`; supplied values as
`{"value": "<text>", "source": "operator"}`.

## Layout, headings and mapping

`pymupdf` is read a second time, for line layout only. The reader verifies the
file's streaming SHA256 against `inspection.sha256` before any line is used, so
layout can never describe a different file than the text it is mapped onto.

Heading candidacy (deterministic heuristics, documented constants):

1. Page body size is the character-weighted mode of line sizes; the heading
   threshold is body + 1.5 pt.
2. A candidate line is at most 80 characters, has no terminal period, sits
   outside the header/footer bands (8 % of page height at each edge) and shows
   an accepted signal: section numbering, a bold face, or ALL CAPS.
3. Heading level is the numbering depth when present, otherwise the size rank
   among that page's candidates.
4. Lines in the bands that repeat on at least 40 % of the document's pages are
   running furniture. They are never heading candidates and are published in
   `layout_exclusions[]` with their M2 offsets.
5. A heading without a verified M2 character offset cannot create a section
   boundary; it is published in `heading_diagnostics[]` with the reason
   (`unmapped_offset`, `layout_mapping_partial`).
6. A page whose layout line mapping ratio falls below `mapping_ratio_threshold`
   (0.90) is marked `layout_mapping_partial`: section splitting is disabled for
   that page and it is chunked with the fixed-window rule instead.

Mapping walks the page text with a forward cursor: an exact match at or after
the cursor, then a whitespace-insensitive retry, then `unmapped`. The cursor is
what keeps repeated identical lines mapped to distinct offsets instead of
collapsing onto the first occurrence.

## Page classes and the OCR/text policy

Buckets are decided **text first**, and a page lands in exactly one of
`covered`, `needs_ocr` or `skipped`:

| Condition | Bucket | Chunk text |
|---|---|---|
| `status="extraction_failed"` | `skipped_pages(reason="extraction_failed")` | none |
| `classification="empty"` | `skipped_pages(reason="empty")` | none |
| non-whitespace characters **>= `min_text_chars`** | `covered`, even when M2 set `ocr_candidate` or found visible images | yes |
| less than `min_text_chars`, with an OCR/visual signal | `needs_ocr[]` | none |
| sparse text, no images | `covered`, chunk warns `low_text_page` | yes |

Useful extracted text is therefore never discarded merely because a page also
carries a visual or OCR signal. The signal is published, not folded into the
bucket: `ocr_diagnostics[]` records one entry per page that has a visual/OCR
signal with `{page_index, page_number, bucket, ocr_candidate, visible_images,
extracted_char_count, reason}`, where `reason` is `useful_text_kept` for text
that was chunked, or `sparse_text_with_visuals` / `no_text_layer` for text that
was left out. A document with a page in the first covered case also warns
`text_pages_with_ocr_signal`.

Buckets reconcile: `pages_covered + pages_needs_ocr + pages_skipped ==
pages_inspected`. A page that is skipped or awaits OCR breaks the covered run, so
a chunk never carries its text; a section that spans such a gap is emitted as
separate parts (starts_at `page_gap`, overlap 0, warning
`section_split_by_page_gap`).

## Encrypted PDFs

A password-protected PDF must be opened twice: once by M2 for text and once for
layout. The CLI prompts once (`getpass`, or stdin when not a terminal), passes
the value to the M2 inspection **and** to the layout pass, and never accepts a
password as a command-line argument. If no usable password is available the
layout pass raises `PdfEncryptedError` with `a password is required` or `the
supplied password was rejected`; it never returns an empty layout that would look
like a document without headings. The password is never written to the result,
a warning, a log line or an exception message.

## Statistics

`stats` is derived, never hand-written:

```text
pages_inspected, pages_covered, pages_needs_ocr, pages_skipped
chunks, chunks_by_section, chunks_by_split
token_estimate_min, token_estimate_median (lower median), token_estimate_max
hard_cuts, section_splits, overlap_total_tokens
layout_mapping_partial_pages, unmapped_headings, layout_exclusions
text_pages_with_ocr_signal, ocr_diagnostics
```

`section_splits` is the number of **section split points**: boundaries created
inside a real section when it was too large for `max_tokens`. Fixed-token windows
are a fixed-window partition and never contribute to this counter, so a document
chunked by the baseline reports `section_splits = 0`. `ocr_diagnostics` is the
number of pages carrying a visual/OCR signal, matching `ocr_diagnostics[]`.

## Limitations

- **Heading-only chunks.** A parent heading immediately followed by a subheading
  produces a very small chunk (2 estimated tokens in the Stage C fixture). That
  is a known limitation, not a bug: no text is dropped, and the chunk is a poor
  retrieval candidate. A later milestone may add a configurable minimum section
  size or heading-merge policy; M3 keeps the literal section boundary.
- `token_estimate` is `whitespace_split_v1`, not a model tokenizer. M4 must add
  the model count beside it rather than redefine this field.
- Headings are heuristics. A false heading mislabels a section but never deletes
  text; a missed heading merges two sections and is visible as a longer
  `section_path` share, not as missing content.
- A page whose layout mapping is partial loses its section boundaries (by
  design): the fallback keeps text and provenance, not the label.
- A page dominated by heading-sized lines has no body-size baseline, so it
  produces no heading candidates. Synthetic feasibility is not an accuracy claim
  for real manuals: ligatures, hyphenation, custom encodings and outlined text
  are exactly why the ratio is published per page.
- Multi-column pages raise `possible_multi_column_layout` and keep M2 reading
  order; M3 corrects no layout.
- Running headers and footers stay inside the chunk text (provenance is never
  broken); only their heading candidacy is suppressed.
- Chunk text is a contiguous M2 substring, so whitespace **between** chunks at a
  boundary is not carried by either chunk. The coverage guarantee is therefore
  stated over non-whitespace characters: every non-whitespace character of a
  covered page appears in at least one chunk, which the regression suite asserts
  for both strategies.
- No retrieval quality or accuracy number exists for this milestone; that needs
  the M4/M5 stack and the gold evaluation set (M7).

## Tests

```text
python -m pytest -q                                  # full suite
python -m pytest tests/test_chunking.py -q           # strategies and contract
python -m pytest tests/test_headings.py -q           # layout, headings, mapping
```

Fixtures are synthetic and built at test runtime; no vendor document is needed,
stored or committed.
