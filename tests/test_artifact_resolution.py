import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from http_security_fakes import (
    FakeAddressResolver,
    FakeClock,
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
from lab_tracker.git_remote_policy import GitRemotePolicy
from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind
from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpPolicy,
    OutboundHttpTransportError,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


_GIT_REMOTE = "https://example.com/org/repo.git"


def _git_policy(*grants: str) -> GitRemotePolicy:
    return GitRemotePolicy.from_config(",".join(grants))


def _git_resolver(*, remote_policy: GitRemotePolicy | None = None, **kwargs):
    return GitResolver(
        remote_policy=remote_policy or _git_policy(_GIT_REMOTE),
        **kwargs,
    )


class _FakeProcessExecutor:
    """Deterministic executor seam that still exercises streaming consumers."""

    def __init__(self, steps, *, on_run=None):
        self.steps = list(steps)
        self.on_run = on_run
        self.calls = []

    def run(
        self,
        command,
        *,
        deadline,
        stdout_limit_bytes,
        stderr_limit_bytes,
        stdout_consumer=None,
        cwd=None,
        env=None,
    ):
        deadline.check()
        index = len(self.calls)
        self.calls.append(
            {
                "command": list(command),
                "deadline": deadline,
                "stdout_limit_bytes": stdout_limit_bytes,
                "stderr_limit_bytes": stderr_limit_bytes,
                "streaming": stdout_consumer is not None,
                "cwd": cwd,
                "env": env,
            }
        )
        if self.on_run is not None:
            self.on_run(index, deadline)
        deadline.check()
        if "ls-remote" in command and "--get-url" in command:
            remote = command[-1]
            stdout = f"{remote}\n".encode()
            assert len(stdout) <= stdout_limit_bytes
            return SimpleNamespace(
                returncode=0,
                stdout=stdout,
                stdout_bytes=len(stdout),
                stderr_bytes=0,
            )
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        returncode, chunks, stderr = step
        stdout = b"".join(chunks)
        assert len(stderr) <= stderr_limit_bytes
        if stdout_consumer is None:
            assert len(stdout) <= stdout_limit_bytes
        else:
            for chunk in chunks:
                deadline.check()
                stdout_consumer(chunk)
                deadline.check()
            stdout = b""
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )


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
    clock: FakeClock | None = None,
    deadline_seconds: float = 30.0,
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
        deadline_seconds=deadline_seconds,
        clock=clock or FakeClock(),
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
    clock = FakeClock(100.0)
    resolver, client, _ = _safe_http_resolver(
        first,
        final,
        dns=dns,
        clock=clock,
        deadline_seconds=5.0,
    )

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
    assert len(dns.deadlines) == 2
    assert len(client.deadlines) == 2
    shared_deadline = dns.deadlines[0]
    assert shared_deadline is not None
    assert shared_deadline.expires_at == 105.0
    assert all(
        deadline is shared_deadline
        for deadline in (*dns.deadlines, *client.deadlines)
    )


