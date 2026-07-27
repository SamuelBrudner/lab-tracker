from __future__ import annotations

import ssl
from collections.abc import Callable

import pytest
from http_security_fakes import (
    FakeAddressResolver,
    FakeClock,
    RecordingPinnedConnector,
    RecordingSocket,
    RecordingSslContext,
)

import lab_tracker.outbound_http as outbound_http
from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpDeadline,
    OutboundHttpDeadlineExceeded,
    OutboundHttpPolicy,
    OutboundHttpTransportError,
    SafeHttpClient,
    SystemPinnedSocketConnector,
)

PUBLIC_IP = "93.184.216.34"
HTTP_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nContent-Type: text/plain\r\n\r\ndata"


class _FakeConnectSocket:
    def __init__(
        self,
        *,
        peer: tuple[str, int],
        on_connect: Callable[[], None] | None = None,
        connect_error: BaseException | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.peer = peer
        self.timeout: float | None = None
        self.timeouts: list[float] = []
        self.connected_to: tuple[object, ...] | None = None
        self.closed = False
        self._on_connect = on_connect
        self._connect_error = connect_error
        self._close_error = close_error

    def settimeout(self, value: float) -> None:
        self.timeout = value
        self.timeouts.append(value)

    def connect(self, sockaddr: tuple[object, ...]) -> None:
        self.connected_to = sockaddr
        if self._on_connect is not None:
            self._on_connect()
        if self._connect_error is not None:
            raise self._connect_error

    def getpeername(self) -> tuple[str, int]:
        return self.peer

    def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def test_system_connector_uses_only_vetted_numeric_sockaddr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = ApprovedSocketAddress.from_ip(PUBLIC_IP, 8443)
    fake_socket = _FakeConnectSocket(peer=(PUBLIC_IP, 8443))
    socket_factory_calls: list[tuple[int, int, int]] = []

    def socket_factory(family: int, socktype: int, protocol: int):
        socket_factory_calls.append((family, socktype, protocol))
        return fake_socket

    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        socket_factory,
    )

    connected = SystemPinnedSocketConnector().connect((approved,), 5.0)

    assert connected is fake_socket
    assert socket_factory_calls == [(approved.family, approved.socktype, approved.protocol)]
    assert fake_socket.connected_to == approved.sockaddr
    assert fake_socket.timeout is not None
    assert 0 < fake_socket.timeout <= 5.0


def test_system_connector_rejects_unexpected_peer_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = ApprovedSocketAddress.from_ip(PUBLIC_IP, 443)
    fake_socket = _FakeConnectSocket(peer=("127.0.0.1", 443))
    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        lambda *args: fake_socket,
    )

    with pytest.raises(OutboundHttpTransportError):
        SystemPinnedSocketConnector().connect((approved,), 5.0)

    assert fake_socket.closed is True


def test_system_connector_rejects_unexpected_peer_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = ApprovedSocketAddress.from_ip(PUBLIC_IP, 443)
    fake_socket = _FakeConnectSocket(peer=(PUBLIC_IP, 8443))
    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        lambda *args: fake_socket,
    )

    with pytest.raises(OutboundHttpTransportError):
        SystemPinnedSocketConnector().connect((approved,), 5.0)

    assert fake_socket.closed is True


def test_system_connector_closes_socket_when_connect_exhausts_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = ApprovedSocketAddress.from_ip(PUBLIC_IP, 443)
    clock = FakeClock()
    deadline = OutboundHttpDeadline.after(1.0, clock=clock)
    fake_socket = _FakeConnectSocket(
        peer=(PUBLIC_IP, 443),
        on_connect=lambda: clock.advance(1.0),
    )
    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        lambda *args: fake_socket,
    )

    with pytest.raises(OutboundHttpDeadlineExceeded):
        SystemPinnedSocketConnector().connect((approved,), deadline)

    assert fake_socket.closed is True
    assert fake_socket.timeouts == [1.0]


