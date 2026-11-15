"""Tests for app.chunking.chunker.

Every fixture is synthetic and built at test runtime: the M2 inspection and the
layout are constructed in memory, so each contract rule (C1-C4, Q1-Q3) is
exercised exactly, offline, with no vendor document and no PDF needed. One
end-to-end test uses a generated PDF to prove the reader integration.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.chunking.chunker import (
    CHUNKER_VERSION,
    MIN_SECTION_FRAGMENT_TOKENS,
    STRATEGIES,
    STRATEGY_FIXED_TOKEN,
    STRATEGY_SECTION_AWARE,
    chunk_document,
    compare_strategies,
)
from app.chunking.errors import (
    ChunkingConfigError,
    LayoutSourceMismatchError,
    LayoutSourceRequiredError,
)
from app.chunking.headings import LayoutDocument, LayoutLine, PageLayout
from app.ingestion.pdf_inspector import (
    CLASSIFICATION_EMPTY,
    CLASSIFICATION_IMAGE_ONLY,
    CLASSIFICATION_TEXT,
    CLASSIFICATION_UNCERTAIN,
    DocumentInspection,
    PageInspection,
    PdfEncryptedError,
    inspect_pdf,
)

PAGE_WIDTH = 600.0
PAGE_HEIGHT = 800.0
SIZES = {
    "header": 8.0,
    "footer": 8.0,
    "heading": 16.0,
    "subheading": 13.0,
    "body": 10.0,
}
HEADER = "INDUSTRIAL RADAR MANUAL"
BODY = "The transmitter output is monitored by the control unit before every shift."
SECOND_BODY = (
    "Reduce power and inspect the antenna feeder before the next transmission."
)
REQUIRED_RESULT_KEYS = {
    "document_id",
    "filename",
    "strategy",
    "chunker_version",
    "config_fingerprint",
    "chunks",
    "needs_ocr",
    "skipped_pages",
    "warnings",
    "stats",
    "duration_ms",
}
REQUIRED_CHUNK_KEYS = {
    "chunk_id",
    "document_id",
    "filename",
    "strategy",
    "chunker_version",
    "config_fingerprint",
    "chunk_index",
    "text",
    "char_count",
    "token_estimate",
    "token_estimate_method",
    "page_index_start",
    "page_index_end",
    "page_start",
    "page_end",
    "section_path",
    "heading",
    "section_inferred",
    "boundary",
    "metadata",
    "source_pages",
    "warnings",
}


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def build_document(
    pages: list[list[tuple[str, str]]],
) -> tuple[DocumentInspection, LayoutDocument]:
    """Build a matching M2 inspection and layout from one page spec.

    Each page is a list of ``(kind, text)`` blocks; the page text is exactly the
    blocks joined by newlines, so every layout line is an exact substring and the
    mapping is 100 % unless a test deliberately breaks it.
    """

    texts: list[str] = []
    layouts: list[PageLayout] = []
    for page_index, blocks in enumerate(pages):
        texts.append("\n".join(text for _kind, text in blocks) + "\n")
        lines: list[LayoutLine] = []
        content_y = 120.0
        for kind, text in blocks:
            size = SIZES[kind]
            if kind == "header":
                y = 40.0
            elif kind == "footer":
                y = 780.0
            else:
                y = content_y
                content_y += size + 10.0
            lines.append(
                LayoutLine(
                    text=text,
                    x0=72.0,
                    y0=y,
                    x1=372.0,
                    y1=y + size,
                    size=size,
                    bold=kind in {"heading", "subheading"},
                    block_no=0,
                )
            )
        layouts.append(
            PageLayout(
                page_index=page_index,
                page_number=page_index + 1,
                width=PAGE_WIDTH,
                height=PAGE_HEIGHT,
                lines=tuple(lines),
            )
        )

    digest = "a" * 64
    inspection_pages = [
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
        for index, text in enumerate(texts)
    ]
    inspection = DocumentInspection(
        document_id=f"sha256:{digest}",
        filename="synthetic.pdf",
        sha256=digest,
        file_size_bytes=4096,
        page_count=len(texts),
        pages=inspection_pages,
    )
    return inspection, LayoutDocument(pages=tuple(layouts))


def manual_document() -> tuple[DocumentInspection, LayoutDocument]:
    return build_document(
        [
            [("header", HEADER), ("heading", "1 Introduction"), ("body", BODY)],
            [
                ("header", HEADER),
                ("heading", "2 Operation"),
                ("subheading", "2.1 Power-up sequence"),
                ("body", BODY),
            ],
            [("header", HEADER), ("heading", "3 Maintenance"), ("body", BODY)],
        ]
    )


def sentence(index: int, words: int = 20) -> str:
    return " ".join(["value"] * (words - 1)) + f" end{index}."


def long_body(sentences: int) -> str:
    return " ".join(sentence(index) for index in range(sentences))


def concat_text(inspection: DocumentInspection) -> str:
    return "".join(page.text for page in inspection.pages)


def mark_image_only(inspection: DocumentInspection, page_index: int) -> None:
    page = inspection.pages[page_index]
    page.classification = CLASSIFICATION_IMAGE_ONLY
    page.ocr_candidate = True
    page.image_count = 1
    page.text = ""
    page.char_count = 0
    page.non_whitespace_char_count = 0


def mark_empty(inspection: DocumentInspection, page_index: int) -> None:
    page = inspection.pages[page_index]
    page.classification = CLASSIFICATION_EMPTY
    page.text = "\n"
    page.char_count = 1
    page.non_whitespace_char_count = 0


def mark_failed(inspection: DocumentInspection, page_index: int) -> None:
    page = inspection.pages[page_index]
    page.status = "extraction_failed"
    page.classification = CLASSIFICATION_UNCERTAIN
    page.text = ""
    page.char_count = 0
    page.non_whitespace_char_count = 0
    page.warnings = ["page_extraction_failed:RuntimeError"]


def mark_uncertain(inspection: DocumentInspection, page_index: int) -> None:
    page = inspection.pages[page_index]
    page.classification = CLASSIFICATION_UNCERTAIN


def chunk(inspection, layout, **kwargs):
    return chunk_document(inspection, layout=layout, **kwargs)


def non_untitled(result):
    return [item for item in result.chunks if item.section_path != ["untitled"]]


# --------------------------------------------------------------------------- #
# F1: result contract
# --------------------------------------------------------------------------- #
def test_result_and_chunk_schema() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    payload = result.as_dict()
    assert REQUIRED_RESULT_KEYS <= set(payload)
    assert payload["strategy"] == STRATEGY_SECTION_AWARE
    assert payload["chunker_version"] == CHUNKER_VERSION
    assert payload["document_id"] == inspection.document_id
    assert REQUIRED_CHUNK_KEYS <= set(payload["chunks"][0])
    assert json.loads(result.to_json())["chunks"][0]["chunk_index"] == 0
    assert result.to_dict() == payload
    assert isinstance(result.duration_ms, int)


def test_stats_keys_and_reconciliation() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    stats = result.stats
    expected_keys = {
        "pages_inspected",
        "pages_covered",
        "pages_needs_ocr",
        "pages_skipped",
        "chunks",
        "chunks_by_section",
        "chunks_by_split",
        "token_estimate_min",
        "token_estimate_median",
        "token_estimate_max",
        "hard_cuts",
        "section_splits",
        "overlap_total_tokens",
    }
    assert expected_keys <= set(stats)
    assert (
        stats["pages_covered"] + stats["pages_needs_ocr"] + stats["pages_skipped"]
        == stats["pages_inspected"]
        == 3
    )
    assert stats["chunks_by_section"] + stats["chunks_by_split"] == stats["chunks"]
    assert stats["chunks"] == len(result.chunks)


def test_chunk_indices_are_contiguous_and_ids_unique() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    assert [item.chunk_index for item in result.chunks] == list(
        range(len(result.chunks))
    )
    assert len(set(result.chunk_ids())) == len(result.chunks)


# --------------------------------------------------------------------------- #
# Strategies: fixed-token baseline
# --------------------------------------------------------------------------- #
def test_fixed_token_respects_target_and_max() -> None:
    inspection, _layout = build_document(
        [[("body", long_body(20))], [("body", long_body(20))]]
    )
    result = chunk_document(
        inspection,
        strategy=STRATEGY_FIXED_TOKEN,
        target_tokens=100,
        max_tokens=120,
        overlap_tokens=0,
    )
    assert len(result.chunks) > 1
    assert all(item.token_estimate <= 120 for item in result.chunks)
    assert all(item.token_estimate <= 100 for item in result.chunks)
    assert all(item.section_path == ["untitled"] for item in result.chunks)
    assert all(item.heading is None for item in result.chunks)
    assert all(item.boundary["origin"] == "split" for item in result.chunks)
    assert "fixed_token_strategy_has_no_sections" in result.warnings
    assert result.page_mapping == []
    assert result.layout_exclusions == []


def test_fixed_token_overlap_equals_configured() -> None:
    inspection, _layout = build_document([[("body", long_body(20))]])
    result = chunk_document(
        inspection,
        strategy=STRATEGY_FIXED_TOKEN,
        target_tokens=80,
        max_tokens=100,
        overlap_tokens=10,
    )
    overlaps = [item.boundary["overlap_token_estimate"] for item in result.chunks]
    assert overlaps[0] == 0
    assert all(value <= 10 for value in overlaps)
    assert max(overlaps) == 10
    assert result.stats["overlap_total_tokens"] == sum(overlaps)


def test_fixed_token_spans_page_boundaries() -> None:
    inspection, _layout = build_document(
        [[("body", sentence(1) + " " + sentence(2))], [("body", sentence(3))]]
    )
    result = chunk_document(
        inspection,
        strategy=STRATEGY_FIXED_TOKEN,
        target_tokens=100,
        max_tokens=120,
        overlap_tokens=0,
    )
    assert len(result.chunks) == 1
    only = result.chunks[0]
    assert only.page_index_start == 0
    assert only.page_index_end == 1
    assert only.page_start == 1
    assert only.page_end == 2
    assert [page["page_number"] for page in only.source_pages] == [1, 2]


# --------------------------------------------------------------------------- #
# Strategies: section-aware
# --------------------------------------------------------------------------- #
def test_sections_follow_verified_headings() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    sections = non_untitled(result)
    assert [item.heading for item in sections] == [
        "1 Introduction",
        "2 Operation",
        "2.1 Power-up sequence",
        "3 Maintenance",
    ]
    assert [item.section_path for item in sections] == [
        ["1"],
        ["2"],
        ["2", "2.1"],
        ["3"],
    ]
    assert [item.section_inferred for item in sections] == [False, False, False, False]
    assert [item.boundary["starts_at"] for item in sections] == ["heading"] * 4


def test_section_chunks_never_cross_a_heading() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    headings = [
        "1 Introduction",
        "2 Operation",
        "2.1 Power-up sequence",
        "3 Maintenance",
    ]
    for item in result.chunks:
        own = item.heading
        others = [heading for heading in headings if heading != own]
        assert not any(other in item.text for other in others)


def test_preamble_and_header_text_are_kept_not_dropped() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    preamble = [item for item in result.chunks if item.section_path == ["untitled"]]
    assert len(preamble) == 1
    assert HEADER in preamble[0].text
    assert "preamble_without_heading" in result.warnings


def test_running_header_is_excluded_from_headings_but_stays_in_text() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    full_text = concat_text(inspection)
    assert HEADER not in {item.heading for item in result.chunks}
    assert len(result.layout_exclusions) == 3
    for exclusion in result.layout_exclusions:
        assert exclusion["reason"] == "running_header"
        assert exclusion["text"] == HEADER
        assert full_text[exclusion["m2_start"] : exclusion["m2_end"]] == HEADER
    assert any(HEADER in item.text for item in result.chunks)


def test_section_continuation_after_page_gap_inherits_section() -> None:
    inspection, layout = build_document(
        [
            [("header", HEADER), ("heading", "3 Maintenance"), ("body", BODY)],
            [("header", HEADER), ("body", BODY)],
            [("header", HEADER), ("body", SECOND_BODY)],
        ]
    )
    mark_image_only(inspection, 1)
    result = chunk(inspection, layout)
    maintenance = [item for item in result.chunks if item.heading == "3 Maintenance"]
    assert len(maintenance) == 2
    assert maintenance[0].section_inferred is False
    assert maintenance[0].boundary["starts_at"] == "heading"
    assert maintenance[1].section_inferred is True
    assert maintenance[1].boundary["starts_at"] == "page_gap"
    assert maintenance[1].boundary["overlap_token_estimate"] == 0
    assert maintenance[1].page_start == 3
    assert "section_split_by_page_gap" in result.warnings
    assert result.needs_ocr == [2]


def test_oversized_section_is_split_without_exceeding_max() -> None:
    inspection, layout = build_document(
        [
            [
                ("header", HEADER),
                ("heading", "4 Maintenance"),
                ("body", long_body(20)),
            ]
        ]
    )
    result = chunk(
        inspection, layout, target_tokens=60, max_tokens=80, overlap_tokens=10
    )
    split_chunks = non_untitled(result)
    assert len(split_chunks) > 1
    assert all(item.token_estimate <= 80 for item in result.chunks)
    assert all(item.boundary["origin"] == "split" for item in split_chunks)
    assert split_chunks[0].boundary["starts_at"] == "heading"
    assert split_chunks[1].boundary["starts_at"] == "overlap"
    assert split_chunks[-1].boundary["ends_at"] == "section_end"
    assert result.stats["section_splits"] >= 1
    assert result.stats["hard_cuts"] == 0
    assert all(item.boundary["overlap_token_estimate"] <= 10 for item in result.chunks)


def test_trailing_fragment_is_merged_backwards() -> None:
    inspection, layout = build_document(
        [[("header", HEADER), ("heading", "5 Checks"), ("body", long_body(7))]]
    )
    result = chunk_document(
        inspection,
        layout=layout,
        target_tokens=60,
        max_tokens=80,
        overlap_tokens=0,
    )
    section_chunks = non_untitled(result)
    assert len(section_chunks) == 2
    # Part one carries the two heading tokens plus 60 body tokens; the 20-token
    # trailing fragment was merged into part two, which reaches exactly max (80).
    assert [item.token_estimate for item in section_chunks] == [62, 80]
    assert all(
        item.token_estimate >= MIN_SECTION_FRAGMENT_TOKENS for item in section_chunks
    )
    assert section_chunks[-1].boundary["ends_at"] == "section_end"


def test_overlap_is_zero_across_section_boundaries() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout, overlap_tokens=10)
    for item in result.chunks:
        if item.boundary["starts_at"] in {"heading", "page_gap", "section_start"}:
            assert item.boundary["overlap_token_estimate"] == 0
        assert item.boundary["overlap_token_estimate"] <= 10


def test_low_text_page_warning_on_chunk() -> None:
    inspection, layout = build_document(
        [[("header", HEADER), ("heading", "6 Detail"), ("body", BODY)]]
    )
    mark_uncertain(inspection, 0)
    result = chunk(inspection, layout)
    assert any("low_text_page" in item.warnings for item in result.chunks)


# --------------------------------------------------------------------------- #
# Page buckets (C2)
# --------------------------------------------------------------------------- #
def test_page_buckets_are_mutually_exclusive() -> None:
    inspection, layout = build_document(
        [
            [("body", BODY)],
            [("body", BODY)],
            [("body", BODY)],
            [("body", BODY)],
            [("body", SECOND_BODY)],
        ]
    )
    mark_image_only(inspection, 1)
    mark_empty(inspection, 2)
    mark_failed(inspection, 3)
    result = chunk_document(
        inspection, strategy=STRATEGY_FIXED_TOKEN, target_tokens=100, max_tokens=120
    )
    assert result.needs_ocr == [2]
    reasons = {page.page_number: page.reason for page in result.skipped_pages}
    assert reasons == {3: "empty", 4: "extraction_failed"}
    assert result.stats["pages_covered"] == 2
    assert result.stats["pages_needs_ocr"] == 1
    assert result.stats["pages_skipped"] == 2
    assert "ocr_required_pages" in result.warnings
    assert "extraction_failed_pages" in result.warnings
    assert "empty_pages_skipped" in result.warnings
    used_pages = {
        source["page_index"] for item in result.chunks for source in item.source_pages
    }
    assert used_pages == {0, 4}
    assert layout.pages[3].page_number == 4


# --------------------------------------------------------------------------- #
# Provenance (I1, I2, I6)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("strategy", list(STRATEGIES))
def test_every_chunk_is_a_contiguous_substring_with_exact_page_shares(
    strategy: str,
) -> None:
    inspection, layout = manual_document()
    mark_image_only(inspection, 2)
    result = chunk(inspection, layout, strategy=strategy)
    full_text = concat_text(inspection)
    starts: list[int] = []
    offset = 0
    for page in inspection.pages:
        starts.append(offset)
        offset += len(page.text)
    for item in result.chunks:
        assert item.text in full_text
        assert sum(page["char_count"] for page in item.source_pages) == item.char_count
        assert item.source_pages[0]["page_index"] == item.page_index_start
        assert item.source_pages[-1]["page_index"] == item.page_index_end
        assert item.page_start == item.page_index_start + 1
        assert item.page_end == item.page_index_end + 1
        assert item.char_count == len(item.text)
        assert item.document_id == inspection.document_id
        assert item.filename == inspection.filename


def test_no_chunk_contains_text_from_two_documents() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    assert {item.document_id for item in result.chunks} == {inspection.document_id}


# --------------------------------------------------------------------------- #
# Determinism and identity (C1, C4, I4, I5)
# --------------------------------------------------------------------------- #
def test_two_runs_produce_identical_ids_and_deterministic_json() -> None:
    inspection, layout = manual_document()
    first = chunk(inspection, layout)
    second = chunk(inspection, layout)
    assert first.chunk_ids() == second.chunk_ids()
    assert first.to_deterministic_json() == second.to_deterministic_json()
    assert "duration_ms" in first.as_dict()
    assert "duration_ms" not in first.deterministic_payload()


def test_chunk_ids_are_pure_sha256_of_documented_inputs() -> None:
    import hashlib

    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    for item in result.chunks:
        payload = (
            f"{inspection.document_id}|{item.strategy}|{item.config_fingerprint}|"
            f"{item.chunk_index}|{item.text}"
        )
        expected = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert item.chunk_id == expected


def test_overlap_change_changes_fingerprint_and_ids() -> None:
    inspection, layout = build_document(
        [[("header", HEADER), ("heading", "7 Checks"), ("body", long_body(12))]]
    )
    first = chunk(inspection, layout, overlap_tokens=8)
    second = chunk(inspection, layout, overlap_tokens=16)
    assert first.config_fingerprint != second.config_fingerprint
    assert first.document_id == second.document_id
    assert first.chunk_ids() != second.chunk_ids()


def test_metadata_change_changes_fingerprint_and_ids() -> None:
    inspection, layout = manual_document()
    plain = chunk(inspection, layout)
    revised = chunk(inspection, layout, metadata={"revision": "R2"})
    assert plain.config_fingerprint != revised.config_fingerprint
    assert plain.chunk_ids() != revised.chunk_ids()
    assert revised.chunks[0].metadata["revision"] == {
        "value": "R2",
        "source": "operator",
    }


def test_absent_metadata_is_null_never_invented() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    metadata = result.chunks[0].metadata
    assert metadata["equipment_model"] == {"value": None, "source": "unknown"}
    assert metadata["revision"] == {"value": None, "source": "unknown"}


def test_chunker_version_is_published_on_every_chunk() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    assert result.chunker_version == CHUNKER_VERSION
    assert {item.chunker_version for item in result.chunks} == {CHUNKER_VERSION}
    assert {item.config_fingerprint for item in result.chunks} == {
        result.config_fingerprint
    }


def test_chunker_version_change_invalidates_ids() -> None:
    inspection, layout = manual_document()
    first = chunk(inspection, layout)
    second = chunk(inspection, layout, chunker_version="9.9.9")
    assert first.chunk_ids() != second.chunk_ids()


def test_origin_categories_reconcile_with_stats() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    by_section = [
        item for item in result.chunks if item.boundary["origin"] == "section"
    ]
    by_split = [item for item in result.chunks if item.boundary["origin"] == "split"]
    assert len(by_section) == result.stats["chunks_by_section"]
    assert len(by_split) == result.stats["chunks_by_split"]
    assert {item.boundary["origin"] for item in result.chunks} <= {"section", "split"}


# --------------------------------------------------------------------------- #
# Configuration validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "kwargs",
    [
        {"strategy": "unknown"},
        {"target_tokens": 0},
        {"max_tokens": 0},
        {"target_tokens": 400, "max_tokens": 300},
        {"target_tokens": 100, "overlap_tokens": 100},
        {"overlap_tokens": -1},
        {"mapping_ratio_threshold": 0.0},
        {"mapping_ratio_threshold": 1.5},
        {"metadata": {"unknown_key": "x"}},
        {"metadata": {"revision": "   "}},
        {"metadata": {"revision": 7}},
        {"chunker_version": "  "},
    ],
)
def test_invalid_configuration_is_rejected(kwargs) -> None:
    inspection, layout = manual_document()
    with pytest.raises(ChunkingConfigError):
        chunk_document(inspection, layout=layout, **kwargs)


def test_section_aware_requires_a_layout_source() -> None:
    inspection, _layout = manual_document()
    with pytest.raises(LayoutSourceRequiredError):
        chunk_document(inspection, strategy=STRATEGY_SECTION_AWARE)


def test_layout_mismatch_is_rejected_against_a_real_pdf(tmp_path: Path) -> None:
    pdf = tmp_path / "synthetic.pdf"
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 120), "1 Introduction", fontsize=16, fontname="hebo")
        page.insert_text((72, 160), BODY, fontsize=10)
        document.save(str(pdf))
    finally:
        document.close()
    inspection = inspect_pdf(str(pdf))
    inspection.sha256 = "c" * 64
    with pytest.raises(LayoutSourceMismatchError):
        chunk_document(inspection, source_path=pdf)


# --------------------------------------------------------------------------- #
# Diagnostics surface (Q2, C3)
# --------------------------------------------------------------------------- #
def test_diagnostics_are_published_for_section_aware() -> None:
    inspection, layout = manual_document()
    result = chunk(inspection, layout)
    assert len(result.page_mapping) == len(inspection.pages)
    assert {"page_index", "mapping_ratio", "mapping_partial"} <= set(
        result.page_mapping[0]
    )
    assert result.heading_diagnostics
    assert {"text", "m2_start", "boundary_usable", "reason"} <= set(
        result.heading_diagnostics[0]
    )
    assert all(entry["mapping_ratio"] >= 0.90 for entry in result.page_mapping)


def test_partial_mapping_page_falls_back_to_windows() -> None:
    inspection, layout = build_document(
        [
            [
                ("header", HEADER),
                ("heading", "8 Checks"),
                ("body", BODY),
                ("body", SECOND_BODY),
            ]
        ]
    )
    broken = LayoutDocument(
        pages=(
            PageLayout(
                page_index=0,
                page_number=1,
                width=PAGE_WIDTH,
                height=PAGE_HEIGHT,
                lines=layout.pages[0].lines
                + (
                    LayoutLine(
                        text="Line that never existed",
                        x0=72.0,
                        y0=600.0,
                        x1=372.0,
                        y1=610.0,
                        size=10.0,
                        bold=False,
                        block_no=1,
                    ),
                ),
            ),
        )
    )
    result = chunk(inspection, broken)
    assert result.page_mapping[0]["mapping_partial"] is True
    assert result.stats["layout_mapping_partial_pages"] == 1
    assert result.heading_diagnostics[0]["boundary_usable"] is False
    assert result.heading_diagnostics[0]["reason"] == "layout_mapping_partial"
    assert "document_without_headings" in result.warnings
    assert result.chunks[0].section_path == ["untitled"]
    assert any("layout_mapping_partial" in warning for warning in result.warnings)


def test_document_without_headings_is_one_untitled_section() -> None:
    inspection, layout = build_document([[("body", BODY)], [("body", SECOND_BODY)]])
    result = chunk(inspection, layout)
    assert len(result.chunks) == 1
    assert result.chunks[0].section_path == ["untitled"]
    assert result.chunks[0].heading is None
    assert "document_without_headings" in result.warnings
    assert "preamble_without_heading" not in result.warnings
    assert result.stats["token_estimate_min"] == result.chunks[0].token_estimate


# --------------------------------------------------------------------------- #
# Comparison harness and CLI
# --------------------------------------------------------------------------- #
def test_compare_strategies_reports_both_rows() -> None:
    inspection, layout = manual_document()
    comparison = compare_strategies(inspection, layout=layout)
    assert [row["strategy"] for row in comparison["strategies"]] == list(STRATEGIES)
    section_row = comparison["strategies"][0]
    fixed_row = comparison["strategies"][1]
    assert section_row["sections"] == 4
    assert fixed_row["sections"] == 0
    assert section_row["chunks_by_section"] >= 4
    assert fixed_row["chunks_by_section"] == 0
    assert comparison["document_id"] == inspection.document_id
    assert all(row["chunks"] > 0 for row in comparison["strategies"])


def _build_cli_pdf(path: Path) -> Path:
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 120), "1 Introduction", fontsize=16, fontname="hebo")
        page.insert_text((72, 160), BODY, fontsize=10)
        page.insert_text((72, 180), SECOND_BODY, fontsize=10)
        document.save(str(path))
    finally:
        document.close()
    return path


def test_cli_compare_runs_end_to_end(tmp_path: Path) -> None:
    pdf = _build_cli_pdf(tmp_path / "cli.pdf")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "chunk_pdf.py"),
            str(pdf),
            "--compare",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "section_aware" in completed.stdout
    assert "fixed_token" in completed.stdout


def test_cli_writes_json_output(tmp_path: Path) -> None:
    pdf = _build_cli_pdf(tmp_path / "cli.pdf")
    output = tmp_path / "chunks.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "chunk_pdf.py"),
            str(pdf),
            "--output",
            str(output),
            "--equipment-model",
            "RDR-9",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["strategy"] == STRATEGY_SECTION_AWARE
    assert payload["chunks"][0]["metadata"]["equipment_model"] == {
        "value": "RDR-9",
        "source": "operator",
    }


def test_cli_refuses_a_password_argument_without_echoing_it(
    tmp_path: Path,
) -> None:
    pdf = _build_cli_pdf(tmp_path / "cli.pdf")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "chunk_pdf.py"),
            str(pdf),
            "--password=hunter2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "not accepted on the command line" in completed.stderr
    assert "hunter2" not in completed.stderr
    assert "hunter2" not in completed.stdout


# --------------------------------------------------------------------------- #
# D-01: complete eligible-text coverage (OSJ source audit)
# --------------------------------------------------------------------------- #
def coverage_document() -> tuple[DocumentInspection, LayoutDocument]:
    """Fixture with punctuation, repeated lines, a page gap and a short tail."""

    return build_document(
        [
            [
                ("header", HEADER),
                ("heading", "1 Introduction"),
                ("body", "Check: the ratio is 1.5:1! Is the feeder warm?"),
                ("body", "Repeat this check before every shift."),
                ("body", "Repeat this check before every shift."),
            ],
            [
                ("header", HEADER),
                ("heading", "2 Operation"),
                ("body", BODY),
            ],
            [("header", HEADER)],
            [
                ("header", HEADER),
                ("body", SECOND_BODY),
                ("body", "Short tail line here."),
            ],
            [
                ("header", HEADER),
                ("heading", "3 Maintenance"),
                ("body", long_body(9)),
            ],
        ]
    )


def chunk_intervals(result, full_text: str) -> list[tuple[int, int]]:
    """Locate every chunk in the M2 concatenation using a forward cursor.

    Chunks may overlap, so the cursor advances by one character rather than to
    the previous end; starts must still increase, which is what proves the chunk
    order matches the document order.
    """

    cursor = 0
    previous_start = -1
    intervals: list[tuple[int, int]] = []
    for chunk in result.chunks:
        position = full_text.find(chunk.text, cursor)
        assert position >= 0, f"chunk {chunk.chunk_index} not found after the cursor"
        assert position > previous_start or chunk.chunk_index == 0
        intervals.append((position, position + len(chunk.text)))
        previous_start = position
        cursor = position + 1
    return intervals


def missing_characters(inspection, result, covered_indexes) -> list[tuple]:
    full_text = concat_text(inspection)
    intervals = chunk_intervals(result, full_text)
    offsets: list[int] = []
    running = 0
    for page in inspection.pages:
        offsets.append(running)
        running += len(page.text)
    missing: list[tuple] = []
    for page_index in covered_indexes:
        page = inspection.pages[page_index]
        base = offsets[page_index]
        for index, character in enumerate(page.text):
            if character.isspace():
                continue
            absolute = base + index
            if not any(start <= absolute < end for start, end in intervals):
                missing.append((page.page_number, index, character))
    return missing


@pytest.mark.parametrize("strategy", list(STRATEGIES))
def test_every_non_whitespace_character_of_a_covered_page_is_in_a_chunk(
    strategy: str,
) -> None:
    inspection, layout = coverage_document()
    mark_image_only(inspection, 2)
    result = chunk(
        inspection, layout, strategy=strategy, target_tokens=40, max_tokens=60
    )
    covered = sorted(
        page.page_index for page in inspection.pages if page.page_index != 2
    )
    assert covered == [0, 1, 3, 4]
    assert result.stats["pages_covered"] == 4
    assert result.needs_ocr == [3]
    missing = missing_characters(inspection, result, covered)
    assert missing == [], f"{len(missing)} non-whitespace characters are uncovered"
    assert any(
        character in {":", "!", "?"}
        for page in inspection.pages
        for character in page.text
    )


@pytest.mark.parametrize("strategy", list(STRATEGIES))
def test_coverage_holds_for_heading_and_trailing_fragment_chunks(
    strategy: str,
) -> None:
    inspection, layout = coverage_document()
    mark_image_only(inspection, 2)
    result = chunk(
        inspection, layout, strategy=strategy, target_tokens=40, max_tokens=60
    )
    last_page = inspection.pages[-1]
    last_token = last_page.text.split()[-1]
    assert last_token in result.chunks[-1].text
    assert missing_characters(inspection, result, [0, 1, 3, 4]) == []


# --------------------------------------------------------------------------- #
# D-02: OCR/visual signals never silently discard useful text
# --------------------------------------------------------------------------- #
def ocr_document() -> tuple[DocumentInspection, LayoutDocument]:
    return build_document(
        [
            [("header", HEADER), ("heading", "1 Introduction"), ("body", BODY)],
            [("header", HEADER), ("body", "Fig. 7")],
            [("header", HEADER), ("heading", "2 Operation"), ("body", SECOND_BODY)],
        ]
    )


def test_page_with_useful_text_and_ocr_signal_is_chunked() -> None:
    inspection, _layout = ocr_document()
    page = inspection.pages[0]
    page.classification = "mixed"
    page.ocr_candidate = True
    page.image_count = 1
    mark_image_only(inspection, 1)
    result = chunk_document(
        inspection, strategy=STRATEGY_FIXED_TOKEN, target_tokens=100, max_tokens=120
    )
    assert result.stats["pages_covered"] == 2
    assert result.stats["pages_needs_ocr"] == 1
    assert result.stats["text_pages_with_ocr_signal"] == 1
    kept = [entry for entry in result.ocr_diagnostics if entry["page_index"] == 0]
    assert kept[0]["bucket"] == "covered"
    assert kept[0]["reason"] == "useful_text_kept"
    assert kept[0]["ocr_candidate"] is True
    assert "text_pages_with_ocr_signal" in result.warnings
    assert any(BODY in item.text for item in result.chunks)


def test_ocr_page_with_sparse_text_publishes_what_was_left_out() -> None:
    inspection, _layout = ocr_document()
    page = inspection.pages[1]
    page.classification = CLASSIFICATION_UNCERTAIN
    page.ocr_candidate = True
    page.image_count = 1
    result = chunk_document(
        inspection, strategy=STRATEGY_FIXED_TOKEN, target_tokens=100, max_tokens=120
    )
    entries = [entry for entry in result.ocr_diagnostics if entry["page_index"] == 1]
    assert entries[0]["bucket"] == "needs_ocr"
    assert entries[0]["reason"] == "sparse_text_with_visuals"
    assert entries[0]["extracted_char_count"] == page.non_whitespace_char_count
    assert entries[0]["extracted_char_count"] > 0
    assert result.needs_ocr == [2]
    used = {
        source["page_index"] for item in result.chunks for source in item.source_pages
    }
    assert 1 not in used
    assert (
        result.stats["pages_covered"]
        + result.stats["pages_needs_ocr"]
        + result.stats["pages_skipped"]
        == result.stats["pages_inspected"]
    )


def test_ocr_diagnostics_make_omission_explicit_not_silent() -> None:
    inspection, layout = ocr_document()
    inspection.pages[1].ocr_candidate = True
    inspection.pages[1].image_count = 1
    inspection.pages[1].classification = CLASSIFICATION_UNCERTAIN
    result = chunk_document(inspection, layout=layout)
    assert len(result.ocr_diagnostics) == 1
    entry = result.ocr_diagnostics[0]
    assert {"page_index", "bucket", "ocr_candidate", "extracted_char_count"} <= set(
        entry
    )
    assert entry["extracted_char_count"] > 0
    assert result.stats["ocr_diagnostics"] == 1


# --------------------------------------------------------------------------- #
# D-03: line-end fallback uses M2 character ranges
# --------------------------------------------------------------------------- #
def line_document() -> tuple[DocumentInspection, LayoutDocument]:
    lines = [" ".join(f"token{line}x{word}" for word in range(10)) for line in range(8)]
    return build_document([[("body", text) for text in lines]])


def test_line_end_fallback_cuts_at_m2_line_breaks() -> None:
    inspection, _layout = line_document()
    result = chunk_document(
        inspection,
        strategy=STRATEGY_FIXED_TOKEN,
        target_tokens=12,
        max_tokens=20,
        overlap_tokens=0,
    )
    assert result.stats["hard_cuts"] == 0
    ends = [item.boundary["ends_at"] for item in result.chunks]
    assert ends[0] == "line_end"
    assert set(ends[:-1]) == {"line_end"}
    full_text = concat_text(inspection)
    intervals = chunk_intervals(result, full_text)
    for start, end in intervals[:-1]:
        assert full_text[end] == "\n"
        assert start == 0 or full_text[start - 1] == "\n"


def test_line_end_fallback_keeps_deterministic_ids() -> None:
    inspection, _layout = line_document()
    first = chunk_document(
        inspection,
        strategy=STRATEGY_FIXED_TOKEN,
        target_tokens=12,
        max_tokens=20,
    )
    second = chunk_document(
        inspection,
        strategy=STRATEGY_FIXED_TOKEN,
        target_tokens=12,
        max_tokens=20,
    )
    assert first.chunk_ids() == second.chunk_ids()
    assert first.to_deterministic_json() == second.to_deterministic_json()


# --------------------------------------------------------------------------- #
# D-04: encrypted PDF layout pass and password hygiene
# --------------------------------------------------------------------------- #
USER_PASSWORD = "s3cret-layout-pw"


def _build_encrypted_pdf(path: Path) -> Path:
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 120), "1 Introduction", fontsize=16, fontname="hebo")
        page.insert_text((72, 160), BODY, fontsize=10)
        page.insert_text((72, 190), SECOND_BODY, fontsize=10)
        document.save(
            str(path),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-layout-pw",
            user_pw=USER_PASSWORD,
        )
    finally:
        document.close()
    return path


def test_encrypted_pdf_layout_pass_uses_the_same_password(tmp_path: Path) -> None:
    pdf = _build_encrypted_pdf(tmp_path / "locked.pdf")
    inspection = inspect_pdf(str(pdf), password=USER_PASSWORD)
    result = chunk_document(inspection, source_path=pdf, password=USER_PASSWORD)
    assert [item.heading for item in result.chunks if item.heading] == [
        "1 Introduction"
    ]
    assert USER_PASSWORD not in result.to_json()
    assert "password" not in json.dumps(result.stats).lower()


def test_encrypted_pdf_without_password_fails_explicitly(tmp_path: Path) -> None:
    pdf = _build_encrypted_pdf(tmp_path / "locked.pdf")
    inspection = inspect_pdf(str(pdf), password=USER_PASSWORD)
    with pytest.raises(PdfEncryptedError, match="password is required"):
        chunk_document(inspection, source_path=pdf)


def test_encrypted_pdf_with_wrong_password_fails_explicitly(tmp_path: Path) -> None:
    pdf = _build_encrypted_pdf(tmp_path / "locked.pdf")
    inspection = inspect_pdf(str(pdf), password=USER_PASSWORD)
    with pytest.raises(PdfEncryptedError, match="rejected") as error:
        chunk_document(inspection, source_path=pdf, password="wrong-pw")
    assert "wrong-pw" not in str(error.value)
    assert USER_PASSWORD not in str(error.value)


def test_cli_prompts_once_and_chunks_an_encrypted_pdf(tmp_path: Path) -> None:
    pdf = _build_encrypted_pdf(tmp_path / "locked.pdf")
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "chunk_pdf.py"),
            str(pdf),
            "--ask-password",
            "--summary",
        ],
        input=f"{USER_PASSWORD}\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "strategy=section_aware" in completed.stdout
    assert USER_PASSWORD not in completed.stdout
    assert USER_PASSWORD not in completed.stderr


# --------------------------------------------------------------------------- #
# D-05: --min-text-chars must reach the comparison path too
# --------------------------------------------------------------------------- #
def short_text_document() -> tuple[DocumentInspection, LayoutDocument]:
    return build_document([[("body", "Fig 7 check detail")]])


def test_compare_strategies_forwards_min_text_chars() -> None:
    inspection, layout = short_text_document()
    page = inspection.pages[0]
    page.image_count = 1
    page.ocr_candidate = True
    page.classification = CLASSIFICATION_UNCERTAIN

    strict = compare_strategies(inspection, layout=layout, min_text_chars=32)
    loose = compare_strategies(inspection, layout=layout, min_text_chars=10)

    assert strict["min_text_chars"] == 32
    assert loose["min_text_chars"] == 10
    assert strict["chunker_version"] == CHUNKER_VERSION
    for row in strict["strategies"]:
        assert row["pages_needs_ocr"] == 1
        assert row["pages_covered"] == 0
        assert row["chunks"] == 0
    for row in loose["strategies"]:
        assert row["pages_needs_ocr"] == 0
        assert row["pages_covered"] == 1
        assert row["chunks"] >= 1


def test_cli_compare_honours_min_text_chars(tmp_path: Path) -> None:
    pdf = tmp_path / "short.pdf"
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 120), "Fig 7 check detail", fontsize=10)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80), 0)
        pixmap.clear_with(190)
        page.insert_image(pymupdf.Rect(72, 200, 272, 320), pixmap=pixmap)
        document.save(str(pdf))
    finally:
        document.close()

    def run(extra: list[str], name: str) -> dict:
        output = tmp_path / name
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "chunk_pdf.py"),
                str(pdf),
                "--compare",
                "--output",
                str(output),
                *extra,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(output.read_text(encoding="utf-8"))

    strict = run([], "strict.json")
    loose = run(["--min-text-chars", "10"], "loose.json")

    assert strict["min_text_chars"] == 32
    assert loose["min_text_chars"] == 10
    assert strict["strategies"][0]["pages_needs_ocr"] == 1
    assert strict["strategies"][0]["chunks"] == 0
    assert loose["strategies"][0]["pages_needs_ocr"] == 0
    assert loose["strategies"][0]["chunks"] >= 1
    assert loose["strategies"][1]["chunks"] >= 1
