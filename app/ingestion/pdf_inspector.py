"""PDF inspection and page-level text extraction with source provenance.

Part of the Industrial RAG Copilot ingestion layer (Issue #2 / Milestone 2).
The module is intentionally narrow: it opens a local PDF, records a streaming
SHA256 fingerprint, extracts text page by page, classifies each page with
documented deterministic heuristics, and returns a JSON-serialisable result.

It does not chunk text, generate embeddings, call an LLM, run OCR, or touch any
network service. The classification fields are heuristics, not detection of
scanned content.

Public API
----------
``inspect_pdf(path, *, password=None, min_text_chars=32, detect_tables=True,
detect_drawings=True, max_pages=None, max_table_analysis_pages=25)``
    Return a :class:`DocumentInspection` for one local PDF file.

``DocumentInspection.to_dict()`` / ``DocumentInspection.to_json()``
    Structured, JSON-serialisable output (document metadata, page metadata,
    warnings).

Exceptions raised for unusable input are subclasses of
:class:`PdfInspectionError`; no raw PyMuPDF exception escapes the module.
Supplied passwords are used for authentication only: they are never stored in
the result, written to a warning, or included in an exception message.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

INSPECTOR_VERSION = "0.2.0"

#: Minimum number of non-whitespace characters for a page to count as carrying
#: a usable digital text layer. Documented heuristic, not a quality judgement.
MIN_TEXT_CHARS = 32

#: A page with at least this many vector drawing operations is flagged as a
#: possible diagram. Documented heuristic; engineering schematics are not
#: reliably identifiable from drawing counts alone.
DIAGRAM_MIN_VECTOR_DRAWINGS = 5

#: Default per-document budget for line-based table analysis. Measured cost is
#: roughly 0.65 s on a simple page and about 3 s on a page with 4,000 drawing
#: operations, so a bulk run must not analyse an unbounded number of pages.
MAX_TABLE_ANALYSIS_PAGES = 25

#: Checksum read size. The file is hashed in streaming chunks so a large manual
#: is never loaded into memory to be fingerprinted.
HASH_CHUNK_BYTES = 1024 * 1024

_PDF_MAGIC = b"%PDF-"
_PDF_SUFFIX = ".pdf"

CLASSIFICATION_TEXT = "text"
CLASSIFICATION_IMAGE_ONLY = "image_only"
CLASSIFICATION_MIXED = "mixed"
CLASSIFICATION_EMPTY = "empty"
CLASSIFICATION_UNCERTAIN = "uncertain"

TABLE_ANALYSIS_DONE = "analysed"
TABLE_ANALYSIS_FAILED = "analysis_failed"
TABLE_ANALYSIS_DISABLED = "disabled"
TABLE_ANALYSIS_SKIPPED_NO_GRAPHICS = "skipped_no_graphics"
TABLE_ANALYSIS_SKIPPED_BUDGET = "skipped_budget"


class PdfInspectionError(Exception):
    """Base class for every controlled PDF inspection failure."""


class PdfNotFoundError(PdfInspectionError):
    """The supplied path does not exist, or is not a regular file."""


class UnsupportedFileTypeError(PdfInspectionError):
    """The supplied file is not a PDF by extension or by its header bytes."""


class PdfEmptyFileError(PdfInspectionError):
    """The supplied file exists but contains zero bytes."""


class PdfCorruptedError(PdfInspectionError):
    """The file has a PDF header but cannot be parsed by PyMuPDF."""


class PdfEncryptedError(PdfInspectionError):
    """The document is password protected and no usable password was supplied.

    The message never contains the supplied password.
    """


@dataclass(frozen=True)
class VisibleImage:
    """One image that is actually displayed on a page.

    Derived from ``page.get_image_info()``: this reports images placed on the
    page, whereas ``page.get_images(full=True)`` reports resources referenced by
    the page's resource dictionary, which can include images that are never
    painted. Classification uses this visible set.
    """

    index: int
    bbox: tuple[float, float, float, float] | None
    width: int
    height: int
    bits_per_component: int
    color_space: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "bbox": list(self.bbox) if self.bbox is not None else None,
            "width": self.width,
            "height": self.height,
            "bits_per_component": self.bits_per_component,
            "color_space": self.color_space,
        }


@dataclass
class PageInspection:
    """Extraction result for a single page, with source provenance."""

    page_index: int
    page_number: int
    text: str
    char_count: int
    non_whitespace_char_count: int
    classification: str
    ocr_candidate: bool
    image_count: int
    image_resource_count: int = 0
    images: list[VisibleImage] = field(default_factory=list)
    vector_drawing_count: int = 0
    table_count: int = 0
    table_candidate: bool = False
    table_analysis: str = TABLE_ANALYSIS_DONE
    diagram_candidate: bool = False
    status: str = "ok"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_number": self.page_number,
            "text": self.text,
            "char_count": self.char_count,
            "non_whitespace_char_count": self.non_whitespace_char_count,
            "classification": self.classification,
            "ocr_candidate": self.ocr_candidate,
            "image_count": self.image_count,
            "image_resource_count": self.image_resource_count,
            "images": [image.as_dict() for image in self.images],
            "vector_drawing_count": self.vector_drawing_count,
            "table_count": self.table_count,
            "table_candidate": self.table_candidate,
            "table_analysis": self.table_analysis,
            "diagram_candidate": self.diagram_candidate,
            "status": self.status,
            "warnings": list(self.warnings),
        }


@dataclass
class DocumentInspection:
    """Inspection result for one PDF document."""

    document_id: str
    filename: str
    sha256: str
    file_size_bytes: int
    page_count: int
    pages: list[PageInspection] = field(default_factory=list)
    pages_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    extraction_duration_ms: int = 0
    inspector_version: str = INSPECTOR_VERSION

    def classification_counts(self) -> dict[str, int]:
        counts = {
            CLASSIFICATION_TEXT: 0,
            CLASSIFICATION_IMAGE_ONLY: 0,
            CLASSIFICATION_MIXED: 0,
            CLASSIFICATION_EMPTY: 0,
            CLASSIFICATION_UNCERTAIN: 0,
        }
        for page in self.pages:
            counts[page.classification] = counts.get(page.classification, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "sha256": self.sha256,
            "file_size_bytes": self.file_size_bytes,
            "page_count": self.page_count,
            "pages_inspected": len(self.pages),
            "pages_skipped": self.pages_skipped,
            "pages": [page.as_dict() for page in self.pages],
            "classification_counts": self.classification_counts(),
            "ocr_candidate_pages": [
                page.page_number for page in self.pages if page.ocr_candidate
            ],
            "warnings": list(self.warnings),
            "extraction_duration_ms": self.extraction_duration_ms,
            "inspector_version": self.inspector_version,
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`as_dict` for JSON serialisation call sites."""

        return self.as_dict()

    def to_json(self, *, indent: int = 2) -> str:
        import json

        return json.dumps(self.as_dict(), indent=indent, ensure_ascii=False)


