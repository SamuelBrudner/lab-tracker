from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from uuid import UUID

import pytest
from http_security_fakes import (
    FakeAddressResolver,
    FakeClock,
    FakeHttpResponse,
    FakeSafeHttpClient,
    RecordingPinnedConnector,
)
from store_authority_fakes import unsafe_probe_target_for_adapter_test

from lab_tracker.http_store_health import HttpStoreHealthProbe
from lab_tracker.models import StoreKind
from lab_tracker.outbound_http import (
    DEFAULT_MAX_HTTP_REDIRECTS,
    HTTP_REDIRECT_STATUS_CODES,
    MAX_OUTBOUND_HTTP_DEADLINE_SECONDS,
    OutboundHttpPolicy,
    SafeHttpClient,
)
from lab_tracker.store_health import (
    HTTP_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

_PUBLIC_IP = "93.184.216.34"
_SECOND_PUBLIC_IP = "142.250.72.14"
def _target(
    *,
    root: str = "https://store.example/base",
    endpoint: str | None = None,
    kind: StoreKind = StoreKind.HTTP,
    credential_ref: str | None = "vault:store-secret",
) -> StoreProbeTarget:
    return unsafe_probe_target_for_adapter_test(
        store_id=UUID(int=1),
        name="web",
        kind=kind,
        root=root,
        endpoint=endpoint,
        credential_ref=credential_ref,
    )


def _probe(
    *outcomes: FakeHttpResponse | BaseException,
    dns: FakeAddressResolver | None = None,
    clock: FakeClock | None = None,
    deadline_seconds: float = 5.0,
    max_redirects: int = DEFAULT_MAX_HTTP_REDIRECTS,
    allowed_authorities: tuple[str, ...] = (),
    allowed_networks: tuple[str, ...] = (),
) -> tuple[HttpStoreHealthProbe, FakeSafeHttpClient, FakeAddressResolver]:
    active_dns = dns or FakeAddressResolver({"store.example": [_PUBLIC_IP]})
    client = FakeSafeHttpClient(outcomes)
    policy = OutboundHttpPolicy(
        address_resolver=active_dns,
        allowed_authorities=allowed_authorities,
        allowed_networks=allowed_networks,
    )
    return (
        HttpStoreHealthProbe(
            policy=policy,
            client=client,
            deadline_seconds=deadline_seconds,
            clock=clock or FakeClock(),
            max_redirects=max_redirects,
        ),
        client,
        active_dns,
    )


def _assert_static_failure(result: StoreHealth) -> None:
    assert result == StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        HTTP_STORE_HEALTH_FAILURE_DETAIL,
    )
    assert result.to_json_dict() == {
        "status": "unreachable",
        "detail": HTTP_STORE_HEALTH_FAILURE_DETAIL,
    }


