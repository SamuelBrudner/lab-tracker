from __future__ import annotations

import asyncio
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import urlsplit

import pytest
from http_security_fakes import FakeAddressResolver, FakeClock

from lab_tracker.outbound_http import (
    DEFAULT_MAX_HTTP_REDIRECTS,
    HTTP_REDIRECT_STATUS_CODES,
    ApprovedSocketAddress,
    OutboundHttpDeadline,
    OutboundHttpPolicy,
    OutboundHttpPolicyError,
    SystemAddressResolver,
    resolve_direct_http_redirect,
)

PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:2800:220:1:248:1893:25c8:1946"


def test_shared_redirect_contract_is_explicit_and_finite() -> None:
    assert DEFAULT_MAX_HTTP_REDIRECTS == 3
    assert frozenset({301, 302, 303, 307, 308}) == HTTP_REDIRECT_STATUS_CODES


@pytest.mark.parametrize(
    ("current_url", "location", "expected"),
    (
        (
            "https://files.example/start",
            "/next",
            "https://files.example/next",
        ),
        (
            "https://files.example/start",
            "https://archive.example/final",
            "https://archive.example/final",
        ),
        (
            "http://files.example/start",
            "https://archive.example/final",
            "https://archive.example/final",
        ),
        (
            "https://files.example/start",
            "http://archive.example/final",
            None,
        ),
        (
            "https://files.example/start",
            "https://[::1",
            None,
        ),
    ),
)
def test_direct_redirect_resolution_preserves_reauthorization_boundary(
    current_url: str,
    location: str,
    expected: str | None,
) -> None:
    assert resolve_direct_http_redirect(current_url, location) == expected


@pytest.mark.parametrize(
    "url",
    (
        "ftp://files.example/artifact.bin",
        "files.example/artifact.bin",
        "https:///artifact.bin",
        "https://files.example:99999/artifact.bin",
        "https://files.example:0/artifact.bin",
        "https://user:secret@files.example/artifact.bin",
        "https://user@files.example/artifact.bin",
        "https://files.example\\@127.0.0.1/artifact.bin",
        "https://%66iles.example/artifact.bin",
        "https://files..example/artifact.bin",
        "https://under_score.example/artifact.bin",
        "https://[::1/artifact.bin",
    ),
)
def test_malformed_or_credentialed_urls_fail_before_dns(url: str) -> None:
    dns = FakeAddressResolver({"files.example": [PUBLIC_IPV4]})
    policy = OutboundHttpPolicy(address_resolver=dns)

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize(url)

    assert dns.calls == []


@pytest.mark.parametrize(
    "url",
    (
        "http://localhost/artifact.bin",
        "http://LOCALHOST./artifact.bin",
        "http://instrument/artifact.bin",
        "http://microscope.local/artifact.bin",
        "http://router.home.arpa/artifact.bin",
    ),
)
def test_local_and_single_label_names_are_default_denied_before_dns(url: str) -> None:
    dns = FakeAddressResolver()
    policy = OutboundHttpPolicy(address_resolver=dns)

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize(url)

    assert dns.calls == []


@pytest.mark.parametrize(
    "url",
    (
        "http://0.0.0.0/artifact.bin",
        "http://127.0.0.1/artifact.bin",
        "http://10.0.0.1/artifact.bin",
        "http://172.16.0.1/artifact.bin",
        "http://192.168.1.1/artifact.bin",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/artifact.bin",
        "http://192.0.2.1/artifact.bin",
        "http://224.0.0.1/artifact.bin",
        "http://240.0.0.1/artifact.bin",
        "http://[::]/artifact.bin",
        "http://[::1]/artifact.bin",
        "http://[fe80::1]/artifact.bin",
        "http://[fc00::1]/artifact.bin",
        "http://[fec0::1]/artifact.bin",
        "http://[ff02::1]/artifact.bin",
        "http://[2001:db8::1]/artifact.bin",
        "http://[::ffff:127.0.0.1]/artifact.bin",
        "http://[64:ff9b::7f00:1]/artifact.bin",
        "http://[2002:7f00:1::]/artifact.bin",
        "http://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/artifact.bin",
    ),
)
def test_non_global_literal_addresses_are_denied_without_dns(url: str) -> None:
    dns = FakeAddressResolver()
    policy = OutboundHttpPolicy(address_resolver=dns)

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize(url)

    assert dns.calls == []


