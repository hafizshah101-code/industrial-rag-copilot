"""Ingestion layer: turn local documents into page-aware, provenance-preserving data."""

from app.ingestion.pdf_inspector import (
    DocumentInspection,
    PageInspection,
    PdfCorruptedError,
    PdfEmptyFileError,
    PdfEncryptedError,
    PdfInspectionError,
    PdfNotFoundError,
    UnsupportedFileTypeError,
    VisibleImage,
    inspect_pdf,
)

__all__ = [
    "DocumentInspection",
    "PageInspection",
    "PdfCorruptedError",
    "PdfEmptyFileError",
    "PdfEncryptedError",
    "PdfInspectionError",
    "PdfNotFoundError",
    "UnsupportedFileTypeError",
    "VisibleImage",
    "inspect_pdf",
]