def test_http_resolver_redirect_chain_expires_before_next_hop() -> None:
    clock = FakeClock()

    def advance_on_location(seconds: float):
        def advance(header_name: str) -> None:
            if header_name.lower() == "location":
                clock.advance(seconds)

        return advance

    first = FakeHttpResponse(
        status_code=302,
        headers={"Location": "https://archive.example/middle"},
        on_get_header=advance_on_location(0.6),
    )
    second = FakeHttpResponse(
        status_code=302,
        headers={"Location": "https://final.example/artifact"},
        on_get_header=advance_on_location(0.5),
    )
    final = FakeHttpResponse(chunks=(b"must not be fetched",))
    dns = FakeAddressResolver(
        {
            "store.example": [_PUBLIC_HTTP_IP],
            "archive.example": ["142.250.72.14"],
            "final.example": ["151.101.1.69"],
        }
    )
    resolver, client, _ = _safe_http_resolver(
        first,
        second,
        final,
        dns=dns,
        clock=clock,
        deadline_seconds=1.0,
    )

    result = resolver.resolve(
        _http_ref("https://store.example/start", _sha256(b"must not be fetched"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.observed_hash is None
    assert result.detail == "HTTP artifact fetch failed or was denied."
    assert clock.now == pytest.approx(1.1)
    assert dns.calls == [
        ("store.example", 443),
        ("archive.example", 443),
    ]
    assert [target.hostname for _, target in client.calls] == [
        "store.example",
        "archive.example",
    ]
    assert first.closed is True
    assert second.closed is True
    assert final.closed is False


def test_http_resolver_hashing_expiry_discards_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"partially hashed artifact"
    reference = _http_ref("https://store.example/x", _sha256(body))
    clock = FakeClock()
    response = FakeHttpResponse(chunks=(body,))
    resolver, client, dns = _safe_http_resolver(
        response,
        clock=clock,
        deadline_seconds=1.0,
    )
    real_hashlib_new = hashlib.new

    class AdvancingHasher:
        def __init__(self, algorithm: str) -> None:
            self._hasher = real_hashlib_new(algorithm)

        def update(self, chunk: bytes) -> None:
            self._hasher.update(chunk)
            clock.advance(1.0)

        def hexdigest(self) -> str:
            return self._hasher.hexdigest()

    monkeypatch.setattr(
        "lab_tracker.artifact_resolution.hashlib.new",
        AdvancingHasher,
    )

    result = resolver.resolve(reference)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.observed_hash is None
    assert result.size_bytes is None
    assert response.closed is True
    assert dns.deadlines[0] is client.deadlines[0]


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
    assert result.detail == "rclone artifact resolution failed."


def test_rclone_resolver_missing_binary_is_unresolved():
    def runner(args):
        raise FileNotFoundError("rclone not installed")

    result = RcloneResolver(runner=runner).resolve(
        _rclone_ref("rclone://r/x", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "rclone artifact resolution failed."


def test_rclone_executor_streams_under_one_deadline_and_verifies():
    data = b"streamed object"
    executor = _FakeProcessExecutor(
        [
            (0, (f'{{"bytes":{len(data)}}}'.encode(),), b""),
            (0, (b"streamed ", b"object"), b""),
        ]
    )
    resolver = RcloneResolver(
        executor=executor,
        allowed_remotes=["lab"],
        max_fetch_bytes=len(data),
    )

    result = resolver.resolve(_rclone_ref("rclone://lab/private/x.bin", _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert [call["command"][1] for call in executor.calls] == ["size", "cat"]
    assert executor.calls[1]["streaming"] is True
    assert executor.calls[1]["stdout_limit_bytes"] == len(data)
    assert len({id(call["deadline"]) for call in executor.calls}) == 1


def test_rclone_actual_stream_growth_over_cap_discards_partial_result():
    executor = _FakeProcessExecutor(
        [
            (0, (b'{"bytes":2}',), b""),
            (0, (b"ab", b"secret-growth"), b""),
        ]
    )
    result = RcloneResolver(
        executor=executor,
        max_fetch_bytes=2,
    ).resolve(_rclone_ref("rclone://private/secret.bin", _sha256(b"ab")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.observed_hash is None
    assert result.size_bytes is None
    assert result.detail == "rclone artifact resolution failed."
    assert result.to_json_dict()["uri"] == "rclone://[redacted]"


@pytest.mark.parametrize(
    "metadata",
    [
        b'{"bytes":true}',
        b"[]",
        b"null",
        b"true",
        b'"text"',
        b"[" * 30_000 + b"0" + b"]" * 30_000,
    ],
    ids=("boolean-size", "array", "null", "boolean", "string", "deeply-nested"),
)
def test_rclone_non_integer_or_non_object_size_metadata_is_rejected(metadata):
    executor = _FakeProcessExecutor([(0, (metadata,), b"")])

    result = RcloneResolver(executor=executor).resolve(
        _rclone_ref("rclone://private/x", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "rclone artifact resolution failed."
    assert len(executor.calls) == 1


def test_rclone_rejects_decoded_control_character_before_spawn():
    executor = _FakeProcessExecutor([])

    result = RcloneResolver(executor=executor).resolve(
        _rclone_ref("rclone://private/path%00secret", _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Reference URI is not an rclone locator."
    assert result.to_json_dict()["uri"] == "rclone://[redacted]"
    assert executor.calls == []


def test_rclone_process_failure_redacts_target_credentials_and_stderr():
    secret = "remote-token-and-target"
    executor = _FakeProcessExecutor([OSError(secret)])
    result = RcloneResolver(executor=executor).resolve(
        _rclone_ref(f"rclone://private/{secret}.bin", _sha256(b"x"))
    )

    serialized = str(result.to_json_dict())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "rclone artifact resolution failed."
    assert secret not in serialized


def test_rclone_deadline_is_not_reset_between_stat_and_cat():
    clock = FakeClock()

    def consume_budget(index, _deadline):
        clock.advance(0.75 if index == 0 else 0.5)

    executor = _FakeProcessExecutor(
        [
            (0, (b'{"bytes":1}',), b""),
            (0, (b"x",), b""),
        ],
        on_run=consume_budget,
    )
    result = RcloneResolver(
        executor=executor,
        deadline_seconds=1.0,
        clock=clock,
    ).resolve(_rclone_ref("rclone://private/x", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "rclone artifact resolution failed."
    assert len(executor.calls) == 2
    assert executor.calls[0]["deadline"] is executor.calls[1]["deadline"]


def test_rclone_rejects_ambiguous_process_seams():
    with pytest.raises(ValueError, match="either runner or executor"):
        RcloneResolver(
            runner=_fake_rclone_runner(size_bytes=1, body=b"x"),
            executor=_FakeProcessExecutor([]),
        )


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


def test_store_relative_reference_local_builds_native_file_uri(tmp_path):
    root = tmp_path / "data store"
    store = _data_store(StoreKind.LOCAL_FS, str(root))
    ref = store_relative_reference(store, path="exp/001/x.txt", content_hash=_sha256(b"x"))
    assert ref is not None
    assert ref.source_system == "local"
    assert ref.uri == (root / "exp" / "001" / "x.txt").as_uri()


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
        [_git_resolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)]
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


def test_check_store_health_git_healthy_via_runner(tmp_path):
    store = _data_store(StoreKind.GIT, "https://example.com/org/repo.git", name="repo")
    calls = []

    def runner(args):
        calls.append(args)
        if "--get-url" in args:
            return GitCompleted(0, f"{store.root}\n".encode(), b"")
        return GitCompleted(0, b"a" * 40 + b"\tHEAD\n", b"")

    health = check_store_health(
        store,
        git_runner=runner,
        git_remote_policy=_git_policy(store.root),
        git_health_cwd=tmp_path,
    )
    assert health.status is StoreHealthStatus.HEALTHY
    assert len(calls) == 2
    expected_config = [
        "-c",
        "http.followRedirects=false",
        "-c",
        f"http.{store.root}.followRedirects=false",
    ]
    assert all(call[:4] == expected_config for call in calls)
    assert calls[0][-4:] == ["ls-remote", "--get-url", "--", store.root]
    assert calls[1][-4:] == ["ls-remote", "--", store.root, "HEAD"]


def test_check_store_health_git_unreachable_via_runner(tmp_path):
    store = _data_store(StoreKind.GIT, "https://example.com/org/missing.git", name="repo")

    def runner(args):
        if "--get-url" in args:
            return GitCompleted(0, f"{store.root}\n".encode(), b"")
        return GitCompleted(128, b"", b"fatal: repository not found")

    health = check_store_health(
        store,
        git_runner=runner,
        git_remote_policy=_git_policy(store.root),
        git_health_cwd=tmp_path,
    )
    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "Git store health check failed."


def test_check_store_health_git_missing_binary(tmp_path):
    store = _data_store(StoreKind.GIT, "https://example.com/org/repo.git", name="repo")

    def runner(args):
        raise OSError("git not found")

    health = check_store_health(
        store,
        git_runner=runner,
        git_remote_policy=_git_policy(store.root),
        git_health_cwd=tmp_path,
    )
    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "Git store health check failed."


def test_check_store_health_git_denial_does_no_process_or_cwd_work(tmp_path):
    store = _data_store(StoreKind.GIT, _GIT_REMOTE, name="repo")
    calls = []

    def runner(args):
        calls.append(args)
        raise AssertionError("denied health check must not invoke Git")

    missing_cwd = tmp_path / "must-not-be-inspected"
    health = check_store_health(
        store,
        git_runner=runner,
        git_remote_policy=GitRemotePolicy.deny_all(),
        git_health_cwd=missing_cwd,
    )

    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "Git store health check failed."
    assert calls == []


def test_check_store_health_git_runner_exception_is_redacted(tmp_path):
    secret = "private-runner-diagnostic"
    store = _data_store(StoreKind.GIT, _GIT_REMOTE, name="repo")

    def runner(args):
        raise RuntimeError(secret)

    health = check_store_health(
        store,
        git_runner=runner,
        git_remote_policy=_git_policy(_GIT_REMOTE),
        git_health_cwd=tmp_path,
    )

    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "Git store health check failed."
    assert secret not in str(health.to_json_dict())


def test_check_store_health_git_preflight_mismatch_prevents_network_call(tmp_path):
    store = _data_store(StoreKind.GIT, _GIT_REMOTE, name="repo")
    calls = []

    def runner(args):
        calls.append(args)
        if "--get-url" in args:
            return GitCompleted(
                0,
                b"https://attacker.invalid/rewrite.git\n",
                b"private preflight diagnostic",
            )
        raise AssertionError("health probe must stop before network-capable ls-remote")

    health = check_store_health(
        store,
        git_runner=runner,
        git_remote_policy=_git_policy(_GIT_REMOTE),
        git_health_cwd=tmp_path,
    )

    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "Git store health check failed."
    assert len(calls) == 1
    assert "--get-url" in calls[0]


def test_check_store_health_git_does_not_inherit_parent_repository_config(
    tmp_path,
    monkeypatch,
):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    _isolate_real_git_config(monkeypatch, tmp_path)
    parent_repo = tmp_path / "parent-repository"
    health_cwd = parent_repo / "health-cwd"
    parent_repo.mkdir()
    health_cwd.mkdir()
    remote = "https://ceiling-regression.example/org/repo.git"
    rewritten = "https://attacker.invalid/rewritten.git"
    clean_env = dict(os.environ)
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        clean_env.pop(variable, None)
    subprocess.run(  # noqa: S603 - fixed executable and controlled test path
        [git, "init", "-q", os.fspath(parent_repo)],
        check=True,
        capture_output=True,
        env=clean_env,
    )
    subprocess.run(  # noqa: S603 - fixed executable and controlled test values
        [
            git,
            "-C",
            os.fspath(parent_repo),
            "config",
            f"url.{rewritten}.insteadOf",
            remote,
        ],
        check=True,
        capture_output=True,
        env=clean_env,
    )
    inherited = subprocess.run(  # noqa: S603 - no-network Git metadata query
        [
            git,
            "-C",
            os.fspath(health_cwd),
            "ls-remote",
            "--get-url",
            "--",
            remote,
        ],
        check=True,
        capture_output=True,
        env=clean_env,
    )
    assert inherited.stdout == f"{rewritten}\n".encode()

    class RealPreflightExecutor:
        def __init__(self):
            self.calls = []
            self.preflight_stdout = None

        def run(
            self,
            command,
            *,
            deadline,
            stdout_limit_bytes,
            stderr_limit_bytes,
            stdout_consumer=None,
            cwd=None,
            env=None,
        ):
            self.calls.append({"command": command, "cwd": cwd, "env": env})
            if "--get-url" in command:
                completed = subprocess.run(  # noqa: S603 - built Git argv
                    command,
                    check=False,
                    capture_output=True,
                    cwd=cwd,
                    env=env,
                )
                self.preflight_stdout = completed.stdout
                return SimpleNamespace(
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                )
            return SimpleNamespace(returncode=128, stdout=b"", stderr=b"intercepted")

    executor = RealPreflightExecutor()
    health = check_store_health(
        _data_store(StoreKind.GIT, remote, name="repo"),
        git_executor=executor,
        git_remote_policy=_git_policy(remote),
        git_health_cwd=health_cwd,
        git_binary=git,
    )

    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "Git store health check failed."
    assert executor.preflight_stdout == f"{remote}\n".encode()
    assert len(executor.calls) == 2
    assert all(call["cwd"] == os.path.realpath(health_cwd) for call in executor.calls)
    assert all(
        call["env"]["GIT_CEILING_DIRECTORIES"] == os.path.realpath(parent_repo)
        for call in executor.calls
    )


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


def test_recovery_prunes_symlinked_directories_outside_allowed_roots(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    data = b"matching bytes outside the recovery boundary"
    _write(outside, "moved.bin", data)
    link = root / "linked-private"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    missing = root / "gone.bin"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root], recovery=RecoveryPolicy(enabled=True)
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


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


def test_recovery_deduplicates_canonical_root_aliases(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    alias = tmp_path / "store-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    target_root = tmp_path / "second-store"
    target_root.mkdir()
    (root / "result.csv").write_bytes(b"same-name decoy")
    data = b"matching result in the later root"
    (target_root / "result.csv").write_bytes(data)
    missing = root / "missing" / "result.csv"
    resolver = LocalFilesystemResolver(
        allowed_roots=[alias, root, target_root],
        recovery=RecoveryPolicy(enabled=True, max_files=2),
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_recovery_never_descends_through_junction_escape(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    data = b"matching bytes outside the recovery boundary"
    outside_file = _write(outside, "moved.bin", data)
    junction = root / "mounted-private"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    missing = root / "gone.bin"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root], recovery=RecoveryPolicy(enabled=True)
    )

    candidates = list(resolver._iter_candidate_files(outside_file.name, True))
    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert candidates == []
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


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


def test_registry_from_env_honors_http_deadline_without_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
        "2.5",
    )

    registry = registry_from_env(
        http_policy=OutboundHttpPolicy(
            address_resolver=FakeAddressResolver(),
        )
    )

    http_resolver = next(
        resolver for resolver in registry._resolvers if isinstance(resolver, HttpResolver)
    )
    assert http_resolver._deadline_seconds == 2.5


@pytest.mark.parametrize("value", ("0", "-1", "nan", "inf", "not-a-number"))
def test_registry_from_env_rejects_invalid_http_deadline(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
        value,
    )

    with pytest.raises(
        ValueError,
        match="LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
    ):
        registry_from_env()


def test_registry_from_env_rejects_http_deadline_above_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
        "86400.0001",
    )

    with pytest.raises(
        ValueError,
        match="LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS",
    ):
        registry_from_env()


def test_registry_from_env_honors_subprocess_deadline_without_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
        "2.5",
    )

    registry = registry_from_env()

    process_resolvers = [
        resolver
        for resolver in registry._resolvers
        if isinstance(resolver, (RcloneResolver, GitResolver))
    ]
    assert len(process_resolvers) == 2
    assert all(resolver._deadline_seconds == 2.5 for resolver in process_resolvers)


@pytest.mark.parametrize("value", ("0", "-1", "nan", "inf", "not-a-number"))
def test_registry_from_env_rejects_invalid_subprocess_deadline(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
        value,
    )

    with pytest.raises(
        ValueError,
        match="LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
    ):
        registry_from_env()


def test_registry_from_env_rejects_subprocess_deadline_above_one_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
        "86400.0001",
    )

    with pytest.raises(
        ValueError,
        match="LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS",
    ):
        registry_from_env()


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
        command = next(
            candidate
            for candidate in ("init", "ls-remote", "fetch", "cat-file")
            if candidate in args
        )
        command_index = args.index(command)
        if command == "init":
            return GitCompleted(0, b"", b"")
        if command == "ls-remote":
            assert args[command_index + 1 : command_index + 3] == [
                "--get-url",
                "--",
            ]
            remote = args[command_index + 3]
            return GitCompleted(0, f"{remote}\n".encode(), b"")
        if command == "fetch":
            return GitCompleted(
                fetch_returncode,
                b"",
                b"" if fetch_returncode == 0 else b"remote error: not found",
            )
        if command == "cat-file":
            mode = args[command_index + 1] if len(args) > command_index + 1 else ""
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
    resolver = _git_resolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.size_bytes == len(data)
    assert result.observed_hash == _sha256(data)
    assert result.content_type == "text/x-python"


def test_git_resolver_reports_drift_on_mismatch(tmp_path):
    resolver = _git_resolver(runner=_fake_git_runner(blob=b"actual"), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(b"what the graph recorded")))

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(b"actual")
    assert result.content is None
    assert result.returned_bytes == 0
    assert result.detail is not None


def test_git_resolver_missing_object_is_unresolved(tmp_path):
    resolver = _git_resolver(
        runner=_fake_git_runner(blob=None, cat_returncode=128), cache_root=tmp_path
    )

    result = resolver.resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."


def test_git_resolver_fetch_failure_is_unresolved(tmp_path):
    resolver = _git_resolver(
        runner=_fake_git_runner(blob=None, fetch_returncode=128), cache_root=tmp_path
    )

    result = resolver.resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."


def test_git_resolver_unverifiable_hash_is_unresolved(tmp_path):
    resolver = _git_resolver(runner=_fake_git_runner(blob=b"data"), cache_root=tmp_path)

    result = resolver.resolve(_git_ref("datalad-key:MD5E-s4--abc"))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "datalad-key" in (result.detail or "")


def test_git_resolver_bad_locator_is_unresolved(tmp_path):
    resolver = _git_resolver(runner=_fake_git_runner(blob=b"data"), cache_root=tmp_path)
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

    result = _git_resolver(runner=runner, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."


def test_git_executor_streams_blob_under_one_deadline(tmp_path):
    data = b"streamed git blob"
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (f"{len(data)}\n".encode(),), b""),
            (0, (b"streamed ", b"git blob"), b""),
        ]
    )
    resolver = _git_resolver(
        executor=executor,
        cache_root=tmp_path,
        max_fetch_bytes=len(data),
    )

    result = resolver.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert executor.calls[-1]["streaming"] is True
    assert executor.calls[-1]["stdout_limit_bytes"] == len(data)
    assert len({id(call["deadline"]) for call in executor.calls}) == 1
    assert all(call["env"]["GIT_TERMINAL_PROMPT"] == "0" for call in executor.calls)
    assert all(
        call["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"
        for call in executor.calls
    )
    assert len({id(call["env"]) for call in executor.calls}) == 1
    assert len({call["cwd"] for call in executor.calls}) == 1
    operation_cwd = executor.calls[0]["cwd"]
    assert executor.calls[0]["env"]["GIT_CEILING_DIRECTORIES"] == os.path.dirname(
        os.path.realpath(operation_cwd)
    )
    config_prefix = [
        "git",
        "-c",
        "http.followRedirects=false",
        "-c",
        f"http.{_GIT_REMOTE}.followRedirects=false",
    ]
    assert all(
        call["command"][: len(config_prefix)] == config_prefix
        for call in executor.calls
    )
    preflight = next(
        call["command"]
        for call in executor.calls
        if "ls-remote" in call["command"]
    )
    assert preflight[-4:] == ["ls-remote", "--get-url", "--", _GIT_REMOTE]
    fetch = next(call["command"] for call in executor.calls if "fetch" in call["command"])
    assert fetch[-3:] == ["--", _GIT_REMOTE, "a" * 40]


def test_git_executor_uses_one_environment_snapshot_across_preflight(
    tmp_path,
    monkeypatch,
):
    data = b"stable environment"
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    def mutate_ambient_environment(index, _deadline):
        if index == 1:  # ls-remote --get-url preflight
            monkeypatch.setenv("GIT_TERMINAL_PROMPT", "ask")
            monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ext:file")

    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (f"{len(data)}\n".encode(),), b""),
            (0, (data,), b""),
        ],
        on_run=mutate_ambient_environment,
    )

    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(_sha256(data))
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert len({id(call["env"]) for call in executor.calls}) == 1
    assert all(
        call["env"]["GIT_TERMINAL_PROMPT"] == "0"
        for call in executor.calls
    )
    assert all(
        call["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"
        for call in executor.calls
    )


def test_git_actual_blob_growth_over_cap_discards_partial_result(tmp_path):
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (b"2\n",), b""),
            (0, (b"ab", b"secret-growth"), b""),
        ]
    )
    result = _git_resolver(
        executor=executor,
        cache_root=tmp_path,
        max_fetch_bytes=2,
    ).resolve(_git_ref(_sha256(b"ab"), path="private/result.bin"))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.observed_hash is None
    assert result.size_bytes is None
    assert result.detail == "Git artifact resolution failed."
    assert result.to_json_dict()["uri"] == "git+[redacted]"


def test_git_process_failure_redacts_remote_path_and_stderr(tmp_path):
    secret = "private-target-and-stderr"
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (128, (), f"fatal: {secret}".encode()),
        ]
    )
    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(
            _sha256(b"x"),
            path=f"private/{secret}.bin",
        )
    )

    serialized = str(result.to_json_dict())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert secret not in serialized
    assert "example.com" not in serialized


def test_git_runner_arbitrary_exception_is_redacted(tmp_path):
    secret = "runner-secret-that-must-not-escape"

    def runner(_args):
        raise KeyError(secret)

    result = _git_resolver(runner=runner, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"))
    )

    serialized = str(result.to_json_dict())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert secret not in serialized
    assert _GIT_REMOTE not in serialized


@pytest.mark.parametrize(
    "remote",
    [
        "https://user:embedded-token@example.com/private.git",
        "https://example.com/private.git?token=embedded-token",
    ],
)
def test_git_rejects_embedded_credentials_before_spawn(tmp_path, remote):
    secret = "embedded-token"
    executor = _FakeProcessExecutor([])
    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(
            _sha256(b"x"),
            remote=remote,
        )
    )

    serialized = str(result.to_json_dict())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Remote is not in the git resolver allowlist."
    assert executor.calls == []
    assert secret not in serialized