def test_public_dns_answers_are_preserved_as_approved_socket_addresses() -> None:
    dns = FakeAddressResolver({"files.example": [PUBLIC_IPV4, PUBLIC_IPV6, PUBLIC_IPV4]})
    policy = OutboundHttpPolicy(address_resolver=dns)
    clock = FakeClock(100.0)
    deadline = OutboundHttpDeadline.after(5.0, clock=clock)

    target = policy.authorize(
        "https://files.example/artifact.bin",
        deadline=deadline,
    )

    assert dns.calls == [("files.example", 443)]
    assert dns.deadlines == [deadline]
    assert target.hostname == "files.example"
    assert target.port == 443
    assert target.addresses == (
        ApprovedSocketAddress.from_ip(PUBLIC_IPV4, 443),
        ApprovedSocketAddress.from_ip(PUBLIC_IPV6, 443),
    )


def test_iri_host_path_and_query_are_canonicalized_for_http_transport() -> None:
    dns = FakeAddressResolver({"xn--bcher-kva.example": [PUBLIC_IPV4]})
    policy = OutboundHttpPolicy(address_resolver=dns)

    target = policy.authorize(
        "https://bücher.example/α/δ?q=β&already=%CE%B1#ignored"
    )

    assert target.hostname == "xn--bcher-kva.example"
    assert target.request_target == (
        "/%CE%B1/%CE%B4?q=%CE%B2&already=%CE%B1"
    )
    assert target.absolute_url == (
        "https://xn--bcher-kva.example/%CE%B1/%CE%B4"
        "?q=%CE%B2&already=%CE%B1"
    )
    assert dns.calls == [("xn--bcher-kva.example", 443)]


def test_unencodable_iri_component_fails_before_dns() -> None:
    dns = FakeAddressResolver({"files.example": [PUBLIC_IPV4]})
    policy = OutboundHttpPolicy(address_resolver=dns)

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize(f"https://files.example/{chr(0xD800)}")

    assert dns.calls == []


def test_system_address_resolver_converts_ipv4_and_ipv6_answers() -> None:
    calls: list[tuple[str, float]] = []
    clock = FakeClock(10.0)
    deadline = OutboundHttpDeadline.after(7.5, clock=clock)

    async def lookup(hostname: str, lifetime: float) -> tuple[str, ...]:
        calls.append((hostname, lifetime))
        return (PUBLIC_IPV4, PUBLIC_IPV6)

    answers = SystemAddressResolver(lookup=lookup).resolve(
        "files.example",
        443,
        deadline=deadline,
    )

    assert calls == [("files.example", 7.5)]
    assert answers == (
        ApprovedSocketAddress.from_ip(PUBLIC_IPV4, 443),
        ApprovedSocketAddress.from_ip(PUBLIC_IPV6, 443),
    )


def test_system_address_resolver_uses_configured_dns_search_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls: list[bool] = []
    resolve_calls: list[tuple[str, int, float, bool]] = []

    class FakeAnswers:
        def addresses(self) -> tuple[str, ...]:
            return (PUBLIC_IPV4, PUBLIC_IPV6)

    class FakeResolver:
        async def resolve_name(
            self,
            hostname: str,
            *,
            family: int,
            lifetime: float,
            search: bool,
        ) -> FakeAnswers:
            resolve_calls.append((hostname, family, lifetime, search))
            return FakeAnswers()

    def resolver_factory(*, configure: bool) -> FakeResolver:
        constructor_calls.append(configure)
        return FakeResolver()

    monkeypatch.setattr(
        "lab_tracker.outbound_http.dns.asyncresolver.Resolver",
        resolver_factory,
    )

    deadline = OutboundHttpDeadline.after(30.0, clock=FakeClock())
    answers = SystemAddressResolver().resolve(
        "instrument",
        8080,
        deadline=deadline,
    )

    assert constructor_calls == [True]
    assert resolve_calls == [
        ("instrument", socket.AF_UNSPEC, 30.0, True),
    ]
    assert answers == (
        ApprovedSocketAddress.from_ip(PUBLIC_IPV4, 8080),
        ApprovedSocketAddress.from_ip(PUBLIC_IPV6, 8080),
    )


