"""Layout probe, heading detection and deterministic offset mapping (M3).

Why a separate pass instead of extending M2
-------------------------------------------
``app.ingestion.pdf_inspector`` is reviewed, merged and frozen: its output is
authoritative text plus page provenance, and nothing here re-extracts that text.
This module performs **one extra read** of the same PDF to obtain *line layout
only* (text, bbox, font size, bold flag) and then maps every layout line back
onto M2 character offsets.

Rules that are not negotiable (OSJ C1-C4, Q1-Q3)
------------------------------------------------
* Layout data proposes **heading candidates only**. No chunk boundary, page span
  or citation offset is ever derived from layout coordinates (C3).
* A heading candidate without a verified M2 character offset cannot create a
  section boundary. It is published as a diagnostic instead of being used.
* Running headers/footers are excluded from *heading candidacy* only; the
  characters stay inside the chunk text so provenance survives (F3).
* Mapping is validated twice: a page-level ratio (default 0.90, Q1) and a
  per-heading individual match. A page below the ratio falls back to fixed
  windows and is reported as ``layout_mapping_partial``.
* If ``source_path`` is given, its SHA256 is re-verified against
  ``inspection.sha256`` before any layout line is used, so the layout can never
  describe a different file than the text it is mapped onto.

Determinism
-----------
Lines are sorted by ``(y, x)`` rounded to two decimals, mapping is a forward
cursor walk (search at or after the cursor, then a whitespace-insensitive
retry), and every published number is derived from that single ordered pass.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from app.chunking.errors import LayoutSourceMismatchError
from app.ingestion.pdf_inspector import (
    DocumentInspection,
    PdfEncryptedError,
    PdfNotFoundError,
    compute_sha256,
)

#: A line whose bbox starts above this share of the page height is in the header
#: band. Documented constant, part of the heading rules and the fingerprint.
HEADER_BAND_PCT = 8.0

#: A line whose bbox ends below ``100 - FOOTER_BAND_PCT`` percent is a footer.
FOOTER_BAND_PCT = 8.0

#: A band line is running furniture when the same normalised text appears in a
#: band on at least this share of the document's pages.
REPEAT_THRESHOLD_PCT = 40.0

#: Repeat detection needs at least two pages: on a single page every band line
#: would trivially "repeat" 100 % of the time.
MIN_PAGES_FOR_FURNITURE = 2

#: A line counts as a heading when its font is at least this much larger than
#: the page's body size. Documented constant (Stage A D2), not a measured optimum.
HEADING_SIZE_DELTA_PT = 1.5

#: Longer lines are never headings, however large they are set.
HEADING_MAX_CHARS = 80

#: Default page-level mapping ratio (OSJ Q1: keep 0.90, add per-heading checks).
MAPPING_RATIO_THRESHOLD = 0.90

#: x positions of blocks more than this far apart count as separate columns.
X_CLUSTER_TOLERANCE_PT = 36.0

#: Two or more x-clusters raise ``possible_multi_column_layout``.
MIN_X_CLUSTERS_FOR_COLUMNS = 2

_NUMBERING_RE = re.compile(r"^(\d+(?:\.\d+)*)[.)]?(?:\s|$)")
_BOLD_FLAG = 16
_PROGRESS_EPSILON = 1e-9


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def _in_band(line: LayoutLine, page_height: float) -> bool:
    if page_height <= 0:
        return False
    return (
        line.y0 < page_height * HEADER_BAND_PCT / 100.0
        or line.y1 > page_height * (100.0 - FOOTER_BAND_PCT) / 100.0
    )


@dataclass(frozen=True)
class LayoutLine:
    """One visual line, read from the PDF for heading purposes only."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    bold: bool
    block_no: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "bbox": [self.x0, self.y0, self.x1, self.y1],
            "size": self.size,
            "bold": self.bold,
            "block_no": self.block_no,
        }


@dataclass(frozen=True)
class PageLayout:
    """Layout lines for one page, in deterministic ``(y, x)`` order."""

    page_index: int
    page_number: int
    width: float
    height: float
    lines: tuple[LayoutLine, ...] = ()
    possible_multi_column: bool = False


@dataclass(frozen=True)
class LayoutDocument:
    """Layout read for a whole document."""

    pages: tuple[PageLayout, ...] = ()
    warnings: tuple[str, ...] = ()
    source_sha256: str | None = None