def classify_page(
    *,
    non_whitespace_char_count: int,
    image_count: int,
    vector_drawing_count: int,
    min_text_chars: int = MIN_TEXT_CHARS,
) -> str:
    """Classify a page using documented deterministic heuristics.

    ``image_count`` is the number of *visible* images on the page.

    Rules, in order:

    1. ``non_whitespace_char_count >= min_text_chars`` and images present -> ``mixed``
    2. ``non_whitespace_char_count >= min_text_chars`` -> ``text``
    3. no text and at least one image -> ``image_only``
    4. some text below the threshold and at least one image -> ``uncertain``
       (sparse text plus an image is not confidently an image-only page)
    5. no text, no images, but vector drawings present -> ``uncertain``
       (a vector-only page is not an empty page, so it is not silently dropped)
    6. no text, no images, no drawings -> ``empty``
    7. some text below the threshold and no images -> ``uncertain``

    A page whose extraction failed is reported with ``status="extraction_failed"``
    and classified ``uncertain`` by the caller.
    """

    if non_whitespace_char_count >= min_text_chars:
        return CLASSIFICATION_MIXED if image_count > 0 else CLASSIFICATION_TEXT
    if non_whitespace_char_count == 0:
        if image_count > 0:
            return CLASSIFICATION_IMAGE_ONLY
        return (
            CLASSIFICATION_UNCERTAIN if vector_drawing_count else CLASSIFICATION_EMPTY
        )
    # Sparse text: never confidently text, and with an image it is also not
    # confidently image-only, because a thin digital text layer is present.
    return CLASSIFICATION_UNCERTAIN


