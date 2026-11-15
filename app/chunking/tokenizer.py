"""Token counting used for chunk sizing.

M3 ships a **documented estimate**, not a model tokenizer: ``whitespace_split_v1``
counts runs of non-whitespace characters. It needs no new dependency, is stable
across machines, and is published on every chunk as ``token_estimate_method`` so
a later milestone can add the model tokenizer beside it instead of silently
changing the meaning of ``token_estimate``.

Token spans are returned as character offsets, which is what makes chunk
boundaries and provenance exact: a window is a token range, and the chunk text
is the substring between the first token's start and the last token's end.
"""

from __future__ import annotations

import re

#: Identifier published on every chunk and inside the configuration fingerprint.
TOKEN_ESTIMATE_METHOD = "whitespace_split_v1"

_TOKEN_RE = re.compile(r"\S+")


def token_spans(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` character offsets for every whitespace token."""

    return [(match.start(), match.end()) for match in _TOKEN_RE.finditer(text)]


def estimate_tokens(text: str) -> int:
    """Count whitespace-separated tokens in ``text``."""

    return sum(1 for _match in _TOKEN_RE.finditer(text))


class WhitespaceTokenCounter:
    """Small object form of the estimator, kept for callers that inject a counter."""

    method: str = TOKEN_ESTIMATE_METHOD

    def count(self, text: str) -> int:
        return estimate_tokens(text)

    def spans(self, text: str) -> list[tuple[int, int]]:
        return token_spans(text)


__all__ = [
    "TOKEN_ESTIMATE_METHOD",
    "WhitespaceTokenCounter",
    "estimate_tokens",
    "token_spans",
]