def test_http_probe_is_an_immutable_slotted_dependency_container() -> None:
    probe, _, _ = _probe()

    assert [item.name for item in fields(HttpStoreHealthProbe)] == [
        "policy",
        "client",
        "deadline_seconds",
        "clock",
        "max_redirects",
    ]
    assert not hasattr(probe, "__dict__")
    with pytest.raises(FrozenInstanceError):
        probe.deadline_seconds = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    (
        ("deadline_seconds", True, TypeError),
        ("deadline_seconds", "1", TypeError),
        ("deadline_seconds", 0.0, ValueError),
        ("deadline_seconds", -1.0, ValueError),
        ("deadline_seconds", float("nan"), ValueError),
        ("deadline_seconds", float("inf"), ValueError),
        (
            "deadline_seconds",
            MAX_OUTBOUND_HTTP_DEADLINE_SECONDS + 1.0,
            ValueError,
        ),
        ("max_redirects", True, TypeError),
        ("max_redirects", 1.0, TypeError),
        ("max_redirects", -1, ValueError),
        ("max_redirects", DEFAULT_MAX_HTTP_REDIRECTS + 1, ValueError),
        ("clock", None, TypeError),
    ),
)
def test_http_probe_rejects_invalid_or_unbounded_configuration(
    field_name: str,
    value: object,
    error_type: type[Exception],
) -> None:
    policy = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver({"store.example": [_PUBLIC_IP]})
    )
    kwargs = {
        "policy": policy,
        "client": FakeSafeHttpClient(()),
        field_name: value,
    }

    with pytest.raises(error_type):
        HttpStoreHealthProbe(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "base",
    (
        "",
        "files.example/base",
        "ftp://files.example/base",
        "https:///base",
        "https://user:hunter2@store.example/base",
        "https://store.example/base?token=hunter2",
        "https://store.example/base#fragment",
        "https://store.example/.",
        "https://store.example/%2e%2e",
        "https://store.example/base/%2Fsecret",
        "https://store.example/base/%5Csecret",
        "https://store.example/base\\secret",
        "https://store.example//base",
        "https://store.example/base\nsecret",
        "https://store.example/%EF%BC%8E%EF%BC%8E/secret",
        "https://store.example/CON",
    ),
)
def test_structurally_invalid_initial_base_fails_before_dns_or_client(
    base: str,
) -> None:
    probe, client, dns = _probe()

    result = probe(_target(root=base))

    _assert_static_failure(result)
    assert dns.calls == []
    assert client.calls == []


def test_non_none_endpoint_is_authoritative_and_blank_fails_without_root_fallback() -> None:
    dns = FakeAddressResolver({"root.example": [_PUBLIC_IP]})
    probe, client, _ = _probe(dns=dns)

    result = probe(
        _target(
            root="https://root.example/safe",
            endpoint="",
        )
    )

    _assert_static_failure(result)
    assert dns.calls == []
    assert client.calls == []


def test_non_none_endpoint_is_probed_instead_of_root() -> None:
    endpoint_response = FakeHttpResponse()
    dns = FakeAddressResolver(
        {
            "endpoint.example": [_SECOND_PUBLIC_IP],
            "root.example": [_PUBLIC_IP],
        }
    )
    probe, client, _ = _probe(endpoint_response, dns=dns)

    result = probe(
        _target(
            root="https://root.example/base",
            endpoint="https://ENDPOINT.example:443/health",
        )
    )

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert dns.calls == [("endpoint.example", 443)]
    assert len(client.calls) == 1
    assert client.calls[0][0] == "HEAD"
    assert client.calls[0][1].absolute_url == "https://endpoint.example/health/"
    assert endpoint_response.closed is True


def test_none_endpoint_uses_canonical_directory_form_of_root() -> None:
    response = FakeHttpResponse(chunks=(b"must never be read",))
    probe, client, dns = _probe(response)

    result = probe(_target(root="https://STORE.example:443/base"))

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert dns.calls == [("store.example", 443)]
    assert client.calls[0][0] == "HEAD"
    assert client.calls[0][1].absolute_url == "https://store.example/base/"
    assert response.iterated_chunks == 0
    assert response.closed is True


@pytest.mark.parametrize(
    "base",
    (
        "http://localhost/base",
        "http://127.0.0.1/base",
        "http://10.0.0.1/base",
        "http://169.254.169.254/latest/meta-data",
        "http://100.64.0.1/base",
        "http://[::1]/base",
        "http://[fe80::1]/base",
        "http://[fc00::1]/base",
        "http://[64:ff9b::808:808]/base",
    ),
)
def test_local_and_unsafe_literal_destinations_send_no_request(base: str) -> None:
    probe, client, dns = _probe()

    result = probe(_target(root=base))

    _assert_static_failure(result)
    assert dns.calls == []
    assert client.calls == []


def test_mixed_public_private_dns_answers_send_no_request() -> None:
    dns = FakeAddressResolver(
        {"store.example": [_PUBLIC_IP, "192.168.1.7"]}
    )
    probe, client, _ = _probe(dns=dns)

    result = probe(_target())

    _assert_static_failure(result)
    assert dns.calls == [("store.example", 443)]
    assert client.calls == []


