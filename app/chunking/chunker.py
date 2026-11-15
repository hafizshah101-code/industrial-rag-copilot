"""Deterministic chunking with page and section provenance (Issue #3 / M3).

Two strategies, one contract
---------------------------
``fixed_token``
    A size baseline: consecutive windows of ``target_tokens`` tokens with
    ``overlap_tokens`` shared tokens, allowed to span page boundaries (OSJ Q3)
    because page spans are tracked exactly. It has no section concept, and says
    so: every chunk carries ``section_path = ["untitled"]``.

``section_aware``
    Heading-delimited sections built from verified M2 character offsets. A
    section that fits ``max_tokens`` stays whole; a larger one is split with
    sentence-preferred boundaries, overlap only between consecutive parts of the
    same section, and a trailing fragment merged backwards when it fits.

Invariants (from the accepted design addendum)
---------------------------------------------
I1  Every chunk's ``text`` is a contiguous substring of the M2 page texts
    concatenated in page order; nothing is rewritten, normalised or joined.
I2  Boundaries and page spans come from M2 character offsets only.
I3  Each page lands in exactly one bucket: ``covered``, ``needs_ocr`` or
    ``skipped(reason)`` (OSJ C2).
I4  Same bytes + same configuration + same operator metadata => identical
    ``chunk_id`` list, on any machine (no paths, timestamps or host data).
I5  Any setting that can change chunk content or identity changes
    ``config_fingerprint``, and therefore every id.
I6  One document per result; a chunk never mixes documents.

``duration_ms`` is telemetry: it is published, and excluded from
:meth:`ChunkingResult.to_deterministic_json` so two runs of the same input are
byte-identical there (OSJ C1).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.chunking.errors import (
    ChunkingConfigError,
    ChunkingError,
    LayoutSourceRequiredError,
)
from app.chunking.headings import (
    MAPPING_RATIO_THRESHOLD,
    LayoutAnalysis,
    LayoutDocument,
    analyse_layout,
    read_layout,
)
from app.chunking.tokenizer import (
    TOKEN_ESTIMATE_METHOD,
    estimate_tokens,
    token_spans,
)
from app.ingestion.pdf_inspector import (
    CLASSIFICATION_EMPTY,
    CLASSIFICATION_IMAGE_ONLY,
    CLASSIFICATION_UNCERTAIN,
    MIN_TEXT_CHARS,
    DocumentInspection,
    PageInspection,
)

#: Version of the chunking implementation. It is part of the configuration
#: fingerprint, so bumping it deliberately invalidates every stored chunk id.
#: 0.2.0: boundary selection reads line breaks from the M2 character ranges
#: (OSJ D-03) and page buckets follow the text-first OCR policy (OSJ D-02), so
#: ids produced by 0.1.0 must not be mistaken for current ones.
CHUNKER_VERSION = "0.2.0"

STRATEGY_FIXED_TOKEN = "fixed_token"
STRATEGY_SECTION_AWARE = "section_aware"
STRATEGIES = (STRATEGY_SECTION_AWARE, STRATEGY_FIXED_TOKEN)
DEFAULT_STRATEGY = STRATEGY_SECTION_AWARE

DEFAULT_TARGET_TOKENS = 320
DEFAULT_MAX_TOKENS = 400

#: Documented defaults, not measured optima (Stage A D1): the baseline overlaps
#: twice as much as a section split because it has no heading to anchor on.
DEFAULT_OVERLAP_TOKENS = {
    STRATEGY_FIXED_TOKEN: 64,
    STRATEGY_SECTION_AWARE: 32,
}

#: A sentence end is searched for from this fraction of the target up to the
#: maximum, so a cut never lands before 80 % of the requested size.
SENTENCE_SEARCH_LOWER_RATIO = 0.80

#: A trailing fragment smaller than this is merged back into the previous part
#: when the merge still fits ``max_tokens``.
MIN_SECTION_FRAGMENT_TOKENS = 40

#: Section label used when a chunk has no heading at all.
UNTITLED_SECTION = "untitled"

#: Longest heading text reused as a section-path label (path elements only).
SECTION_LABEL_MAX_CHARS = 40

SENTENCE_END_RE = re.compile(r"[.!?;:][\"'\u2019\u201d)\]]*$")

PAGE_REASON_EXTRACTION_FAILED = "extraction_failed"
PAGE_REASON_EMPTY = "empty"
ORIGIN_SECTION = "section"
ORIGIN_SPLIT = "split"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
METADATA_KEYS = ("equipment_model", "revision")


def normalise_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, dict[str, str | None]]:
    """Return the published metadata block, never inventing a value.

    Absent keys serialise as ``{"value": None, "source": "unknown"}``. Only
    operator-supplied values appear as ``"source": "operator"``; M3 never parses
    a model or revision out of document text.
    """

    block: dict[str, dict[str, str | None]] = {
        key: {"value": None, "source": "unknown"} for key in METADATA_KEYS
    }
    if metadata is None:
        return block
    if not isinstance(metadata, Mapping):
        raise ChunkingConfigError(
            "metadata must be a mapping of "
            f"{', '.join(METADATA_KEYS)} to strings, got {type(metadata).__name__}"
        )
    unknown = sorted(str(key) for key in metadata if key not in METADATA_KEYS)
    if unknown:
        raise ChunkingConfigError(
            "unsupported metadata key(s): "
            f"{', '.join(unknown)}; allowed keys are {', '.join(METADATA_KEYS)}"
        )
    for key in METADATA_KEYS:
        if key not in metadata:
            continue
        value = metadata[key]
        if value is None:
            continue
        if not isinstance(value, str):
            raise ChunkingConfigError(
                f"metadata[{key!r}] must be a string or None, "
                f"got {type(value).__name__}"
            )
        stripped = value.strip()
        if not stripped:
            raise ChunkingConfigError(f"metadata[{key!r}] must not be blank")
        block[key] = {"value": stripped, "source": "operator"}
    return block


def _canonical_json(payload: Any) -> str:
    """Canonical JSON: sorted keys, compact separators, UTF-8 friendly."""

    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChunkingConfig:
    """Every input that can change chunk content or chunk identity."""

    strategy: str = DEFAULT_STRATEGY
    target_tokens: int = DEFAULT_TARGET_TOKENS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int | None = None
    min_text_chars: int = MIN_TEXT_CHARS
    mapping_ratio_threshold: float = MAPPING_RATIO_THRESHOLD
    per_heading_validation: bool = True
    metadata: dict[str, dict[str, str | None]] = field(
        default_factory=lambda: normalise_metadata(None)
    )
    chunker_version: str = CHUNKER_VERSION
    token_estimate_method: str = TOKEN_ESTIMATE_METHOD

    @property
    def effective_overlap(self) -> int:
        if self.overlap_tokens is None:
            return DEFAULT_OVERLAP_TOKENS[self.strategy]
        return self.overlap_tokens

    def heading_rules(self) -> dict[str, Any]:
        """Heading/mapping rules that participate in the fingerprint."""

        from app.chunking.headings import (
            FOOTER_BAND_PCT,
            HEADER_BAND_PCT,
            HEADING_MAX_CHARS,
            HEADING_SIZE_DELTA_PT,
            REPEAT_THRESHOLD_PCT,
        )

        return {
            "size_delta_pt": HEADING_SIZE_DELTA_PT,
            "max_chars": HEADING_MAX_CHARS,
            "accept_bold": True,
            "accept_upper": True,
            "accept_numbering": True,
            "header_band_pct": HEADER_BAND_PCT,
            "footer_band_pct": FOOTER_BAND_PCT,
            "repeat_threshold_pct": REPEAT_THRESHOLD_PCT,
            "mapping_ratio_threshold": self.mapping_ratio_threshold,
            "per_heading_validation": self.per_heading_validation,
        }

    def as_config_dict(self) -> dict[str, Any]:
        return {
            "chunker_version": self.chunker_version,
            "strategy": self.strategy,
            "target_tokens": self.target_tokens,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.effective_overlap,
            "min_text_chars": self.min_text_chars,
            "token_estimate_method": self.token_estimate_method,
            "heading_rules": self.heading_rules(),
            "metadata": self.metadata,
        }

    def fingerprint(self) -> str:
        return "sha256:" + _sha256_hex(_canonical_json(self.as_config_dict()))


def _require_int(name: str, value: Any, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChunkingConfigError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise ChunkingConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def build_config(
    *,
    strategy: str = DEFAULT_STRATEGY,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int | None = None,
    min_text_chars: int = MIN_TEXT_CHARS,
    mapping_ratio_threshold: float = MAPPING_RATIO_THRESHOLD,
    per_heading_validation: bool = True,
    metadata: Mapping[str, Any] | None = None,
    chunker_version: str = CHUNKER_VERSION,
) -> ChunkingConfig:
    """Validate chunking inputs and return the immutable configuration."""

    if strategy not in STRATEGIES:
        raise ChunkingConfigError(
            f"unknown strategy {strategy!r}; expected one of {', '.join(STRATEGIES)}"
        )
    _require_int("target_tokens", target_tokens, minimum=1)
    _require_int("max_tokens", max_tokens, minimum=1)
    _require_int("min_text_chars", min_text_chars, minimum=1)
    if target_tokens > max_tokens:
        raise ChunkingConfigError(
            f"target_tokens ({target_tokens}) must not exceed max_tokens ({max_tokens})"
        )
    if overlap_tokens is not None:
        _require_int("overlap_tokens", overlap_tokens, minimum=0)
        if overlap_tokens >= target_tokens:
            raise ChunkingConfigError(
                f"overlap_tokens ({overlap_tokens}) must be smaller than "
                f"target_tokens ({target_tokens})"
            )
    if isinstance(mapping_ratio_threshold, bool) or not isinstance(
        mapping_ratio_threshold, (int, float)
    ):
        raise ChunkingConfigError(
            "mapping_ratio_threshold must be a number, got "
            f"{type(mapping_ratio_threshold).__name__}"
        )
    if not 0.0 < float(mapping_ratio_threshold) <= 1.0:
        raise ChunkingConfigError(
            "mapping_ratio_threshold must be in (0.0, 1.0], got "
            f"{mapping_ratio_threshold}"
        )
    if not isinstance(per_heading_validation, bool):
        raise ChunkingConfigError(
            "per_heading_validation must be a bool, got "
            f"{type(per_heading_validation).__name__}"
        )
    if not isinstance(chunker_version, str) or not chunker_version.strip():
        raise ChunkingConfigError("chunker_version must be a non-empty string")
    return ChunkingConfig(
        strategy=strategy,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        min_text_chars=min_text_chars,
        mapping_ratio_threshold=float(mapping_ratio_threshold),
        per_heading_validation=per_heading_validation,
        metadata=normalise_metadata(metadata),
        chunker_version=chunker_version,
    )


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SkippedPage:
    """A page that contributed no text to any chunk, with the reason."""

    page_index: int
    page_number: int
    reason: str
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_number": self.page_number,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit, self-describing and traceable to M2 pages."""

    chunk_id: str
    document_id: str
    filename: str
    strategy: str
    chunker_version: str
    config_fingerprint: str
    chunk_index: int
    text: str
    char_count: int
    token_estimate: int
    token_estimate_method: str
    page_index_start: int
    page_index_end: int
    page_start: int
    page_end: int
    section_path: list[str]
    heading: str | None
    section_inferred: bool
    boundary: dict[str, Any]
    metadata: dict[str, Any]
    source_pages: list[dict[str, Any]]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "filename": self.filename,
            "strategy": self.strategy,
            "chunker_version": self.chunker_version,
            "config_fingerprint": self.config_fingerprint,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "char_count": self.char_count,
            "token_estimate": self.token_estimate,
            "token_estimate_method": self.token_estimate_method,
            "page_index_start": self.page_index_start,
            "page_index_end": self.page_index_end,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_path": list(self.section_path),
            "heading": self.heading,
            "section_inferred": self.section_inferred,
            "boundary": dict(self.boundary),
            "metadata": dict(self.metadata),
            "source_pages": [dict(page) for page in self.source_pages],
            "warnings": list(self.warnings),
        }