@pytest.mark.parametrize(
    "answer",
    (
        "not-an-ip-address",
        "fe80::1%en0",
        "93.184.216.34:443",
    ),
)
def test_system_address_resolver_rejects_malformed_answers(
    answer: str,
) -> None:
    async def lookup(_hostname: str, _lifetime: float) -> tuple[str, ...]:
        return (answer,)

    with pytest.raises(OSError):
        SystemAddressResolver(lookup=lookup).resolve("files.example", 443)


def test_system_address_resolver_real_timeout_cancels_slow_dns_coroutine() -> None:
    started = Event()
    cancelled = Event()

    async def lookup(_hostname: str, _lifetime: float) -> tuple[str, ...]:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    resolver = SystemAddressResolver(lookup=lookup, default_timeout=0.05)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            resolver.resolve,
            "secret.internal.example",
            443,
        )
        assert started.wait(timeout=1.0)
        with pytest.raises(OSError) as exc_info:
            future.result(timeout=1.0)

    assert cancelled.wait(timeout=1.0)
    assert "secret.internal.example" not in str(exc_info.value)


@pytest.mark.parametrize(
    "answers",
    (
        (),
        ("127.0.0.1",),
        ("10.20.30.40",),
        (PUBLIC_IPV4, "10.20.30.40"),
        (PUBLIC_IPV6, "fe80::1"),
    ),
)
def test_empty_private_and_mixed_dns_answers_fail_closed(
    answers: tuple[str, ...],
) -> None:
    dns = FakeAddressResolver({"files.example": answers})
    policy = OutboundHttpPolicy(address_resolver=dns)

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize("https://files.example/artifact.bin")

    assert dns.calls == [("files.example", 443)]


def test_dns_errors_are_normalized_to_policy_denial() -> None:
    dns = FakeAddressResolver(
        {"secret.internal.example": socket.gaierror("resolver leaked 10.0.0.8")}
    )
    policy = OutboundHttpPolicy(address_resolver=dns)

    with pytest.raises(OutboundHttpPolicyError) as exc_info:
        policy.authorize("https://secret.internal.example/artifact.bin")

    assert "10.0.0.8" not in str(exc_info.value)
    assert dns.calls == [("secret.internal.example", 443)]


@pytest.mark.parametrize(
    ("allowed_authorities", "allowed_networks"),
    (
        (("https://minio.lab.example",), ()),
        ((), ("10.20.0.0/16",)),
    ),
)
def test_internal_override_configuration_requires_both_halves(
    allowed_authorities: tuple[str, ...],
    allowed_networks: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        OutboundHttpPolicy(
            address_resolver=FakeAddressResolver(),
            allowed_authorities=allowed_authorities,
            allowed_networks=allowed_networks,
        )


@pytest.mark.parametrize(
    "authority",
    (
        "https://user:secret@minio.lab.example",
        "https://minio.lab.example/path",
        "https://minio.lab.example?token=secret",
        "https://*.lab.example",
        "https://minio.lab.example:0",
    ),
)
def test_internal_override_rejects_invalid_authorities(authority: str) -> None:
    with pytest.raises(ValueError):
        OutboundHttpPolicy(
            allowed_authorities=(authority,),
            allowed_networks=("10.20.0.0/16",),
        )


def test_internal_override_rejects_host_bits_in_network() -> None:
    with pytest.raises(ValueError, match="network configuration"):
        OutboundHttpPolicy(
            allowed_authorities=("https://minio.lab.example",),
            allowed_networks=("10.20.1.7/16",),
        )


def test_internal_override_rejects_normalized_duplicate_authorities() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        OutboundHttpPolicy(
            allowed_authorities=(
                "https://MINIO.lab.example",
                "https://minio.lab.example:443",
            ),
            allowed_networks=("10.20.0.0/16",),
        )


def test_internal_override_requires_an_exact_authority_match() -> None:
    dns = FakeAddressResolver({"minio.lab.example": ["10.20.1.7"]})
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("https://other.lab.example",),
        allowed_networks=("10.20.0.0/16",),
    )
    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize("https://minio.lab.example/artifact.bin")