def test_internal_destination_requires_exact_authority_and_network_opt_in() -> None:
    denied_dns = FakeAddressResolver({"internal.example": ["10.20.1.7"]})
    denied, denied_client, _ = _probe(dns=denied_dns)

    denied_result = denied(_target(root="http://internal.example/data"))

    _assert_static_failure(denied_result)
    assert denied_client.calls == []

    response = FakeHttpResponse()
    allowed_dns = FakeAddressResolver({"internal.example": ["10.20.1.7"]})
    allowed, allowed_client, _ = _probe(
        response,
        dns=allowed_dns,
        allowed_authorities=("http://internal.example",),
        allowed_networks=("10.20.0.0/16",),
    )

    allowed_result = allowed(_target(root="http://internal.example/data"))

    assert allowed_result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert allowed_dns.calls == [("internal.example", 80)]
    assert allowed_client.calls[0][1].addresses[0].ip.compressed == "10.20.1.7"
    assert response.closed is True


@pytest.mark.parametrize("status_code", (200, 204, 299, 403, 405))
def test_only_documented_terminal_success_statuses_are_healthy(
    status_code: int,
) -> None:
    response = FakeHttpResponse(
        status_code=status_code,
        chunks=(b"large body must not be read",),
    )
    probe, client, dns = _probe(response)

    result = probe(_target())

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert response.iterated_chunks == 0
    assert response.closed is True
    assert [method for method, _ in client.calls] == ["HEAD"]
    assert dns.deadlines[0] is client.deadlines[0]


@pytest.mark.parametrize("status_code", (100, 300, 304, 400, 401, 404, 500, 599))
def test_other_terminal_statuses_have_one_static_redacted_failure(
    status_code: int,
) -> None:
    response = FakeHttpResponse(status_code=status_code)
    probe, client, _ = _probe(response)

    result = probe(_target())

    _assert_static_failure(result)
    assert str(status_code) not in (result.detail or "")
    assert response.closed is True
    assert len(client.calls) == 1


@pytest.mark.parametrize("status_code", sorted(HTTP_REDIRECT_STATUS_CODES))
def test_every_redirect_status_reauthorizes_cross_origin_and_preserves_head(
    status_code: int,
) -> None:
    redirect = FakeHttpResponse(
        status_code=status_code,
        headers={"Location": "https://archive.example/final"},
        chunks=(b"redirect body must not be read",),
    )
    final = FakeHttpResponse(status_code=204, chunks=(b"must not be read",))
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_IP],
            "archive.example": [_SECOND_PUBLIC_IP],
        }
    )
    clock = FakeClock(100.0)
    probe, client, _ = _probe(
        redirect,
        final,
        dns=dns,
        clock=clock,
        deadline_seconds=5.0,
    )

    result = probe(_target())

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert dns.calls == [
        ("store.example", 443),
        ("archive.example", 443),
    ]
    assert [method for method, _ in client.calls] == ["HEAD", "HEAD"]
    assert [target.hostname for _, target in client.calls] == [
        "store.example",
        "archive.example",
    ]
    shared_deadline = dns.deadlines[0]
    assert shared_deadline is not None
    assert shared_deadline.expires_at == 105.0
    assert all(
        deadline is shared_deadline
        for deadline in (*dns.deadlines, *client.deadlines)
    )
    assert redirect.iterated_chunks == 0
    assert final.iterated_chunks == 0
    assert redirect.closed is True
    assert final.closed is True


@pytest.mark.parametrize(
    "location",
    (
        "http://archive.example/downgrade",
        "https://user:hunter2@archive.example/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1",
        "file:///etc/passwd",
    ),
)
def test_unsafe_redirect_is_closed_and_never_opened(
    location: str,
) -> None:
    redirect = FakeHttpResponse(
        status_code=302,
        headers={"Location": location},
    )
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_IP],
            "archive.example": [_SECOND_PUBLIC_IP],
        }
    )
    probe, client, _ = _probe(redirect, dns=dns)

    result = probe(_target())

    _assert_static_failure(result)
    assert len(client.calls) == 1
    assert dns.calls == [("store.example", 443)]
    assert redirect.closed is True