@dataclass(frozen=True)
class LineMatch:
    """A layout line and where (if anywhere) it was found in the M2 page text.

    ``m2_start`` / ``m2_end`` are **absolute** offsets into the concatenation of
    all M2 page texts, so two pages never share a coordinate space.
    """

    line: LayoutLine
    m2_start: int | None
    m2_end: int | None
    match_kind: str  # "exact" | "whitespace_insensitive" | "unmapped"


@dataclass(frozen=True)
class LayoutExclusion:
    """A running header/footer kept out of heading candidacy (published, F3/Q2)."""

    page_index: int
    page_number: int
    text: str
    m2_start: int | None
    m2_end: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_number": self.page_number,
            "text": self.text,
            "m2_start": self.m2_start,
            "m2_end": self.m2_end,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HeadingCandidate:
    """A heading candidate with its verification state (C3).

    ``boundary_usable`` is the only field a caller may use to create a section
    boundary. Everything with ``boundary_usable=False`` is a diagnostic.
    """

    page_index: int
    page_number: int
    text: str
    level: int
    numbering: str | None
    size: float
    m2_start: int | None
    m2_end: int | None
    offset_source: str
    boundary_usable: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_number": self.page_number,
            "text": self.text,
            "level": self.level,
            "numbering": self.numbering,
            "size": self.size,
            "m2_start": self.m2_start,
            "m2_end": self.m2_end,
            "offset_source": self.offset_source,
            "boundary_usable": self.boundary_usable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PageLayoutAnalysis:
    """Per-page mapping outcome, published so the ratio is auditable."""

    page_index: int
    page_number: int
    mapped_lines: int
    total_lines: int
    exact_matches: int
    whitespace_matches: int
    mapping_ratio: float
    mapping_partial: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_index": self.page_index,
            "page_number": self.page_number,
            "mapped_lines": self.mapped_lines,
            "total_lines": self.total_lines,
            "exact_matches": self.exact_matches,
            "whitespace_matches": self.whitespace_matches,
            "mapping_ratio": self.mapping_ratio,
            "mapping_partial": self.mapping_partial,
        }


@dataclass
class LayoutAnalysis:
    """Everything layout could contribute, with verification states attached."""

    pages: list[PageLayoutAnalysis] = field(default_factory=list)
    headings: list[HeadingCandidate] = field(default_factory=list)
    exclusions: list[LayoutExclusion] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def usable_headings(self) -> list[HeadingCandidate]:
        return [heading for heading in self.headings if heading.boundary_usable]

    @property
    def unmapped_headings(self) -> list[HeadingCandidate]:
        return [heading for heading in self.headings if heading.m2_start is None]

    @property
    def partial_pages(self) -> list[int]:
        return [page.page_index for page in self.pages if page.mapping_partial]


# --------------------------------------------------------------------------- #
# Layout read
# --------------------------------------------------------------------------- #
def _is_bold_span(span: dict[str, Any]) -> bool:
    if int(span.get("flags", 0)) & _BOLD_FLAG:
        return True
    return "bold" in str(span.get("font", "")).lower()


def _x_cluster_count(lines: tuple[LayoutLine, ...]) -> int:
    starts = sorted({round(line.x0, 1) for line in lines})
    if not starts:
        return 0
    clusters = 1
    previous = starts[0]
    for value in starts[1:]:
        if value - previous > X_CLUSTER_TOLERANCE_PT:
            clusters += 1
        previous = value
    return clusters


def _page_layout(document: pymupdf.Document, page_index: int) -> PageLayout:
    page = document.load_page(page_index)
    raw = page.get_text("dict")
    lines: list[LayoutLine] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_no = int(block.get("number", 0))
        for raw_line in block.get("lines", []):
            spans = raw_line.get("spans", [])
            text = "".join(str(span.get("text", "")) for span in spans).strip()
            if not text:
                continue
            sizes = [float(span.get("size", 0.0)) for span in spans]
            bbox = raw_line.get("bbox") or (0.0, 0.0, 0.0, 0.0)
            lines.append(
                LayoutLine(
                    text=text,
                    x0=round(float(bbox[0]), 2),
                    y0=round(float(bbox[1]), 2),
                    x1=round(float(bbox[2]), 2),
                    y1=round(float(bbox[3]), 2),
                    size=round(max(sizes) if sizes else 0.0, 2),
                    bold=any(_is_bold_span(span) for span in spans),
                    block_no=block_no,
                )
            )
    lines.sort(key=lambda line: (line.y0, line.x0, line.text))
    ordered = tuple(lines)
    rect = page.rect
    return PageLayout(
        page_index=page_index,
        page_number=page_index + 1,
        width=round(float(rect.width), 2),
        height=round(float(rect.height), 2),
        lines=ordered,
        possible_multi_column=_x_cluster_count(ordered) >= MIN_X_CLUSTERS_FOR_COLUMNS,
    )


