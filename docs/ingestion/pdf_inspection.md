# PDF Inspection and Page Provenance

Module: `app/ingestion/pdf_inspector.py` (version 0.2.0)
Milestone: M2 / GitHub Issue #2
Status: implemented and tested locally on a CPU-only laptop; not yet reviewed or merged.

## Scope

This module turns one local PDF into structured, page-aware metadata that a later ingestion stage can chunk and index:

- streaming SHA256 fingerprint of the original file bytes,
- page count and per-page text extraction with PyMuPDF,
- zero-based page index and one-based page number stored next to every page,
- documented heuristic page classification (`text`, `image_only`, `mixed`, `empty`, `uncertain`),
- an independent `ocr_candidate` flag,
- visible-image, vector-drawing and table-candidate indicators,
- a JSON-serialisable result with explicit warnings.

Out of scope by design: chunking, embeddings, vector stores, LLM calls, OCR execution, retrieval. The module never contacts the network and never writes the document anywhere except the path the caller passes for JSON output.

## Public API

```python
from app.ingestion.pdf_inspector import inspect_pdf

result = inspect_pdf("data/synthetic_radar_manual.pdf")
payload = result.to_dict()  # dict, JSON-serialisable
print(result.to_json(indent=2))  # JSON string
```

`inspect_pdf(path, *, password=None, min_text_chars=32, detect_tables=True, detect_drawings=True, max_pages=None, max_table_analysis_pages=25)`

| Parameter | Meaning |
|---|---|
| `path` | Local PDF file. Existence, extension, non-zero size and the `%PDF-` header are validated before parsing. |
| `password` | Password for an encrypted document. A document that needs a password and does not get a working one is refused, never parsed partially. The password is used for authentication only and is never stored, logged, or included in warnings, metadata or exception messages. |
| `min_text_chars` | Non-whitespace character threshold used by the classification heuristics (default 32). |
| `detect_tables` | Run PyMuPDF line-based table analysis on pages that carry drawings or visible images. Default on; see the performance section. |
| `detect_drawings` | Read vector drawing counts. Disable for very complex CAD-style exports where `get_drawings()` is expensive. |
| `max_pages` | Inspect at most this many pages. The remainder are counted in `pages_skipped` and a `page_limit_reached` warning is added. |
| `max_table_analysis_pages` | Per-document budget for table analysis (default 25 pages). Spent pages report `table_analysis="skipped_budget"` and a `table_analysis_budget_exhausted` warning is added. `None` removes the budget. |

### Argument validation

Limits are validated before any file is opened, so a bad call cannot produce a
silently truncated run:

- `min_text_chars`, `max_pages`, `max_table_analysis_pages` must be integers
  (`bool` is rejected) — otherwise `TypeError`.
- A limit below 1 (`0`, negative) raises `ValueError`; `None` is only accepted
  for `max_pages` and `max_table_analysis_pages`, where it means "no limit".

A run that was cut short is never presented as a full inspection: `pages_skipped`
counts the pages that were not inspected, `page_limit_reached` is added to the
document warnings, and the skipped pages have no entries in `pages`.

### Exceptions

All failures are subclasses of `PdfInspectionError`, so a caller never sees a raw PyMuPDF exception:

| Exception | Raised when |
|---|---|
| `PdfNotFoundError` | Path missing, or not a regular file (a directory is rejected). |
| `UnsupportedFileTypeError` | Extension is not `.pdf`, or the file does not start with `%PDF-`. |
| `PdfEmptyFileError` | The file exists but has zero bytes. |
| `PdfCorruptedError` | The file has a PDF header but PyMuPDF cannot parse it. |
| `PdfEncryptedError` | The document needs a password and none (or a wrong one) was supplied. Two distinct messages: "a password is required" and "the supplied password was rejected". |

Invalid argument values are raised as `TypeError`/`ValueError` before any I/O, as described above.

A single unreadable page does not abort the run: that page is reported with `status="extraction_failed"`, classification `uncertain`, and a `page_extraction_failed:<ErrorType>` warning, and the remaining pages are still extracted.

## Output schema

