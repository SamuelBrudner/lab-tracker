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
facades retain their typed errors and SDK-vs-MCP transport-failure split.
Connection failures include stage-specific diagnostics from the same request.

Uploads stream from a file handle with a local size preflight, so a note or
visualization upload never materializes a whole file in memory and an oversize
file is rejected before any bytes cross the wire; the server remains the
authority and re-checks Content-Length.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import httpx

from lab_tracker_client.connection_diagnostics import ConnectionTrace

JsonObject = dict[str, Any]

# Neutral copy of the server's upload ceiling. Kept here rather than imported
# from the server config (which would pull starlette into the consumer package)
# so a client can reject an oversize file locally before transferring it.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


# The advisory /health probes (`lt setup status`, the `lt-mcp` startup check) must
# not hold a session. httpx timeouts apply to each connect, write, and read on its
# own, so a server that answers and then trickles its headers or body a byte at a
# time never trips them; this is the wall-clock limit on the response.
HEALTH_PROBE_DEADLINE_SECONDS = 4.0
# /health answers with a small JSON document; anything larger is cut, not read.
HEALTH_PROBE_MAX_BODY_BYTES = 64 * 1024
# Headers that describe the bytes on the wire, which the returned response no longer has.
_WIRE_ENCODING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})
# What a stalled read raises once the watchdog has closed its connection. Connect
# and proxy failures are not in it: they keep their own, more specific diagnosis.
_READ_FAILURES = (httpx.ReadError, httpx.RemoteProtocolError)


def request_within_deadline(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    deadline_seconds: float,
    max_body_bytes: int = HEALTH_PROBE_MAX_BODY_BYTES,
    **kwargs: Any,
) -> httpx.Response:
    """Send one request, giving up once its response has taken ``deadline_seconds``.

    The body is streamed, so a slow or endless one is noticed as it arrives
    instead of after ``read()`` returns, and it is cut at ``max_body_bytes``
    (a cut body no longer parses as JSON, so it reads as an unusable reply).
    The result is an ordinary, fully read :class:`httpx.Response` with the
    body decoded, so callers use it exactly as they would ``client.request``'s.

    The deadline runs from the call and covers the wait for the response
    headers as well as the body. It is checked as each chunk arrives, and a
    watchdog timer closes ``client`` when it expires, which fails a read still
    waiting on a server that trickles its headers or body a byte at a time.
    That read notices the close when its next byte or per-phase timeout
    arrives, so the deadline can be overrun by one read, which httpx's own
    per-phase timeout bounds. Getting connected is not covered: the watchdog
    cannot interrupt name resolution or a connect in progress, resolution has no
    limit of its own, and httpx's connect timeout applies to each address a name
    resolves to, not to all of them together. Exceeding the deadline raises
    :class:`httpx.ReadTimeout`, like any stalled response.

    An expired deadline leaves ``client`` closed, so pass one that serves this
    request alone.
    """

    started = time.monotonic()
    expired = threading.Event()

    def out_of_time() -> httpx.ReadTimeout:
        return httpx.ReadTimeout(
            f"The response did not finish within {deadline_seconds:g} seconds."
        )

    def check_deadline() -> None:
        if time.monotonic() - started > deadline_seconds:
            raise out_of_time()

    def close_client() -> None:
        expired.set()
        client.close()

    watchdog = threading.Timer(deadline_seconds, close_client)
    watchdog.daemon = True
    watchdog.start()
    try:
        return _read_response(
            client, method, url, max_body_bytes, check_deadline=check_deadline, **kwargs
        )
    except _READ_FAILURES as exc:
        # The watchdog closed the connection under a read that was still waiting;
        # a plain socket reports that as a read error, a TLS one as a disconnect.
        if expired.is_set():
            raise out_of_time() from exc
        raise
    finally:
        watchdog.cancel()


def _read_response(
    client: httpx.Client,
    method: str,
    url: str,
    max_body_bytes: int,
    *,
    check_deadline: Callable[[], None],
    **kwargs: Any,
) -> httpx.Response:
    body = bytearray()
    with client.stream(method, url, **kwargs) as response:
        check_deadline()
        for chunk in response.iter_bytes():
            body += chunk
            if len(body) >= max_body_bytes:
                break
            check_deadline()
        headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _WIRE_ENCODING_HEADERS
        ]
        return httpx.Response(
            response.status_code,
            headers=headers,
            content=bytes(body[:max_body_bytes]),
            request=response.request,
        )


_SENTENCE_END = (".", "!", "?")


def _join_sentences(*parts: str) -> str:
    """Join message parts with spaces, ending each non-final part with a full stop."""

    return " ".join(
        part if part.endswith(_SENTENCE_END) or index == len(parts) - 1 else part + "."
        for index, part in enumerate(parts)
    )


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

    def send(
        self,
        method: str,
        path: str,
        *,
        timeout: Any = None,
        deadline_seconds: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Raw send with connection-error wrapping; no auth header, no retry.

        ``deadline_seconds`` also limits the whole response, headers included (see
        :func:`request_within_deadline`), and an expired one closes this transport's
        client; ``None`` keeps httpx's per-phase timeouts.
        """

        # Only forward an explicit per-request timeout; passing timeout=None to
        # httpx would disable the timeout rather than use the client default.
        if timeout is not None:
            kwargs["timeout"] = timeout
        trace = ConnectionTrace(self._base_url)
        kwargs["extensions"] = {"trace": trace}
        try:
            if deadline_seconds is None:
                return self._client.request(method, path, **kwargs)
            return request_within_deadline(
                self._client, method, path, deadline_seconds=deadline_seconds, **kwargs
            )
        except httpx.HTTPError as exc:
            diagnostic = trace.diagnose(exc)
            wrapped = self._auth.wrap_transport_error(method, path, exc)
            # Keep each facade's public exception type while attaching safe metadata.
            setattr(wrapped, "connection_diagnostic", diagnostic)  # noqa: B010
            wrapped.args = (
                _join_sentences(str(wrapped), diagnostic["detail"], diagnostic["next_step"]),
            )
            raise wrapped from exc

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
        preserve_json_nulls: bool = False,
        timeout: Any = None,
        deadline_seconds: float | None = None,
    ) -> httpx.Response:
        """Send with the surface header + bearer auth and a single 401 retry.

        Returns the raw response; the facade translates status codes and JSON so
        its exact error types and messages are preserved. JSON ``None`` values
        are omitted by default; callers that already distinguish field presence
        may opt in to preserving them for explicit-null PATCH semantics.
        """

        headers = self._headers(authenticated)
        prepared_json = json if preserve_json_nulls else drop_empty(json)
        response = self.send(
            method,
            path,
            params=drop_empty(params),
            json=prepared_json,
            data=data,
            files=files,
            headers=headers,
            timeout=timeout,
            deadline_seconds=deadline_seconds,
        )
        if response.status_code == 401 and authenticated and retry_on_unauthorized:
            headers["Authorization"] = f"Bearer {self._auth.refresh_bearer(response)}"
            response = self.send(
                method,
                path,
                params=drop_empty(params),
                json=prepared_json,
                data=data,
                files=files,
                headers=headers,
                timeout=timeout,
                deadline_seconds=deadline_seconds,
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
