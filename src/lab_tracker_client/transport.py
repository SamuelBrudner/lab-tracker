"""Shared streaming HTTP transport for the SDK and MCP client facades.

The consumer ``LabTracker`` SDK and the server-side MCP ``LabTrackerAPIClient``
historically each carried their own copy of the same wire mechanics — base URL
and timeout config, the ``X-LabTracker-Surface`` header, a single 401 retry, and
connection-error wrapping — and the copies had drifted. This module owns those
mechanics once.

Each facade injects a small :class:`TransportAuth` policy (its surface label, how
to obtain a bearer token, how to react to a 401, and which exception a transport
failure becomes) and keeps its own response/error translation and public
exception classes. The transport therefore stays domain-free — it lives in the
consumer package and is imported *up* into the server-side MCP client — and the
facades' observable contracts (exact messages, typed errors, the SDK-vs-MCP
transport-failure split) are unchanged.

Uploads stream from a file handle with a local size preflight, so a note or
visualization upload never materializes a whole file in memory and an oversize
file is rejected before any bytes cross the wire; the server remains the
authority and re-checks Content-Length.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import httpx

JsonObject = dict[str, Any]

# Neutral copy of the server's upload ceiling. Kept here rather than imported
# from the server config (which would pull starlette into the consumer package)
# so a client can reject an oversize file locally before transferring it.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class TransportAuth(Protocol):
    """Per-facade auth policy injected into the shared transport."""

    @property
    def surface(self) -> str:
        """Value of the ``X-LabTracker-Surface`` header (e.g. ``cli``/``mcp``)."""

    def initial_bearer(self) -> str | None:
        """Bearer token for the first authenticated attempt (``None`` to omit)."""

    def refresh_bearer(self, response: httpx.Response) -> str:
        """React to a 401: return a fresh bearer token, or raise the facade's error."""

    def wrap_transport_error(self, method: str, path: str, exc: Exception) -> Exception:
        """Translate a connection/transport failure into the facade's exception type."""


class UploadTooLargeError(Exception):
    """Raised by :func:`preflight_upload_size`; facades translate it to their own type."""

    def __init__(self, message: str, *, size: int) -> None:
        super().__init__(message)
        self.size = size


def drop_empty(payload: JsonObject | None) -> JsonObject | None:
    """Drop ``None``-valued keys so an omitted field inherits the server default."""

    if payload is None:
        return None
    return {key: value for key, value in payload.items() if value is not None}


def preflight_upload_size(path: Path, *, max_bytes: int = MAX_UPLOAD_BYTES) -> int:
    """Return the file's size, rejecting an empty or oversize file before transfer."""

    try:
        size = os.stat(path).st_size
    except OSError as exc:
        raise UploadTooLargeError(f"Could not stat upload file {path}: {exc}", size=-1) from exc
    if size <= 0:
        raise UploadTooLargeError(f"Upload file must not be empty: {path}", size=0)
    if size > max_bytes:
        raise UploadTooLargeError(
            f"Upload file is {size} bytes, over the {max_bytes}-byte limit: {path}",
            size=size,
        )
    return size


class HttpTransport:
    """Owns one ``httpx.Client`` and the shared request/retry/upload path."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        auth: TransportAuth,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = str(base_url).rstrip("/")
        self._auth = auth
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def client(self) -> httpx.Client:
        return self._client

    def close(self) -> None:
        self._client.close()

    def send(self, method: str, path: str, *, timeout: Any = None, **kwargs: Any) -> httpx.Response:
        """Raw send with connection-error wrapping; no auth header, no retry."""

        # Only forward an explicit per-request timeout; passing timeout=None to
        # httpx would disable the timeout rather than use the client default.
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise self._auth.wrap_transport_error(method, path, exc) from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        params: JsonObject | None = None,
        json: JsonObject | None = None,
        data: Mapping[str, str] | None = None,
        files: dict[str, Any] | None = None,
        retry_on_unauthorized: bool = True,
        timeout: Any = None,
    ) -> httpx.Response:
        """Send with the surface header + bearer auth and a single 401 retry.

        Returns the raw response; the facade translates status codes and JSON so
        its exact error types and messages are preserved.
        """

        headers = self._headers(authenticated)
        response = self.send(
            method,
            path,
            params=drop_empty(params),
            json=drop_empty(json),
            data=data,
            files=files,
            headers=headers,
            timeout=timeout,
        )
        if response.status_code == 401 and authenticated and retry_on_unauthorized:
            headers["Authorization"] = f"Bearer {self._auth.refresh_bearer(response)}"
            response = self.send(
                method,
                path,
                params=drop_empty(params),
                json=drop_empty(json),
                data=data,
                files=files,
                headers=headers,
                timeout=timeout,
            )
        return response

    def upload(
        self,
        method: str,
        path: str,
        *,
        field_name: str,
        open_file: Callable[[], BinaryIO],
        filename: str,
        content_type: str,
        data: Mapping[str, str] | None = None,
        authenticated: bool = True,
        retry_on_unauthorized: bool = True,
        timeout: Any = None,
    ) -> httpx.Response:
        """Streaming multipart upload with a single 401 retry.

        ``open_file`` returns a *fresh* binary handle per attempt, so a 401 retry
        re-opens the file rather than replaying an already-consumed stream. A
        seekable handle lets httpx send Content-Length, so the server's size
        preflight still fires.
        """

        headers = self._headers(authenticated)
        with open_file() as handle:
            response = self.send(
                method,
                path,
                data=data,
                files={field_name: (filename, handle, content_type)},
                headers=headers,
                timeout=timeout,
            )
        if response.status_code == 401 and authenticated and retry_on_unauthorized:
            headers["Authorization"] = f"Bearer {self._auth.refresh_bearer(response)}"
            with open_file() as handle:
                response = self.send(
                    method,
                    path,
                    data=data,
                    files={field_name: (filename, handle, content_type)},
                    headers=headers,
                    timeout=timeout,
                )
        return response

    def _headers(self, authenticated: bool) -> dict[str, str]:
        headers: dict[str, str] = {"X-LabTracker-Surface": self._auth.surface}
        if authenticated:
            token = self._auth.initial_bearer()
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
        return headers