def is_ocr_candidate(
    *,
    non_whitespace_char_count: int,
    image_count: int,
    min_text_chars: int = MIN_TEXT_CHARS,
) -> bool:
    """Return True when a page looks like it needs OCR before text extraction.

    Independent of the classification label: a page with sparse text and an
    image classifies as ``uncertain`` yet still needs OCR. This only flags the
    page; no OCR engine is invoked by this module.
    """

    return image_count > 0 and non_whitespace_char_count < min_text_chars


def compute_sha256(path: str | Path, *, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """Hash a file in streaming chunks and return the lowercase hex digest."""

    digest = hashlib.sha256()
    file_path = Path(path)
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_bytes)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_positive_int(name: str, value: int | None, *, allow_none: bool) -> None:
    """Reject nonsensical processing limits before any file is opened."""

    if value is None and allow_none:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        expected = "an int or None" if allow_none else "an int"
        raise TypeError(f"{name} must be {expected}, got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")


def _validate_limits(
    *,
    min_text_chars: int,
    max_pages: int | None,
    max_table_analysis_pages: int | None,
) -> None:
    _validate_positive_int("min_text_chars", min_text_chars, allow_none=False)
    _validate_positive_int("max_pages", max_pages, allow_none=True)
    _validate_positive_int(
        "max_table_analysis_pages", max_table_analysis_pages, allow_none=True
    )


def _validate_path(path: str | Path) -> Path:
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise PdfNotFoundError(f"PDF file not found: {file_path}")
    if file_path.suffix.lower() != _PDF_SUFFIX:
        raise UnsupportedFileTypeError(
            f"Unsupported file extension for {file_path.name!r}; expected .pdf"
        )
    if file_path.stat().st_size == 0:
        raise PdfEmptyFileError(f"PDF file is empty (0 bytes): {file_path}")
    with file_path.open("rb") as handle:
        header = handle.read(len(_PDF_MAGIC))
    if header != _PDF_MAGIC:
        raise UnsupportedFileTypeError(
            f"File does not start with a PDF header: {file_path.name!r}"
        )
    return file_path


def _open_document(file_path: Path, password: str | None) -> pymupdf.Document:
    try:
        document = pymupdf.open(str(file_path))
    except pymupdf.EmptyFileError as error:
        raise PdfEmptyFileError(f"PDF file is empty (0 bytes): {file_path}") from error
    except (pymupdf.FileDataError, RuntimeError, ValueError) as error:
        raise PdfCorruptedError(
            f"Unable to parse PDF file {file_path.name!r}: {error}"
        ) from error

    if document.needs_pass:
        authenticated = bool(password) and bool(document.authenticate(password))
        if not authenticated:
            document.close()
            # The supplied password is never echoed back in the message.
            reason = (
                "a password is required"
                if not password
                else "the supplied password was rejected"
            )
            raise PdfEncryptedError(f"{file_path.name!r} is encrypted: {reason}")
    return document


def _visible_images(page: pymupdf.Page) -> list[VisibleImage]:
    """Return the images actually displayed on the page."""

    images: list[VisibleImage] = []
    for index, info in enumerate(page.get_image_info()):
        bbox = info.get("bbox")
        box: tuple[float, float, float, float] | None = None
        if bbox is not None and len(bbox) == 4:
            box = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        images.append(
            VisibleImage(
                index=index,
                bbox=box,
                width=int(info.get("width") or 0),
                height=int(info.get("height") or 0),
                bits_per_component=int(info.get("bpc") or 0),
                color_space=str(info.get("cs-name") or info.get("colorspace") or ""),
            )
        )
    return images


def _image_resource_count(page: pymupdf.Page) -> int:
    """Count image resources referenced by the page (not all are displayed)."""

    try:
        return len(page.get_images(full=True))
    except (RuntimeError, ValueError):
        return 0


