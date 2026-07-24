from __future__ import annotations

import ssl

import pytest
from http_security_fakes import (
    FakeAddressResolver,
    RecordingPinnedConnector,
    RecordingSslContext,
)

from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpPolicy,
    OutboundHttpTransportError,
    SafeHttpClient,
    SystemPinnedSocketConnector,
)

PUBLIC_IP = "93.184.216.34"
HTTP_RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nContent-Type: text/plain\r\n\r\ndata"


class _FakeConnectSocket:
    def __init__(self, *, peer: tuple[str, int]) -> None:
        self.peer = peer
        self.timeout: float | None = None
        self.connected_to: tuple[object, ...] | None = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, sockaddr: tuple[object, ...]) -> None:
        self.connected_to = sockaddr

    def getpeername(self) -> tuple[str, int]:
        return self.peer

    def close(self) -> None:
        self.closed = True


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

    with client.open("GET", target) as response:
        assert response.status_code == 200
        assert response.get_header("Content-Type") == "text/plain"
        assert b"".join(response.iter_bytes()) == b"data"

    assert dns.calls == [("files.example", 8080)]
    assert connector.calls == [
        (
            (ApprovedSocketAddress.from_ip(PUBLIC_IP, 8080),),
            7.5,
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


def test_safe_http_client_redacts_connector_failures() -> None:
    target = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"files.example": [PUBLIC_IP]})
    ).authorize("https://files.example/secret?token=hunter2")
    connector = RecordingPinnedConnector(
        b"",
        error=OSError("failed https://user:hunter2@files.example/secret?token=hunter2"),
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