@pytest.mark.parametrize("headers", ({}, {"Location": ""}))
def test_redirect_without_location_is_static_failure_and_closes_response(
    headers: dict[str, str],
) -> None:
    redirect = FakeHttpResponse(status_code=302, headers=headers)
    probe, client, dns = _probe(redirect)

    result = probe(_target())

    _assert_static_failure(result)
    assert len(client.calls) == 1
    assert len(dns.calls) == 1
    assert redirect.closed is True


def test_redirect_rebinding_is_denied_before_second_request() -> None:
    redirect = FakeHttpResponse(status_code=302, headers={"Location": "/next"})
    dns = FakeAddressResolver(
        sequences={
            "store.example": (
                (_PUBLIC_IP,),
                ("169.254.169.254",),
            )
        }
    )
    probe, client, _ = _probe(redirect, dns=dns)

    result = probe(_target())

    _assert_static_failure(result)
    assert dns.calls == [
        ("store.example", 443),
        ("store.example", 443),
    ]
    assert len(client.calls) == 1
    assert redirect.closed is True


def test_canonical_redirect_loop_is_detected_before_second_request() -> None:
    redirect = FakeHttpResponse(
        status_code=302,
        headers={"Location": "https://STORE.example:443/base/"},
    )
    dns = FakeAddressResolver(
        sequences={
            "store.example": (
                (_PUBLIC_IP,),
                (_PUBLIC_IP,),
            )
        }
    )
    probe, client, _ = _probe(redirect, dns=dns)

    result = probe(_target())

    _assert_static_failure(result)
    assert dns.calls == [
        ("store.example", 443),
        ("store.example", 443),
    ]
    assert len(client.calls) == 1
    assert redirect.closed is True


def test_redirect_limit_bounds_requests_and_closes_every_response() -> None:
    first = FakeHttpResponse(
        status_code=302,
        headers={"Location": "https://archive.example/middle"},
    )
    second = FakeHttpResponse(
        status_code=307,
        headers={"Location": "https://final.example/end"},
    )
    never_opened = FakeHttpResponse()
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_IP],
            "archive.example": [_SECOND_PUBLIC_IP],
            "final.example": ["151.101.1.69"],
        }
    )
    probe, client, _ = _probe(
        first,
        second,
        never_opened,
        dns=dns,
        max_redirects=1,
    )

    result = probe(_target())

    _assert_static_failure(result)
    assert dns.calls == [
        ("store.example", 443),
        ("archive.example", 443),
    ]
    assert [method for method, _ in client.calls] == ["HEAD", "HEAD"]
    assert first.closed is True
    assert second.closed is True
    assert never_opened.closed is False


def test_one_deadline_expires_during_redirect_inspection_and_closes_response() -> None:
    clock = FakeClock()

    def expire_on_location(name: str) -> None:
        if name.lower() == "location":
            clock.advance(1.0)

    redirect = FakeHttpResponse(
        status_code=302,
        headers={"Location": "https://archive.example/next"},
        on_get_header=expire_on_location,
    )
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_IP],
            "archive.example": [_SECOND_PUBLIC_IP],
        }
    )
    probe, client, _ = _probe(
        redirect,
        dns=dns,
        clock=clock,
        deadline_seconds=1.0,
    )

    result = probe(_target())

    _assert_static_failure(result)
    assert dns.calls == [("store.example", 443)]
    assert len(client.calls) == 1
    assert dns.deadlines[0] is client.deadlines[0]
    assert redirect.closed is True