```json
{
  "document_id": "sha256:2359d803c9e33be52ed3ff267861d0c25f98bcac1c2a2788ff9a0457f8901f06",
  "filename": "synthetic_radar_manual.pdf",
  "sha256": "2359d803c9e33be52ed3ff267861d0c25f98bcac1c2a2788ff9a0457f8901f06",
  "file_size_bytes": 2215,
  "page_count": 3,
  "pages_inspected": 3,
  "pages_skipped": 0,
  "pages": [
    {
      "page_index": 0,
      "page_number": 1,
      "text": "Radar Overview\nThe radar processor receives heading data from the heading sensor.\n",
      "char_count": 82,
      "non_whitespace_char_count": 70,
      "classification": "text",
      "ocr_candidate": false,
      "image_count": 0,
      "image_resource_count": 0,
      "images": [],
      "vector_drawing_count": 0,
      "table_count": 0,
      "table_candidate": false,
      "table_analysis": "skipped_no_graphics",
      "diagram_candidate": false,
      "status": "ok",
      "warnings": []
    }
  ],
  "classification_counts": {
    "text": 3,
    "image_only": 0,
    "mixed": 0,
    "empty": 0,
    "uncertain": 0
  },
  "ocr_candidate_pages": [],
  "warnings": [],
  "extraction_duration_ms": 9,
  "inspector_version": "0.2.0"
}
```

Field notes:

- `document_id` is `sha256:<digest>` so a later stage can key chunks by content, not by path.
- `char_count` is the length of the extracted text (whitespace included). `non_whitespace_char_count` is what the classification thresholds actually compare, so every classification decision is auditable from the output alone.
- `image_count` counts images **displayed** on the page (`page.get_image_info()`). `image_resource_count` counts image resources referenced by the page (`page.get_images(full=True)`), which can include resources that are never painted. When resources exceed displayed images the page warns `image_resources_not_displayed`, and classification uses the visible set.
- `images[]` entries carry `index`, `bbox`, `width`, `height`, `bits_per_component` and `color_space` for displayed images.
- `table_analysis` records what happened: `analysed`, `analysis_failed` (PyMuPDF raised during table analysis, so the result is unknown, **not** negative), `disabled` (`detect_tables=False`), `skipped_no_graphics` (page has no drawings or visible images), `skipped_budget` (per-document budget spent). A failed analysis does not consume the budget and never reports `analysed`.
- `status` is `ok` or `extraction_failed`; it is not a quality claim about the text.
- No manufacturer, model, printed page label or revision is ever invented. Those fields do not exist in the output.

## Page classification heuristics

Rules, applied in this order (`min_text_chars = 32` by default, `image_count` = displayed images):

1. `non_whitespace_char_count >= 32` and images present -> `mixed`
2. `non_whitespace_char_count >= 32` -> `text`
3. no text and at least one image -> `image_only`
4. some text below the threshold and at least one image -> `uncertain`
5. no text, no images, but vector drawings present -> `uncertain`
6. no text, no images, no drawings -> `empty`
7. some text below the threshold and no images -> `uncertain`

Sparse text next to an image is deliberately **not** labelled `image_only`: a thin digital text layer is present, so the page is not confidently a scan, and it is not confidently text either.

`ocr_candidate` is independent of the classification label: it is `True` when the page displays at least one image and has fewer than `min_text_chars` non-whitespace characters. A page can be `uncertain` and still be an OCR candidate. It is a flag only — no OCR engine is invoked.

These are heuristics, not detection of scanned content. A scanned page with an invisible OCR text layer will classify as `text`; that is expected and is why the raw counters are preserved.

## Indicators (FR-04)

| Field | Source | Caveat |
|---|---|---|
| `image_count`, `images[]` | `page.get_image_info()` | Images actually displayed on the page. Inline images are included; a resource that is never painted is not. |
| `image_resource_count` | `page.get_images(full=True)` | Resources referenced by the page dictionary, displayed or not. |
| `vector_drawing_count` | `page.get_drawings()` | Count of vector drawing operations (lines, curves, fills). Costs ~230 ms on a page with 4,000 operations; disable with `detect_drawings=False`. |
| `table_count`, `table_candidate` | `page.find_tables()` (line-based) | Tables rendered as a raster image are not detected. Analysis is skipped per `table_analysis`, and skipped pages report `0`. |
| `diagram_candidate` | `vector_drawing_count >= 5` | A drawing-count heuristic. Engineering schematics are not reliably identifiable this way, and a busy page border can trip it. |