def _table_count(
    page: pymupdf.Page,
    *,
    detect_tables: bool,
    budget_available: bool,
    vector_drawing_count: int,
    image_count: int,
) -> tuple[int, str]:
    """Return the number of line-based table candidates and the analysis state.

    ``page.find_tables()`` measured about 0.65 s on a simple page and about 3 s
    on a page with 4,000 drawing operations, so analysis is skipped when it is
    disabled, when the per-document budget is spent, or when the page carries
    neither drawings nor images (the default line-based strategy has nothing to
    find there). Skipped pages report 0 rather than a guessed value, and a page
    whose analysis raised reports ``analysis_failed`` instead of a negative
    result. A failed analysis does not consume the per-document budget.
    """

    if not detect_tables:
        return 0, TABLE_ANALYSIS_DISABLED
    if not budget_available:
        return 0, TABLE_ANALYSIS_SKIPPED_BUDGET
    if vector_drawing_count == 0 and image_count == 0:
        return 0, TABLE_ANALYSIS_SKIPPED_NO_GRAPHICS
    try:
        return len(page.find_tables().tables), TABLE_ANALYSIS_DONE
    except (RuntimeError, ValueError):
        # A page that could not be analysed is NOT reported as analysed: the
        # failure state is published so a consumer cannot read it as "no tables
        # found". The exception text is deliberately not copied into the output.
        return 0, TABLE_ANALYSIS_FAILED


def _inspect_page(
    page: pymupdf.Page,
    *,
    page_index: int,
    min_text_chars: int,
    detect_tables: bool,
    detect_drawings: bool,
    table_budget_available: bool,
) -> PageInspection:
    text = page.get_text("text")
    non_whitespace_char_count = len("".join(text.split()))
    images = _visible_images(page)
    image_count = len(images)
    image_resource_count = _image_resource_count(page)
    vector_drawing_count = len(page.get_drawings()) if detect_drawings else 0
    table_count, table_analysis = _table_count(
        page,
        detect_tables=detect_tables,
        budget_available=table_budget_available,
        vector_drawing_count=vector_drawing_count,
        image_count=image_count,
    )

    classification = classify_page(
        non_whitespace_char_count=non_whitespace_char_count,
        image_count=image_count,
        vector_drawing_count=vector_drawing_count,
        min_text_chars=min_text_chars,
    )
    ocr_candidate = is_ocr_candidate(
        non_whitespace_char_count=non_whitespace_char_count,
        image_count=image_count,
        min_text_chars=min_text_chars,
    )

    warnings: list[str] = []
    if non_whitespace_char_count == 0 and image_count > 0:
        warnings.append("no_text_layer_with_images")
    elif 0 < non_whitespace_char_count < min_text_chars and image_count > 0:
        warnings.append("sparse_text_with_images")
    elif 0 < non_whitespace_char_count < min_text_chars:
        warnings.append("below_min_text_chars")
    if ocr_candidate:
        warnings.append("possible_scanned_page")
    if image_resource_count > image_count:
        warnings.append("image_resources_not_displayed")
    if table_analysis == TABLE_ANALYSIS_FAILED:
        warnings.append("table_analysis_failed")
    if table_count > 0:
        warnings.append("table_candidate_line_based")

    return PageInspection(
        page_index=page_index,
        page_number=page_index + 1,
        text=text,
        char_count=len(text),
        non_whitespace_char_count=non_whitespace_char_count,
        classification=classification,
        ocr_candidate=ocr_candidate,
        image_count=image_count,
        image_resource_count=image_resource_count,
        images=images,
        vector_drawing_count=vector_drawing_count,
        table_count=table_count,
        table_candidate=table_count > 0,
        table_analysis=table_analysis,
        diagram_candidate=vector_drawing_count >= DIAGRAM_MIN_VECTOR_DRAWINGS,
        warnings=warnings,
    )


def _failed_page(page_index: int, error: Exception) -> PageInspection:
    return PageInspection(
        page_index=page_index,
        page_number=page_index + 1,
        text="",
        char_count=0,
        non_whitespace_char_count=0,
        classification=CLASSIFICATION_UNCERTAIN,
        ocr_candidate=False,
        image_count=0,
        status="extraction_failed",
        table_analysis=TABLE_ANALYSIS_DISABLED,
        warnings=[f"page_extraction_failed:{type(error).__name__}"],
    )