def test_git_allows_ssh_routing_username(tmp_path):
    data = b"ssh-routed blob"
    remote = "ssh://git@example.com/org/repo.git"
    result = _git_resolver(
        runner=_fake_git_runner(blob=data),
        cache_root=tmp_path,
        remote_policy=_git_policy(remote),
    ).resolve(
        _git_ref(
            _sha256(data),
            remote=remote,
        )
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


def test_git_deadline_is_not_reset_between_commands(tmp_path):
    clock = FakeClock()

    def consume_budget(_index, _deadline):
        clock.advance(0.3)

    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (b"1\n",), b""),
            (0, (b"x",), b""),
        ],
        on_run=consume_budget,
    )
    result = _git_resolver(
        executor=executor,
        cache_root=tmp_path,
        deadline_seconds=1.0,
        clock=clock,
    ).resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert len(executor.calls) == 4
    assert len({id(call["deadline"]) for call in executor.calls}) == 1


@pytest.mark.parametrize("size_output", [b"not-a-size\n", b"-1\n", b"\xff\n"])
def test_git_malformed_object_size_is_generic_unresolved(tmp_path, size_output):
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (size_output,), b"private diagnostic"),
        ]
    )
    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert result.observed_hash is None
    assert result.content is None


def test_git_excessive_integer_metadata_is_generic_unresolved(tmp_path):
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (b"9" * 5000,), b"private diagnostic"),
        ]
    )

    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert result.observed_hash is None
    assert result.content is None