## Warnings vocabulary

Page level:

- `no_text_layer_with_images` — no text but at least one displayed image: possibly a scan.
- `sparse_text_with_images` — under the text threshold *and* at least one displayed image.
- `possible_scanned_page` — same signal as `ocr_candidate`.
- `below_min_text_chars` — some text, under the threshold, no images.
- `table_candidate_line_based` — line-based table analysis found at least one table.
- `table_analysis_failed` — table analysis raised; the table result is unknown. The exception text is not copied into the output.
- `image_resources_not_displayed` — the page references more image resources than it displays.
- `page_extraction_failed:<ErrorType>` — page-level extraction error.

Document level:

- `document_has_no_pages`
- `document_has_no_extractable_text` — every inspected page had zero non-whitespace characters.
- `document_contains_ocr_candidates`
- `page_limit_reached` — `max_pages` cut the run short.
- `table_analysis_budget_exhausted` — `max_table_analysis_pages` was reached.
- `page_<n>_extraction_failed`

## Command line

```powershell
python scripts/inspect_pdf.py data/synthetic_radar_manual.pdf
python scripts/inspect_pdf.py data/synthetic_radar_manual.pdf --summary
python scripts/inspect_pdf.py data/manual.pdf --output data/inspection.json
python scripts/inspect_pdf.py data/locked.pdf --ask-password
python scripts/inspect_pdf.py data/manual.pdf --no-table-detection
```

An encrypted PDF is opened through `--ask-password`, which prompts with `getpass`
so the value is neither echoed nor stored in shell history, and there is
deliberately **no** command-line option that accepts a literal password — passing
`--password` (or `--password=...`) is refused explicitly and the value is not
echoed back. When
stdin is redirected (automation or tests) the password is read from stdin, which
still keeps it out of the process argument list. The Python API
`inspect_pdf(path, password=...)` is unchanged for callers that hold the secret
in a variable.

Exit code `0` on success; `2` on a controlled inspection error (the message names the exception type and never includes document content beyond the file name).

## Performance, measured on the development laptop

Same machine, Python 3.12.10, CPU only, peak measured with `tracemalloc`. Fixture: 40-page synthetic manual (33,965 bytes) with four pages carrying a drawn table.

| Configuration | Wall time | Peak traced memory | Tables found |
|---|---|---|---|
| `detect_tables=True` (default) | 5,244 ms / 6,463 ms (two runs) | 22.24–22.25 MiB | 4 |
| `detect_tables=False` | 99 ms / 119 ms (two runs) | 1.04 MiB | 0 (skipped) |

Single-call costs on the same machine: `get_text` ~2.1 ms, `get_images` ~0.05 ms, `get_drawings` ~0.12 ms on a simple page and ~230 ms on a page with 4,000 drawing operations, `find_tables` ~651 ms on a simple page and ~3,013 ms on that 4,000-drawing page.

Because table analysis dominates, it runs only on pages that carry drawings or visible images, is capped at 25 analysed pages per document by default, and can be disabled entirely. The checksum is computed in 1 MiB streaming chunks, so a large manual is never loaded into memory to be hashed.

## Limitations

- Classification and indicators are heuristics; they are not a substitute for OCR or layout analysis.
- No OCR is performed, so `ocr_candidate` pages carry no text.
- Tables without drawn lines, and tables inside images, are not detected.
- `diagram_candidate` cannot distinguish an engineering schematic from decorative vector art.
- Text extraction uses PyMuPDF's default reading order; multi-column layouts may interleave lines. Corrected layout extraction (Docling, PyMuPDF layout mode) is a later experiment.
- Page inspection is sequential and single-document; there is no batch runner yet.