def test_system_connector_preserves_base_exception_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = ApprovedSocketAddress.from_ip(PUBLIC_IP, 443)
    interruption = KeyboardInterrupt("connector interrupted")
    fake_socket = _FakeConnectSocket(
        peer=(PUBLIC_IP, 443),
        connect_error=interruption,
        close_error=RuntimeError("cleanup failed"),
    )
    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        lambda *args: fake_socket,
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        SystemPinnedSocketConnector().connect((approved,), 5.0)

    assert exc_info.value is interruption
    assert fake_socket.closed is True


def test_system_connector_closes_socket_and_redacts_timeout_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = ApprovedSocketAddress.from_ip(PUBLIC_IP, 443)
    fake_socket = _FakeConnectSocket(peer=(PUBLIC_IP, 443))

    def reject_timeout(_value: float) -> None:
        raise OverflowError("platform time_t cannot represent secret deadline")

    fake_socket.settimeout = reject_timeout  # type: ignore[method-assign]
    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        lambda *args: fake_socket,
    )

    with pytest.raises(OutboundHttpTransportError) as exc_info:
        SystemPinnedSocketConnector().connect((approved,), 5.0)

    assert fake_socket.closed is True
    assert "secret" not in str(exc_info.value)
    assert "time_t" not in str(exc_info.value)


def test_system_connector_does_not_reset_deadline_between_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_address = ApprovedSocketAddress.from_ip("93.184.216.34", 443)
    second_address = ApprovedSocketAddress.from_ip("142.250.72.14", 443)
    clock = FakeClock()
    deadline = OutboundHttpDeadline.after(1.0, clock=clock)
    first_socket = _FakeConnectSocket(
        peer=(str(first_address.ip), 443),
        on_connect=lambda: clock.advance(0.25),
        connect_error=OSError("first address refused"),
    )
    second_socket = _FakeConnectSocket(peer=(str(second_address.ip), 443))
    sockets = iter((first_socket, second_socket))
    monkeypatch.setattr(
        "lab_tracker.outbound_http.socket.socket",
        lambda *args: next(sockets),
    )

    connected = SystemPinnedSocketConnector().connect(
        (first_address, second_address),
        deadline,
    )

    assert connected is second_socket
    assert first_socket.closed is True
    assert first_socket.timeouts == [1.0]
    assert second_socket.timeouts == [0.75, 0.75]


def test_safe_http_client_ignores_proxy_env_and_uses_vetted_ip_with_logical_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    dns = FakeAddressResolver(
        sequences={
            "files.example": (
                (PUBLIC_IP,),
                ("127.0.0.1",),
            )
        }
    )
    target = OutboundHttpPolicy(address_resolver=dns).authorize(
        "http://files.example:8080/path/to/artifact?version=1"
    )
    connector = RecordingPinnedConnector(HTTP_RESPONSE)
    client = SafeHttpClient(timeout=7.5, connector=connector)
    clock = FakeClock(10.0)
    deadline = OutboundHttpDeadline.after(7.5, clock=clock)

    with client.open("GET", target, deadline=deadline) as response:
        assert response.status_code == 200
        assert response.get_header("Content-Type") == "text/plain"
        assert b"".join(response.iter_bytes()) == b"data"

    assert dns.calls == [("files.example", 8080)]
    assert connector.calls == [
        (
            (ApprovedSocketAddress.from_ip(PUBLIC_IP, 8080),),
            deadline,
        )
    ]
    request = bytes(connector.socket.sent)
    assert request.startswith(b"GET /path/to/artifact?version=1 HTTP/1.1\r\n")
    assert b"\r\nHost: files.example:8080\r\n" in request
    assert b"127.0.0.1" not in request


def test_safe_http_client_sends_percent_encoded_iri_request_target() -> None:
    dns = FakeAddressResolver({"xn--bcher-kva.example": [PUBLIC_IP]})
    target = OutboundHttpPolicy(address_resolver=dns).authorize(
        "http://bücher.example/α?q=β"
    )
    connector = RecordingPinnedConnector(HTTP_RESPONSE)

    with SafeHttpClient(connector=connector).open("GET", target) as response:
        assert b"".join(response.iter_bytes()) == b"data"

    request = bytes(connector.socket.sent)
    assert request.startswith(b"GET /%CE%B1?q=%CE%B2 HTTP/1.1\r\n")
    assert b"\r\nHost: xn--bcher-kva.example\r\n" in request