def test_git_rejects_control_character_before_spawn(tmp_path):
    executor = _FakeProcessExecutor([])
    ref = ExternalArtifactReference(
        source_system="git",
        uri=f"git+https://example.com/repo.git#{'a' * 40}:private/\x00secret",
        content_hash=_sha256(b"x"),
    )

    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(ref)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Reference URI is not a git locator."
    assert result.to_json_dict()["uri"] == "git+[redacted]"
    assert executor.calls == []


def test_git_rejects_ambiguous_process_seams(tmp_path):
    with pytest.raises(ValueError, match="either runner or executor"):
        _git_resolver(
            runner=_fake_git_runner(blob=b"x"),
            executor=_FakeProcessExecutor([]),
            cache_root=tmp_path,
        )


def test_git_resolver_truncates_payload_but_verifies(tmp_path):
    data = b"y" * 100
    resolver = _git_resolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(data)), max_bytes=10)

    assert result.status is ResolutionStatus.VERIFIED  # full blob hashed
    assert result.truncated is True
    assert result.content == data[:10]
    assert result.size_bytes == len(data)


def test_git_resolver_returns_byte_range_slice(tmp_path):
    data = b"0123456789abcdef"
    resolver = _git_resolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(data)), byte_range=(4, 8))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data[4:8]


