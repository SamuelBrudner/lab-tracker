from __future__ import annotations

import io
import socket
import ssl
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpTransportError,
)


class FakeAddressResolver:
    """Deterministic DNS seam with call recording and optional rebinding answers."""

    def __init__(
        self,
        answers: Mapping[str, Sequence[str] | BaseException] | None = None,
        *,
        sequences: Mapping[
            str,
            Sequence[Sequence[str] | BaseException],
        ]
        | None = None,
    ) -> None:
        self._answers = dict(answers or {})
        self._sequences = {
            hostname: list(host_answers) for hostname, host_answers in (sequences or {}).items()
        }
        self.calls: list[tuple[str, int]] = []

    def __call__(
        self,
        hostname: str,
        port: int,
    ) -> tuple[ApprovedSocketAddress, ...]:
        return self.resolve(hostname, port)

    def resolve(
        self,
        hostname: str,
        port: int,
    ) -> tuple[ApprovedSocketAddress, ...]:
        self.calls.append((hostname, port))
        if hostname in self._sequences:
            queued = self._sequences[hostname]
            if not queued:
                raise AssertionError(f"No DNS answers left for {hostname}")
            value: Sequence[str] | BaseException = queued.pop(0)
        else:
            value = self._answers.get(hostname, ())
        if isinstance(value, BaseException):
            raise value
        return tuple(ApprovedSocketAddress.from_ip(ip, port) for ip in value)


@dataclass
class FakeHttpResponse:
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: Sequence[bytes | BaseException] = field(default_factory=tuple)
    closed: bool = False
    iterated_chunks: int = 0

    def get_header(self, name: str) -> str | None:
        lowered = name.lower()
        return next(
            (value for key, value in self.headers.items() if key.lower() == lowered),
            None,
        )

    def iter_bytes(self) -> Iterable[bytes]:
        for chunk in self.chunks:
            self.iterated_chunks += 1
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class FakeSafeHttpClient:
    """Sequential SafeHttpClient fake that never opens a real socket."""

    def __init__(
        self,
        outcomes: Sequence[FakeHttpResponse | BaseException],
    ) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, Any]] = []

    def open(self, method: str, target: Any) -> FakeHttpResponse:
        self.calls.append((method, target))
        if not self._outcomes:
            raise AssertionError("No fake HTTP outcome remains")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingSocket:
    """The socket methods http.client needs, backed by a canned HTTP response."""

    def __init__(self, response_bytes: bytes) -> None:
        self._response_bytes = response_bytes
        self.sent = bytearray()
        self.closed = False

    def sendall(
        self,
        data: bytes,
        flags: int = 0,
    ) -> None:
        if flags:
            raise AssertionError("SafeHttpClient unexpectedly used socket flags")
        self.sent.extend(data)

    def makefile(
        self,
        mode: str,
        buffering: int | None = None,
    ) -> io.BytesIO:
        if mode != "rb":
            raise AssertionError(f"Unexpected socket file mode: {mode}")
        del buffering
        return io.BytesIO(self._response_bytes)

    def close(self) -> None:
        self.closed = True


class RecordingPinnedConnector:
    """Pinned connector fake that records the numeric addresses it receives."""

    def __init__(
        self,
        response_bytes: bytes,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.socket = RecordingSocket(response_bytes)
        self.error = error
        self.calls: list[tuple[tuple[ApprovedSocketAddress, ...], float]] = []

    def connect(
        self,
        addresses: Sequence[ApprovedSocketAddress],
        timeout: float,
    ) -> socket.socket:
        self.calls.append((tuple(addresses), timeout))
        if self.error is not None:
            if isinstance(self.error, OutboundHttpTransportError):
                raise self.error
            raise self.error
        return self.socket  # type: ignore[return-value]


class RecordingSslContext:
    """TLS seam that records SNI while leaving the canned socket untouched."""

    def __init__(self) -> None:
        # Python 3.11's HTTPSConnection inspects this attribute and enables
        # hostname checking before it delegates to wrap_socket.
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True
        self.calls: list[tuple[object, str | None]] = []

    def wrap_socket(
        self,
        raw_socket: object,
        *,
        server_hostname: str | None,
    ) -> object:
        self.calls.append((raw_socket, server_hostname))
        return raw_socket