def read_layout(
    source_path: str | Path,
    *,
    expected_sha256: str | None = None,
    password: str | None = None,
) -> LayoutDocument:
    """Read line layout for ``source_path``, verifying its digest first.

    Args:
        source_path: the PDF the M2 inspection was produced from.
        expected_sha256: when given, the file is rejected unless its streaming
            SHA256 matches. The chunker always passes ``inspection.sha256`` so
            layout and text cannot describe different bytes.
        password: optional password for an encrypted document, used only for
            authentication. It is never stored in the result, a warning or an
            exception message (OSJ D-04).

    Raises:
        PdfNotFoundError: the path does not exist or is not a regular file.
        LayoutSourceMismatchError: the digest differs from ``expected_sha256``.
        PdfEncryptedError: the document is encrypted and no usable password was
            supplied, so the layout pass fails explicitly instead of returning a
            layout with no lines.
    """

    path = Path(source_path)
    if not path.exists() or not path.is_file():
        raise PdfNotFoundError(f"PDF file not found: {path}")
    actual_sha256 = compute_sha256(path)
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise LayoutSourceMismatchError(
            "layout source does not match the inspected document "
            f"(expected sha256 {expected_sha256}, found {actual_sha256})"
        )

    warnings: list[str] = []
    pages: list[PageLayout] = []
    document = pymupdf.open(str(path))
    try:
        if document.needs_pass:
            authenticated = bool(password) and bool(document.authenticate(password))
            if not authenticated:
                # The supplied password is never echoed back in the message.
                reason = (
                    "a password is required"
                    if not password
                    else "the supplied password was rejected"
                )
                raise PdfEncryptedError(f"{path.name!r} is encrypted: {reason}")
        for page_index in range(document.page_count):
            layout = _page_layout(document, page_index)
            pages.append(layout)
            if layout.possible_multi_column:
                warnings.append(f"possible_multi_column_layout:page={page_index + 1}")
    finally:
        document.close()
    return LayoutDocument(
        pages=tuple(pages),
        warnings=tuple(warnings),
        source_sha256=actual_sha256,
    )


# --------------------------------------------------------------------------- #
# Offset mapping
# --------------------------------------------------------------------------- #
def _stripped_index(text: str) -> tuple[str, list[int]]:
    """Return whitespace-free text plus a map back to original offsets."""

    kept: list[str] = []
    index: list[int] = []
    for position, character in enumerate(text):
        if character.isspace():
            continue
        kept.append(character)
        index.append(position)
    index.append(len(text))
    return "".join(kept), index


def _non_whitespace_prefix(text: str) -> list[int]:
    """Prefix counts of non-whitespace characters (index -> stripped position)."""

    prefix = [0] * (len(text) + 1)
    running = 0
    for position, character in enumerate(text):
        if not character.isspace():
            running += 1
        prefix[position + 1] = running
    return prefix


def map_page_lines(
    page_text: str, page_offset: int, lines: tuple[LayoutLine, ...] | list[LayoutLine]
) -> list[LineMatch]:
    """Map layout lines onto ``page_text`` character offsets, in read order.

    The walk is a forward cursor: a hit must start at or after the position the
    previous line ended at, which is what makes *repeated identical lines* map
    to distinct offsets instead of all landing on the first occurrence. A line
    that does not match exactly is retried whitespace-insensitively (line wraps
    and spacing differences), and only then reported unmapped.
    """

    stripped_text, index_map = _stripped_index(page_text)
    prefix = _non_whitespace_prefix(page_text)
    cursor = 0
    stripped_cursor = 0
    matches: list[LineMatch] = []

    for line in lines:
        needle = line.text.strip()
        if not needle:
            continue
        exact = page_text.find(needle, cursor)
        if exact >= 0:
            end = exact + len(needle)
            matches.append(
                LineMatch(
                    line=line,
                    m2_start=page_offset + exact,
                    m2_end=page_offset + end,
                    match_kind="exact",
                )
            )
            cursor = end
            stripped_cursor = prefix[end]
            continue

        stripped_needle = "".join(needle.split())
        found = (
            stripped_text.find(stripped_needle, stripped_cursor)
            if stripped_needle
            else -1
        )
        if found >= 0:
            start = index_map[found]
            end = index_map[found + len(stripped_needle) - 1] + 1
            if start >= cursor:
                matches.append(
                    LineMatch(
                        line=line,
                        m2_start=page_offset + start,
                        m2_end=page_offset + end,
                        match_kind="whitespace_insensitive",
                    )
                )
                cursor = end
                stripped_cursor = found + len(stripped_needle)
                continue

        matches.append(
            LineMatch(line=line, m2_start=None, m2_end=None, match_kind="unmapped")
        )
    return matches