def test_git_resolver_can_resolve_by_source_system_and_scheme():
    resolver = _git_resolver(runner=_fake_git_runner(blob=b""))

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
            _git_resolver(runner=_fake_git_runner(blob=data), cache_root=tmp_path),
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
    resolver = _git_resolver(
        runner=runner,
        cache_root=tmp_path,
        remote_policy=_git_policy("https://allowed.example/"),
    )

    result = resolver.resolve(_git_ref(_sha256(b"data")))  # remote example.com

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "allowlist" in (result.detail or "")
    assert runner.calls == []  # refused before any git subprocess
    assert list(tmp_path.iterdir()) == []


def test_git_resolver_default_policy_denies_before_cache_or_executor(tmp_path):
    executor = _FakeProcessExecutor([])
    resolver = GitResolver(executor=executor, cache_root=tmp_path)

    result = resolver.resolve(_git_ref(_sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Remote is not in the git resolver allowlist."
    assert executor.calls == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "raw_remote",
    [f" {_GIT_REMOTE}", f"{_GIT_REMOTE} "],
    ids=["leading-space", "trailing-space"],
)
def test_git_resolver_does_not_strip_remote_before_authorization(
    tmp_path,
    raw_remote,
):
    executor = _FakeProcessExecutor([])
    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"), remote=raw_remote)
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Remote is not in the git resolver allowlist."
    assert executor.calls == []
    assert list(tmp_path.iterdir()) == []


def test_git_resolver_allows_remote_in_allowlist_by_prefix(tmp_path):
    data = b"allowed remote payload"
    resolver = _git_resolver(
        runner=_fake_git_runner(blob=data),
        cache_root=tmp_path,
        remote_policy=_git_policy("https://example.com/"),
    )

    result = resolver.resolve(_git_ref(_sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


def test_git_resolver_passes_only_policy_canonical_remote_to_git(tmp_path):
    data = b"canonicalized remote"
    raw_remote = "HTTPS://EXAMPLE.COM:443/org/repo.git"
    runner = _fake_git_runner(blob=data)

    result = _git_resolver(runner=runner, cache_root=tmp_path).resolve(
        _git_ref(_sha256(data), remote=raw_remote)
    )

    assert result.status is ResolutionStatus.VERIFIED
    assert all(raw_remote not in call for call in runner.calls)
    preflight = next(call for call in runner.calls if "ls-remote" in call)
    assert preflight[-4:] == ["ls-remote", "--get-url", "--", _GIT_REMOTE]
    fetch = next(call for call in runner.calls if "fetch" in call)
    assert fetch[-3:] == ["--", _GIT_REMOTE, "a" * 40]


@pytest.mark.parametrize(
    "preflight",
    [
        GitCompleted(0, b"https://attacker.invalid/rewrite.git\n", b""),
        GitCompleted(128, f"{_GIT_REMOTE}\n".encode(), b"private error"),
        GitCompleted(0, b"", b""),
        GitCompleted(
            0,
            f"{_GIT_REMOTE}\n{_GIT_REMOTE}\n".encode(),
            b"",
        ),
        GitCompleted(0, _GIT_REMOTE.encode(), b""),
        GitCompleted(0, b"\xff\n", b""),
        GitCompleted(0, b"x" * (64 * 1024 + 1), b""),
    ],
    ids=[
        "mismatch",
        "nonzero",
        "empty",
        "multiple-lines",
        "missing-newline",
        "invalid-utf8",
        "output-overflow",
    ],
)
def test_git_preflight_failure_prevents_fetch_and_is_redacted(
    tmp_path,
    preflight,
):
    calls = []

    def runner(args):
        calls.append(args)
        if "init" in args:
            return GitCompleted(0, b"", b"")
        if "--get-url" in args:
            return preflight
        raise AssertionError("preflight failure must prevent git fetch")

    result = _git_resolver(runner=runner, cache_root=tmp_path).resolve(
        _git_ref(_sha256(b"x"))
    )

    serialized = str(result.to_json_dict())
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert not any("fetch" in call for call in calls)
    assert "attacker.invalid" not in serialized
    assert "private error" not in serialized


def test_git_resolver_refuses_oversized_blob(tmp_path):
    resolver = _git_resolver(
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
    resolver = _git_resolver(
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


def test_default_registry_preserves_explicit_git_policy_identity():
    policy = _git_policy(_GIT_REMOTE)

    registry = default_registry(git_remote_policy=policy)

    resolver = next(
        candidate
        for candidate in registry._resolvers
        if isinstance(candidate, GitResolver)
    )
    assert resolver._remote_policy is policy


def test_registry_from_env_explicit_git_policy_overrides_environment(monkeypatch):
    policy = _git_policy(_GIT_REMOTE)
    monkeypatch.setenv(
        "LAB_TRACKER_GIT_ALLOWED_REMOTES",
        "this environment value is intentionally invalid",
    )

    registry = registry_from_env(
        git_remote_policy=policy,
        http_policy=OutboundHttpPolicy(),
    )

    resolver = next(
        candidate
        for candidate in registry._resolvers
        if isinstance(candidate, GitResolver)
    )
    assert resolver._remote_policy is policy


def _isolate_real_git_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    for variable in tuple(os.environ):
        if variable.startswith("GIT_CONFIG_"):
            monkeypatch.delenv(variable, raising=False)
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", os.fspath(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(xdg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def test_real_git_preflight_detects_inherited_instead_of_rewrite(
    tmp_path,
    monkeypatch,
):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    _isolate_real_git_config(monkeypatch, tmp_path)
    rewritten = "https://attacker.invalid/private.git"
    subprocess.run(  # noqa: S603 - fixed executable and controlled test values
        [
            git,
            "config",
            "--global",
            f"url.{rewritten}.insteadOf",
            _GIT_REMOTE,
        ],
        check=True,
        capture_output=True,
    )
    calls = []

    def runner(args):
        calls.append(args)
        completed = subprocess.run(  # noqa: S603 - fixed executable, built argv
            [git, *args],
            check=False,
            capture_output=True,
        )
        return GitCompleted(
            completed.returncode,
            completed.stdout,
            completed.stderr,
        )

    result = _git_resolver(runner=runner, cache_root=tmp_path / "cache").resolve(
        _git_ref(_sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Git artifact resolution failed."
    assert not any("fetch" in call for call in calls)
    preflight = next(call for call in calls if "--get-url" in call)
    assert preflight[-1] == _GIT_REMOTE


def test_real_git_cli_config_overrides_inherited_exact_redirect_setting(
    tmp_path,
    monkeypatch,
):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    _isolate_real_git_config(monkeypatch, tmp_path)
    exact_key = f"http.{_GIT_REMOTE}.followRedirects"
    subprocess.run(  # noqa: S603 - fixed executable and controlled test values
        [git, "config", "--global", exact_key, "true"],
        check=True,
        capture_output=True,
    )

    generic_only = subprocess.run(  # noqa: S603 - fixed executable and controlled argv
        [
            git,
            "-c",
            "http.followRedirects=false",
            "config",
            "--get-urlmatch",
            "http.followRedirects",
            _GIT_REMOTE,
        ],
        check=True,
        capture_output=True,
    )
    assert generic_only.stdout == b"true\n"

    completed = subprocess.run(  # noqa: S603 - fixed executable and controlled argv
        [
            git,
            "-c",
            "http.followRedirects=false",
            "-c",
            f"{exact_key}=false",
            "config",
            "--get-urlmatch",
            "http.followRedirects",
            _GIT_REMOTE,
        ],
        check=True,
        capture_output=True,
    )

    assert completed.stdout == b"false\n"


def test_git_resolver_rejects_option_like_components(tmp_path):
    runner = _fake_git_runner(blob=b"data")
    resolver = _git_resolver(runner=runner, cache_root=tmp_path)
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
