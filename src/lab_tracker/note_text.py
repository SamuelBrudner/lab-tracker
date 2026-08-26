"""Shared policy and bounded decoding for uploaded text note assets."""

from __future__ import annotations

import codecs
from dataclasses import dataclass

DEFAULT_NOTE_TEXT_PREVIEW_CHARS = 100_000
MAX_NOTE_TEXT_PREVIEW_CHARS = 256_000

_TEXT_APPLICATION_CONTENT_TYPES = {
    "application/json",
    "application/ld+json",
    "application/markdown",
    "application/x-ndjson",
    "application/xml",
}


def is_text_content_type(content_type: str) -> bool:
    """Return whether an uploaded asset is safe to expose as UTF-8 text."""

    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    return normalized.startswith("text/") or normalized in _TEXT_APPLICATION_CONTENT_TYPES


@dataclass(frozen=True, slots=True)
class NoteTextExcerpt:
    text: str
    truncated: bool
    included_bytes: int
    omitted_bytes: int


def decode_utf8_excerpt(
    payload: bytes,
    *,
    total_size_bytes: int,
    max_chars: int,
    payload_is_complete: bool,
) -> NoteTextExcerpt:
    """Strictly decode a bounded UTF-8 prefix without splitting a code point."""

    if max_chars < 0:
        raise ValueError("max_chars must not be negative.")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    decoded = decoder.decode(payload, final=payload_is_complete)
    text = decoded[:max_chars]
    included_bytes = len(text.encode("utf-8"))
    omitted_bytes = max(0, total_size_bytes - included_bytes)
    return NoteTextExcerpt(
        text=text,
        truncated=omitted_bytes > 0,
        included_bytes=included_bytes,
        omitted_bytes=omitted_bytes,
    )