def test_one_deadline_also_bounds_terminal_response_cleanup() -> None:
    clock = FakeClock()

    class SlowCloseResponse(FakeHttpResponse):
        def close(self) -> None:
            super().close()
            clock.advance(1.0)

    response = SlowCloseResponse()
    probe, _, _ = _probe(
        response,
        clock=clock,
        deadline_seconds=1.0,
    )

    result = probe(_target())

    _assert_static_failure(result)
    assert response.closed is True


def test_ordinary_open_and_header_failures_are_static_and_redacted() -> None:
    secret = "hunter2"
    open_probe, open_client, _ = _probe(
        RuntimeError(
            f"failed https://user:{secret}@store.example/?token={secret}"
        )
    )

    open_result = open_probe(_target(credential_ref=f"vault:{secret}"))

    _assert_static_failure(open_result)
    assert secret not in repr(open_result)
    assert len(open_client.calls) == 1

    def fail_header(_name: str) -> None:
        raise RuntimeError(f"redirect Location contained {secret}")

    response = FakeHttpResponse(
        status_code=302,
        headers={"Location": "https://archive.example/secret"},
        on_get_header=fail_header,
    )
    header_probe, _, _ = _probe(response)

    header_result = header_probe(_target(credential_ref=f"vault:{secret}"))

    _assert_static_failure(header_result)
    assert secret not in repr(header_result)
    assert response.closed is True


def test_close_failure_replaces_success_with_static_redacted_failure() -> None:
    secret = "close-secret"

    class CloseFailureResponse(FakeHttpResponse):
        def close(self) -> None:
            super().close()
            raise RuntimeError(f"close failed for {secret}@store.example")

    response = CloseFailureResponse()
    probe, _, _ = _probe(response)

    result = probe(_target())

    _assert_static_failure(result)
    assert secret not in repr(result)
    assert response.closed is True


def test_close_exception_cannot_replace_active_base_exception() -> None:
    class CloseFailureResponse(FakeHttpResponse):
        def close(self) -> None:
            super().close()
            raise RuntimeError("ordinary cleanup failure")

    response = CloseFailureResponse(
        status_code=302,
        headers={"Location": "https://archive.example/next"},
    )

    def interrupt(_name: str) -> None:
        raise KeyboardInterrupt

    response.on_get_header = interrupt
    probe, _, _ = _probe(response)

    with pytest.raises(KeyboardInterrupt):
        probe(_target())

    assert response.closed is True


def test_non_http_target_fails_closed_without_dns_or_client() -> None:
    probe, client, dns = _probe()

    result = probe(
        _target(
            kind=StoreKind.LOCAL_FS,
            root="https://store.example/base",
        )
    )

    _assert_static_failure(result)
    assert dns.calls == []
    assert client.calls == []


def test_real_safe_client_sends_head_without_credentials_or_proxy_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "credential-reference-secret"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    connector = RecordingPinnedConnector(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Length: 4096\r\n"
        b"Content-Encoding: gzip\r\n"
        b"\r\n"
    )
    dns = FakeAddressResolver({"store.example": [_PUBLIC_IP]})
    probe = HttpStoreHealthProbe(
        policy=OutboundHttpPolicy(address_resolver=dns),
        client=SafeHttpClient(connector=connector),
        deadline_seconds=5.0,
        clock=FakeClock(),
    )

    result = probe(
        _target(
            root="http://store.example/base",
            credential_ref=secret,
        )
    )

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert len(connector.calls) == 1
    request = bytes(connector.socket.sent)
    assert request.startswith(b"HEAD /base/ HTTP/1.1\r\n")
    assert request.endswith(b"\r\n\r\n")
    assert b"\r\nHost: store.example\r\n" in request
    assert b"\r\nAccept-Encoding: identity\r\n" in request
    assert b"\r\nUser-Agent: lab-tracker/0.1\r\n" in request
    assert secret.encode() not in request
    lowered = request.lower()
    assert b"\r\nauthorization:" not in lowered
    assert b"\r\ncookie:" not in lowered
    assert b"\r\nproxy-authorization:" not in lowered
    assert connector.socket.closed is True
