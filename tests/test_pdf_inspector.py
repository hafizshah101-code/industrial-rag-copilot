"""Tests for app.ingestion.pdf_inspector.

All fixtures are synthetic PDFs generated at test runtime inside pytest's
temporary directory, so the suite runs offline and no vendor document is ever
needed, stored or committed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tracemalloc
from pathlib import Path

import pymupdf
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.ingestion.pdf_inspector import (
    MIN_TEXT_CHARS,
    PdfCorruptedError,
    PdfEmptyFileError,
    PdfEncryptedError,
    PdfInspectionError,
    PdfNotFoundError,
    UnsupportedFileTypeError,
    classify_page,
    inspect_pdf,
)

LONG_TEXT = "Marine radar manual section: transmitter power supply nominal 24 V DC."


# --------------------------------------------------------------------------- #
# Synthetic fixture builders
# --------------------------------------------------------------------------- #
def _new_pdf(path: Path) -> pymupdf.Document:
    return pymupdf.open()


def _add_text_page(document: pymupdf.Document, heading: str, body: str = LONG_TEXT):
    page = document.new_page()
    page.insert_text((72, 100), heading, fontsize=14)
    page.insert_text((72, 140), body, fontsize=10)
    return page


def _add_image_only_page(document: pymupdf.Document):
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 240, 160), 0)
    pixmap.clear_with(200)
    page.insert_image(pymupdf.Rect(72, 72, 472, 332), pixmap=pixmap)
    return page


def _add_mixed_page(document: pymupdf.Document):
    page = _add_text_page(document, "Alarm E-104")
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80), 0)
    pixmap.clear_with(120)
    page.insert_image(pymupdf.Rect(72, 200, 272, 320), pixmap=pixmap)
    return page


def _add_empty_page(document: pymupdf.Document):
    return document.new_page()


def _add_grid_page(document: pymupdf.Document):
    page = document.new_page()
    x0, y0, width, height, rows, cols = 72.0, 72.0, 300.0, 120.0, 3, 3
    for row in range(rows + 1):
        y = y0 + row * height / rows
        page.draw_line((x0, y), (x0 + width, y))
    for col in range(cols + 1):
        x = x0 + col * width / cols
        page.draw_line((x, y0), (x, y0 + height))
    for row in range(rows):
        for col in range(cols):
            page.insert_text(
                (x0 + col * width / cols + 4, y0 + row * height / rows + 16),
                f"R{row}C{col}",
                fontsize=8,
            )
    return page


def _add_vector_diagram_page(document: pymupdf.Document):
    page = document.new_page()
    page.insert_text((72, 90), "Schematic: antenna drive", fontsize=12)
    for step in range(10):
        y = 140 + step * 18
        page.draw_line((90, y), (380, y))
        page.draw_circle((110, y), 6)
    return page


def _add_sparse_text_with_image_page(document: pymupdf.Document):
    page = document.new_page()
    page.insert_text((72, 100), "Fig. 7", fontsize=10)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 120), 0)
    pixmap.clear_with(90)
    page.insert_image(pymupdf.Rect(72, 140, 472, 440), pixmap=pixmap)
    return page


def build_pdf(path: Path, kind: str) -> Path:
    document = _new_pdf(path)
    try:
        if kind == "three_pages":
            _add_text_page(document, "Radar Overview")
            _add_text_page(document, "Troubleshooting - Alarm E-104")
            _add_text_page(document, "Technical Specifications - 24 V DC")
        elif kind == "text":
            _add_text_page(document, "Radar Overview")
        elif kind == "image_only":
            _add_image_only_page(document)
        elif kind == "mixed":
            _add_mixed_page(document)
        elif kind == "empty":
            _add_empty_page(document)
        elif kind == "grid":
            _add_grid_page(document)
        elif kind == "diagram":
            _add_vector_diagram_page(document)
        elif kind == "sparse_text_with_image":
            _add_sparse_text_with_image_page(document)
        elif kind == "two_grids":
            _add_grid_page(document)
            _add_grid_page(document)
        elif kind == "two_pages":
            _add_text_page(document, "First page marker ALPHA")
            _add_text_page(document, "Second page marker BETA")
        else:  # pragma: no cover - guards against typos in tests
            raise AssertionError(f"unknown fixture kind: {kind}")
        document.save(str(path))
    finally:
        document.close()
    return path


@pytest.fixture()
def pdf_factory(tmp_path: Path):
    def _factory(kind: str, name: str | None = None) -> Path:
        return build_pdf(tmp_path / (name or f"{kind}.pdf"), kind)

    return _factory


# --------------------------------------------------------------------------- #
# FR-01 document inspection
# --------------------------------------------------------------------------- #
def test_inspect_valid_three_page_pdf(pdf_factory):
    result = inspect_pdf(pdf_factory("three_pages"))

    assert result.page_count == 3
    assert len(result.pages) == 3
    assert [page.page_index for page in result.pages] == [0, 1, 2]
    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert result.filename == "three_pages.pdf"
    assert result.file_size_bytes > 0
    assert result.document_id == f"sha256:{result.sha256}"
    assert result.extraction_duration_ms >= 0
    assert all(page.status == "ok" for page in result.pages)


def test_missing_file_raises_controlled_error(tmp_path: Path):
    with pytest.raises(PdfNotFoundError):
        inspect_pdf(tmp_path / "does_not_exist.pdf")


def test_directory_path_is_rejected(tmp_path: Path):
    with pytest.raises(PdfNotFoundError):
        inspect_pdf(tmp_path)


def test_zero_byte_pdf_raises_empty_file_error(tmp_path: Path):
    empty = tmp_path / "zero.pdf"
    empty.write_bytes(b"")

    with pytest.raises(PdfEmptyFileError):
        inspect_pdf(empty)


def test_invalid_pdf_raises_corrupted_error(tmp_path: Path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.7\nthis is not a valid pdf body\n")

    with pytest.raises(PdfCorruptedError):
        inspect_pdf(broken)


def test_non_pdf_extension_is_rejected(tmp_path: Path):
    fake = tmp_path / "manual.txt"
    fake.write_bytes(b"%PDF-1.7\n" + LONG_TEXT.encode())

    with pytest.raises(UnsupportedFileTypeError):
        inspect_pdf(fake)


def test_pdf_without_header_is_rejected(tmp_path: Path):
    fake = tmp_path / "not_really.pdf"
    fake.write_bytes(b"just plain text, no header\n")

    with pytest.raises(UnsupportedFileTypeError):
        inspect_pdf(fake)


def test_every_controlled_error_shares_base_class(tmp_path: Path):
    for exception in (
        PdfNotFoundError,
        UnsupportedFileTypeError,
        PdfEmptyFileError,
        PdfCorruptedError,
        PdfEncryptedError,
    ):
        assert issubclass(exception, PdfInspectionError)


# --------------------------------------------------------------------------- #
# FR-01 encryption
# --------------------------------------------------------------------------- #
def test_encrypted_pdf_is_refused_without_password(tmp_path: Path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), LONG_TEXT, fontsize=10)
    target = tmp_path / "locked.pdf"
    document.save(
        str(target),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="secret123",
        owner_pw="secret123",
    )
    document.close()

    with pytest.raises(PdfEncryptedError):
        inspect_pdf(target)


def test_encrypted_pdf_with_correct_password_is_readable(tmp_path: Path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), LONG_TEXT, fontsize=10)
    target = tmp_path / "locked.pdf"
    document.save(
        str(target),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="secret123",
        owner_pw="secret123",
    )
    document.close()

    result = inspect_pdf(target, password="secret123")

    assert result.page_count == 1
    assert "24 V DC" in result.pages[0].text


def test_encrypted_pdf_with_wrong_password_is_refused(tmp_path: Path):
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), LONG_TEXT, fontsize=10)
    target = tmp_path / "locked.pdf"
    document.save(
        str(target),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="secret123",
        owner_pw="secret123",
    )
    document.close()

    with pytest.raises(PdfEncryptedError):
        inspect_pdf(target, password="wrong-password")


# --------------------------------------------------------------------------- #
# FR-02 page-level extraction and provenance
# --------------------------------------------------------------------------- #
def test_text_only_page_extracts_text(pdf_factory):
    result = inspect_pdf(pdf_factory("text"))
    page = result.pages[0]

    assert page.char_count > 0
    assert "Radar Overview" in page.text
    assert page.non_whitespace_char_count >= MIN_TEXT_CHARS
    assert page.classification == "text"
    assert page.ocr_candidate is False


def test_page_provenance_matches_source_pages(pdf_factory):
    result = inspect_pdf(pdf_factory("two_pages"))

    assert "ALPHA" in result.pages[0].text
    assert "BETA" in result.pages[1].text
    assert result.pages[0].page_index == 0 and result.pages[0].page_number == 1
    assert result.pages[1].page_index == 1 and result.pages[1].page_number == 2


def test_text_line_breaks_are_preserved(pdf_factory):
    result = inspect_pdf(pdf_factory("three_pages"))
    page = result.pages[0]

    assert "\n" in page.text
    assert page.text.count("\n") >= 1


# --------------------------------------------------------------------------- #
# FR-03 classification
# --------------------------------------------------------------------------- #
def test_image_only_page_is_flagged_for_ocr(pdf_factory):
    result = inspect_pdf(pdf_factory("image_only"))
    page = result.pages[0]

    assert page.char_count == 0
    assert page.image_count == 1
    assert page.classification == "image_only"
    assert page.ocr_candidate is True
    assert "possible_scanned_page" in page.warnings
    assert result.as_dict()["ocr_candidate_pages"] == [1]
    assert "document_contains_ocr_candidates" in result.warnings


def test_mixed_page_reports_text_and_image_indicators(pdf_factory):
    result = inspect_pdf(pdf_factory("mixed"))
    page = result.pages[0]

    assert page.char_count > 0
    assert page.image_count >= 1
    assert page.classification == "mixed"
    assert page.ocr_candidate is False


def test_empty_page_is_classified_empty(pdf_factory):
    result = inspect_pdf(pdf_factory("empty"))
    page = result.pages[0]

    assert page.char_count == 0
    assert page.image_count == 0
    assert page.vector_drawing_count == 0
    assert page.classification == "empty"
    assert page.ocr_candidate is False
    assert "document_has_no_extractable_text" in result.warnings


def test_classification_rules_are_deterministic():
    assert (
        classify_page(
            non_whitespace_char_count=64, image_count=0, vector_drawing_count=0
        )
        == "text"
    )
    assert (
        classify_page(
            non_whitespace_char_count=64, image_count=2, vector_drawing_count=0
        )
        == "mixed"
    )
    assert (
        classify_page(
            non_whitespace_char_count=0, image_count=1, vector_drawing_count=0
        )
        == "image_only"
    )
    assert (
        classify_page(
            non_whitespace_char_count=0, image_count=0, vector_drawing_count=0
        )
        == "empty"
    )
    assert (
        classify_page(
            non_whitespace_char_count=0, image_count=0, vector_drawing_count=9
        )
        == "uncertain"
    )
    assert (
        classify_page(
            non_whitespace_char_count=5, image_count=0, vector_drawing_count=0
        )
        == "uncertain"
    )
    # Sparse text plus a visible image is not confidently image-only.
    assert (
        classify_page(
            non_whitespace_char_count=6, image_count=1, vector_drawing_count=0
        )
        == "uncertain"
    )


# --------------------------------------------------------------------------- #
# FR-04 image, table and diagram indicators
# --------------------------------------------------------------------------- #
def test_image_metadata_is_recorded(pdf_factory):
    result = inspect_pdf(pdf_factory("image_only"))
    image = result.pages[0].images[0]

    assert image.width == 240
    assert image.height == 160
    assert image.bits_per_component == 8
    assert image.index == 0
    assert result.pages[0].as_dict()["images"][0]["width"] == 240


def test_table_candidate_is_detected_on_drawn_grid(pdf_factory):
    result = inspect_pdf(pdf_factory("grid"))
    page = result.pages[0]

    assert page.vector_drawing_count >= 6
    assert page.table_count >= 1
    assert page.table_candidate is True
    assert page.diagram_candidate is True
    assert "table_candidate_line_based" in page.warnings


def test_table_analysis_can_be_disabled(pdf_factory):
    result = inspect_pdf(pdf_factory("grid"), detect_tables=False)
    page = result.pages[0]

    assert page.vector_drawing_count >= 6
    assert page.table_count == 0
    assert page.table_candidate is False


def test_table_analysis_is_skipped_without_graphics(pdf_factory):
    # A text-only page carries no drawn lines or images, so the expensive
    # line-based table analysis is skipped and no candidate is reported.
    result = inspect_pdf(pdf_factory("text"))
    page = result.pages[0]

    assert page.vector_drawing_count == 0
    assert page.image_count == 0
    assert page.table_count == 0
    assert page.table_candidate is False


def test_diagram_candidate_only_for_drawing_heavy_pages(pdf_factory):
    diagram = inspect_pdf(pdf_factory("diagram", "diagram.pdf")).pages[0]
    text_page = inspect_pdf(pdf_factory("text", "text_only.pdf")).pages[0]

    assert diagram.vector_drawing_count >= 5
    assert diagram.diagram_candidate is True
    assert text_page.vector_drawing_count == 0
    assert text_page.diagram_candidate is False


# --------------------------------------------------------------------------- #
# FR-05 structured output
# --------------------------------------------------------------------------- #
def test_output_is_json_serialisable_with_documented_schema(pdf_factory):
    payload = inspect_pdf(pdf_factory("three_pages")).as_dict()

    encoded = json.dumps(payload)
    decoded = json.loads(encoded)

    assert set(decoded) == {
        "document_id",
        "filename",
        "sha256",
        "file_size_bytes",
        "page_count",
        "pages_inspected",
        "pages_skipped",
        "pages",
        "classification_counts",
        "ocr_candidate_pages",
        "warnings",
        "extraction_duration_ms",
        "inspector_version",
    }
    assert set(decoded["pages"][0]) == {
        "page_index",
        "page_number",
        "text",
        "char_count",
        "non_whitespace_char_count",
        "classification",
        "ocr_candidate",
        "image_count",
        "image_resource_count",
        "images",
        "vector_drawing_count",
        "table_count",
        "table_candidate",
        "table_analysis",
        "diagram_candidate",
        "status",
        "warnings",
    }
    assert sum(decoded["classification_counts"].values()) == decoded["page_count"]
    sha = decoded["sha256"]
    assert len(sha) == 64
    assert all(character in "0123456789abcdef" for character in sha)


def test_to_json_matches_to_dict(pdf_factory):
    result = inspect_pdf(pdf_factory("text"))

    assert json.loads(result.to_json()) == result.to_dict()


def test_no_invented_document_metadata(pdf_factory):
    payload = inspect_pdf(pdf_factory("three_pages")).as_dict()

    for invented in ("manufacturer_model", "printed_page_label", "manual_revision"):
        assert invented not in payload


# --------------------------------------------------------------------------- #
# FR-06 checksum behaviour and performance measurement
# --------------------------------------------------------------------------- #
def test_sha256_matches_independent_computation(pdf_factory):
    path = pdf_factory("three_pages")
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    result = inspect_pdf(path)

    assert result.sha256 == expected
    assert result.document_id == f"sha256:{expected}"


def test_identical_bytes_produce_identical_hash(tmp_path: Path):
    first = build_pdf(tmp_path / "a.pdf", "text")
    second = tmp_path / "b.pdf"
    second.write_bytes(first.read_bytes())

    assert inspect_pdf(first).sha256 == inspect_pdf(second).sha256


def test_different_bytes_produce_different_hash(tmp_path: Path):
    first = build_pdf(tmp_path / "a.pdf", "text")
    second = build_pdf(tmp_path / "b.pdf", "three_pages")

    assert inspect_pdf(first).sha256 != inspect_pdf(second).sha256


def test_extraction_duration_and_peak_memory_are_measured(pdf_factory):
    path = pdf_factory("three_pages")

    tracemalloc.start()
    result = inspect_pdf(path)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result.extraction_duration_ms >= 0
    assert result.extraction_duration_ms < 10_000
    assert peak > 0
    # Recorded for the milestone report; not a hard performance budget.
    print(
        f"\npeak_memory_kb={peak / 1024:.1f} duration_ms={result.extraction_duration_ms}"
    )


# --------------------------------------------------------------------------- #
# Visible images versus image resources (OSJ refinement 1)
# --------------------------------------------------------------------------- #
def test_visible_images_are_distinguished_from_image_resources(pdf_factory):
    image_page = inspect_pdf(pdf_factory("image_only")).pages[0]
    text_page = inspect_pdf(pdf_factory("text")).pages[0]

    assert image_page.image_count == 1
    assert image_page.image_resource_count == 1
    assert image_page.images[0].bbox is not None
    assert text_page.image_count == 0
    assert text_page.image_resource_count == 0
    assert "image_resources_not_displayed" not in image_page.warnings


def test_sparse_text_with_image_is_uncertain_but_still_an_ocr_candidate(pdf_factory):
    page = inspect_pdf(pdf_factory("sparse_text_with_image")).pages[0]

    assert 0 < page.non_whitespace_char_count < MIN_TEXT_CHARS
    assert page.image_count == 1
    assert page.classification == "uncertain"
    assert page.ocr_candidate is True
    assert "sparse_text_with_images" in page.warnings


# --------------------------------------------------------------------------- #
# Resource guards (OSJ refinement 3)
# --------------------------------------------------------------------------- #
def test_max_pages_limits_inspection_and_reports_skip(pdf_factory):
    result = inspect_pdf(pdf_factory("three_pages"), max_pages=2)

    assert result.page_count == 3
    assert len(result.pages) == 2
    assert [page.page_number for page in result.pages] == [1, 2]
    assert result.as_dict()["pages_inspected"] == 2
    assert result.as_dict()["pages_skipped"] == 1
    assert "page_limit_reached" in result.warnings


def test_table_analysis_budget_is_enforced(pdf_factory):
    result = inspect_pdf(pdf_factory("two_grids"), max_table_analysis_pages=1)

    first, second = result.pages
    assert first.table_analysis == "analysed"
    assert first.table_count >= 1
    assert second.table_analysis == "skipped_budget"
    assert second.table_count == 0
    assert second.table_candidate is False
    assert "table_analysis_budget_exhausted" in result.warnings


@pytest.mark.parametrize("value", [0, -1, -25])
def test_max_pages_rejects_non_positive_values(pdf_factory, value):
    with pytest.raises(ValueError, match="max_pages"):
        inspect_pdf(pdf_factory("text"), max_pages=value)


@pytest.mark.parametrize("value", [0, -1])
def test_table_budget_rejects_non_positive_values(pdf_factory, value):
    with pytest.raises(ValueError, match="max_table_analysis_pages"):
        inspect_pdf(pdf_factory("text"), max_table_analysis_pages=value)


def test_min_text_chars_rejects_non_positive_values(pdf_factory):
    with pytest.raises(ValueError, match="min_text_chars"):
        inspect_pdf(pdf_factory("text"), min_text_chars=0)


@pytest.mark.parametrize(
    ("name", "value"),
    [("max_pages", "5"), ("max_table_analysis_pages", 2.5), ("min_text_chars", True)],
)
def test_limits_reject_non_integer_values(pdf_factory, name, value):
    with pytest.raises(TypeError, match=name):
        inspect_pdf(pdf_factory("text"), **{name: value})


def test_skipped_pages_are_never_reported_as_inspected(pdf_factory):
    payload = inspect_pdf(pdf_factory("three_pages"), max_pages=1).as_dict()

    assert payload["page_count"] == 3
    assert payload["pages_inspected"] == 1
    assert payload["pages_skipped"] == 2
    assert [page["page_number"] for page in payload["pages"]] == [1]
    assert sum(payload["classification_counts"].values()) == payload["pages_inspected"]
    assert "page_limit_reached" in payload["warnings"]


def test_skipped_table_analysis_is_distinguishable_from_a_negative_result(pdf_factory):
    payload = inspect_pdf(
        pdf_factory("two_grids"), max_table_analysis_pages=1
    ).as_dict()

    analysed, skipped = payload["pages"]
    assert analysed["table_analysis"] == "analysed"
    assert analysed["table_count"] >= 1
    assert skipped["table_analysis"] == "skipped_budget"
    assert skipped["table_count"] == 0
    assert skipped["table_candidate"] is False
    assert "table_analysis_budget_exhausted" in payload["warnings"]


def test_drawing_analysis_can_be_disabled(pdf_factory):
    result = inspect_pdf(pdf_factory("diagram"), detect_drawings=False)
    page = result.pages[0]

    assert page.vector_drawing_count == 0
    assert page.diagram_candidate is False
    assert page.table_analysis == "skipped_no_graphics"


def test_plain_text_page_skips_table_analysis_with_a_reason(pdf_factory):
    page = inspect_pdf(pdf_factory("text")).pages[0]

    assert page.table_analysis == "skipped_no_graphics"


def test_table_analysis_failure_is_not_reported_as_analysed(pdf_factory, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("table analysis unavailable")

    monkeypatch.setattr(pymupdf.Page, "find_tables", boom)
    payload = inspect_pdf(pdf_factory("grid")).as_dict()
    page = payload["pages"][0]

    assert page["table_analysis"] == "analysis_failed"
    assert page["table_count"] == 0
    assert page["table_candidate"] is False
    assert "table_analysis_failed" in page["warnings"]
    # The exception text is not copied into the output.
    assert "table analysis unavailable" not in json.dumps(payload)


def test_failed_table_analysis_does_not_consume_the_budget(pdf_factory, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("table analysis unavailable")

    monkeypatch.setattr(pymupdf.Page, "find_tables", boom)
    payload = inspect_pdf(
        pdf_factory("two_grids"), max_table_analysis_pages=1
    ).as_dict()

    assert [page["table_analysis"] for page in payload["pages"]] == [
        "analysis_failed",
        "analysis_failed",
    ]
    assert "table_analysis_budget_exhausted" not in payload["warnings"]


def test_successful_table_analysis_does_consume_the_budget(pdf_factory):
    payload = inspect_pdf(
        pdf_factory("two_grids"), max_table_analysis_pages=1
    ).as_dict()

    assert [page["table_analysis"] for page in payload["pages"]] == [
        "analysed",
        "skipped_budget",
    ]


# --------------------------------------------------------------------------- #
# CLI (FIX-02: password is never a command-line argument)
# --------------------------------------------------------------------------- #
CLI_PATH = REPO_ROOT / "scripts" / "inspect_pdf.py"


def _run_cli(*args: str, stdin_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_PATH), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        check=False,
    )


def _encrypted_pdf(tmp_path: Path, secret: str) -> Path:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), LONG_TEXT, fontsize=10)
    target = tmp_path / "locked.pdf"
    document.save(
        str(target),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw=secret,
        owner_pw=secret,
    )
    document.close()
    return target


def test_cli_prints_json_for_text_pdf(pdf_factory):
    proc = _run_cli(str(pdf_factory("text")))

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["page_count"] == 1
    assert payload["pages"][0]["classification"] == "text"


def test_cli_summary_shows_table_analysis_state(pdf_factory):
    proc = _run_cli(str(pdf_factory("grid")), "--summary")

    assert proc.returncode == 0, proc.stderr
    assert "table_analysis=analysed" in proc.stdout
    assert "inspected=1 skipped=0" in proc.stdout


def test_cli_rejects_password_as_a_command_line_argument(pdf_factory):
    proc = _run_cli(str(pdf_factory("text")), "--password", "secret123")

    assert proc.returncode == 2
    assert "not accepted on the command line" in proc.stderr
    # The rejected value is not echoed back into the terminal.
    assert "secret123" not in proc.stdout
    assert "secret123" not in proc.stderr


def test_cli_rejects_equals_form_password_argument(pdf_factory):
    proc = _run_cli(str(pdf_factory("text")), "--password=secret123")

    assert proc.returncode == 2
    assert "not accepted on the command line" in proc.stderr
    assert "secret123" not in proc.stderr


def test_cli_reads_the_password_from_a_prompt(tmp_path: Path):
    secret = "s3cret-passphrase-value"
    target = _encrypted_pdf(tmp_path, secret)

    proc = _run_cli(str(target), "--ask-password", stdin_text=secret + "\n")

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["page_count"] == 1
    assert secret not in proc.stdout
    assert secret not in proc.stderr


def test_cli_refuses_an_encrypted_pdf_without_prompt(tmp_path: Path):
    secret = "s3cret-passphrase-value"
    target = _encrypted_pdf(tmp_path, secret)

    proc = _run_cli(str(target))

    assert proc.returncode == 2
    assert "PdfEncryptedError" in proc.stderr
    assert secret not in proc.stderr


def test_cli_refuses_an_empty_password(tmp_path: Path):
    target = _encrypted_pdf(tmp_path, "s3cret-passphrase-value")

    proc = _run_cli(str(target), "--ask-password", stdin_text="\n")

    assert proc.returncode == 2
    assert "no password supplied" in proc.stderr


def test_cli_missing_file_exits_2(tmp_path: Path):
    proc = _run_cli(str(tmp_path / "nope.pdf"))

    assert proc.returncode == 2
    assert "PdfNotFoundError" in proc.stderr


# --------------------------------------------------------------------------- #
# Password handling (OSJ refinement 4)
# --------------------------------------------------------------------------- #
def test_password_is_never_stored_or_reported(tmp_path: Path):
    secret = "s3cret-passphrase-value"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 100), LONG_TEXT, fontsize=10)
    target = tmp_path / "locked.pdf"
    document.save(
        str(target),
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw=secret,
        owner_pw=secret,
    )
    document.close()

    result = inspect_pdf(target, password=secret)
    assert secret not in json.dumps(result.as_dict())
    assert secret not in result.to_json()

    with pytest.raises(PdfEncryptedError) as missing:
        inspect_pdf(target)
    assert secret not in str(missing.value)

    with pytest.raises(PdfEncryptedError) as wrong:
        inspect_pdf(target, password="not-the-password")
    assert secret not in str(wrong.value)


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #
def test_repository_tracks_no_restricted_documents():
    git = shutil.which("git")
    if git is None:  # pragma: no cover - git is present on the development laptop
        pytest.skip("git is not available; cannot assert repository privacy")

    tracked = subprocess.run(
        [git, "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    restricted = (".pdf", ".docx", ".xlsx", ".csv", ".sqlite", ".db", ".parquet")
    offenders = [path for path in tracked if path.lower().endswith(restricted)]

    assert offenders == []
