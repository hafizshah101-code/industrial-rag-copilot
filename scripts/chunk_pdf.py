"""Command-line entry point for the chunking module (Issue #3 / M3).

Usage::

    python scripts/chunk_pdf.py data/manual.pdf
    python scripts/chunk_pdf.py data/manual.pdf --strategy fixed_token --summary
    python scripts/chunk_pdf.py data/manual.pdf --target-tokens 320 --max-tokens 400
    python scripts/chunk_pdf.py data/manual.pdf --output data/chunks.json
    python scripts/chunk_pdf.py data/manual.pdf --compare
    python scripts/chunk_pdf.py data/manual.pdf --ask-password

The script inspects the PDF with the frozen M2 inspector and then chunks the
result, so the text used for chunking is exactly the text M2 published. An
encrypted PDF is opened through an interactive prompt (``getpass``): a password
is never accepted as a command-line argument, so it cannot leak into a process
listing or shell history. Nothing is uploaded; no document content leaves this
machine.
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

from app.chunking.chunker import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TARGET_TOKENS,
    STRATEGIES,
    STRATEGY_SECTION_AWARE,
    chunk_document,
    compare_strategies,
)
from app.chunking.errors import ChunkingError
from app.ingestion.pdf_inspector import (
    MIN_TEXT_CHARS,
    PdfEncryptedError,
    PdfInspectionError,
    inspect_pdf,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect a local PDF and chunk it with page and section provenance."
        )
    )
    parser.add_argument("pdf", help="Path to a local PDF file.")
    parser.add_argument(
        "--strategy",
        choices=list(STRATEGIES),
        default=STRATEGY_SECTION_AWARE,
        help=f"Chunking strategy (default {STRATEGY_SECTION_AWARE}).",
    )
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=DEFAULT_TARGET_TOKENS,
        help=f"Soft size target in whitespace tokens (default {DEFAULT_TARGET_TOKENS}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Hard size limit in whitespace tokens (default {DEFAULT_MAX_TOKENS}).",
    )
    parser.add_argument(
        "--overlap-tokens",
        type=int,
        default=None,
        help="Shared tokens between parts (default 32 section / 64 baseline).",
    )
    parser.add_argument(
        "--min-text-chars",
        type=int,
        default=MIN_TEXT_CHARS,
        help=f"Page classification threshold (default {MIN_TEXT_CHARS}).",
    )
    parser.add_argument(
        "--equipment-model",
        default=None,
        help="Operator-supplied equipment model; never parsed from the document.",
    )
    parser.add_argument(
        "--revision",
        default=None,
        help="Operator-supplied revision label; never parsed from the document.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the JSON result to this path instead of standard output.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print a one-line-per-chunk summary instead of the full JSON.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run both strategies and print a comparison table.",
    )
    parser.add_argument(
        "--ask-password",
        action="store_true",
        help=(
            "Prompt for the PDF password interactively (getpass). A password is "
            "never accepted as a command-line argument."
        ),
    )
    return parser


def _reject_inline_password(argv: list[str]) -> bool:
    """Refuse a password passed as an argument, without echoing its value."""

    for argument in argv:
        name = argument.split("=", 1)[0]
        if name in {"--password", "--pass", "-p"}:
            print(
                "Chunking failed: passwords are not accepted on the command line; "
                "use --ask-password.",
                file=sys.stderr,
            )
            return True
    return False


def _prompt_password() -> str | None:
    """Read the PDF password; never from the command line."""

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


def _inspect_with_password(
    pdf: str, *, min_text_chars: int, ask_password: bool
) -> tuple[object, str | None]:
    """Inspect the PDF and return the inspection plus the password to reuse.

    The same password must travel into the section-aware layout pass: M2
    authenticates one PyMuPDF handle, and the layout probe opens the file again
    (OSJ D-04). It is read from getpass/stdin only, never from ``argv``.
    """

    password = _prompt_password() if ask_password else None
    if ask_password and password is None:
        raise PdfEncryptedError("no password supplied")
    try:
        return (
            inspect_pdf(pdf, password=password, min_text_chars=min_text_chars),
            password,
        )
    except PdfEncryptedError:
        if ask_password or not sys.stdin.isatty():
            raise
        password = _prompt_password()
        return (
            inspect_pdf(pdf, password=password, min_text_chars=min_text_chars),
            password,
        )


def print_summary(result) -> None:
    stats = result.stats
    print(
        f"file={result.filename} strategy={result.strategy} "
        f"chunks={stats['chunks']} pages_covered={stats['pages_covered']} "
        f"needs_ocr={stats['pages_needs_ocr']} skipped={stats['pages_skipped']} "
        f"tokens min/median/max="
        f"{stats['token_estimate_min']}/{stats['token_estimate_median']}/"
        f"{stats['token_estimate_max']} hard_cuts={stats['hard_cuts']} "
        f"section_splits={stats['section_splits']}"
    )
    for chunk in result.chunks:
        heading = chunk.heading or "-"
        print(
            f"  [{chunk.chunk_index:>3}] pages {chunk.page_start}-{chunk.page_end} "
            f"tokens={chunk.token_estimate:<5} path={'/'.join(chunk.section_path):<12} "
            f"origin={chunk.boundary['origin']:<7} "
            f"start={chunk.boundary['starts_at']:<14} "
            f"end={chunk.boundary['ends_at']:<12} heading={heading}"
        )
    print(f"  needs_ocr: {result.needs_ocr or 'none'}")
    print(f"  skipped: {[page.as_dict() for page in result.skipped_pages] or 'none'}")
    print(f"  warnings: {result.warnings or 'none'}")
    print(f"  duration_ms: {result.duration_ms}")


def print_comparison(comparison) -> None:
    print(f"file={comparison['filename']}")
    header = (
        f"  {'strategy':<14}{'chunks':>7}{'section':>9}{'split':>7}"
        f"{'min':>6}{'median':>8}{'max':>6}{'sections':>10}{'cut':>5}"
    )
    print(header)
    for row in comparison["strategies"]:
        print(
            f"  {row['strategy']:<14}{row['chunks']:>7}"
            f"{row['chunks_by_section']:>9}{row['chunks_by_split']:>7}"
            f"{row['token_estimate_min']:>6}{row['token_estimate_median']:>8}"
            f"{row['token_estimate_max']:>6}{row['sections']:>10}"
            f"{row['hard_cuts']:>5}"
        )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _reject_inline_password(raw_argv):
        return 2
    args = build_parser().parse_args(raw_argv)

    try:
        inspection, password = _inspect_with_password(
            args.pdf,
            min_text_chars=args.min_text_chars,
            ask_password=args.ask_password,
        )
    except PdfInspectionError as error:
        print(
            f"PDF inspection failed: {type(error).__name__}: {error}", file=sys.stderr
        )
        return 2

    metadata = {
        key: value
        for key, value in (
            ("equipment_model", args.equipment_model),
            ("revision", args.revision),
        )
        if value is not None
    }

    try:
        if args.compare:
            comparison = compare_strategies(
                inspection,
                source_path=args.pdf,
                target_tokens=args.target_tokens,
                max_tokens=args.max_tokens,
                overlap_tokens=args.overlap_tokens,
                metadata=metadata or None,
                password=password,
                min_text_chars=args.min_text_chars,
            )
            if args.output:
                Path(args.output).write_text(
                    json.dumps(comparison, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                print(f"Wrote {args.output}")
            print_comparison(comparison)
            return 0

        result = chunk_document(
            inspection,
            strategy=args.strategy,
            source_path=args.pdf,
            target_tokens=args.target_tokens,
            max_tokens=args.max_tokens,
            overlap_tokens=args.overlap_tokens,
            min_text_chars=args.min_text_chars,
            metadata=metadata or None,
            password=password,
        )
    except (ChunkingError, PdfInspectionError) as error:
        print(f"Chunking failed: {type(error).__name__}: {error}", file=sys.stderr)
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