# --------------------------------------------------------------------------- #
# Heading detection
# --------------------------------------------------------------------------- #
def body_font_size(page: PageLayout) -> float:
    """Character-weighted mode of the page's line sizes (0.0 for a textless page).

    Weighting by characters instead of by line count keeps a heading-heavy page
    from promoting its headings to "body": a 20-character heading carries far
    less weight than a page of 80-character body lines.
    """

    weights: Counter[float] = Counter()
    for line in page.lines:
        weights[round(line.size, 2)] += len(line.text)
    if not weights:
        return 0.0
    return max(weights.items(), key=lambda item: (item[1], -item[0]))[0]


def detect_running_furniture(layout: LayoutDocument) -> dict[int, set[str]]:
    """Return, per page, the normalised band texts that repeat often enough.

    Only lines inside the header/footer bands are considered and only when the
    document has at least :data:`MIN_PAGES_FOR_FURNITURE` pages. Scoping the
    repeat test to the bands is deliberate: a naive "this line appears on many
    pages" test also flags duplicated body paragraphs.
    """

    page_count = len(layout.pages)
    furniture: dict[int, set[str]] = {page.page_index: set() for page in layout.pages}
    if page_count < MIN_PAGES_FOR_FURNITURE:
        return furniture

    appearances: Counter[str] = Counter()
    per_page: dict[int, set[str]] = {}
    for page in layout.pages:
        texts = {
            _normalise(line.text)
            for line in page.lines
            if _in_band(line, page.height) and _normalise(line.text)
        }
        per_page[page.page_index] = texts
        for text in texts:
            appearances[text] += 1

    needed = page_count * REPEAT_THRESHOLD_PCT / 100.0
    repeated = {text for text, count in appearances.items() if count >= needed}
    for page_index, texts in per_page.items():
        furniture[page_index] = texts & repeated
    return furniture


def _heading_level(numbering: str | None, size_rank: int) -> int:
    if numbering:
        return numbering.count(".") + 1
    return size_rank


def is_heading_candidate(
    line: LayoutLine,
    *,
    size_threshold: float,
    page_height: float,
    furniture_texts: set[str] | None = None,
) -> bool:
    """Apply the documented heading heuristics (Stage A 4.7, OSJ-accepted).

    A candidate must be large enough, short enough, free of a terminal period,
    outside the margin bands, not running furniture, and additionally show an
    accepted signal: section numbering, a bold face, or ALL CAPS.
    """

    text = line.text.strip()
    if not text or len(text) > HEADING_MAX_CHARS:
        return False
    if text.endswith("."):
        return False
    if _in_band(line, page_height):
        return False
    if furniture_texts and _normalise(text) in furniture_texts:
        return False
    if line.size + _PROGRESS_EPSILON < size_threshold:
        return False
    if _NUMBERING_RE.match(text):
        return True
    if line.bold:
        return True
    letters = [character for character in text if character.isalpha()]
    return len(letters) >= 2 and text.upper() == text


def _page_offsets(inspection: DocumentInspection) -> dict[int, tuple[int, int]]:
    offsets: dict[int, tuple[int, int]] = {}
    cursor = 0
    for page in inspection.pages:
        offsets[page.page_index] = (cursor, cursor + len(page.text))
        cursor += len(page.text)
    return offsets