def test_safe_https_client_preserves_logical_hostname_for_sni_and_host() -> None:
    dns = FakeAddressResolver({"files.example": [PUBLIC_IP]})
    target = OutboundHttpPolicy(address_resolver=dns).authorize(
        "https://files.example/artifact.bin"
    )
    connector = RecordingPinnedConnector(HTTP_RESPONSE)
    ssl_context = RecordingSslContext()
    client = SafeHttpClient(
        connector=connector,
        ssl_context=ssl_context,  # type: ignore[arg-type]
    )

    with client.open("GET", target) as response:
        assert b"".join(response.iter_bytes()) == b"data"

    assert ssl_context.calls == [(connector.socket, "files.example")]
    assert b"\r\nHost: files.example\r\n" in bytes(connector.socket.sent)


def test_safe_https_client_closes_socket_when_tls_exhausts_deadline() -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("https://files.example/artifact.bin")
    clock = FakeClock()
    deadline = OutboundHttpDeadline.after(1.0, clock=clock)
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    wrapped_socket = RecordingSocket(HTTP_RESPONSE)
    connector = RecordingPinnedConnector(b"", socket=raw_socket)
    ssl_context = RecordingSslContext(
        on_wrap=lambda: clock.advance(1.0),
        wrapped_socket=wrapped_socket,
    )

    with pytest.raises(OutboundHttpTransportError):
        SafeHttpClient(
            connector=connector,
            ssl_context=ssl_context,  # type: ignore[arg-type]
        ).open("GET", target, deadline=deadline)

    assert connector.calls == [(target.addresses, deadline)]
    assert raw_socket.closed is True
    assert wrapped_socket.closed is True


def test_safe_https_client_preserves_tls_base_exception_when_cleanup_fails() -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("https://files.example/artifact.bin")
    interruption = KeyboardInterrupt("TLS interrupted")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    original_close = raw_socket.close

    def close_then_fail() -> None:
        original_close()
        raise RuntimeError("TLS cleanup failed")

    def interrupt_wrap() -> None:
        raise interruption

    raw_socket.close = close_then_fail  # type: ignore[method-assign]
    connector = RecordingPinnedConnector(b"", socket=raw_socket)
    ssl_context = RecordingSslContext(on_wrap=interrupt_wrap)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        SafeHttpClient(
            connector=connector,
            ssl_context=ssl_context,  # type: ignore[arg-type]
        ).open("GET", target)

    assert exc_info.value is interruption
    assert raw_socket.closed is True