def inspect_pdf(
    path: str | Path,
    *,
    password: str | None = None,
    min_text_chars: int = MIN_TEXT_CHARS,
    detect_tables: bool = True,
    detect_drawings: bool = True,
    max_pages: int | None = None,
    max_table_analysis_pages: int | None = MAX_TABLE_ANALYSIS_PAGES,
) -> DocumentInspection:
    """Inspect a local PDF and return document plus page-level metadata.

    Args:
        path: Path to a local PDF file.
        password: Optional password for an encrypted document. A document that
            needs a password and is not given a working one raises
            :class:`PdfEncryptedError`. The password is used for authentication
            only and never stored or reported.
        min_text_chars: Threshold of non-whitespace characters used by the page
            classification heuristics.
        detect_tables: Run PyMuPDF line-based table analysis on pages that carry
            vector drawings or images. Measured at roughly 0.65 s per analysed
            page, so it is skipped for pages without drawn lines; set to False to
            skip table analysis entirely.
        detect_drawings: Read vector drawing counts. Disable for very complex
            CAD-style exports where ``get_drawings()`` is expensive (measured
            ~230 ms on a page with 4,000 drawing operations).
        max_pages: Inspect at most this many pages; the rest are reported as
            skipped and a ``page_limit_reached`` warning is added.
        max_table_analysis_pages: Per-document budget for table analysis. When
            the budget is spent, remaining pages report
            ``table_analysis="skipped_budget"`` and a warning is added. ``None``
            removes the budget.

    Raises:
        TypeError: a limit is not an integer (``bool`` is rejected too).
        ValueError: ``min_text_chars``, ``max_pages`` or
            ``max_table_analysis_pages`` is below 1.
        PdfNotFoundError, UnsupportedFileTypeError, PdfEmptyFileError,
        PdfCorruptedError, PdfEncryptedError

    Returns:
        A :class:`DocumentInspection` whose ``to_dict()`` output is
        JSON-serialisable.
    """

    _validate_limits(
        min_text_chars=min_text_chars,
        max_pages=max_pages,
        max_table_analysis_pages=max_table_analysis_pages,
    )
    file_path = _validate_path(path)
    started = time.perf_counter()
    sha256 = compute_sha256(file_path)
    file_size_bytes = file_path.stat().st_size

    document = _open_document(file_path, password)
    warnings: list[str] = []
    pages: list[PageInspection] = []
    pages_skipped = 0
    table_pages_analysed = 0
    try:
        page_count = document.page_count
        if page_count == 0:
            warnings.append("document_has_no_pages")
        inspect_limit = page_count if max_pages is None else min(page_count, max_pages)
        if inspect_limit < page_count:
            pages_skipped = page_count - inspect_limit
            warnings.append("page_limit_reached")
        for page_index in range(inspect_limit):
            budget_available = (
                max_table_analysis_pages is None
                or table_pages_analysed < max_table_analysis_pages
            )
            try:
                page = document.load_page(page_index)
                inspection = _inspect_page(
                    page,
                    page_index=page_index,
                    min_text_chars=min_text_chars,
                    detect_tables=detect_tables,
                    detect_drawings=detect_drawings,
                    table_budget_available=budget_available,
                )
                if inspection.table_analysis == TABLE_ANALYSIS_DONE:
                    table_pages_analysed += 1
                pages.append(inspection)
            except Exception as error:  # noqa: BLE001 - one bad page must not kill the run
                pages.append(_failed_page(page_index, error))
                warnings.append(f"page_{page_index + 1}_extraction_failed")
    finally:
        document.close()

    extraction_duration_ms = int((time.perf_counter() - started) * 1000)

    inspected = DocumentInspection(
        document_id=f"sha256:{sha256}",
        filename=file_path.name,
        sha256=sha256,
        file_size_bytes=file_size_bytes,
        page_count=page_count,
        pages=pages,
        pages_skipped=pages_skipped,
        warnings=warnings,
        extraction_duration_ms=extraction_duration_ms,
    )

    if pages and all(page.non_whitespace_char_count == 0 for page in pages):
        inspected.warnings.append("document_has_no_extractable_text")
    if any(page.ocr_candidate for page in pages):
        inspected.warnings.append("document_contains_ocr_candidates")
    if any(page.table_analysis == TABLE_ANALYSIS_SKIPPED_BUDGET for page in pages):
        inspected.warnings.append("table_analysis_budget_exhausted")
    return inspected