def analyse_layout(
    inspection: DocumentInspection,
    layout: LayoutDocument,
    *,
    page_offsets: dict[int, tuple[int, int]] | None = None,
    mapping_ratio_threshold: float = MAPPING_RATIO_THRESHOLD,
) -> LayoutAnalysis:
    """Map layout onto M2 offsets and keep only verified heading candidates.

    Per page: map every line, publish the ratio, and disable section splitting
    when the ratio falls below ``mapping_ratio_threshold``. Per heading: require
    an individual verified offset (Q1) and a strictly increasing position, so a
    repeated line can never produce two boundaries at one place (C3).
    """

    offsets = page_offsets if page_offsets is not None else _page_offsets(inspection)
    pages_by_index = {page.page_index: page for page in inspection.pages}
    furniture = detect_running_furniture(layout)

    analysis = LayoutAnalysis()
    last_boundary = -1

    for layout_page in layout.pages:
        inspection_page = pages_by_index.get(layout_page.page_index)
        if inspection_page is None:
            analysis.warnings.append(
                f"layout_page_without_inspection:page={layout_page.page_number}"
            )
            continue
        page_start, _page_end = offsets[layout_page.page_index]
        matches = map_page_lines(inspection_page.text, page_start, layout_page.lines)
        total_lines = len(matches)
        mapped = [match for match in matches if match.m2_start is not None]
        exact = sum(1 for match in matches if match.match_kind == "exact")
        whitespace = sum(
            1 for match in matches if match.match_kind == "whitespace_insensitive"
        )
        ratio = (len(mapped) / total_lines) if total_lines else 1.0
        partial = (
            bool(total_lines) and ratio + _PROGRESS_EPSILON < mapping_ratio_threshold
        )

        analysis.pages.append(
            PageLayoutAnalysis(
                page_index=layout_page.page_index,
                page_number=layout_page.page_number,
                mapped_lines=len(mapped),
                total_lines=total_lines,
                exact_matches=exact,
                whitespace_matches=whitespace,
                mapping_ratio=round(ratio, 6),
                mapping_partial=partial,
            )
        )
        if partial:
            analysis.warnings.append(
                f"layout_mapping_partial:page={layout_page.page_number}"
            )
        multi_column = layout_page.possible_multi_column or (
            _x_cluster_count(layout_page.lines) >= MIN_X_CLUSTERS_FOR_COLUMNS
        )
        if multi_column:
            analysis.warnings.append(
                f"possible_multi_column_layout:page={layout_page.page_number}"
            )

        furniture_texts = furniture.get(layout_page.page_index, set())
        for match in matches:
            text = _normalise(match.line.text)
            if (
                match.line.text
                and _in_band(match.line, layout_page.height)
                and text in furniture_texts
            ):
                reason = (
                    "running_header"
                    if match.line.y0 < layout_page.height * HEADER_BAND_PCT / 100.0
                    else "running_footer"
                )
                analysis.exclusions.append(
                    LayoutExclusion(
                        page_index=layout_page.page_index,
                        page_number=layout_page.page_number,
                        text=match.line.text,
                        m2_start=match.m2_start,
                        m2_end=match.m2_end,
                        reason=reason,
                    )
                )

        body_size = body_font_size(layout_page)
        threshold = body_size + HEADING_SIZE_DELTA_PT
        candidates: list[LineMatch] = [
            match
            for match in matches
            if is_heading_candidate(
                match.line,
                size_threshold=threshold,
                page_height=layout_page.height,
                furniture_texts=furniture_texts,
            )
        ]
        size_order = sorted({match.line.size for match in candidates}, reverse=True)
        for match in candidates:
            numbering_match = _NUMBERING_RE.match(match.line.text.strip())
            numbering = numbering_match.group(1) if numbering_match else None
            size_rank = size_order.index(match.line.size) + 1
            level = _heading_level(numbering, size_rank)
            usable = match.m2_start is not None and not partial
            reason: str | None = None
            if match.m2_start is None:
                reason = "unmapped_offset"
            elif partial:
                reason = "layout_mapping_partial"
            elif match.m2_start <= last_boundary:
                usable = False
                reason = "non_monotonic_offset"
            if usable and match.m2_start is not None:
                last_boundary = match.m2_start
            analysis.headings.append(
                HeadingCandidate(
                    page_index=layout_page.page_index,
                    page_number=layout_page.page_number,
                    text=match.line.text.strip(),
                    level=level,
                    numbering=numbering,
                    size=match.line.size,
                    m2_start=match.m2_start,
                    m2_end=match.m2_end,
                    offset_source=(
                        "text_mapping" if match.m2_start is not None else "unmapped"
                    ),
                    boundary_usable=usable,
                    reason=reason,
                )
            )

    return analysis


__all__ = [
    "FOOTER_BAND_PCT",
    "HEADER_BAND_PCT",
    "HEADING_MAX_CHARS",
    "HEADING_SIZE_DELTA_PT",
    "MAPPING_RATIO_THRESHOLD",
    "REPEAT_THRESHOLD_PCT",
    "HeadingCandidate",
    "LayoutAnalysis",
    "LayoutDocument",
    "LayoutExclusion",
    "LayoutLine",
    "LineMatch",
    "PageLayout",
    "PageLayoutAnalysis",
    "analyse_layout",
    "body_font_size",
    "detect_running_furniture",
    "is_heading_candidate",
    "map_page_lines",
    "read_layout",
]