def test_exact_authority_and_network_conjunctively_allow_internal_target() -> None:
    dns = FakeAddressResolver({"minio.lab.example": ["10.20.1.7", "10.20.1.8"]})
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("https://MINIO.lab.example:443",),
        allowed_networks=("10.20.0.0/16",),
    )

    target = policy.authorize("https://minio.lab.example/artifact.bin")

    assert target.addresses == (
        ApprovedSocketAddress.from_ip("10.20.1.7", 443),
        ApprovedSocketAddress.from_ip("10.20.1.8", 443),
    )


def test_exact_authority_can_opt_a_single_label_internal_name_into_policy() -> None:
    dns = FakeAddressResolver({"instrument": ["10.20.1.7"]})
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("http://instrument:8080",),
        allowed_networks=("10.20.0.0/16",),
    )

    target = policy.authorize("http://instrument:8080/artifact.bin")

    assert dns.calls == [("instrument", 8080)]
    assert target.addresses == (ApprovedSocketAddress.from_ip("10.20.1.7", 8080),)


def test_internal_override_denies_any_answer_outside_approved_networks() -> None:
    dns = FakeAddressResolver({"minio.lab.example": ["10.20.1.7", "10.99.1.8"]})
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("https://minio.lab.example",),
        allowed_networks=("10.20.0.0/16",),
    )

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize("https://minio.lab.example/artifact.bin")


def test_internal_override_rejects_mixed_public_and_internal_answers() -> None:
    dns = FakeAddressResolver(
        {"minio.lab.example": ["10.20.1.7", PUBLIC_IPV4]}
    )
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("https://minio.lab.example",),
        allowed_networks=("0.0.0.0/0",),
    )

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize("https://minio.lab.example/artifact.bin")


@pytest.mark.parametrize(
    ("url", "network"),
    (
        ("http://0.0.0.0/artifact.bin", "0.0.0.0/0"),
        ("http://169.254.169.254/latest/meta-data/", "0.0.0.0/0"),
        ("http://224.0.0.1/artifact.bin", "0.0.0.0/0"),
        ("http://[::]/artifact.bin", "::/0"),
        ("http://[fe80::1]/artifact.bin", "::/0"),
        ("http://[ff02::1]/artifact.bin", "::/0"),
        ("http://[64:ff9b::7f00:1]/artifact.bin", "::/0"),
    ),
)
def test_internal_override_cannot_enable_forbidden_address_classes(
    url: str,
    network: str,
) -> None:
    parsed = urlsplit(url)
    policy = OutboundHttpPolicy(
        allowed_authorities=(f"{parsed.scheme}://{parsed.netloc}",),
        allowed_networks=(network,),
    )

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize(url)


def test_exact_authority_does_not_suffix_match_attacker_hostname() -> None:
    dns = FakeAddressResolver({"minio.lab.example.attacker.test": ["10.20.1.7"]})
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("https://minio.lab.example",),
        allowed_networks=("10.20.0.0/16",),
    )

    with pytest.raises(OutboundHttpPolicyError):
        policy.authorize("https://minio.lab.example.attacker.test/artifact.bin")


def test_rebinding_answers_are_not_consulted_after_target_approval() -> None:
    dns = FakeAddressResolver(
        sequences={
            "files.example": (
                (PUBLIC_IPV4,),
                ("127.0.0.1",),
            )
        }
    )
    policy = OutboundHttpPolicy(address_resolver=dns)

    target = policy.authorize("https://files.example/artifact.bin")

    assert dns.calls == [("files.example", 443)]
    assert target.addresses == (ApprovedSocketAddress.from_ip(PUBLIC_IPV4, 443),)
