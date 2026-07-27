from __future__ import annotations

import io
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpDeadline,
    OutboundHttpTransportError,
)


class FakeClock:
    """Manually advanced monotonic clock for deterministic deadline tests."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Fake monotonic clocks cannot move backwards.")
        self.now += seconds


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
        self.deadlines: list[OutboundHttpDeadline | None] = []

    def __call__(
        self,
        hostname: str,
        port: int,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> tuple[ApprovedSocketAddress, ...]:
        return self.resolve(hostname, port, deadline=deadline)

    def resolve(
        self,
        hostname: str,
        port: int,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> tuple[ApprovedSocketAddress, ...]:
        self.calls.append((hostname, port))
        self.deadlines.append(deadline)
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
    on_get_header: Callable[[str], None] | None = None
    on_chunk: Callable[[int], None] | None = None

    def get_header(self, name: str) -> str | None:
        if self.on_get_header is not None:
            self.on_get_header(name)
        lowered = name.lower()
        return next(
            (value for key, value in self.headers.items() if key.lower() == lowered),
            None,
        )

    def iter_bytes(self) -> Iterable[bytes]:
        for chunk in self.chunks:
            self.iterated_chunks += 1
            if self.on_chunk is not None:
                self.on_chunk(self.iterated_chunks)
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
        self.deadlines: list[OutboundHttpDeadline | None] = []

    def open(
        self,
        method: str,
        target: Any,
        *,
        deadline: OutboundHttpDeadline | None = None,
    ) -> FakeHttpResponse:
        self.calls.append((method, target))
        self.deadlines.append(deadline)
        if not self._outcomes:
            raise AssertionError("No fake HTTP outcome remains")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingSocket:
    """The socket methods http.client needs, backed by a canned HTTP response."""

    def __init__(
        self,
        response_bytes: bytes,
        *,
        clock: FakeClock | None = None,
        recv_delays: Sequence[float] = (),
        recv_sizes: Sequence[int] = (),
        send_delay: float = 0.0,
    ) -> None:
        self._response = io.BytesIO(response_bytes)
        self._clock = clock
        self._recv_delays = list(recv_delays)
        self._recv_sizes = list(recv_sizes)
        self._send_delay = send_delay
        self.sent = bytearray()
        self.closed = False
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendall(
        self,
        data: bytes,
        flags: int = 0,
    ) -> None:
        if flags:
            raise AssertionError("SafeHttpClient unexpectedly used socket flags")
        if self._clock is not None:
            self._clock.advance(self._send_delay)
        self.sent.extend(data)

    def recv(self, size: int, flags: int = 0) -> bytes:
        if flags:
            raise AssertionError("SafeHttpClient unexpectedly used socket flags")
        buffer = bytearray(size)
        count = self.recv_into(buffer)
        return bytes(buffer[:count])

    def recv_into(self, buffer: bytearray | memoryview, nbytes: int = 0) -> int:
        if self._clock is not None and self._recv_delays:
            self._clock.advance(self._recv_delays.pop(0))
        requested = nbytes or len(buffer)
        if self._recv_sizes:
            requested = min(requested, self._recv_sizes.pop(0))
        chunk = self._response.read(requested)
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def makefile(
        self,
        mode: str,
        buffering: int | None = None,
    ) -> io.BufferedReader:
        if mode != "rb":
            raise AssertionError(f"Unexpected socket file mode: {mode}")
        buffer_size = (
            buffering if buffering is not None and buffering > 0 else io.DEFAULT_BUFFER_SIZE
        )
        return io.BufferedReader(_RecordingRawIO(self), buffer_size=buffer_size)

    def close(self) -> None:
        self.closed = True


class _RecordingRawIO(io.RawIOBase):
    def __init__(self, sock: RecordingSocket) -> None:
        self._socket = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        return self._socket.recv_into(buffer)


class RecordingPinnedConnector:
    """Pinned connector fake that records the numeric addresses it receives."""

    def __init__(
        self,
        response_bytes: bytes,
        *,
        error: BaseException | None = None,
        socket: RecordingSocket | None = None,
    ) -> None:
        self.socket = socket or RecordingSocket(response_bytes)
        self.error = error
        self.calls: list[
            tuple[
                tuple[ApprovedSocketAddress, ...],
                OutboundHttpDeadline | float,
            ]
        ] = []

    def connect(
        self,
        addresses: Sequence[ApprovedSocketAddress],
        deadline: OutboundHttpDeadline | float,
    ) -> socket.socket:
        self.calls.append((tuple(addresses), deadline))
        if self.error is not None:
            if isinstance(self.error, OutboundHttpTransportError):
                raise self.error
            raise self.error
        return self.socket  # type: ignore[return-value]


class RecordingSslContext:
    """TLS seam that records SNI while leaving the canned socket untouched."""

    def __init__(
        self,
        *,
        on_wrap: Callable[[], None] | None = None,
        wrapped_socket: object | None = None,
    ) -> None:
        # Python 3.11's HTTPSConnection inspects this attribute and enables
        # hostname checking before it delegates to wrap_socket.
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True
        self.calls: list[tuple[object, str | None]] = []
        self._on_wrap = on_wrap
        self._wrapped_socket = wrapped_socket

    def wrap_socket(
        self,
        raw_socket: object,
        *,
        server_hostname: str | None,
    ) -> object:
        self.calls.append((raw_socket, server_hostname))
        if self._on_wrap is not None:
            self._on_wrap()
        return self._wrapped_socket or raw_socket
