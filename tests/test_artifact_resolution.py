import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from http_security_fakes import (
    FakeAddressResolver,
    FakeHttpResponse,
    FakeSafeHttpClient,
)

from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    ArtifactResolver,
    GitCompleted,
    GitResolver,
    HttpResolver,
    LocalFilesystemResolver,
    RcloneCompleted,
    RcloneResolver,
    RecoveryPolicy,
    ResolutionStatus,
    ResolvedArtifact,
    ResolverRegistry,
    StoreHealthStatus,
    check_store_health,
    default_registry,
    is_verifiable_hash,
    outbound_http_policy_from_env,
    parse_content_hash,
    registry_from_env,
    store_relative_reference,
)
from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind
from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpPolicy,
    OutboundHttpTransportError,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _local_ref(
    path: Path, content_hash: str, *, source_system: str = "local"
) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system=source_system,
        uri=path.as_uri(),
        content_hash=content_hash,
    )


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    target = tmp_path / name
    target.write_bytes(data)
    return target


# --- hash parsing ---------------------------------------------------------


def test_parse_content_hash_with_prefix():
    assert parse_content_hash("sha256:ABCdef") == ("sha256", "abcdef")


def test_parse_content_hash_bare_hex_assumes_sha256():
    assert parse_content_hash("abcDEF") == ("sha256", "abcdef")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("sha256:abc", True),
        ("md5:abc", True),
        ("abc", True),  # bare hex -> sha256
        ("datalad-key:xyz", False),
        ("sha256:", False),
    ],
)
def test_is_verifiable_hash(value, expected):
    assert is_verifiable_hash(value) is expected


# --- LocalFilesystemResolver ---------------------------------------------


