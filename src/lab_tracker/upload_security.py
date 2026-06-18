"""Shared upload size and content-type validation."""

from __future__ import annotations

from collections.abc import Iterable

from starlette.requests import Request

from lab_tracker.errors import PayloadTooLargeError, ValidationError

DEFAULT_CONTENT_TYPE = "application/octet-stream"

_BLOCKED_UPLOAD_CONTENT_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/x-httpd-php",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/ecmascript",
    "text/html",
    "text/javascript",
}

_ALLOWED_UPLOAD_EXACT_TYPES = {
    DEFAULT_CONTENT_TYPE,
    "application/gzip",
    "application/json",
    "application/nwb",
    "application/pdf",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/x-bzip2",
    "application/x-gzip",
    "application/x-hdf5",
    "application/x-nwb",
    "application/x-tar",
    "application/zip",
}

_ALLOWED_UPLOAD_PREFIXES = (
    "audio/",
    "image/",
    "text/",
    "video/",
)


def normalize_upload_content_type(content_type: str | None) -> str:
    """Return a lowercase, parameter-free upload content type."""

    cleaned = (content_type or DEFAULT_CONTENT_TYPE).split(";", 1)[0].strip().lower()
    return cleaned or DEFAULT_CONTENT_TYPE


def validate_upload_content_type(content_type: str | None) -> str:
    """Normalize and validate a client-supplied upload content type."""

    normalized = normalize_upload_content_type(content_type)
    if normalized in _BLOCKED_UPLOAD_CONTENT_TYPES:
        raise ValidationError(f"Upload content type {normalized!r} is not allowed.")
    if normalized in _ALLOWED_UPLOAD_EXACT_TYPES or any(
        normalized.startswith(prefix) for prefix in _ALLOWED_UPLOAD_PREFIXES
    ):
        return normalized
    raise ValidationError(f"Upload content type {normalized!r} is not allowed.")


def enforce_request_content_length_limit(
    request: Request,
    *,
    max_bytes: int,
) -> None:
    """Reject obviously oversized uploads before reading the body."""

    raw_content_length = request.headers.get("content-length")
    if raw_content_length is None:
        return
    try:
        content_length = int(raw_content_length)
    except ValueError:
        return
    if content_length > max_bytes:
        raise PayloadTooLargeError(
            f"Upload exceeds the configured limit of {max_bytes} bytes."
        )


def enforce_stream_size_limit(size_bytes: int, *, max_bytes: int | None) -> None:
    if max_bytes is not None and size_bytes > max_bytes:
        raise PayloadTooLargeError(
            f"Upload exceeds the configured limit of {max_bytes} bytes."
        )


def size_limited_chunks(
    chunks: Iterable[bytes],
    *,
    max_bytes: int | None,
) -> Iterable[bytes]:
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        enforce_stream_size_limit(total, max_bytes=max_bytes)
        yield chunk
