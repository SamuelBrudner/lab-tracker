"""Shared upload size and content-type validation."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lab_tracker.errors import PayloadTooLargeError, ValidationError

_logger = logging.getLogger(__name__)

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
    """Reject a request whose declared Content-Length exceeds ``max_bytes``.

    Route handlers run after FastAPI has parsed (and spooled) a multipart body,
    so this is a secondary check; ``UploadBodySizeLimitMiddleware`` enforces the
    limit before and while the body is read.
    """

    content_length = _declared_content_length(request.headers.get("content-length"))
    if content_length is not None and content_length > max_bytes:
        raise PayloadTooLargeError(_upload_limit_message(max_bytes))


def _declared_content_length(raw_content_length: str | None) -> int | None:
    if raw_content_length is None:
        return None
    try:
        return int(raw_content_length)
    except ValueError:
        return None


def _upload_limit_message(max_bytes: int) -> str:
    return f"Upload exceeds the configured limit of {max_bytes} bytes."


class _UploadBodyTooLarge(MultiPartException):
    """Raised from the wrapped ``receive`` once a body passes its byte budget.

    It is a ``MultiPartException`` so Starlette's multipart parser closes the
    temporary files it already spooled before propagating the failure.
    """


class UploadBodySizeLimitMiddleware:
    """Cap multipart request bodies at the configured upload limit while they stream.

    FastAPI parses ``File()`` parameters with Starlette's multipart parser, which
    spools file parts to disk without a size cap before any route code runs, and
    chunked requests carry no Content-Length to pre-check. This middleware
    rejects a declared oversized Content-Length without reading the body, and
    otherwise counts body bytes as they are received, aborting the read with
    413 as soon as the total passes the limit. The limit applies to the whole
    multipart body, matching the Content-Length checks in the upload routes.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: Callable[[Scope], int]) -> None:
        self.app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_multipart_request(scope):
            await self.app(scope, receive, send)
            return
        max_bytes = self._max_bytes(scope)
        declared = _declared_content_length(_header(scope, b"content-length"))
        if declared is not None and declared > max_bytes:
            await _send_payload_too_large(scope, send, max_bytes)
            return

        received_bytes = 0
        exceeded = False
        response_started = False
        replaced_response = False

        async def limited_receive() -> Message:
            nonlocal received_bytes, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_bytes:
                    exceeded = True
                    raise _UploadBodyTooLarge(_upload_limit_message(max_bytes))
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started, replaced_response
            if replaced_response:
                return
            if exceeded and not response_started:
                # Inner layers turned the aborted body read into their own error
                # response (FastAPI reports body-parse failures as 400); answer
                # with the size error the client actually hit.
                replaced_response = True
                response_started = True
                await _send_payload_too_large(scope, send, max_bytes)
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded or response_started:
                raise
            await _send_payload_too_large(scope, send, max_bytes)


def _is_multipart_request(scope: Scope) -> bool:
    content_type = _header(scope, b"content-type") or ""
    return content_type.split(";", 1)[0].strip().lower() == "multipart/form-data"


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if bytes(key).lower() == name:
            return bytes(value).decode("latin-1")
    return None


async def _send_payload_too_large(scope: Scope, send: Send, max_bytes: int) -> None:
    message = _upload_limit_message(max_bytes)
    _logger.warning(
        "Handled HTTP error: method=%s path=%s status_code=%s code=%s detail=%s",
        scope.get("method"),
        scope.get("path"),
        413,
        "payload_too_large",
        message,
    )
    body = json.dumps(
        {"error": {"code": "payload_too_large", "message": message, "issues": None}},
        separators=(",", ":"),
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # The rest of the request body is never read.
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def enforce_stream_size_limit(size_bytes: int, *, max_bytes: int | None) -> None:
    if max_bytes is not None and size_bytes > max_bytes:
        raise PayloadTooLargeError(_upload_limit_message(max_bytes))