@dataclass
class ChunkingResult:
    """Structured result of chunking one document (F1 contract)."""

    document_id: str
    filename: str
    strategy: str
    chunker_version: str
    config_fingerprint: str
    chunks: list[Chunk]
    needs_ocr: list[int]
    skipped_pages: list[SkippedPage]
    warnings: list[str]
    stats: dict[str, Any]
    duration_ms: int
    layout_exclusions: list[dict[str, Any]] = field(default_factory=list)
    heading_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    page_mapping: list[dict[str, Any]] = field(default_factory=list)
    ocr_diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "filename": self.filename,
            "strategy": self.strategy,
            "chunker_version": self.chunker_version,
            "config_fingerprint": self.config_fingerprint,
            "chunks": [chunk.as_dict() for chunk in self.chunks],
            "needs_ocr": list(self.needs_ocr),
            "skipped_pages": [page.as_dict() for page in self.skipped_pages],
            "warnings": list(self.warnings),
            "stats": dict(self.stats),
            "duration_ms": self.duration_ms,
            # Additive diagnostics required by OSJ Q2, C3 and D-02; they are
            # deterministic, so they stay inside the deterministic payload.
            "layout_exclusions": [dict(item) for item in self.layout_exclusions],
            "heading_diagnostics": [dict(item) for item in self.heading_diagnostics],
            "page_mapping": [dict(item) for item in self.page_mapping],
            "ocr_diagnostics": [dict(item) for item in self.ocr_diagnostics],
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias for :meth:`as_dict` for JSON serialisation call sites."""

        return self.as_dict()

    def deterministic_payload(self) -> dict[str, Any]:
        """Payload without runtime telemetry, for byte-identical comparisons."""

        payload = self.as_dict()
        payload.pop("duration_ms", None)
        return payload

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, ensure_ascii=False)

    def to_deterministic_json(self, *, indent: int = 2) -> str:
        return json.dumps(
            self.deterministic_payload(), indent=indent, ensure_ascii=False
        )

    def chunk_ids(self) -> list[str]:
        return [chunk.chunk_id for chunk in self.chunks]


# --------------------------------------------------------------------------- #
# Page spans, buckets and runs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _PageSpan:
    page_index: int
    page_number: int
    start: int
    end: int


@dataclass
class _Buckets:
    covered: list[int] = field(default_factory=list)
    needs_ocr: list[int] = field(default_factory=list)
    skipped: list[SkippedPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    low_text_pages: set[int] = field(default_factory=set)
    ocr_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    text_pages_with_visuals: set[int] = field(default_factory=set)


def _page_spans(inspection: DocumentInspection) -> list[_PageSpan]:
    spans: list[_PageSpan] = []
    cursor = 0
    for page in inspection.pages:
        length = len(page.text)
        spans.append(
            _PageSpan(
                page_index=page.page_index,
                page_number=page.page_number,
                start=cursor,
                end=cursor + length,
            )
        )
        cursor += length
    return spans


def _bucket_pages(inspection: DocumentInspection, *, min_text_chars: int) -> _Buckets:
    """Assign every page to exactly one bucket, text first (OSJ C2 and D-02).

    Order of decisions, documented and deterministic:

    1. ``extraction_failed`` -> ``skipped``.
    2. ``classification == "empty"`` -> ``skipped``.
    3. **useful extracted text** (``non_whitespace_char_count >= min_text_chars``)
       -> ``covered``, *even when* M2 flagged the page ``ocr_candidate`` or found
       visible images. Useful text is never discarded merely because a visual
       signal is present (OSJ D-02); the OCR/visual signal is published in
       ``ocr_diagnostics[]`` instead of being folded into the bucket.
    4. no useful text and a visual/OCR signal -> ``needs_ocr``; the character
       count that was left out is published in ``ocr_diagnostics[]``, so the
       omission is explicit rather than silent.
    5. sparse text without images -> ``covered`` with a ``low_text_page`` warning.

    Buckets stay mutually exclusive: a page appears in exactly one of
    ``covered``, ``needs_ocr`` or ``skipped``.
    """

    buckets = _Buckets()
    for page in inspection.pages:
        non_whitespace = page.non_whitespace_char_count
        if page.status == "extraction_failed":
            buckets.skipped.append(
                SkippedPage(
                    page_index=page.page_index,
                    page_number=page.page_number,
                    reason=PAGE_REASON_EXTRACTION_FAILED,
                    detail=_first_warning(page),
                )
            )
            continue
        if page.classification == CLASSIFICATION_EMPTY:
            buckets.skipped.append(
                SkippedPage(
                    page_index=page.page_index,
                    page_number=page.page_number,
                    reason=PAGE_REASON_EMPTY,
                    detail=None,
                )
            )
            continue

        visual_signal = bool(
            page.ocr_candidate
            or page.image_count
            or page.classification == CLASSIFICATION_IMAGE_ONLY
        )
        if non_whitespace >= min_text_chars:
            buckets.covered.append(page.page_index)
            if page.classification == CLASSIFICATION_UNCERTAIN:
                buckets.low_text_pages.add(page.page_index)
            if visual_signal:
                buckets.text_pages_with_visuals.add(page.page_index)
                buckets.ocr_diagnostics.append(
                    {
                        "page_index": page.page_index,
                        "page_number": page.page_number,
                        "bucket": "covered",
                        "ocr_candidate": bool(page.ocr_candidate),
                        "visible_images": page.image_count,
                        "extracted_char_count": non_whitespace,
                        "reason": "useful_text_kept",
                    }
                )
            continue

        if visual_signal:
            buckets.needs_ocr.append(page.page_number)
            buckets.ocr_diagnostics.append(
                {
                    "page_index": page.page_index,
                    "page_number": page.page_number,
                    "bucket": "needs_ocr",
                    "ocr_candidate": bool(page.ocr_candidate),
                    "visible_images": page.image_count,
                    "extracted_char_count": non_whitespace,
                    "reason": (
                        "sparse_text_with_visuals"
                        if non_whitespace
                        else "no_text_layer"
                    ),
                }
            )
            continue

        buckets.covered.append(page.page_index)
        if page.classification == CLASSIFICATION_UNCERTAIN:
            buckets.low_text_pages.add(page.page_index)

    if buckets.needs_ocr:
        buckets.warnings.append("ocr_required_pages")
    if buckets.text_pages_with_visuals:
        buckets.warnings.append("text_pages_with_ocr_signal")
    if any(page.reason == PAGE_REASON_EXTRACTION_FAILED for page in buckets.skipped):
        buckets.warnings.append("extraction_failed_pages")
    if any(page.reason == PAGE_REASON_EMPTY for page in buckets.skipped):
        buckets.warnings.append("empty_pages_skipped")
    return buckets


def _first_warning(page: PageInspection) -> str | None:
    return page.warnings[0] if page.warnings else None


def _eligible_runs(
    spans: Sequence[_PageSpan], covered: Sequence[int]
) -> list[tuple[int, int]]:
    """Contiguous character ranges of consecutive covered pages.

    A skipped or OCR page breaks a run, so a chunk never carries text from a
    page that was classified into another bucket (keeps I1 and C2 compatible).
    """

    covered_set = set(covered)
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    run_end = 0
    for span in spans:
        if span.page_index in covered_set:
            if run_start is None:
                run_start = span.start
            run_end = span.end
            continue
        if run_start is not None:
            runs.append((run_start, run_end))
            run_start = None
    if run_start is not None:
        runs.append((run_start, run_end))
    return runs


def _page_index_for_offset(spans: Sequence[_PageSpan], offset: int) -> int:
    starts = [span.start for span in spans]
    position = bisect_right(starts, offset) - 1
    return spans[max(position, 0)].page_index


def _source_pages(
    spans: Sequence[_PageSpan], start: int, end: int
) -> list[dict[str, Any]]:
    """Per-page character shares of ``[start, end)``; they sum to ``end - start``."""

    sources: list[dict[str, Any]] = []
    for span in spans:
        overlap_start = max(start, span.start)
        overlap_end = min(end, span.end)
        if overlap_end <= overlap_start:
            continue
        sources.append(
            {
                "page_index": span.page_index,
                "page_number": span.page_number,
                "char_count": overlap_end - overlap_start,
            }
        )
    return sources


# --------------------------------------------------------------------------- #
# Boundary selection
# --------------------------------------------------------------------------- #
def _ends_sentence(token: str) -> bool:
    return bool(SENTENCE_END_RE.search(token.rstrip()))


def _line_breaks(text: str, spans: Sequence[tuple[int, int]]) -> list[bool]:
    """Whether the authoritative M2 text breaks a line right after each token.

    Whitespace tokens never contain a newline, so a line boundary can only be
    observed in the *gap* between two tokens. Deriving it from the M2 character
    ranges is what makes the ``line_end`` fallback real instead of dead code
    (OSJ D-03). The final token always reports True: the segment ends there.
    """

    breaks: list[bool] = []
    for index, span in enumerate(spans):
        if index + 1 >= len(spans):
            breaks.append(True)
            continue
        gap = text[span[1] : spans[index + 1][0]]
        breaks.append("\n" in gap or "\r" in gap)
    return breaks


def _choose_end(
    tokens: Sequence[str],
    start: int,
    *,
    target_tokens: int,
    max_tokens: int,
    line_breaks: Sequence[bool] | None = None,
) -> tuple[int, str]:
    """Pick the end token index and the boundary kind for one chunk.

    Preference order: a sentence end between 80 % of the target and the hard
    maximum, then a line end in the same window (read from the M2 character
    ranges via ``line_breaks``), then a hard cut at the maximum, then the end of
    the available tokens.
    """

    total = len(tokens)
    lower = start + max(1, int(target_tokens * SENTENCE_SEARCH_LOWER_RATIO))
    soft_cap = min(start + target_tokens, total)
    hard_cap = min(start + max_tokens, total)

    for index in range(soft_cap, lower - 1, -1):
        if index > start and _ends_sentence(tokens[index - 1]):
            return index, "sentence"
    for index in range(hard_cap, soft_cap, -1):
        if _ends_sentence(tokens[index - 1]):
            return index, "sentence"
    if line_breaks is not None:
        for index in range(hard_cap, lower - 1, -1):
            if index > start and line_breaks[index - 1]:
                return index, "line_end"
    if hard_cap < total:
        return hard_cap, "hard_cut"
    return total, "document_end"


@dataclass
class _Pending:
    """A chunk before its index (and therefore its id) is known."""

    start: int
    end: int
    section_path: list[str]
    heading: str | None
    section_inferred: bool
    boundary: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _section_label(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= SECTION_LABEL_MAX_CHARS:
        return collapsed
    return collapsed[: SECTION_LABEL_MAX_CHARS - 3].rstrip() + "..."


def _empty_tail_warning(ends_at: str) -> list[str]:
    return ["hard_cut_applied"] if ends_at == "hard_cut" else []


# --------------------------------------------------------------------------- #
# Strategy: fixed-token baseline
# --------------------------------------------------------------------------- #
def _chunk_fixed_token(
    full_text: str, runs: Sequence[tuple[int, int]], config: ChunkingConfig
) -> tuple[list[_Pending], dict[str, int]]:
    pendings: list[_Pending] = []
    counters = {"section_splits": 0}
    overlap_cap = config.effective_overlap

    for run_start, run_end in runs:
        run_text = full_text[run_start:run_end]
        spans = token_spans(run_text)
        if not spans:
            continue
        tokens = [run_text[start:end] for start, end in spans]
        breaks = _line_breaks(run_text, spans)
        total = len(tokens)
        start = 0
        previous_end: int | None = None

        while start < total:
            end, ends_at = _choose_end(
                tokens,
                start,
                target_tokens=config.target_tokens,
                max_tokens=config.max_tokens,
                line_breaks=breaks,
            )
            overlap_count = 0
            if previous_end is not None and start < previous_end:
                overlap_count = previous_end - start
            if overlap_count:
                starts_at = "overlap"
            elif start == 0:
                starts_at = "page_start"
            else:
                starts_at = "window"
            char_start = run_start + spans[start][0]
            char_end = run_start + spans[end - 1][1]
            pendings.append(
                _Pending(
                    start=char_start,
                    end=char_end,
                    section_path=[UNTITLED_SECTION],
                    heading=None,
                    section_inferred=True,
                    boundary={
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                        "overlap_token_estimate": overlap_count,
                        "origin": ORIGIN_SPLIT,
                    },
                    warnings=_empty_tail_warning(ends_at),
                )
            )
            previous_end = end
            if end >= total:
                break
            step = min(overlap_cap, end - start - 1)
            start = end - step
    return pendings, counters


# --------------------------------------------------------------------------- #
# Strategy: section-aware
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Section:
    start: int
    end: int
    section_path: list[str]
    heading: str | None
    heading_offset: int | None


def _build_sections(full_text: str, analysis: LayoutAnalysis) -> list[_Section]:
    """Turn verified headings into sections; unverified ones never split."""

    headings = [
        heading for heading in analysis.usable_headings if heading.m2_start is not None
    ]
    headings.sort(key=lambda heading: (heading.m2_start, heading.page_index))
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []

    if not headings:
        # No verified heading anywhere: the document is one untitled section and
        # text is never dropped for lacking a heading (Stage A 4.7.6).
        if full_text.strip():
            sections.append(
                _Section(
                    start=0,
                    end=len(full_text),
                    section_path=[UNTITLED_SECTION],
                    heading=None,
                    heading_offset=None,
                )
            )
        return sections

    first_offset = headings[0].m2_start if headings[0].m2_start is not None else 0
    if full_text[:first_offset].strip():
        sections.append(
            _Section(
                start=0,
                end=first_offset,
                section_path=[UNTITLED_SECTION],
                heading=None,
                heading_offset=None,
            )
        )

    for position, heading in enumerate(headings):
        offset = heading.m2_start if heading.m2_start is not None else 0
        if position + 1 < len(headings):
            following = headings[position + 1].m2_start
            next_offset = following if following is not None else len(full_text)
        else:
            next_offset = len(full_text)
        while stack and stack[-1][0] >= heading.level:
            stack.pop()
        label = heading.numbering or _section_label(heading.text)
        stack.append((heading.level, label))
        sections.append(
            _Section(
                start=offset,
                end=next_offset,
                section_path=[entry for _level, entry in stack],
                heading=heading.text,
                heading_offset=offset,
            )
        )
    return sections


def _split_segment(
    section: _Section,
    segment_start: int,
    segment_end: int,
    full_text: str,
    config: ChunkingConfig,
    *,
    is_last_segment: bool,
) -> tuple[list[tuple[int, int, dict[str, Any]]], int, int]:
    """Split one section segment into chunk ranges.

    Returns ``(ranges, split_points, overlap_tokens)`` where each range carries
    its ``boundary`` block. Overlap only ever applies between consecutive parts
    of this segment (same section, same covered-page run).
    """

    segment_text = full_text[segment_start:segment_end]
    spans = token_spans(segment_text)
    if not spans:
        return [], 0, 0
    tokens = [segment_text[start:end] for start, end in spans]
    breaks = _line_breaks(segment_text, spans)
    total = len(tokens)
    overlap_cap = config.effective_overlap
    heading_offset = section.heading_offset
    base_starts_at = (
        "heading"
        if heading_offset is not None and heading_offset == segment_start
        else ("section_start" if section.start == segment_start else "page_gap")
    )
    base_ends_at = "section_end" if is_last_segment else "page_gap"

    if total <= config.max_tokens:
        boundary = {
            "starts_at": base_starts_at,
            "ends_at": base_ends_at,
            "overlap_token_estimate": 0,
            "origin": ORIGIN_SECTION,
        }
        return [(segment_start, segment_end, boundary)], 0, 0

    parts: list[tuple[int, int, dict[str, Any]]] = []
    start = 0
    previous_end: int | None = None
    while start < total:
        end, ends_at = _choose_end(
            tokens,
            start,
            target_tokens=config.target_tokens,
            max_tokens=config.max_tokens,
            line_breaks=breaks,
        )
        overlap_count = 0
        if previous_end is not None and start < previous_end:
            overlap_count = previous_end - start
        starts_at = "overlap" if overlap_count else base_starts_at
        last_part = end >= total
        boundary = {
            "starts_at": starts_at,
            "ends_at": base_ends_at if last_part else ends_at,
            "overlap_token_estimate": overlap_count,
            "origin": ORIGIN_SPLIT,
        }
        parts.append(
            (
                segment_start + spans[start][0],
                segment_start + spans[end - 1][1],
                boundary,
            )
        )
        previous_end = end
        if last_part:
            break
        step = min(overlap_cap, end - start - 1)
        start = end - step

    split_points = len(parts) - 1
    overlap_total = sum(part[2]["overlap_token_estimate"] for part in parts)

    if len(parts) > 1:
        last_start, last_end, last_boundary = parts[-1]
        last_tokens = estimate_tokens(full_text[last_start:last_end])
        previous_start, _previous_end, previous_boundary = parts[-2]
        merged_tokens = estimate_tokens(full_text[previous_start:last_end])
        if (
            last_tokens < MIN_SECTION_FRAGMENT_TOKENS
            and merged_tokens <= config.max_tokens
        ):
            previous_boundary["ends_at"] = last_boundary["ends_at"]
            parts = parts[:-2] + [(previous_start, last_end, previous_boundary)]
            split_points -= 1
            overlap_total = sum(part[2]["overlap_token_estimate"] for part in parts)
    return parts, split_points, overlap_total


def _chunk_section_aware(
    full_text: str,
    runs: Sequence[tuple[int, int]],
    sections: Sequence[_Section],
    config: ChunkingConfig,
) -> tuple[list[_Pending], dict[str, int]]:
    pendings: list[_Pending] = []
    counters = {"section_splits": 0}

    for section in sections:
        segments = [
            (max(section.start, run_start), min(section.end, run_end))
            for run_start, run_end in runs
            if min(section.end, run_end) > max(section.start, run_start)
        ]
        if len(segments) > 1:
            counters["gapped_sections"] = counters.get("gapped_sections", 0) + 1
        for position, (segment_start, segment_end) in enumerate(segments):
            parts, split_points, _overlap = _split_segment(
                section,
                segment_start,
                segment_end,
                full_text,
                config,
                is_last_segment=position == len(segments) - 1,
            )
            counters["section_splits"] += split_points
            for part_start, part_end, boundary in parts:
                inferred = section.heading_offset is None or (
                    part_start != section.heading_offset
                )
                pendings.append(
                    _Pending(
                        start=part_start,
                        end=part_end,
                        section_path=list(section.section_path),
                        heading=section.heading,
                        section_inferred=inferred,
                        boundary=boundary,
                        warnings=_empty_tail_warning(boundary["ends_at"]),
                    )
                )
    return pendings, counters


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _chunk_id(
    document_id: str, strategy: str, fingerprint: str, index: int, text: str
) -> str:
    payload = f"{document_id}|{strategy}|{fingerprint}|{index}|{text}"
    return "sha256:" + _sha256_hex(payload)


def _finalise(
    pendings: Sequence[_Pending],
    *,
    full_text: str,
    spans: Sequence[_PageSpan],
    config: ChunkingConfig,
    fingerprint: str,
    inspection: DocumentInspection,
    low_text_pages: set[int],
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for index, pending in enumerate(pendings):
        text = full_text[pending.start : pending.end]
        sources = _source_pages(spans, pending.start, pending.end)
        warnings = list(pending.warnings)
        if (
            any(source["page_index"] in low_text_pages for source in sources)
            and "low_text_page" not in warnings
        ):
            warnings.append("low_text_page")
        chunks.append(
            Chunk(
                chunk_id=_chunk_id(
                    inspection.document_id, config.strategy, fingerprint, index, text
                ),
                document_id=inspection.document_id,
                filename=inspection.filename,
                strategy=config.strategy,
                chunker_version=config.chunker_version,
                config_fingerprint=fingerprint,
                chunk_index=index,
                text=text,
                char_count=len(text),
                token_estimate=estimate_tokens(text),
                token_estimate_method=config.token_estimate_method,
                page_index_start=sources[0]["page_index"],
                page_index_end=sources[-1]["page_index"],
                page_start=sources[0]["page_number"],
                page_end=sources[-1]["page_number"],
                section_path=list(pending.section_path),
                heading=pending.heading,
                section_inferred=pending.section_inferred,
                boundary=dict(pending.boundary),
                metadata=dict(config.metadata),
                source_pages=sources,
                warnings=warnings,
            )
        )
    return chunks


def _lower_median(values: Sequence[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def _build_stats(
    *,
    inspection: DocumentInspection,
    buckets: _Buckets,
    chunks: Sequence[Chunk],
    analysis: LayoutAnalysis | None,
    counters: Mapping[str, int],
) -> dict[str, Any]:
    estimates = [chunk.token_estimate for chunk in chunks]
    stats: dict[str, Any] = {
        "pages_inspected": len(inspection.pages),
        "pages_covered": len(buckets.covered),
        "pages_needs_ocr": len(buckets.needs_ocr),
        "pages_skipped": len(buckets.skipped),
        "chunks": len(chunks),
        "chunks_by_section": sum(
            1 for chunk in chunks if chunk.boundary.get("origin") == ORIGIN_SECTION
        ),
        "chunks_by_split": sum(
            1 for chunk in chunks if chunk.boundary.get("origin") == ORIGIN_SPLIT
        ),
        "token_estimate_min": min(estimates) if estimates else 0,
        "token_estimate_median": _lower_median(estimates),
        "token_estimate_max": max(estimates) if estimates else 0,
        "hard_cuts": sum(
            1 for chunk in chunks if chunk.boundary.get("ends_at") == "hard_cut"
        ),
        "section_splits": counters.get("section_splits", 0),
        "overlap_total_tokens": sum(
            chunk.boundary.get("overlap_token_estimate", 0) for chunk in chunks
        ),
        "layout_mapping_partial_pages": len(analysis.partial_pages) if analysis else 0,
        "unmapped_headings": len(analysis.unmapped_headings) if analysis else 0,
        "layout_exclusions": len(analysis.exclusions) if analysis else 0,
        "text_pages_with_ocr_signal": len(buckets.text_pages_with_visuals),
        "ocr_diagnostics": len(buckets.ocr_diagnostics),
    }
    return stats


def _check_invariants(
    stats: Mapping[str, Any], chunks: Sequence[Chunk], *, max_tokens: int
) -> None:
    """Reconciliation is asserted, not assumed (F1, OSJ C2/C4)."""

    bucket_total = (
        stats["pages_covered"] + stats["pages_needs_ocr"] + stats["pages_skipped"]
    )
    if bucket_total != stats["pages_inspected"]:
        raise ChunkingError(
            "page bucket reconciliation failed: "
            f"{bucket_total} != {stats['pages_inspected']} inspected"
        )
    origin_total = stats["chunks_by_section"] + stats["chunks_by_split"]
    if origin_total != stats["chunks"]:
        raise ChunkingError(
            "chunk origin reconciliation failed: "
            f"{origin_total} != {stats['chunks']} chunks"
        )
    for chunk in chunks:
        if chunk.token_estimate > max_tokens:
            raise ChunkingError(
                f"chunk {chunk.chunk_index} has {chunk.token_estimate} tokens, "
                f"above max_tokens={max_tokens}"
            )


def chunk_document(
    inspection: DocumentInspection,
    *,
    strategy: str = DEFAULT_STRATEGY,
    source_path: str | Path | None = None,
    layout: LayoutDocument | None = None,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int | None = None,
    min_text_chars: int = MIN_TEXT_CHARS,
    mapping_ratio_threshold: float = MAPPING_RATIO_THRESHOLD,
    per_heading_validation: bool = True,
    metadata: Mapping[str, Any] | None = None,
    chunker_version: str = CHUNKER_VERSION,
    password: str | None = None,
) -> ChunkingResult:
    """Chunk one inspected document and return the structured result.

    Args:
        inspection: the M2 result whose ``pages[].text`` is the only text source.
        strategy: ``"section_aware"`` (default) or ``"fixed_token"``.
        source_path: the PDF to read layout from. Required for ``section_aware``
            unless ``layout`` is supplied; its SHA256 is re-verified against
            ``inspection.sha256`` before any layout data is used.
        layout: pre-read layout (tests and callers that already opened the PDF).
        target_tokens / max_tokens: soft and hard size limits in whitespace
            tokens.
        overlap_tokens: shared tokens; defaults to 32 (section) / 64 (baseline).
        min_text_chars: threshold of non-whitespace characters that makes a
            page's extracted text useful. It is part of the fingerprint and the
            bucket decision (OSJ D-02): text at or above it is chunked even when
            M2 flagged the page ``ocr_candidate``.
        mapping_ratio_threshold: page-level layout mapping ratio below which
            section splitting is disabled for that page (OSJ Q1).
        per_heading_validation: recorded in the fingerprint. M3 still refuses an
            unmapped heading as a boundary either way (OSJ C3).
        metadata: operator-supplied ``equipment_model`` / ``revision``; never
            parsed from document text.
        chunker_version: overrides :data:`CHUNKER_VERSION` (invalidates every id).
        password: optional password for an encrypted PDF, used only to open the
            layout pass. It is never stored in the result, a warning or an
            exception message (OSJ D-04).

    Raises:
        ChunkingConfigError: an input is not acceptable.
        LayoutSourceRequiredError: section-aware chunking without a layout source.
        LayoutSourceMismatchError: the layout source is a different file.
        PdfEncryptedError: the layout pass needs a password that was not usable.
        ChunkingError: an internal reconciliation invariant was violated.

    Returns:
        A :class:`ChunkingResult` whose ``to_dict()`` is JSON-serialisable.
    """

    config = build_config(
        strategy=strategy,
        target_tokens=target_tokens,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
        min_text_chars=min_text_chars,
        mapping_ratio_threshold=mapping_ratio_threshold,
        per_heading_validation=per_heading_validation,
        metadata=metadata,
        chunker_version=chunker_version,
    )
    started = time.perf_counter()

    full_text = "".join(page.text for page in inspection.pages)
    spans = _page_spans(inspection)
    buckets = _bucket_pages(inspection, min_text_chars=config.min_text_chars)
    runs = _eligible_runs(spans, buckets.covered)

    analysis: LayoutAnalysis | None = None
    if config.strategy == STRATEGY_SECTION_AWARE:
        layout_data = layout
        if layout_data is None:
            if source_path is None:
                raise LayoutSourceRequiredError(
                    "section_aware chunking needs source_path or a pre-read layout"
                )
            layout_data = read_layout(
                source_path,
                expected_sha256=inspection.sha256,
                password=password,
            )
        analysis = analyse_layout(
            inspection,
            layout_data,
            page_offsets={span.page_index: (span.start, span.end) for span in spans},
            mapping_ratio_threshold=config.mapping_ratio_threshold,
        )

    if config.strategy == STRATEGY_FIXED_TOKEN:
        pendings, counters = _chunk_fixed_token(full_text, runs, config)
        sections: list[_Section] = []
    else:
        if analysis is None:
            raise LayoutSourceRequiredError(
                "section_aware chunking needs a layout analysis"
            )
        sections = _build_sections(full_text, analysis)
        pendings, counters = _chunk_section_aware(full_text, runs, sections, config)

    fingerprint = config.fingerprint()
    chunks = _finalise(
        pendings,
        full_text=full_text,
        spans=spans,
        config=config,
        fingerprint=fingerprint,
        inspection=inspection,
        low_text_pages=buckets.low_text_pages,
    )

    warnings: list[str] = list(buckets.warnings)
    if config.strategy == STRATEGY_FIXED_TOKEN:
        warnings.append("fixed_token_strategy_has_no_sections")
    if analysis is not None:
        warnings.extend(analysis.warnings)
        if not analysis.usable_headings:
            warnings.append("document_without_headings")
        elif any(section.heading is None for section in sections):
            warnings.append("preamble_without_heading")
        for heading in analysis.unmapped_headings:
            warnings.append(f"heading_unmapped:page={heading.page_number}")
    if counters.get("gapped_sections"):
        warnings.append("section_split_by_page_gap")
    if not chunks:
        warnings.append("document_has_no_chunks")

    stats = _build_stats(
        inspection=inspection,
        buckets=buckets,
        chunks=chunks,
        analysis=analysis,
        counters=counters,
    )
    _check_invariants(stats, chunks, max_tokens=config.max_tokens)

    duration_ms = int((time.perf_counter() - started) * 1000)
    return ChunkingResult(
        document_id=inspection.document_id,
        filename=inspection.filename,
        strategy=config.strategy,
        chunker_version=config.chunker_version,
        config_fingerprint=fingerprint,
        chunks=chunks,
        needs_ocr=list(buckets.needs_ocr),
        skipped_pages=list(buckets.skipped),
        warnings=warnings,
        stats=stats,
        duration_ms=duration_ms,
        layout_exclusions=[
            exclusion.as_dict()
            for exclusion in (analysis.exclusions if analysis else [])
        ],
        heading_diagnostics=[
            heading.as_dict() for heading in (analysis.headings if analysis else [])
        ],
        page_mapping=[page.as_dict() for page in (analysis.pages if analysis else [])],
        ocr_diagnostics=[dict(item) for item in buckets.ocr_diagnostics],
    )


def compare_strategies(
    inspection: DocumentInspection,
    *,
    source_path: str | Path | None = None,
    layout: LayoutDocument | None = None,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    chunker_version: str = CHUNKER_VERSION,
    password: str | None = None,
    min_text_chars: int = MIN_TEXT_CHARS,
) -> dict[str, Any]:
    """Run both strategies on one document and return comparable measurements.

    ``min_text_chars`` must travel with the comparison: it decides which pages
    count as useful text, so a comparison that silently used the default would
    report different buckets than the single-strategy path (OSJ D-05).
    """

    rows: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        result = chunk_document(
            inspection,
            strategy=strategy,
            source_path=source_path,
            layout=layout,
            target_tokens=target_tokens,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            metadata=metadata,
            chunker_version=chunker_version,
            password=password,
            min_text_chars=min_text_chars,
        )
        stats = result.stats
        sections = {
            tuple(chunk.section_path)
            for chunk in result.chunks
            if chunk.section_path != [UNTITLED_SECTION]
        }
        rows.append(
            {
                "strategy": strategy,
                "chunks": stats["chunks"],
                "chunks_by_section": stats["chunks_by_section"],
                "chunks_by_split": stats["chunks_by_split"],
                "token_estimate_min": stats["token_estimate_min"],
                "token_estimate_median": stats["token_estimate_median"],
                "token_estimate_max": stats["token_estimate_max"],
                "pages_covered": stats["pages_covered"],
                "pages_needs_ocr": stats["pages_needs_ocr"],
                "pages_skipped": stats["pages_skipped"],
                "hard_cuts": stats["hard_cuts"],
                "section_splits": stats["section_splits"],
                "sections": len(sections),
                "overlap_total_tokens": stats["overlap_total_tokens"],
            }
        )
    return {
        "document_id": inspection.document_id,
        "filename": inspection.filename,
        "target_tokens": target_tokens,
        "max_tokens": max_tokens,
        "min_text_chars": min_text_chars,
        "chunker_version": chunker_version,
        "strategies": rows,
    }


__all__ = [
    "CHUNKER_VERSION",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_STRATEGY",
    "DEFAULT_TARGET_TOKENS",
    "METADATA_KEYS",
    "MIN_SECTION_FRAGMENT_TOKENS",
    "STRATEGIES",
    "STRATEGY_FIXED_TOKEN",
    "STRATEGY_SECTION_AWARE",
    "UNTITLED_SECTION",
    "Chunk",
    "ChunkingConfig",
    "ChunkingResult",
    "SkippedPage",
    "build_config",
    "chunk_document",
    "compare_strategies",
    "normalise_metadata",
]
