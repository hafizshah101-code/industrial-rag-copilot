"""Chunking layer: turn page-aware text into retrievable, traceable chunks (M3).

The public entry point is :func:`chunk_document`. Layout reading, heading
detection and offset mapping live in :mod:`app.chunking.headings`; token
estimation lives in :mod:`app.chunking.tokenizer`. Nothing here calls an LLM,
runs OCR, generates embeddings or touches the network.
"""

from app.chunking.chunker import (
    CHUNKER_VERSION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_STRATEGY,
    DEFAULT_TARGET_TOKENS,
    METADATA_KEYS,
    MIN_SECTION_FRAGMENT_TOKENS,
    STRATEGIES,
    STRATEGY_FIXED_TOKEN,
    STRATEGY_SECTION_AWARE,
    UNTITLED_SECTION,
    Chunk,
    ChunkingConfig,
    ChunkingResult,
    SkippedPage,
    build_config,
    chunk_document,
    compare_strategies,
    normalise_metadata,
)
from app.chunking.errors import (
    ChunkingConfigError,
    ChunkingError,
    LayoutSourceMismatchError,
    LayoutSourceRequiredError,
)
from app.chunking.headings import (
    MAPPING_RATIO_THRESHOLD,
    HeadingCandidate,
    LayoutAnalysis,
    LayoutDocument,
    LayoutExclusion,
    LayoutLine,
    PageLayout,
    PageLayoutAnalysis,
    analyse_layout,
    read_layout,
)
from app.chunking.tokenizer import (
    TOKEN_ESTIMATE_METHOD,
    WhitespaceTokenCounter,
    estimate_tokens,
)

__all__ = [
    "CHUNKER_VERSION",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "DEFAULT_STRATEGY",
    "DEFAULT_TARGET_TOKENS",
    "MAPPING_RATIO_THRESHOLD",
    "METADATA_KEYS",
    "MIN_SECTION_FRAGMENT_TOKENS",
    "STRATEGIES",
    "STRATEGY_FIXED_TOKEN",
    "STRATEGY_SECTION_AWARE",
    "TOKEN_ESTIMATE_METHOD",
    "UNTITLED_SECTION",
    "Chunk",
    "ChunkingConfig",
    "ChunkingConfigError",
    "ChunkingError",
    "ChunkingResult",
    "HeadingCandidate",
    "LayoutAnalysis",
    "LayoutDocument",
    "LayoutExclusion",
    "LayoutLine",
    "LayoutSourceMismatchError",
    "LayoutSourceRequiredError",
    "PageLayout",
    "PageLayoutAnalysis",
    "SkippedPage",
    "WhitespaceTokenCounter",
    "analyse_layout",
    "build_config",
    "chunk_document",
    "compare_strategies",
    "estimate_tokens",
    "normalise_metadata",
    "read_layout",
]