def test_safe_http_client_preserves_header_base_exception_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    interruption = KeyboardInterrupt("header inspection interrupted")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    original_socket_close = raw_socket.close

    def close_socket_then_fail() -> None:
        original_socket_close()
        raise RuntimeError("socket cleanup failed")

    raw_socket.close = close_socket_then_fail  # type: ignore[method-assign]
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    class InterruptingResponse:
        status = 200

        def __init__(self) -> None:
            self.closed = False

        def getheader(self, _name: str) -> str | None:
            raise interruption

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("response cleanup failed")

    response = InterruptingResponse()
    monkeypatch.setattr(
        outbound_http._PinnedHttpConnection,
        "getresponse",
        lambda _connection: response,
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        SafeHttpClient(connector=connector).open("HEAD", target)

    assert exc_info.value is interruption
    assert response.closed is True
    assert raw_socket.closed is True


def test_safe_http_client_closes_connection_after_cleanup_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    cleanup_interruption = KeyboardInterrupt("response cleanup interrupted")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    original_socket_close = raw_socket.close

    def close_socket_then_fail() -> None:
        original_socket_close()
        raise SystemExit("connection cleanup interrupted")

    raw_socket.close = close_socket_then_fail  # type: ignore[method-assign]
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    class FailingResponse:
        status = 200

        def __init__(self) -> None:
            self.closed = False

        def getheader(self, _name: str) -> str | None:
            raise RuntimeError("header inspection failed")

        def close(self) -> None:
            self.closed = True
            raise cleanup_interruption

    response = FailingResponse()
    monkeypatch.setattr(
        outbound_http._PinnedHttpConnection,
        "getresponse",
        lambda _connection: response,
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        SafeHttpClient(connector=connector).open("HEAD", target)

    assert exc_info.value is cleanup_interruption
    assert response.closed is True
    assert raw_socket.closed is True


def test_safe_http_client_preserves_send_base_exception_when_cleanup_fails() -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    interruption = KeyboardInterrupt("send interrupted")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    original_close = raw_socket.close

    def interrupt_send(_data: bytes, _flags: int = 0) -> None:
        raise interruption

    def close_then_fail() -> None:
        original_close()
        raise RuntimeError("send cleanup failed")

    raw_socket.sendall = interrupt_send  # type: ignore[method-assign]
    raw_socket.close = close_then_fail  # type: ignore[method-assign]
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        SafeHttpClient(connector=connector).open("HEAD", target)

    assert exc_info.value is interruption
    assert raw_socket.closed is True


def test_streaming_response_cleanup_cannot_mask_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    interruption = KeyboardInterrupt("post-open header interrupted")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    original_socket_close = raw_socket.close

    def close_socket_then_fail() -> None:
        original_socket_close()
        raise RuntimeError("socket cleanup failed")

    raw_socket.close = close_socket_then_fail  # type: ignore[method-assign]
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    class LaterInterruptingResponse:
        status = 200

        def __init__(self) -> None:
            self.closed = False

        def getheader(self, name: str) -> str | None:
            if name.lower() == "content-encoding":
                return None
            raise interruption

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("response cleanup failed")

    inner_response = LaterInterruptingResponse()
    monkeypatch.setattr(
        outbound_http._PinnedHttpConnection,
        "getresponse",
        lambda _connection: inner_response,
    )

    with (
        pytest.raises(KeyboardInterrupt) as exc_info,
        SafeHttpClient(connector=connector).open("HEAD", target) as response,
    ):
        response.get_header("location")

    assert exc_info.value is interruption
    assert inner_response.closed is True
    assert raw_socket.closed is True


def test_streaming_response_redacts_close_failure_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    class FailingCloseResponse:
        status = 200

        def getheader(self, _name: str) -> str | None:
            return None

        def close(self) -> None:
            raise RuntimeError("cleanup leaked https://files.example/secret")

    monkeypatch.setattr(
        outbound_http._PinnedHttpConnection,
        "getresponse",
        lambda _connection: FailingCloseResponse(),
    )
    response = SafeHttpClient(connector=connector).open("HEAD", target)

    with pytest.raises(OutboundHttpTransportError) as exc_info:
        response.close()

    assert str(exc_info.value) == "Outbound HTTP request failed."
    assert "files.example" not in str(exc_info.value)
    assert raw_socket.closed is True


def test_streaming_response_preserves_cleanup_base_exception_after_other_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    cleanup_interruption = KeyboardInterrupt("connection cleanup interrupted")
    raw_socket = RecordingSocket(HTTP_RESPONSE)
    original_socket_close = raw_socket.close

    def close_socket_then_interrupt() -> None:
        original_socket_close()
        raise cleanup_interruption

    raw_socket.close = close_socket_then_interrupt  # type: ignore[method-assign]
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    class FailingCloseResponse:
        status = 200

        def getheader(self, _name: str) -> str | None:
            return None

        def close(self) -> None:
            raise RuntimeError("ordinary cleanup failure")

    monkeypatch.setattr(
        outbound_http._PinnedHttpConnection,
        "getresponse",
        lambda _connection: FailingCloseResponse(),
    )
    response = SafeHttpClient(connector=connector).open("HEAD", target)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        response.close()

    assert exc_info.value is cleanup_interruption
    assert raw_socket.closed is True


def test_safe_http_client_stops_drip_fed_headers_at_total_deadline() -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    clock = FakeClock()
    deadline = OutboundHttpDeadline.after(1.0, clock=clock)
    raw_socket = RecordingSocket(
        HTTP_RESPONSE,
        clock=clock,
        recv_delays=(0.4, 0.4, 0.4, 0.4),
        recv_sizes=(1, 1, 1, 1),
    )
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    with pytest.raises(OutboundHttpTransportError):
        SafeHttpClient(connector=connector).open(
            "GET",
            target,
            deadline=deadline,
        )

    assert raw_socket.closed is True
    assert clock.now >= deadline.expires_at
    assert all(timeout <= 1.0 for timeout in raw_socket.timeouts)


def test_safe_http_client_stops_drip_fed_body_at_total_deadline() -> None:
    header = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nContent-Type: text/plain\r\n\r\n"
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    clock = FakeClock()
    deadline = OutboundHttpDeadline.after(1.0, clock=clock)
    raw_socket = RecordingSocket(
        header + b"data",
        clock=clock,
        recv_delays=(0.0, 0.4, 0.4, 0.4, 0.4),
        recv_sizes=(len(header), 1, 1, 1, 1),
    )
    connector = RecordingPinnedConnector(b"", socket=raw_socket)

    response = SafeHttpClient(connector=connector).open(
        "GET",
        target,
        deadline=deadline,
    )

    with pytest.raises(OutboundHttpTransportError):
        b"".join(response.iter_bytes())

    assert raw_socket.closed is True
    assert clock.now >= deadline.expires_at
    assert all(timeout <= 1.0 for timeout in raw_socket.timeouts)


@pytest.mark.parametrize("error_type", (OSError, RuntimeError))
def test_safe_http_client_redacts_connector_failures(
    error_type: type[Exception],
) -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("https://files.example/secret?token=hunter2")
    connector = RecordingPinnedConnector(
        b"",
        error=error_type(
            "failed https://user:hunter2@files.example/secret?token=hunter2"
        ),
    )
    client = SafeHttpClient(connector=connector)

    with pytest.raises(OutboundHttpTransportError) as exc_info:
        client.open("GET", target)

    rendered = str(exc_info.value)
    assert "hunter2" not in rendered
    assert "files.example" not in rendered


def test_safe_http_client_rejects_content_encoding_and_closes_connection() -> None:
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 4\r\n"
        b"Content-Encoding: gzip\r\n"
        b"\r\n"
        b"data"
    )
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/artifact.bin")
    connector = RecordingPinnedConnector(response)

    with pytest.raises(OutboundHttpTransportError):
        SafeHttpClient(connector=connector).open("GET", target)

    request = bytes(connector.socket.sent)
    assert b"\r\nAccept-Encoding: identity\r\n" in request
    assert connector.socket.closed is True


def test_safe_http_client_allows_content_encoding_metadata_for_head() -> None:
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 4096\r\n"
        b"Content-Encoding: gzip\r\n"
        b"\r\n"
    )
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("http://files.example/health")
    connector = RecordingPinnedConnector(response)

    with SafeHttpClient(connector=connector).open("HEAD", target) as opened:
        assert opened.status_code == 200

    request = bytes(connector.socket.sent)
    assert request.startswith(b"HEAD /health HTTP/1.1\r\n")
    assert b"\r\nAccept-Encoding: identity\r\n" in request
    assert connector.socket.closed is True


def test_safe_http_client_uses_verifying_default_tls_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ssl.create_default_context()
    calls = 0

    def create_default_context() -> ssl.SSLContext:
        nonlocal calls
        calls += 1
        return context

    monkeypatch.setattr(
        "lab_tracker.outbound_http.ssl.create_default_context",
        create_default_context,
    )

    client = SafeHttpClient()

    assert calls == 1
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert client._ssl_context is context
