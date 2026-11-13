"""Command-line entry point for the PDF inspection module.

Usage::

    python scripts/inspect_pdf.py data/synthetic_radar_manual.pdf
    python scripts/inspect_pdf.py data/manual.pdf --output data/inspection.json
    python scripts/inspect_pdf.py data/manual.pdf --summary
    python scripts/inspect_pdf.py data/locked.pdf --ask-password

An encrypted PDF is opened through an interactive prompt (``getpass``): the
password is never accepted as a command-line argument, so it cannot leak into a
process listing, shell history or terminal scrollback. The JSON output is the
module's structured result. Nothing is uploaded and no document content leaves
this machine.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.ingestion.pdf_inspector import (
    MIN_TEXT_CHARS,
    PdfInspectionError,
    inspect_pdf,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect a local PDF and print page-aware extraction metadata as JSON."
    )
    parser.add_argument("pdf", help="Path to a local PDF file.")
    parser.add_argument(
        "--output",
        help="Write the JSON result to this path instead of standard output.",
    )
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help=(
            "Prompt for the PDF password interactively (getpass). A password is "
            "never accepted as a command-line argument."
        ),
    )
    parser.add_argument(
        "--min-text-chars",
        type=int,
        default=MIN_TEXT_CHARS,
        help=f"Non-whitespace character threshold for page classification (default {MIN_TEXT_CHARS}).",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a one-line-per-page summary instead of the full JSON document.",
    )
    parser.add_argument(
        "--no-table-detection",
        action="store_true",
        help="Skip PyMuPDF table analysis (about 0.65 s per analysed page on the development laptop).",
    )
    return parser


def print_summary(result) -> None:
    document = result.as_dict()
    print(
        f"file={document['filename']} pages={document['page_count']} "
        f"inspected={document['pages_inspected']} skipped={document['pages_skipped']} "
        f"size={document['file_size_bytes']}B sha256={document['sha256']}"
    )
    for page in document["pages"]:
        print(
            f"  page {page['page_number']} (index {page['page_index']}): "
            f"{page['classification']:<10} chars={page['char_count']:<5} "
            f"images={page['image_count']} drawings={page['vector_drawing_count']} "
            f"tables={page['table_count']} table_analysis={page['table_analysis']} "
            f"ocr_candidate={page['ocr_candidate']}"
        )
    print(f"  warnings: {document['warnings'] or 'none'}")
    print(f"  duration_ms: {document['extraction_duration_ms']}")


def _prompt_password() -> str | None:
    """Read the PDF password; never from the command line.

    Interactive runs use :func:`getpass.getpass` so the typed value is not
    echoed. When stdin is redirected (automation, tests), the password is read
    from stdin, which still keeps it out of the process argument list.
    """

    if not sys.stdin.isatty():
        print(
            "Note: stdin is not a terminal; reading the password from stdin.",
            file=sys.stderr,
        )
        secret = sys.stdin.readline().rstrip("\r\n")
        return secret or None
    try:
        return getpass.getpass("PDF password: ")
    except (EOFError, KeyboardInterrupt):
        return None


def _reject_inline_password(argv: list[str]) -> bool:
    """Refuse a password passed as an argument, without echoing its value."""

    for argument in argv:
        name = argument.split("=", 1)[0]
        if name in {"--password", "--pass", "-p"}:
            print(
                "PDF inspection failed: passwords are not accepted on the command "
                "line; use --ask-password.",
                file=sys.stderr,
            )
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _reject_inline_password(raw_argv):
        return 2
    args = build_parser().parse_args(raw_argv)
    password = _prompt_password() if args.ask_password else None
    if args.ask_password and password is None:
        print("PDF inspection failed: no password supplied", file=sys.stderr)
        return 2
    try:
        result = inspect_pdf(
            args.pdf,
            password=password,
            min_text_chars=args.min_text_chars,
            detect_tables=not args.no_table_detection,
        )
    except PdfInspectionError as error:
        print(
            f"PDF inspection failed: {type(error).__name__}: {error}", file=sys.stderr
        )
        return 2

    if args.summary:
        print_summary(result)
        return 0

    payload = json.dumps(result.as_dict(), indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