def test_local_resolver_verifies_matching_file(tmp_path):
    data = b"differential expression results"
    path = _write(tmp_path, "result.txt", data)
    resolver = LocalFilesystemResolver()

    result = resolver.resolve(_local_ref(path, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.is_verified is True
    assert result.content == data
    assert result.size_bytes == len(data)
    assert result.truncated is False
    assert result.observed_hash == _sha256(data)
    assert result.content_type == "text/plain"


def test_local_resolver_accepts_bare_hex_hash(tmp_path):
    data = b"plate map"
    path = _write(tmp_path, "plate.csv", data)
    bare = hashlib.sha256(data).hexdigest()

    result = LocalFilesystemResolver().resolve(_local_ref(path, bare))

    assert result.status is ResolutionStatus.VERIFIED


def test_local_resolver_reports_drift_on_hash_mismatch(tmp_path):
    path = _write(tmp_path, "result.txt", b"actual content")
    stale = _sha256(b"what the graph recorded")

    result = LocalFilesystemResolver().resolve(_local_ref(path, stale))

    assert result.status is ResolutionStatus.DRIFTED
    assert result.is_verified is False
    assert result.observed_hash == _sha256(b"actual content")
    assert result.content is None
    assert result.returned_bytes == 0
    assert result.detail is not None


def test_local_resolver_missing_file_is_unresolved(tmp_path):
    missing = tmp_path / "gone.txt"
    ref = ExternalArtifactReference(
        source_system="local",
        uri=missing.as_uri(),
        content_hash=_sha256(b"x"),
    )

    result = LocalFilesystemResolver().resolve(ref)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert "not found" in (result.detail or "").lower()


def test_local_resolver_unverifiable_algorithm_is_unresolved(tmp_path):
    data = b"datalad managed"
    path = _write(tmp_path, "annex.bin", data)

    result = LocalFilesystemResolver().resolve(_local_ref(path, "datalad-key:MD5E-s4--abc"))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert "datalad-key" in (result.detail or "")


def test_local_resolver_respects_allowed_roots(tmp_path):
    allowed = tmp_path / "store"
    allowed.mkdir()
    outside = _write(tmp_path, "secret.txt", b"should not be readable")
    resolver = LocalFilesystemResolver(allowed_roots=[allowed])

    result = resolver.resolve(_local_ref(outside, _sha256(b"should not be readable")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowed" in (result.detail or "").lower()
    assert result.content is None


def test_local_resolver_allows_paths_within_roots(tmp_path):
    allowed = tmp_path / "store"
    allowed.mkdir()
    data = b"inside the store"
    inside = _write(allowed, "a.txt", data)

    result = LocalFilesystemResolver(allowed_roots=[allowed]).resolve(
        _local_ref(inside, _sha256(data))
    )

    assert result.status is ResolutionStatus.VERIFIED


def test_local_resolver_truncates_payload_but_still_verifies(tmp_path):
    data = b"abcdefghij"  # 10 bytes
    path = _write(tmp_path, "big.bin", data)

    result = LocalFilesystemResolver().resolve(_local_ref(path, _sha256(data)), max_bytes=4)

    assert result.status is ResolutionStatus.VERIFIED  # full file hashed
    assert result.content == b"abcd"
    assert result.truncated is True
    assert result.size_bytes == 10
    assert result.returned_bytes == 4


def test_local_resolver_withholds_truncated_payload_when_hash_drifts(tmp_path):
    data = b"abcdefghij"
    path = _write(tmp_path, "drifted-big.bin", data)

    result = LocalFilesystemResolver().resolve(
        _local_ref(path, _sha256(b"recorded bytes")),
        max_bytes=4,
    )

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(data)
    assert result.content is None
    assert result.returned_bytes == 0
    assert result.truncated is True
    assert result.size_bytes == len(data)


def test_local_resolver_returns_byte_range_slice(tmp_path):
    data = b"0123456789"
    path = _write(tmp_path, "ranged.bin", data)

    result = LocalFilesystemResolver().resolve(
        _local_ref(path, _sha256(data)), byte_range=(2, 5)
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == b"234"
    assert result.truncated is True
    assert result.size_bytes == 10


def test_local_resolver_withholds_byte_range_when_hash_drifts(tmp_path):
    data = b"0123456789"
    path = _write(tmp_path, "drifted-range.bin", data)

    result = LocalFilesystemResolver().resolve(
        _local_ref(path, _sha256(b"recorded bytes")),
        byte_range=(2, 5),
    )

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(data)
    assert result.content is None
    assert result.returned_bytes == 0
    assert result.truncated is True
    assert result.size_bytes == len(data)
    assert result.detail == "Recomputed hash does not match content_hash."


def test_local_resolver_rejects_invalid_byte_range(tmp_path):
    data = b"0123456789"
    path = _write(tmp_path, "ranged.bin", data)

    result = LocalFilesystemResolver().resolve(
        _local_ref(path, _sha256(data)), byte_range=(5, 2)
    )

    assert result.status is ResolutionStatus.UNRESOLVED


def test_local_resolver_can_resolve_by_source_system_and_scheme(tmp_path):
    path = _write(tmp_path, "a.txt", b"x")
    resolver = LocalFilesystemResolver()

    by_scheme = ExternalArtifactReference(
        source_system="anything", uri=path.as_uri(), content_hash=_sha256(b"x")
    )
    by_source = ExternalArtifactReference(
        source_system="local_fs", uri="https://example.com/a", content_hash=_sha256(b"x")
    )
    neither = ExternalArtifactReference(
        source_system="s3", uri="s3://bucket/key", content_hash=_sha256(b"x")
    )

    assert resolver.can_resolve(by_scheme) is True
    assert resolver.can_resolve(by_source) is True
    assert resolver.can_resolve(neither) is False


# --- HttpResolver ---------------------------------------------------------

_PUBLIC_HTTP_IP = "93.184.216.34"


def _http_ref(
    url: str, content_hash: str, *, source_system: str = "http"
) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system=source_system, uri=url, content_hash=content_hash
    )


def _safe_http_resolver(
    *outcomes: FakeHttpResponse | BaseException,
    dns: FakeAddressResolver | None = None,
    max_fetch_bytes: int = 64 * 1024 * 1024,
    max_redirects: int = 3,
) -> tuple[HttpResolver, FakeSafeHttpClient, FakeAddressResolver]:
    address_resolver = dns or FakeAddressResolver(
        {"store.example": [_PUBLIC_HTTP_IP]}
    )
    client = FakeSafeHttpClient(outcomes)
    resolver = HttpResolver(
        policy=OutboundHttpPolicy(address_resolver=address_resolver),
        client=client,
        max_fetch_bytes=max_fetch_bytes,
        max_redirects=max_redirects,
    )
    return resolver, client, address_resolver


def test_http_resolver_verifies_matching_body():
    body = b"flow cytometry export"
    response = FakeHttpResponse(
        headers={"content-type": "text/csv; charset=utf-8"},
        chunks=(body,),
    )
    resolver, client, dns = _safe_http_resolver(response)

    result = resolver.resolve(_http_ref("https://store.example/sample.csv", _sha256(body)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == body
    assert result.size_bytes == len(body)
    assert result.content_type == "text/csv"
    assert response.closed is True
    assert dns.calls == [("store.example", 443)]
    assert client.calls[0][0] == "GET"
    assert client.calls[0][1].addresses == (
        ApprovedSocketAddress.from_ip(_PUBLIC_HTTP_IP, 443),
    )


def test_http_resolver_reports_drift_on_mismatch():
    resolver, _, _ = _safe_http_resolver(
        FakeHttpResponse(chunks=(b"actual bytes",))
    )

    result = resolver.resolve(_http_ref("https://store.example/x", _sha256(b"recorded bytes")))

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(b"actual bytes")
    assert result.content is None
    assert result.returned_bytes == 0


def test_http_resolver_http_error_status_is_unresolved():
    resolver, _, _ = _safe_http_resolver(FakeHttpResponse(status_code=404))

    result = resolver.resolve(_http_ref("https://store.example/missing", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "404" in (result.detail or "")


def test_http_resolver_refuses_oversized_fetch():
    body = b"0123456789" * 10  # 100 bytes
    response = FakeHttpResponse(chunks=(body[:10], body[10:]))
    resolver, _, _ = _safe_http_resolver(
        response,
        max_fetch_bytes=16,
    )

    result = resolver.resolve(_http_ref("https://store.example/big.bin", _sha256(body)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "limit" in (result.detail or "").lower()
    assert result.content is None
    assert response.closed is True


def test_http_resolver_rejects_declared_oversize_without_reading_body():
    response = FakeHttpResponse(
        headers={"content-length": "100"},
        chunks=(b"body must not be read",),
    )
    resolver, _, _ = _safe_http_resolver(
        response,
        max_fetch_bytes=16,
    )

    result = resolver.resolve(
        _http_ref("https://store.example/big.bin", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert response.iterated_chunks == 0
    assert response.closed is True


def test_http_resolver_truncates_payload_but_verifies():
    body = b"abcdefghij"
    resolver, _, _ = _safe_http_resolver(FakeHttpResponse(chunks=(body,)))

    result = resolver.resolve(_http_ref("https://store.example/x", _sha256(body)), max_bytes=4)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == b"abcd"
    assert result.truncated is True
    assert result.size_bytes == 10


def test_http_resolver_transport_error_is_unresolved_and_redacted():
    secret = "hunter2"
    resolver, _, _ = _safe_http_resolver(
        OutboundHttpTransportError(
            f"cannot fetch https://user:{secret}@store.example/x?token={secret}"
        )
    )

    result = resolver.resolve(_http_ref("https://store.example/x", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "fetch" in (result.detail or "").lower()
    assert secret not in (result.detail or "")
    assert "store.example" not in (result.detail or "")


def test_http_resolver_read_error_is_unresolved_and_closes_response():
    secret = "read-secret"
    response = FakeHttpResponse(
        chunks=(
            b"partial",
            OutboundHttpTransportError(
                f"socket error at store.example?token={secret}"
            ),
        )
    )
    resolver, _, _ = _safe_http_resolver(response)

    result = resolver.resolve(
        _http_ref("https://store.example/x", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert secret not in (result.detail or "")
    assert "store.example" not in (result.detail or "")
    assert response.closed is True


def test_http_resolver_credentialed_uri_is_denied_before_open():
    secret = "hunter2"
    resolver, client, dns = _safe_http_resolver()

    result = resolver.resolve(
        _http_ref(
            f"https://user:{secret}@store.example/x",
            _sha256(b"x"),
        )
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert secret not in (result.detail or "")
    assert client.calls == []
    assert dns.calls == []
    payload = result.to_json_dict()
    assert payload["uri"] == "http(s)://[redacted]"
    assert secret not in str(payload)


def test_http_resolver_omits_query_credentials_from_verified_display_uri():
    body = b"authorized signed download"
    resolver, _, _ = _safe_http_resolver(FakeHttpResponse(chunks=(body,)))

    result = resolver.resolve(
        _http_ref(
            "https://store.example/x?signature=hunter2#fragment",
            _sha256(body),
        )
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.to_json_dict()["uri"] == "https://store.example/x"


def test_http_resolver_denies_unsafe_literal_without_opening_client():
    resolver, client, dns = _safe_http_resolver()

    result = resolver.resolve(
        _http_ref(
            "http://169.254.169.254/latest/meta-data/",
            _sha256(b"x"),
        )
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert client.calls == []
    assert dns.calls == []


def test_http_resolver_revalidates_and_pins_each_redirect_hop():
    first = FakeHttpResponse(
        status_code=302,
        headers={"location": "https://archive.example/final.bin"},
    )
    body = b"redirected artifact"
    final = FakeHttpResponse(chunks=(body,))
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_HTTP_IP],
            "archive.example": ["142.250.72.14"],
        }
    )
    resolver, client, _ = _safe_http_resolver(first, final, dns=dns)

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(body))
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == body
    assert dns.calls == [
        ("store.example", 443),
        ("archive.example", 443),
    ]
    assert [target.hostname for _, target in client.calls] == [
        "store.example",
        "archive.example",
    ]
    assert client.calls[1][1].addresses == (
        ApprovedSocketAddress.from_ip("142.250.72.14", 443),
    )


def test_http_resolver_unsafe_redirect_never_reaches_second_target():
    redirect = FakeHttpResponse(
        status_code=302,
        headers={
            "location": "http://169.254.169.254/latest/meta-data/",
        },
    )
    resolver, client, dns = _safe_http_resolver(redirect)

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert len(client.calls) == 1
    assert dns.calls == [("store.example", 443)]


@pytest.mark.parametrize(
    "location",
    (
        "http://archive.example/final.bin",
        "https://user:secret@archive.example/final.bin",
    ),
)
def test_http_resolver_rejects_downgrade_and_credentialed_redirects_before_open(
    location: str,
):
    redirect = FakeHttpResponse(
        status_code=302,
        headers={"location": location},
    )
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_HTTP_IP],
            "archive.example": ["142.250.72.14"],
        }
    )
    resolver, client, _ = _safe_http_resolver(redirect, dns=dns)

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert len(client.calls) == 1
    assert dns.calls == [("store.example", 443)]


def test_http_resolver_malformed_redirect_is_redacted_unresolved():
    redirect = FakeHttpResponse(
        status_code=302,
        headers={"location": "http://[::1"},
    )
    resolver, client, dns = _safe_http_resolver(redirect)

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.detail == "HTTP artifact fetch failed or was denied."
    assert redirect.closed is True
    assert len(client.calls) == 1
    assert dns.calls == [("store.example", 443)]


def test_http_resolver_redirect_rebinding_is_denied_before_second_open():
    redirect = FakeHttpResponse(status_code=302, headers={"location": "/next"})
    dns = FakeAddressResolver(
        sequences={
            "store.example": (
                (_PUBLIC_HTTP_IP,),
                ("127.0.0.1",),
            )
        }
    )
    resolver, client, _ = _safe_http_resolver(redirect, dns=dns)

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert len(client.calls) == 1
    assert dns.calls == [
        ("store.example", 443),
        ("store.example", 443),
    ]


def test_http_resolver_enforces_finite_redirect_limit():
    resolver, client, _ = _safe_http_resolver(
        FakeHttpResponse(
            status_code=302,
            headers={"location": "https://archive.example/next"},
        ),
        max_redirects=0,
    )

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "redirect" in (result.detail or "").lower()
    assert len(client.calls) == 1


def test_http_resolver_can_resolve_only_http_schemes():
    resolver = HttpResolver()
    assert resolver.can_resolve(_http_ref("https://x/y", _sha256(b"x"))) is True
    assert resolver.can_resolve(_http_ref("http://x/y", _sha256(b"x"))) is True
    assert (
        resolver.can_resolve(
            ExternalArtifactReference(
                source_system="s3", uri="s3://b/k", content_hash=_sha256(b"x")
            )
        )
        is False
    )


def test_http_resolver_treats_malformed_legacy_reference_as_unresolved():
    resolver = HttpResolver()
    ref = _http_ref("https://[::1", _sha256(b"x"))

    assert resolver.can_resolve(ref) is True

    result = resolver.resolve(ref)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.to_json_dict()["uri"] == "http(s)://[redacted]"
    assert result.detail == "HTTP artifact fetch failed or was denied."


def test_malformed_legacy_http_uri_is_redacted_despite_mismatched_source_label():
    secret = "hunter2"
    ref = ExternalArtifactReference(
        source_system="anything",
        uri=f"HTTP://user:{secret}@[::1",
        content_hash=_sha256(b"x"),
    )

    result = ResolverRegistry().resolve(ref)
    payload = result.to_json_dict()

    assert result.status is ResolutionStatus.UNRESOLVED
    assert payload["uri"] == "http(s)://[redacted]"
    assert secret not in str(payload)


def test_default_registry_dispatches_http():
    body = b"served over http"
    client = FakeSafeHttpClient((FakeHttpResponse(chunks=(body,)),))
    policy = OutboundHttpPolicy(
        address_resolver=FakeAddressResolver(
            {"store.example": [_PUBLIC_HTTP_IP]}
        )
    )
    registry = default_registry(
        http_policy=policy,
        http_client=client,
    )
    result = registry.resolve(_http_ref("https://store.example/x", _sha256(body)))
    assert result.status is ResolutionStatus.VERIFIED


def test_http_resolver_resolves_explicitly_approved_internal_destination():
    body = b"internal instrument export"
    dns = FakeAddressResolver({"instrument.lab.example": ["10.20.1.7"]})
    policy = OutboundHttpPolicy(
        address_resolver=dns,
        allowed_authorities=("https://instrument.lab.example",),
        allowed_networks=("10.20.0.0/16",),
    )
    client = FakeSafeHttpClient((FakeHttpResponse(chunks=(body,)),))
    resolver = HttpResolver(policy=policy, client=client)

    result = resolver.resolve(
        _http_ref(
            "https://instrument.lab.example/export.bin",
            _sha256(body),
        )
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == body
    assert client.calls[0][1].addresses == (
        ApprovedSocketAddress.from_ip("10.20.1.7", 443),
    )


# --- RcloneResolver -------------------------------------------------------


def _rclone_ref(uri: str, content_hash: str) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system="rclone", uri=uri, content_hash=content_hash
    )


def _fake_rclone_runner(*, size_bytes: int | None, body: bytes, cat_returncode: int = 0):
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        if args[0] == "size":
            if size_bytes is None:
                return RcloneCompleted(1, b"", b"directory not found")
            return RcloneCompleted(
                0, f'{{"count":1,"bytes":{size_bytes},"sizeless":0}}'.encode(), b""
            )
        if args[0] == "cat":
            return RcloneCompleted(cat_returncode, body, b"" if cat_returncode == 0 else b"boom")
        raise AssertionError(f"unexpected rclone args: {args}")

    runner.calls = calls
    return runner


def test_rclone_resolver_verifies_object():
    data = b"object stored in onedrive via rclone"
    runner = _fake_rclone_runner(size_bytes=len(data), body=data)
    resolver = RcloneResolver(runner=runner)

    result = resolver.resolve(
        _rclone_ref("rclone://lab-onedrive/experiments/001/x.bin", _sha256(data))
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.size_bytes == len(data)
    # rclone target maps rclone://remote/path -> remote:path
    assert runner.calls[0] == ["size", "--json", "lab-onedrive:experiments/001/x.bin"]
    assert runner.calls[1] == ["cat", "lab-onedrive:experiments/001/x.bin"]


def test_rclone_resolver_reports_drift():
    data = b"actual remote bytes"
    runner = _fake_rclone_runner(size_bytes=len(data), body=data)
    resolver = RcloneResolver(runner=runner)

    result = resolver.resolve(_rclone_ref("rclone://r/x", _sha256(b"recorded")))

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(data)
    assert result.size_bytes == len(data)
    assert result.content is None
    assert result.returned_bytes == 0


def test_rclone_resolver_missing_object_is_unresolved():
    runner = _fake_rclone_runner(size_bytes=None, body=b"")
    resolver = RcloneResolver(runner=runner)

    result = resolver.resolve(_rclone_ref("rclone://r/missing", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    # cat must not run when the object cannot be stat-ed.
    assert all(call[0] != "cat" for call in runner.calls)


def test_rclone_resolver_refuses_oversized_object():
    runner = _fake_rclone_runner(size_bytes=1_000_000, body=b"x")
    resolver = RcloneResolver(runner=runner, max_fetch_bytes=1024)

    result = resolver.resolve(_rclone_ref("rclone://r/big", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "limit" in (result.detail or "").lower()
    assert all(call[0] != "cat" for call in runner.calls)


def test_rclone_resolver_truncates_payload_but_verifies():
    data = b"abcdefghij"
    runner = _fake_rclone_runner(size_bytes=len(data), body=data)
    resolver = RcloneResolver(runner=runner)

    result = resolver.resolve(_rclone_ref("rclone://r/x", _sha256(data)), max_bytes=4)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == b"abcd"
    assert result.truncated is True


def test_rclone_resolver_cat_failure_is_unresolved():
    data = b"data"
    runner = _fake_rclone_runner(size_bytes=len(data), body=data, cat_returncode=1)
    resolver = RcloneResolver(runner=runner)

    result = resolver.resolve(_rclone_ref("rclone://r/x", _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "rclone" in (result.detail or "").lower()


def test_rclone_resolver_missing_binary_is_unresolved():
    def runner(args):
        raise FileNotFoundError("rclone not installed")

    result = RcloneResolver(runner=runner).resolve(
        _rclone_ref("rclone://r/x", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "unavailable" in (result.detail or "").lower()


def test_rclone_resolver_can_resolve_only_rclone_scheme():
    resolver = RcloneResolver(runner=lambda args: RcloneCompleted(0, b"", b""))
    assert resolver.can_resolve(_rclone_ref("rclone://r/x", _sha256(b"x"))) is True
    assert (
        resolver.can_resolve(
            ExternalArtifactReference(
                source_system="s3", uri="s3://b/k", content_hash=_sha256(b"x")
            )
        )
        is False
    )


# --- ExternalArtifactReference.for_store (field form) ---------------------


def test_external_artifact_reference_for_store_sets_fields_and_uri():
    ref = ExternalArtifactReference.for_store(
        store_name="lab-onedrive", locator="/exp/001/x.fcs", content_hash=_sha256(b"x")
    )
    assert ref.store_name == "lab-onedrive"
    assert ref.locator == "exp/001/x.fcs"  # leading slash stripped
    assert ref.uri == "store://lab-onedrive/exp/001/x.fcs"
    assert ref.source_system == "store"


def test_external_artifact_reference_rejects_unpaired_store_fields():
    with pytest.raises(ValueError, match="together"):
        ExternalArtifactReference(
            source_system="store",
            uri="store://lab/x",
            content_hash=_sha256(b"x"),
            store_name="lab",
        )


def test_external_artifact_reference_without_store_fields_is_valid():
    ref = ExternalArtifactReference(
        source_system="s3", uri="s3://bucket/key", content_hash=_sha256(b"x")
    )
    assert ref.store_name is None
    assert ref.locator is None


# --- store_relative_reference --------------------------------------------


def _data_store(kind: StoreKind, root: str, **overrides) -> DataStore:
    fields = {
        "store_id": uuid4(),
        "project_id": uuid4(),
        "name": "store",
        "kind": kind,
        "root": root,
    }
    fields.update(overrides)
    return DataStore(**fields)


def test_store_relative_reference_local_builds_file_uri():
    store = _data_store(StoreKind.LOCAL_FS, "/data/store")
    ref = store_relative_reference(store, path="exp/001/x.txt", content_hash=_sha256(b"x"))
    assert ref is not None
    assert ref.source_system == "local"
    assert ref.uri == Path("/data/store/exp/001/x.txt").as_uri()


def test_store_relative_reference_rclone_uses_credential_ref_as_remote():
    store = _data_store(
        StoreKind.ONEDRIVE, "experiments", name="lab", credential_ref="lab-onedrive"
    )
    ref = store_relative_reference(store, path="001/x.fcs", content_hash=_sha256(b"x"))
    assert ref is not None
    assert ref.source_system == "rclone"
    assert ref.uri == "rclone://lab-onedrive/experiments/001/x.fcs"


def test_store_relative_reference_http_joins_endpoint_or_root():
    store = _data_store(StoreKind.HTTP, "https://files.example/base", name="web")
    ref = store_relative_reference(store, path="x.bin", content_hash=_sha256(b"x"))
    assert ref is not None
    assert ref.source_system == "http"
    assert ref.uri == "https://files.example/base/x.bin"


def test_store_relative_reference_unsupported_kind_returns_none():
    store = _data_store(StoreKind.DATABASE, "postgresql://lims", name="lims")
    assert store_relative_reference(store, path="q", content_hash=_sha256(b"x")) is None


def test_store_relative_reference_git_builds_commit_pin():
    store = _data_store(
        StoreKind.GIT, "https://example.com/org/repo.git", name="repo"
    )
    commit = "a" * 40
    ref = store_relative_reference(
        store, path=f"analysis/run.py@{commit}", content_hash=_sha256(b"x")
    )
    assert ref is not None
    assert ref.source_system == "git"
    assert ref.uri == f"git+https://example.com/org/repo.git#{commit}:analysis/run.py"


def test_store_relative_reference_git_without_commit_returns_none():
    store = _data_store(
        StoreKind.GIT, "https://example.com/org/repo.git", name="repo"
    )
    assert (
        store_relative_reference(store, path="analysis/run.py", content_hash=_sha256(b"x"))
        is None
    )


def test_store_relative_reference_git_resolves_end_to_end(tmp_path):
    # The locator produced from a git store must be consumable by GitResolver.
    data = b"pinned analysis code"
    store = _data_store(
        StoreKind.GIT, "https://example.com/org/repo.git", name="repo"
    )
    commit = "b" * 40
    ref = store_relative_reference(
        store, path=f"src/model.py@{commit}", content_hash=_sha256(data)
    )
    assert ref is not None
    registry = ResolverRegistry(
        [GitResolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)]
    )

    result = registry.resolve(ref)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


# --- check_store_health ---------------------------------------------------


def test_check_store_health_local_fs_healthy(tmp_path):
    store = _data_store(StoreKind.LOCAL_FS, str(tmp_path))
    health = check_store_health(store)
    assert health.status is StoreHealthStatus.HEALTHY
    assert health.is_healthy is True


def test_check_store_health_local_fs_missing_root(tmp_path):
    store = _data_store(StoreKind.LOCAL_FS, str(tmp_path / "absent"))
    health = check_store_health(store)
    assert health.status is StoreHealthStatus.UNREACHABLE
    assert "not found" in (health.detail or "").lower()


def test_check_store_health_rclone_healthy_via_runner():
    store = _data_store(
        StoreKind.ONEDRIVE, "experiments", name="lab", credential_ref="lab-onedrive"
    )
    calls = []

    def runner(args):
        calls.append(args)
        return RcloneCompleted(0, b"001/\n002/\n", b"")

    health = check_store_health(store, rclone_runner=runner)
    assert health.status is StoreHealthStatus.HEALTHY
    assert calls[0] == ["lsf", "--max-depth", "1", "lab-onedrive:experiments"]


def test_check_store_health_rclone_unreachable_via_runner():
    store = _data_store(StoreKind.S3, "lab-archive", name="arch", credential_ref="s3")

    def runner(args):
        return RcloneCompleted(1, b"", b"directory not found")

    health = check_store_health(store, rclone_runner=runner)
    assert health.status is StoreHealthStatus.UNREACHABLE


def test_check_store_health_rclone_missing_binary():
    store = _data_store(StoreKind.BOX, "root", name="b", credential_ref="box")

    def runner(args):
        raise FileNotFoundError("rclone not installed")

    health = check_store_health(store, rclone_runner=runner)
    assert health.status is StoreHealthStatus.UNREACHABLE
    assert "unavailable" in (health.detail or "").lower()


def test_check_store_health_http_healthy_via_client():
    store = _data_store(StoreKind.HTTP, "https://files.example/base", name="web")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    health = check_store_health(store, http_client=client)
    assert health.status is StoreHealthStatus.HEALTHY


def test_check_store_health_http_unreachable_via_client():
    store = _data_store(StoreKind.HTTP, "https://files.example/base", name="web")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    health = check_store_health(store, http_client=client)
    assert health.status is StoreHealthStatus.UNREACHABLE


def test_check_store_health_database_is_unsupported():
    store = _data_store(StoreKind.DATABASE, "postgresql://lims", name="lims")
    health = check_store_health(store)
    assert health.status is StoreHealthStatus.UNSUPPORTED


def test_check_store_health_git_healthy_via_runner():
    store = _data_store(StoreKind.GIT, "https://example.com/org/repo.git", name="repo")

    def runner(args):
        assert args == ["ls-remote", store.root, "HEAD"]
        return GitCompleted(0, b"a" * 40 + b"\tHEAD\n", b"")

    health = check_store_health(store, git_runner=runner)
    assert health.status is StoreHealthStatus.HEALTHY


def test_check_store_health_git_unreachable_via_runner():
    store = _data_store(StoreKind.GIT, "https://example.com/org/missing.git", name="repo")

    def runner(args):
        return GitCompleted(128, b"", b"fatal: repository not found")

    health = check_store_health(store, git_runner=runner)
    assert health.status is StoreHealthStatus.UNREACHABLE
    assert "not found" in (health.detail or "")


def test_check_store_health_git_missing_binary():
    store = _data_store(StoreKind.GIT, "https://example.com/org/repo.git", name="repo")

    def runner(args):
        raise OSError("git not found")

    health = check_store_health(store, git_runner=runner)
    assert health.status is StoreHealthStatus.UNREACHABLE
    assert "git is unavailable" in (health.detail or "")


# --- ResolverRegistry -----------------------------------------------------


def test_registry_dispatches_to_matching_resolver(tmp_path):
    data = b"hello"
    path = _write(tmp_path, "a.txt", data)
    registry = default_registry()

    result = registry.resolve(_local_ref(path, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED


def test_registry_unresolved_when_no_resolver_matches():
    registry = default_registry()
    ref = ExternalArtifactReference(
        source_system="s3", uri="s3://bucket/key", content_hash=_sha256(b"x")
    )

    result = registry.resolve(ref)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "s3" in (result.detail or "")


def test_registry_allowed_roots_thread_through(tmp_path):
    allowed = tmp_path / "store"
    allowed.mkdir()
    outside = _write(tmp_path, "secret.txt", b"nope")
    registry = default_registry(allowed_roots=[allowed])

    result = registry.resolve(_local_ref(outside, _sha256(b"nope")))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_registry_prefers_first_capable_resolver(tmp_path):
    data = b"first wins"
    path = _write(tmp_path, "a.txt", data)

    class StubResolver(ArtifactResolver):
        def can_resolve(self, ref):
            return True

        def resolve(self, ref, *, max_bytes=DEFAULT_MAX_BYTES, byte_range=None):
            from datetime import datetime, timezone

            return ResolvedArtifact(
                status=ResolutionStatus.DRIFTED,
                source_system=ref.source_system,
                uri=ref.uri,
                expected_hash=ref.content_hash,
                fetched_at=datetime.now(timezone.utc),
                detail="stub",
            )

    registry = ResolverRegistry([LocalFilesystemResolver(), StubResolver()])
    result = registry.resolve(_local_ref(path, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED  # local resolver matched first


def test_resolved_artifact_to_json_dict_omits_raw_bytes(tmp_path):
    data = b"payload"
    path = _write(tmp_path, "a.txt", data)
    result = LocalFilesystemResolver().resolve(_local_ref(path, _sha256(data)))

    payload = result.to_json_dict()

    assert payload["status"] == "verified"
    assert payload["returned_bytes"] == len(data)
    assert payload["size_bytes"] == len(data)
    assert "content" not in payload


@pytest.mark.parametrize(
    "status",
    [ResolutionStatus.DRIFTED, ResolutionStatus.UNRESOLVED],
)
def test_resolved_artifact_discards_content_unless_verified(status):
    result = ResolvedArtifact(
        status=status,
        source_system="custom",
        uri="custom://artifact",
        expected_hash=_sha256(b"recorded"),
        observed_hash=_sha256(b"attacker-controlled"),
        content=b"attacker-controlled",
        size_bytes=len(b"attacker-controlled"),
        fetched_at=datetime.now(timezone.utc),
        detail="diagnostic",
    )

    assert result.content is None
    assert result.returned_bytes == 0
    assert result.observed_hash == _sha256(b"attacker-controlled")
    assert result.size_bytes == len(b"attacker-controlled")
    assert result.detail == "diagnostic"


def test_resolved_artifact_downgrades_false_verified_status():
    result = ResolvedArtifact(
        status=ResolutionStatus.VERIFIED,
        source_system="custom",
        uri="custom://artifact",
        expected_hash=_sha256(b"recorded"),
        observed_hash=_sha256(b"attacker-controlled"),
        content=b"attacker-controlled",
        size_bytes=len(b"attacker-controlled"),
        fetched_at=datetime.now(timezone.utc),
    )

    assert result.status is ResolutionStatus.DRIFTED
    assert result.is_verified is False
    assert result.content is None
    assert result.returned_bytes == 0


# --- content-hash recovery of moved/renamed local artifacts ---------------


def test_recovery_finds_moved_file_by_hash(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"analysis output that was moved"
    moved = root / "new" / "result.csv"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(data)
    missing = root / "old" / "result.csv"  # never existed here
    resolver = LocalFilesystemResolver(
        allowed_roots=[root], recovery=RecoveryPolicy(enabled=True)
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.observed_hash == _sha256(data)
    assert "Recovered from" in (result.detail or "")


def test_recovery_finds_renamed_file_by_hash(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"same bytes, different name"
    (root / "result_final.csv").write_bytes(data)
    missing = root / "result.csv"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root], recovery=RecoveryPolicy(enabled=True)
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


def test_recovery_disabled_by_default_stays_unresolved(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"present but recovery off"
    (root / "moved.bin").write_bytes(data)
    missing = root / "gone.bin"
    resolver = LocalFilesystemResolver(allowed_roots=[root])  # recovery defaults off

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "not found" in (result.detail or "").lower()


def test_recovery_requires_allowed_roots(tmp_path):
    # Enabled but unscoped: must not walk the whole filesystem.
    data = b"unscoped recovery is refused"
    (tmp_path / "present.bin").write_bytes(data)
    missing = tmp_path / "gone.bin"
    resolver = LocalFilesystemResolver(recovery=RecoveryPolicy(enabled=True))

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_recovery_never_reads_outside_allowed_roots(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"secret bytes outside the store"
    outside = tmp_path / "elsewhere" / "secret.bin"
    outside.parent.mkdir()
    outside.write_bytes(data)
    missing = root / "gone.bin"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root], recovery=RecoveryPolicy(enabled=True)
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_recovery_does_not_falsely_match_same_name_different_bytes(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    (root / "result.csv").write_bytes(b"a decoy with the same name")
    missing = root / "sub" / "result.csv"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root], recovery=RecoveryPolicy(enabled=True)
    )

    result = resolver.resolve(_local_ref(missing, _sha256(b"the real content")))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_recovery_respects_byte_budget(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"z" * 1024
    (root / "moved.bin").write_bytes(data)
    missing = root / "gone.bin"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True, max_bytes=16),
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_recovery_respects_file_budget(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"budget exhausted before hashing"
    (root / "result.csv").write_bytes(data)
    missing = root / "sub" / "result.csv"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True, max_files=0),
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_registry_from_env_enables_recovery(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    data = b"recovery wired through the environment"
    (root / "moved.bin").write_bytes(data)
    missing = root / "gone.bin"
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_ALLOWED_ROOTS", str(root))
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_RECOVERY", "1")

    result = registry_from_env().resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


def test_registry_from_env_recovery_off_by_default(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    data = b"present but env flag unset"
    (root / "moved.bin").write_bytes(data)
    missing = root / "gone.bin"
    monkeypatch.setenv("LAB_TRACKER_RESOLVER_ALLOWED_ROOTS", str(root))
    monkeypatch.delenv("LAB_TRACKER_RESOLVER_RECOVERY", raising=False)

    result = registry_from_env().resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_outbound_http_policy_from_env_requires_complete_internal_override(
    monkeypatch,
):
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES",
        "http://10.20.1.7",
    )
    monkeypatch.delenv(
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS",
        raising=False,
    )

    with pytest.raises(ValueError, match="both exact authorities and networks"):
        outbound_http_policy_from_env()


def test_outbound_http_policy_from_env_allows_exact_internal_literal(
    monkeypatch,
):
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES",
        "http://10.20.1.7",
    )
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS",
        "10.20.0.0/16",
    )

    target = outbound_http_policy_from_env().authorize(
        "http://10.20.1.7/artifact.bin"
    )

    assert target.addresses == (
        ApprovedSocketAddress.from_ip("10.20.1.7", 80),
    )


def test_outbound_http_policy_from_env_rejects_empty_list_entries(
    monkeypatch,
):
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES",
        "http://10.20.1.7,",
    )
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS",
        "10.20.0.0/16",
    )

    with pytest.raises(ValueError, match="contains an empty entry"):
        outbound_http_policy_from_env()


# --- GitResolver ----------------------------------------------------------


_GIT_REMOTE = "https://example.com/org/repo.git"


def _git_ref(
    content_hash: str,
    *,
    commit: str = "a" * 40,
    path: str = "analysis/run.py",
    remote: str = _GIT_REMOTE,
    source_system: str = "git",
) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system=source_system,
        uri=f"git+{remote}#{commit}:{path}",
        content_hash=content_hash,
    )


def _fake_git_runner(
    *,
    blob: bytes | None,
    fetch_returncode: int = 0,
    cat_returncode: int = 0,
    size_returncode: int = 0,
    reported_size: int | None = None,
):
    calls: list[list[str]] = []

    def runner(args):
        calls.append(args)
        # args are like ["-C", <cache>, <command>, ...].
        command = args[2] if len(args) >= 3 and args[0] == "-C" else args[0]
        if command == "init":
            return GitCompleted(0, b"", b"")
        if command == "fetch":
            return GitCompleted(
                fetch_returncode,
                b"",
                b"" if fetch_returncode == 0 else b"remote error: not found",
            )
        if command == "cat-file":
            mode = args[3] if len(args) >= 4 else ""
            if mode == "-s":
                if size_returncode != 0:
                    return GitCompleted(size_returncode, b"", b"fatal: bad object")
                size = reported_size if reported_size is not None else len(blob or b"")
                return GitCompleted(0, f"{size}\n".encode(), b"")
            if cat_returncode != 0:
                return GitCompleted(cat_returncode, b"", b"fatal: bad object")
            return GitCompleted(0, blob or b"", b"")
        raise AssertionError(f"unexpected git args: {args}")

    runner.calls = calls
    return runner


def test_git_resolver_verifies_blob(tmp_path):
    data = b"import numpy as np  # the pinned analysis script"
    resolver = GitResolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.size_bytes == len(data)
    assert result.observed_hash == _sha256(data)
    assert result.content_type == "text/x-python"


def test_git_resolver_reports_drift_on_mismatch(tmp_path):
    resolver = GitResolver(runner=_fake_git_runner(blob=b"actual"), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(b"what the graph recorded")))

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(b"actual")
    assert result.content is None
    assert result.returned_bytes == 0
    assert result.detail is not None


def test_git_resolver_missing_object_is_unresolved(tmp_path):
    resolver = GitResolver(
        runner=_fake_git_runner(blob=None, cat_returncode=128), cache_root=tmp_path
    )

    result = resolver.resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "git failed" in (result.detail or "")


def test_git_resolver_fetch_failure_is_unresolved(tmp_path):
    resolver = GitResolver(
        runner=_fake_git_runner(blob=None, fetch_returncode=128), cache_root=tmp_path
    )

    result = resolver.resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "not found" in (result.detail or "")


def test_git_resolver_unverifiable_hash_is_unresolved(tmp_path):
    resolver = GitResolver(runner=_fake_git_runner(blob=b"data"), cache_root=tmp_path)

    result = resolver.resolve(_git_ref("datalad-key:MD5E-s4--abc"))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "datalad-key" in (result.detail or "")


def test_git_resolver_bad_locator_is_unresolved(tmp_path):
    resolver = GitResolver(runner=_fake_git_runner(blob=b"data"), cache_root=tmp_path)
    ref = ExternalArtifactReference(
        source_system="git",
        uri=f"git+{_GIT_REMOTE}",  # no #<commit>:<path>
        content_hash=_sha256(b"data"),
    )

    result = resolver.resolve(ref)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "not a git locator" in (result.detail or "")


def test_git_resolver_missing_binary_is_unresolved(tmp_path):
    def runner(args):
        raise OSError("git not found")

    result = GitResolver(runner=runner, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "git is unavailable" in (result.detail or "")


def test_git_resolver_truncates_payload_but_verifies(tmp_path):
    data = b"y" * 100
    resolver = GitResolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(data)), max_bytes=10)

    assert result.status is ResolutionStatus.VERIFIED  # full blob hashed
    assert result.truncated is True
    assert result.content == data[:10]
    assert result.size_bytes == len(data)


def test_git_resolver_returns_byte_range_slice(tmp_path):
    data = b"0123456789abcdef"
    resolver = GitResolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(data)), byte_range=(4, 8))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data[4:8]


def test_git_resolver_can_resolve_by_source_system_and_scheme():
    resolver = GitResolver(runner=_fake_git_runner(blob=b""))

    assert resolver.can_resolve(_git_ref(_sha256(b""))) is True
    # A git+ URI is recognised even under a different source_system.
    assert resolver.can_resolve(_git_ref(_sha256(b""), source_system="other")) is True
    assert (
        resolver.can_resolve(
            ExternalArtifactReference(
                source_system="local",
                uri="file:///tmp/x",
                content_hash=_sha256(b""),
            )
        )
        is False
    )


def test_registry_dispatches_git(tmp_path):
    data = b"pinned code"
    registry = ResolverRegistry(
        [
            LocalFilesystemResolver(),
            GitResolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path),
        ]
    )

    result = registry.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED


def test_default_capabilities_for_git():
    from lab_tracker.models import StoreCapability, default_store_capabilities

    assert default_store_capabilities(StoreKind.GIT) == [
        StoreCapability.BYTES_BY_PATH,
        StoreCapability.BYTE_RANGE,
        StoreCapability.VERSIONED_SNAPSHOT,
    ]


# --- GitResolver security gate (lt-81s6.7) --------------------------------


def test_git_resolver_refuses_remote_not_in_allowlist(tmp_path):
    runner = _fake_git_runner(blob=b"data")
    resolver = GitResolver(
        runner=runner,
        cache_root=tmp_path,
        allowed_remotes=["https://allowed.example/"],
    )

    result = resolver.resolve(_git_ref(_sha256(b"data")))  # remote example.com

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (result.detail or "")
    assert runner.calls == []  # refused before any git subprocess


def test_git_resolver_allows_remote_in_allowlist_by_prefix(tmp_path):
    data = b"allowed remote payload"
    resolver = GitResolver(
        runner=_fake_git_runner(blob=data),
        cache_root=tmp_path,
        allowed_remotes=["https://example.com/"],
    )

    result = resolver.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


def test_git_resolver_refuses_oversized_blob(tmp_path):
    resolver = GitResolver(
        runner=_fake_git_runner(blob=b"x", reported_size=10_000),
        cache_root=tmp_path,
        max_fetch_bytes=1024,
    )

    result = resolver.resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "exceeds" in (result.detail or "")


def test_git_resolver_evicts_cache_over_quota(tmp_path):
    base = tmp_path / "gitcache"
    base.mkdir()
    old = base / "oldremotecache"
    old.mkdir()
    (old / "objects.pack").write_bytes(b"x" * 5000)
    os.utime(old, (1, 1))  # mark as the least-recently-used cache

    data = b"new pinned code"
    resolver = GitResolver(
        runner=_fake_git_runner(blob=data),
        cache_root=base,
        max_cache_bytes=1024,  # smaller than the 5000-byte old cache
    )

    result = resolver.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert not old.exists()  # evicted before the new fetch


def test_registry_from_env_git_denies_remotes_by_default(monkeypatch):
    monkeypatch.delenv("LAB_TRACKER_GIT_ALLOWED_REMOTES", raising=False)

    result = registry_from_env().resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (result.detail or "")


def test_registry_from_env_git_allowlist_excludes_unlisted_remote(monkeypatch):
    monkeypatch.setenv("LAB_TRACKER_GIT_ALLOWED_REMOTES", "https://other.example/")

    result = registry_from_env().resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (result.detail or "")


def test_git_resolver_rejects_option_like_components(tmp_path):
    runner = _fake_git_runner(blob=b"data")
    resolver = GitResolver(runner=runner, cache_root=tmp_path)
    # A remote that would be read as a git option must not reach the subprocess.
    ref = ExternalArtifactReference(
        source_system="git",
        uri=f"git+--upload-pack=evil#{'a' * 40}:run.py",
        content_hash=_sha256(b"data"),
    )

    result = resolver.resolve(ref)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "not a git locator" in (result.detail or "")
    assert runner.calls == []


# --- RcloneResolver remote allowlist ---------------------------------------


def test_rclone_resolver_refuses_remote_not_in_allowlist():
    runner = _fake_rclone_runner(size_bytes=4, body=b"data")
    resolver = RcloneResolver(runner=runner, allowed_remotes=["lab-onedrive"])

    result = resolver.resolve(
        _rclone_ref("rclone://other-remote/exp/x.bin", _sha256(b"data"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (result.detail or "")
    assert runner.calls == []  # refused before any rclone subprocess


def test_rclone_resolver_allows_listed_remote():
    data = b"allowed remote payload"
    runner = _fake_rclone_runner(size_bytes=len(data), body=data)
    resolver = RcloneResolver(runner=runner, allowed_remotes=["lab-onedrive"])

    result = resolver.resolve(
        _rclone_ref("rclone://lab-onedrive/exp/x.bin", _sha256(data))
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


def test_registry_from_env_rclone_denies_remotes_by_default(monkeypatch):
    monkeypatch.delenv("LAB_TRACKER_RCLONE_ALLOWED_REMOTES", raising=False)

    result = registry_from_env().resolve(
        _rclone_ref("rclone://any-remote/x.bin", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (result.detail or "")


def test_registry_from_env_rclone_allowlist_admits_named_remote(monkeypatch):
    monkeypatch.setenv("LAB_TRACKER_RCLONE_ALLOWED_REMOTES", "lab-onedrive, backup")

    registry = registry_from_env()
    denied = registry.resolve(_rclone_ref("rclone://other/x.bin", _sha256(b"x")))

    assert denied.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (denied.detail or "")
    # A listed remote passes the allowlist gate (and then fails on the real
    # rclone binary being absent/unreachable in this environment, which is a
    # different detail message).
    allowed = registry.resolve(_rclone_ref("rclone://lab-onedrive/x.bin", _sha256(b"x")))
    assert "allowlist" not in (allowed.detail or "")
