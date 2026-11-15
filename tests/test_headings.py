"""Tests for the layout probe, heading detection and offset mapping.

Fixtures are synthetic: layout objects are constructed in memory so each rule can
be exercised exactly, and one PDF is generated at test runtime for the reader.
No vendor document is needed, stored or committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.chunking.errors import LayoutSourceMismatchError
from app.chunking.headings import (
    HEADER_BAND_PCT,
    MIN_PAGES_FOR_FURNITURE,
    LayoutDocument,
    LayoutLine,
    PageLayout,
    analyse_layout,
    body_font_size,
    detect_running_furniture,
    is_heading_candidate,
    map_page_lines,
    read_layout,
)
from app.ingestion.pdf_inspector import (
    CLASSIFICATION_TEXT,
    DocumentInspection,
    PageInspection,
    compute_sha256,
    inspect_pdf,
)

PAGE_WIDTH = 600.0
PAGE_HEIGHT = 800.0
BODY_SIZE = 10.0
HEADING_SIZE = 16.0
SUBHEADING_SIZE = 13.0
FURNITURE_SIZE = 8.0

HEADER_TEXT = "INDUSTRIAL RADAR MANUAL"
BODY_TEXT = (
    "The transmitter output is monitored by the control unit before every shift."
)
SECOND_BODY = (
    "Reduce power and inspect the antenna feeder before the next transmission."
)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def line(
    text: str,
    *,
    size: float = BODY_SIZE,
    bold: bool = False,
    y: float = 120.0,
    x: float = 72.0,
) -> LayoutLine:
    return LayoutLine(
        text=text,
        x0=x,
        y0=y,
        x1=x + 300.0,
        y1=y + size,
        size=size,
        bold=bold,
        block_no=0,
    )


def page_layout(page_index: int, lines: list[LayoutLine]) -> PageLayout:
    return PageLayout(
        page_index=page_index,
        page_number=page_index + 1,
        width=PAGE_WIDTH,
        height=PAGE_HEIGHT,
        lines=tuple(lines),
    )


def inspection_for(page_texts: list[str]) -> DocumentInspection:
    pages = []
    for index, text in enumerate(page_texts):
        pages.append(
            PageInspection(
                page_index=index,
                page_number=index + 1,
                text=text,
                char_count=len(text),
                non_whitespace_char_count=len("".join(text.split())),
                classification=CLASSIFICATION_TEXT,
                ocr_candidate=False,
                image_count=0,
            )
        )
    digest = "a" * 64
    return DocumentInspection(
        document_id=f"sha256:{digest}",
        filename="synthetic.pdf",
        sha256=digest,
        file_size_bytes=1024,
        page_count=len(page_texts),
        pages=pages,
    )


def two_page_document() -> tuple[DocumentInspection, LayoutDocument]:
    first = f"{HEADER_TEXT}\n1 Introduction\n{BODY_TEXT}\n"
    second = f"{HEADER_TEXT}\n2 Operation\n{BODY_TEXT}\n{SECOND_BODY}\n"
    inspection = inspection_for([first, second])
    layout = LayoutDocument(
        pages=(
            page_layout(
                0,
                [
                    line(HEADER_TEXT, size=FURNITURE_SIZE, y=40.0),
                    line("1 Introduction", size=HEADING_SIZE, bold=True),
                    line(BODY_TEXT),
                ],
            ),
            page_layout(
                1,
                [
                    line(HEADER_TEXT, size=FURNITURE_SIZE, y=40.0),
                    line("2 Operation", size=HEADING_SIZE, bold=True),
                    line(BODY_TEXT),
                    line(SECOND_BODY),
                ],
            ),
        )
    )
    return inspection, layout


def boundary_text(inspection: DocumentInspection) -> str:
    return "".join(page.text for page in inspection.pages)


# --------------------------------------------------------------------------- #
# Body size and candidacy
# --------------------------------------------------------------------------- #
def test_body_font_size_is_character_weighted() -> None:
    page = page_layout(
        0,
        [
            line("1 Introduction", size=HEADING_SIZE, bold=True),
            line("short heading line", size=HEADING_SIZE, bold=True, y=150.0),
            line(BODY_TEXT),
            line(SECOND_BODY, y=160.0),
        ],
    )
    assert body_font_size(page) == BODY_SIZE


def test_body_font_size_of_textless_page_is_zero() -> None:
    assert body_font_size(page_layout(0, [])) == 0.0


def test_body_font_size_tie_prefers_smaller_size() -> None:
    page = page_layout(
        0,
        [
            line("aaaa", size=12.0),
            line("bbbb", size=10.0, y=140.0),
        ],
    )
    assert body_font_size(page) == BODY_SIZE


@pytest.mark.parametrize(
    ("text", "size", "bold", "expected"),
    [
        ("1 Introduction", HEADING_SIZE, False, True),
        ("Operation", HEADING_SIZE, True, True),
        ("OPERATION", HEADING_SIZE, False, True),
        ("Operation.", HEADING_SIZE, False, False),
        ("Operation", BODY_SIZE + 1.0, True, False),
        ("x" * 81, HEADING_SIZE, True, False),
    ],
)
def test_heading_candidate_rules(
    text: str, size: float, bold: bool, *, expected: bool
) -> None:
    candidate = line(text, size=size, bold=bold)
    assert (
        is_heading_candidate(
            candidate, size_threshold=BODY_SIZE + 1.5, page_height=PAGE_HEIGHT
        )
        is expected
    )


def test_heading_candidate_rejects_margin_band_lines() -> None:
    in_header = line("1 Running Title", size=HEADING_SIZE, bold=True, y=10.0)
    in_footer = line("2 Page Footer", size=HEADING_SIZE, bold=True, y=780.0)
    for candidate in (in_header, in_footer):
        assert not is_heading_candidate(
            candidate, size_threshold=BODY_SIZE + 1.5, page_height=PAGE_HEIGHT
        )


def test_heading_candidate_rejects_running_furniture_text() -> None:
    candidate = line(HEADER_TEXT, size=HEADING_SIZE, bold=True, y=200.0)
    assert not is_heading_candidate(
        candidate,
        size_threshold=BODY_SIZE + 1.5,
        page_height=PAGE_HEIGHT,
        furniture_texts={HEADER_TEXT.lower()},
    )


# --------------------------------------------------------------------------- #
# Running furniture
# --------------------------------------------------------------------------- #
def test_running_furniture_needs_the_band_and_a_repeat() -> None:
    _inspection, layout = two_page_document()
    furniture = detect_running_furniture(layout)
    assert furniture[0] == {HEADER_TEXT.lower()}
    assert furniture[1] == {HEADER_TEXT.lower()}


def test_single_page_document_flags_no_furniture() -> None:
    layout = LayoutDocument(
        pages=(
            page_layout(
                0, [line(HEADER_TEXT, size=FURNITURE_SIZE, y=40.0), line(BODY_TEXT)]
            ),
        )
    )
    assert len(layout.pages) < MIN_PAGES_FOR_FURNITURE
    assert detect_running_furniture(layout)[0] == set()


def test_repeated_body_paragraph_is_not_furniture() -> None:
    inspection, layout = two_page_document()
    furniture = detect_running_furniture(layout)
    assert BODY_TEXT.lower() not in furniture[0]
    assert BODY_TEXT.lower() not in furniture[1]
    assert inspection.page_count == 2


# --------------------------------------------------------------------------- #
# Offset mapping
# --------------------------------------------------------------------------- #
def test_mapping_is_absolute_across_page_transitions() -> None:
    inspection, layout = two_page_document()
    first_offset = 0
    second_offset = len(inspection.pages[0].text)
    first_matches = map_page_lines(
        inspection.pages[0].text, first_offset, layout.pages[0].lines
    )
    second_matches = map_page_lines(
        inspection.pages[1].text, second_offset, layout.pages[1].lines
    )
    first_heading = first_matches[1]
    second_heading = second_matches[1]
    assert first_heading.m2_start == inspection.pages[0].text.index("1 Introduction")
    assert second_heading.m2_start == second_offset + inspection.pages[1].text.index(
        "2 Operation"
    )
    assert second_heading.m2_start > len(inspection.pages[0].text)


def test_repeated_identical_lines_map_to_distinct_offsets() -> None:
    page_text = f"{HEADER_TEXT}\nRepeat this check.\nRepeat this check.\n"
    lines = [line(HEADER_TEXT, size=FURNITURE_SIZE, y=40.0)]
    lines.append(line("Repeat this check."))
    lines.append(line("Repeat this check.", y=140.0))
    matches = map_page_lines(page_text, 0, lines)
    offsets = [match.m2_start for match in matches[1:]]
    assert offsets[0] is not None
    assert offsets[0] != offsets[1]
    assert page_text[offsets[1] : matches[2].m2_end] == "Repeat this check."


def test_whitespace_insensitive_fallback_maps_wrapped_line() -> None:
    page_text = "1 Introduction\nOutput monitor stays on\nduring the test.\n"
    lines = [
        line("1 Introduction", size=HEADING_SIZE, bold=True),
        line("Output monitor stays on during the test."),
    ]
    matches = map_page_lines(page_text, 0, lines)
    assert matches[0].match_kind == "exact"
    assert matches[1].match_kind == "whitespace_insensitive"
    assert (
        page_text[matches[1].m2_start : matches[1].m2_end]
        == "Output monitor stays on\nduring the test."
    )


def test_unmapped_line_is_reported_not_invented() -> None:
    page_text = "1 Introduction\nBody line.\n"
    lines = [line("1 Introduction", size=HEADING_SIZE, bold=True)]
    lines.append(line("Text that is not on the page"))
    lines.append(line("Body line.", y=160.0))
    matches = map_page_lines(page_text, 0, lines)
    assert matches[1].m2_start is None
    assert matches[1].match_kind == "unmapped"
    assert matches[2].m2_start is not None


# --------------------------------------------------------------------------- #
# Layout analysis
# --------------------------------------------------------------------------- #
def test_analysis_publishes_running_header_exclusion_and_keeps_text() -> None:
    inspection, layout = two_page_document()
    analysis = analyse_layout(inspection, layout)
    full_text = boundary_text(inspection)
    assert len(analysis.exclusions) == 2
    exclusion = analysis.exclusions[0]
    assert exclusion.reason == "running_header"
    assert exclusion.m2_start is not None
    assert exclusion.m2_end is not None
    assert full_text[exclusion.m2_start : exclusion.m2_end] == HEADER_TEXT
    assert HEADER_TEXT not in [heading.text for heading in analysis.headings]


def test_analysis_marks_headings_usable_with_verified_offsets() -> None:
    inspection, layout = two_page_document()
    analysis = analyse_layout(inspection, layout)
    usable = analysis.usable_headings
    assert [heading.text for heading in usable] == ["1 Introduction", "2 Operation"]
    assert all(heading.offset_source == "text_mapping" for heading in usable)
    assert [heading.level for heading in usable] == [1, 1]
    assert analysis.unmapped_headings == []


def test_analysis_numbering_depth_sets_heading_level() -> None:
    page_text = "2 Operation\n2.1 Power-up\n2.1.3 Detail\n" + BODY_TEXT + "\n"
    inspection = inspection_for([page_text])
    layout = LayoutDocument(
        pages=(
            page_layout(
                0,
                [
                    line("2 Operation", size=HEADING_SIZE, bold=True),
                    line("2.1 Power-up", size=SUBHEADING_SIZE, bold=True, y=150.0),
                    line("2.1.3 Detail", size=SUBHEADING_SIZE, bold=True, y=180.0),
                    line(BODY_TEXT, y=220.0),
                ],
            ),
        )
    )
    analysis = analyse_layout(inspection, layout)
    assert [heading.level for heading in analysis.headings] == [1, 2, 3]
    assert [heading.numbering for heading in analysis.headings] == [
        "2",
        "2.1",
        "2.1.3",
    ]


def test_analysis_partial_mapping_disables_section_boundary() -> None:
    page_text = "1 Introduction\nBody line.\n"
    inspection = inspection_for([page_text])
    layout = LayoutDocument(
        pages=(
            page_layout(
                0,
                [
                    line("1 Introduction", size=HEADING_SIZE, bold=True),
                    line("Absent line one"),
                    line("Absent line two", y=160.0),
                    line(BODY_TEXT, y=190.0),
                    line(SECOND_BODY, y=210.0),
                ],
            ),
        )
    )
    analysis = analyse_layout(inspection, layout, mapping_ratio_threshold=0.90)
    page = analysis.pages[0]
    assert page.mapping_ratio < 0.90
    assert page.mapping_partial is True
    heading = analysis.headings[0]
    assert heading.boundary_usable is False
    assert heading.reason == "layout_mapping_partial"
    assert any("layout_mapping_partial" in warning for warning in analysis.warnings)


BODY_LINES = tuple(
    f"Body line number {index} records the measured value in the log."
    for index in range(8)
)


def _body_line_specs(start_y: float) -> list[LayoutLine]:
    return [
        line(text, y=start_y + 12.0 * index) for index, text in enumerate(BODY_LINES)
    ]


def test_analysis_keeps_unmapped_heading_as_diagnostic_only() -> None:
    page_text = "1 Introduction\n" + "\n".join(BODY_LINES) + "\n"
    inspection = inspection_for([page_text])
    layout = LayoutDocument(
        pages=(
            page_layout(
                0,
                [
                    line("9 Ghost Heading", size=HEADING_SIZE, bold=True),
                    line("1 Introduction", size=HEADING_SIZE, bold=True, y=150.0),
                    *_body_line_specs(200.0),
                ],
            ),
        )
    )
    analysis = analyse_layout(inspection, layout)
    ghost = analysis.headings[0]
    assert ghost.text == "9 Ghost Heading"
    assert ghost.m2_start is None
    assert ghost.boundary_usable is False
    assert ghost.reason == "unmapped_offset"
    assert analysis.unmapped_headings == [ghost]
    assert [heading.text for heading in analysis.usable_headings] == ["1 Introduction"]
    assert analysis.pages[0].mapping_ratio >= 0.90


def test_repeated_identical_heading_creates_one_boundary_only() -> None:
    page_text = "1 Introduction\n" + "\n".join(BODY_LINES) + "\n"
    inspection = inspection_for([page_text])
    layout = LayoutDocument(
        pages=(
            page_layout(
                0,
                [
                    line("1 Introduction", size=HEADING_SIZE, bold=True),
                    line("1 Introduction", size=HEADING_SIZE, bold=True, y=150.0),
                    *_body_line_specs(200.0),
                ],
            ),
        )
    )
    analysis = analyse_layout(inspection, layout)
    assert len(analysis.headings) == 2
    assert analysis.headings[0].boundary_usable is True
    assert analysis.headings[1].boundary_usable is False
    assert analysis.headings[1].reason == "unmapped_offset"
    assert len(analysis.usable_headings) == 1


def test_analysis_flags_possible_multi_column_page() -> None:
    page_text = "Left column text\nRight column text\n"
    inspection = inspection_for([page_text])
    layout = LayoutDocument(
        pages=(
            page_layout(
                0,
                [
                    line("Left column text", x=72.0),
                    line("Right column text", x=320.0, y=140.0),
                ],
            ),
        )
    )
    analysis = analyse_layout(inspection, layout)
    assert any(
        "possible_multi_column_layout" in warning for warning in analysis.warnings
    )
    assert analysis.pages[0].mapped_lines == 2


# --------------------------------------------------------------------------- #
# Reader on a real PDF
# --------------------------------------------------------------------------- #
def build_pdf(path: Path) -> Path:
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 30), HEADER_TEXT, fontsize=8)
        page.insert_text((72, 120), "1 Introduction", fontsize=16, fontname="hebo")
        page.insert_text((72, 160), BODY_TEXT, fontsize=10)
        page.insert_text((72, 810), "Page 1", fontsize=8)
        document.save(str(path))
    finally:
        document.close()
    return path


def test_read_layout_reads_lines_and_verifies_digest(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "synthetic.pdf")
    digest = compute_sha256(pdf)
    layout = read_layout(pdf, expected_sha256=digest)
    assert len(layout.pages) == 1
    texts = [line.text for line in layout.pages[0].lines]
    assert "1 Introduction" in texts
    bold_lines = [line.text for line in layout.pages[0].lines if line.bold]
    assert "1 Introduction" in bold_lines
    assert layout.source_sha256 == digest


def test_read_layout_rejects_a_different_file(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "synthetic.pdf")
    with pytest.raises(LayoutSourceMismatchError, match="does not match"):
        read_layout(pdf, expected_sha256="b" * 64)


def test_real_pdf_heading_is_detected_and_usable(tmp_path: Path) -> None:
    pdf = build_pdf(tmp_path / "synthetic.pdf")
    inspection = inspect_pdf(str(pdf))
    layout = read_layout(pdf, expected_sha256=inspection.sha256)
    analysis = analyse_layout(inspection, layout)
    assert [heading.text for heading in analysis.usable_headings] == ["1 Introduction"]
    assert analysis.headings[0].m2_start == inspection.pages[0].text.index(
        "1 Introduction"
    )
    assert HEADER_BAND_PCT == 8.0
