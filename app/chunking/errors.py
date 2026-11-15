"""Controlled exceptions for the M3 chunking layer.

Every failure the chunking layer raises itself is a subclass of
:class:`ChunkingError`, so a caller can distinguish "the chunker refused this
input" from an unexpected programming error. Configuration mistakes raise
:class:`ChunkingConfigError`, which is also a :class:`ValueError` so ordinary
argument validation idioms keep working.
"""

from __future__ import annotations


class ChunkingError(Exception):
    """Base class for every controlled chunking failure."""


class ChunkingConfigError(ChunkingError, ValueError):
    """A chunking setting, strategy or metadata value is not acceptable."""


class LayoutSourceRequiredError(ChunkingError):
    """Section-aware chunking needs a layout source that was not supplied."""


class LayoutSourceMismatchError(ChunkingError):
    """The layout source does not describe the inspected document.

    The chunker re-verifies the SHA256 of ``source_path`` against
    ``inspection.sha256`` before any layout data is used, so layout can never
    describe a different file than the authoritative M2 text.
    """
