import hashlib
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from http_security_fakes import (
    FakeAddressResolver,
    FakeClock,
    FakeHttpResponse,
    FakeSafeHttpClient,
)

import lab_tracker.artifact_resolution as artifact_resolution
import lab_tracker.local_file_access as local_file_access
from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    ArtifactResolver,
    GitCompleted,
    GitResolver,
    GitStoreResolutionTarget,
    HttpResolver,
    HttpStoreResolutionTarget,
    LocalFilesystemResolver,
    LocalStoreResolutionTarget,
    RcloneCompleted,
    RcloneResolver,
    RcloneStoreResolutionTarget,
    RecoveryPolicy,
    ResolutionStatus,
    ResolvedArtifact,
    ResolverRegistry,
    ScopedGitStoreResolver,
    ScopedHttpStoreResolver,
    ScopedLocalStoreResolver,
    ScopedRcloneStoreResolver,
    StoreHealthStatus,
    check_store_health,
    default_registry,
    git_store_resolution_target,
    http_store_resolution_target,
    is_verifiable_hash,
    local_store_resolution_target,
    outbound_http_policy_from_env,
    parse_content_hash,
    rclone_store_resolution_target,
    registry_from_env,
    store_relative_reference,
)
from lab_tracker.git_remote_policy import (
    GitRemotePolicy,
    parse_git_remote_address,
)
from lab_tracker.git_store_locator import PinnedGitPath
from lab_tracker.local_file_access import (
    LocalOpenFailure,
    LocalOpenFailureReason,
    OpenedLocalFile,
)
from lab_tracker.local_path_policy import LocalPathPolicy
from lab_tracker.local_store_locator import LocalStoreLocator, PortableStorePath
from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind
from lab_tracker.outbound_http import (
    ApprovedSocketAddress,
    OutboundHttpPolicy,
    OutboundHttpTransportError,
    RegisteredHttpPrefix,
)
from lab_tracker.rclone_store_locator import RcloneRemoteName, RegisteredRcloneRoot


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
    assert "authorized" in (result.detail or "").lower()
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


def test_local_resolver_hashes_opened_stream_without_reopening_path(tmp_path):
    recorded = b"descriptor-owned recorded bytes"
    replacement = b"replacement pathname bytes"
    path = _write(tmp_path, "artifact.bin", recorded)

    class SwappingReader:
        @contextmanager
        def open_regular_file(self, requested_path):
            assert os.fspath(requested_path) == str(path)
            stream = BytesIO(recorded)
            path.write_bytes(replacement)
            try:
                yield OpenedLocalFile(
                    stream=stream,
                    display_path=str(path),
                    size_hint_bytes=len(recorded),
                )
            finally:
                stream.close()

    result = LocalFilesystemResolver(
        file_reader_factory=lambda _policy: SwappingReader()
    ).resolve(
        _local_ref(path, _sha256(recorded))
    )

    assert path.read_bytes() == replacement
    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == recorded


def test_local_resolver_operational_read_failure_is_static(tmp_path):
    path = tmp_path / "sensitive-name.bin"

    class FailingStream(BytesIO):
        def read(self, _size=-1):
            raise OSError(f"cannot read secret host path {path}")

    class FailingReader:
        @contextmanager
        def open_regular_file(self, _requested_path):
            stream = FailingStream()
            try:
                yield OpenedLocalFile(
                    stream=stream,
                    display_path=str(path),
                    size_hint_bytes=1,
                )
            finally:
                stream.close()

    result = LocalFilesystemResolver(
        file_reader_factory=lambda _policy: FailingReader()
    ).resolve(
        _local_ref(path, _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Failed to read local artifact."
    assert str(path) not in (result.detail or "")


@pytest.mark.parametrize(
    "reason",
    [LocalOpenFailureReason.DENIED, LocalOpenFailureReason.IO_ERROR],
)
def test_local_resolver_only_recovers_after_missing(reason, tmp_path, monkeypatch):
    path = tmp_path / "artifact.bin"

    class FailingReader:
        @contextmanager
        def open_regular_file(self, _requested_path):
            yield LocalOpenFailure(reason)

    resolver = LocalFilesystemResolver(
        allowed_roots=[tmp_path],
        recovery=RecoveryPolicy(enabled=True),
        file_reader_factory=lambda _policy: FailingReader(),
    )

    def unexpected_recovery(*_args, **_kwargs):
        raise AssertionError("non-missing failure started recovery")

    monkeypatch.setattr(resolver, "_recover_by_hash", unexpected_recovery)

    result = resolver.resolve(_local_ref(path, _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED


def test_local_resolver_denied_detail_covers_in_root_unsupported_target(tmp_path):
    path = tmp_path / "directory"
    path.mkdir()

    result = LocalFilesystemResolver(allowed_roots=[tmp_path]).resolve(
        _local_ref(path, _sha256(b"x"))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == (
        "Local artifact is not an authorized readable regular file."
    )


def test_local_resolver_policy_cannot_be_bypassed_by_reader_miscomposition(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = _write(tmp_path, "outside.bin", b"secret")

    resolver = LocalFilesystemResolver(
        allowed_roots=[allowed],
        file_reader_factory=lambda _policy: local_file_access.HandleBoundLocalFileAccess(
            LocalPathPolicy()
        ),
    )

    result = resolver.resolve(_local_ref(outside, _sha256(b"secret")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


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


def _registered_http_target(
    *,
    base: str = "https://store.example/base",
    locator: str = "nested/artifact.bin",
    content_hash: str | None = None,
    name: str = "web",
) -> HttpStoreResolutionTarget:
    store = DataStore(
        store_id=uuid4(),
        project_id=uuid4(),
        name=name,
        kind=StoreKind.HTTP,
        root=base,
    )
    target = store_relative_reference(
        store,
        path=locator,
        content_hash=content_hash or _sha256(b"artifact"),
    )
    assert isinstance(target, HttpStoreResolutionTarget)
    return target


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


def test_registered_http_store_resolves_beneath_prefix_with_logical_identity():
    body = b"registered HTTP artifact"
    resolver, client, dns = _safe_http_resolver(
        FakeHttpResponse(
            headers={"content-type": "application/octet-stream"},
            chunks=(body,),
        )
    )
    target = _registered_http_target(
        locator="nested/file name.bin",
        content_hash=_sha256(body),
    )

    result = ResolverRegistry([resolver]).resolve_http_store(target)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == body
    assert result.source_system == "store"
    assert result.uri == "store://web/nested/file%20name.bin"
    assert result.to_json_dict()["uri"] == result.uri
    assert dns.calls == [("store.example", 443)]
    assert client.calls[0][1].absolute_url == (
        "https://store.example/base/nested/file%20name.bin"
    )


def test_registered_http_store_allows_same_prefix_redirect_and_keeps_identity():
    body = b"moved within registered HTTP prefix"
    resolver, client, dns = _safe_http_resolver(
        FakeHttpResponse(
            status_code=302,
            headers={"location": "/base/archive/final.bin"},
        ),
        FakeHttpResponse(chunks=(body,)),
    )
    target = _registered_http_target(content_hash=_sha256(body))

    result = ResolverRegistry([resolver]).resolve_http_store(target)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.uri == "store://web/nested/artifact.bin"
    assert result.content == body
    assert dns.calls == [
        ("store.example", 443),
        ("store.example", 443),
    ]
    assert [call[1].absolute_url for call in client.calls] == [
        "https://store.example/base/nested/artifact.bin",
        "https://store.example/base/archive/final.bin",
    ]


@pytest.mark.parametrize(
    "location",
    (
        "/baseevil/secret.bin",
        "/base/../secret.bin",
        "/base/%2e%2e/secret.bin",
        "/base/..%EF%BC%8Fsecret.bin",
        "/base/%EF%BC%8E%EF%BC%8E/secret.bin",
        "/base/%EF%BC%852e%EF%BC%852e/secret.bin",
        "https://archive.example/base/secret.bin",
        "/base/nested\\..\\secret.bin",
        "/base/nested;parameter/secret.bin",
        "/base/nested%EF%BC%9Bparameter/secret.bin",
        "?token=secret",
        "#fragment",
    ),
)
def test_registered_http_store_rejects_redirect_escape_before_second_hop_io(
    location: str,
):
    resolver, client, dns = _safe_http_resolver(
        FakeHttpResponse(status_code=302, headers={"location": location})
    )
    target = _registered_http_target()

    result = ResolverRegistry([resolver]).resolve_http_store(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == "store://web/nested/artifact.bin"
    assert result.detail == "HTTP artifact fetch failed or was denied."
    assert dns.calls == [("store.example", 443)]
    assert len(client.calls) == 1


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


def test_rclone_resolver_uses_registered_target_without_generic_uri_reparse(
    monkeypatch,
):
    data = b"registered rclone object"
    runner = _fake_rclone_runner(size_bytes=len(data), body=data)
    resolver = RcloneResolver(
        runner=runner,
        allowed_remotes=["Lab Team@org"],
    )
    target = _rclone_store_target(
        root="/experiments",
        remote="Lab Team@org",
        locator_path="--results/x.bin",
        content_hash=_sha256(data),
    )

    def unexpected_generic_parse(_uri):
        raise AssertionError("registered target reached generic rclone URI parsing")

    monkeypatch.setattr(resolver, "_rclone_target", unexpected_generic_parse)

    result = resolver.resolve_within_rclone_store(target)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.uri == "store://lab/--results/x.bin"
    assert result.content == data
    assert runner.calls == [
        ["size", "--json", "Lab Team@org:/experiments/--results/x.bin"],
        ["cat", "Lab Team@org:/experiments/--results/x.bin"],
    ]


def test_rclone_registered_target_denied_by_exact_allowlist_before_process():
    runner = _fake_rclone_runner(size_bytes=1, body=b"x")
    target = _rclone_store_target(remote="lab+archive")

    result = RcloneResolver(
        runner=runner,
        allowed_remotes=["LAB+archive"],
    ).resolve_within_rclone_store(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Remote is not in the rclone resolver allowlist."
    assert result.uri == "store://lab/001/x.fcs"
    assert runner.calls == []


def test_rclone_registered_target_invalid_hash_does_no_process_work():
    runner = _fake_rclone_runner(size_bytes=1, body=b"x")
    target = _rclone_store_target(content_hash="datalad-key:opaque")

    result = RcloneResolver(runner=runner).resolve_within_rclone_store(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.detail == "Cannot verify content hash with algorithm 'datalad-key'."
    assert runner.calls == []


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


def test_external_artifact_reference_for_store_rejects_leading_slash():
    with pytest.raises(ValueError, match="Invalid local-store"):
        ExternalArtifactReference.for_local_store(
            store_name="lab-onedrive",
            locator="/exp/001/x.fcs",
            content_hash=_sha256(b"x"),
        )


def test_external_artifact_reference_for_store_sets_canonical_fields_and_uri():
    ref = ExternalArtifactReference.for_local_store(
        store_name="lab-onedrive",
        locator="exp/001/x.fcs",
        content_hash=_sha256(b"x"),
    )

    assert ref.store_name == "lab-onedrive"
    assert ref.locator == "exp/001/x.fcs"
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


def _rclone_store_target(
    *,
    root: str = "experiments",
    remote: str | None = "lab-onedrive",
    locator_path: str = "001/x.fcs",
    content_hash: str | None = None,
) -> RcloneStoreResolutionTarget:
    store = _data_store(
        StoreKind.ONEDRIVE,
        root,
        name="lab",
        credential_ref=remote,
    )
    target = store_relative_reference(
        store,
        path=locator_path,
        content_hash=content_hash or _sha256(b"x"),
    )
    assert isinstance(target, RcloneStoreResolutionTarget)
    return target


def _git_store_target(
    *,
    remote: str = _GIT_REMOTE,
    repository_path: str = "analysis/run.py",
    object_id: str = "a" * 40,
    content_hash: str | None = None,
    name: str = "repo",
) -> GitStoreResolutionTarget:
    store = _data_store(StoreKind.GIT, remote, name=name)
    target = store_relative_reference(
        store,
        path=f"{repository_path}@{object_id}",
        content_hash=content_hash or _sha256(b"artifact"),
    )
    assert isinstance(target, GitStoreResolutionTarget)
    return target


def _local_store_target(
    store: DataStore,
    locator_path: str,
    content_hash: str,
) -> LocalStoreResolutionTarget:
    locator = LocalStoreLocator.parse_decoded(locator_path)
    assert locator is not None
    logical_reference = ExternalArtifactReference.for_local_store(
        store_name=store.name,
        locator=locator.path,
        content_hash=content_hash,
    )
    target = local_store_resolution_target(
        store,
        locator=locator,
        logical_reference=logical_reference,
    )
    assert isinstance(target, LocalStoreResolutionTarget)
    return target


def test_store_relative_reference_local_builds_scoped_logical_target(tmp_path):
    root = tmp_path / "data store"
    store = _data_store(StoreKind.LOCAL_FS, str(root))
    target = store_relative_reference(
        store,
        path="exp/001/x.txt",
        content_hash=_sha256(b"x"),
    )

    assert isinstance(target, LocalStoreResolutionTarget)
    assert target.store_root == str(root)
    assert target.locator.components == ("exp", "001", "x.txt")
    assert target.logical_reference.source_system == "store"
    assert target.logical_reference.uri == "store://store/exp/001/x.txt"


def test_local_store_target_factory_rejects_mismatched_logical_identity(tmp_path):
    store = _data_store(StoreKind.LOCAL_FS, str(tmp_path), name="lab-fs")
    locator = LocalStoreLocator.parse_decoded("nested/artifact.bin")
    other_locator = LocalStoreLocator.parse_decoded("other/artifact.bin")
    assert locator is not None
    assert other_locator is not None
    reference = ExternalArtifactReference.for_local_store(
        store_name=store.name,
        locator=locator.path,
        content_hash=_sha256(b"x"),
    )
    wrong_store_reference = reference.model_copy(
        update={"store_name": "other", "uri": "store://other/nested/artifact.bin"}
    )
    wrong_uri_reference = reference.model_copy(
        update={"uri": "store://lab-fs/other/artifact.bin"}
    )

    assert (
        local_store_resolution_target(
            store,
            locator=locator,
            logical_reference=wrong_store_reference,
        )
        is None
    )
    assert (
        local_store_resolution_target(
            store,
            locator=other_locator,
            logical_reference=reference,
        )
        is None
    )
    assert (
        local_store_resolution_target(
            store,
            locator=locator,
            logical_reference=wrong_uri_reference,
        )
        is None
    )


def test_local_store_target_cannot_be_constructed_without_validated_factory(
    tmp_path,
):
    locator = LocalStoreLocator.parse_decoded("artifact.bin")
    assert locator is not None
    reference = ExternalArtifactReference.for_local_store(
        store_name="lab-fs",
        locator=locator.path,
        content_hash=_sha256(b"x"),
    )

    with pytest.raises(TypeError, match="validated factory"):
        LocalStoreResolutionTarget(
            logical_reference=reference,
            store_root=str(tmp_path),
            locator=locator,
            _factory_token=object(),
        )


@pytest.mark.parametrize(
    ("root_factory", "path"),
    (
        (lambda tmp_path: "relative/store", "exp/001/x.txt"),
        (lambda tmp_path: str(tmp_path / "store"), "../sibling-secret.txt"),
    ),
)
def test_invalid_local_store_materialization_does_no_host_path_io(
    tmp_path, monkeypatch, root_factory, path
):
    store = _data_store(StoreKind.LOCAL_FS, root_factory(tmp_path))

    def unexpected_host_path_operation(*_args, **_kwargs):
        raise AssertionError("invalid local materialization touched a host path")

    monkeypatch.setattr(
        artifact_resolution.Path,
        "as_uri",
        unexpected_host_path_operation,
    )
    monkeypatch.setattr(
        artifact_resolution.os.path,
        "realpath",
        unexpected_host_path_operation,
    )
    monkeypatch.setattr(
        artifact_resolution.os,
        "open",
        unexpected_host_path_operation,
    )

    assert (
        store_relative_reference(
            store,
            path=path,
            content_hash=_sha256(b"x"),
        )
        is None
    )


def test_broader_global_root_does_not_make_sibling_escape_a_store_locator(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    sibling = _write(tmp_path, "sibling-secret.txt", b"secret")
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    resolver = LocalFilesystemResolver(allowed_roots=[tmp_path])

    target = store_relative_reference(
        store,
        path="../sibling-secret.txt",
        content_hash=_sha256(b"secret"),
    )
    ordinary = resolver.resolve(_local_ref(sibling, _sha256(b"secret")))

    assert target is None
    assert ordinary.status is ResolutionStatus.VERIFIED


def test_store_relative_reference_rclone_uses_credential_ref_as_remote():
    store = _data_store(
        StoreKind.ONEDRIVE, "experiments", name="lab", credential_ref="lab-onedrive"
    )
    target = store_relative_reference(
        store,
        path="001/x.fcs",
        content_hash=_sha256(b"x"),
    )

    assert isinstance(target, RcloneStoreResolutionTarget)
    assert target.remote.value == "lab-onedrive"
    assert target.registered_root.rooted is False
    assert target.registered_root.components == ("experiments",)
    assert target.locator.components == ("001", "x.fcs")
    assert target.argv_target == "lab-onedrive:experiments/001/x.fcs"
    assert target.logical_reference.source_system == "store"
    assert target.logical_reference.uri == "store://lab/001/x.fcs"


@pytest.mark.parametrize(
    ("root", "expected"),
    (
        ("experiments", "lab-onedrive:experiments/001/x.fcs"),
        ("/experiments", "lab-onedrive:/experiments/001/x.fcs"),
        ("/", "lab-onedrive:/001/x.fcs"),
    ),
)
def test_rclone_store_target_preserves_registered_root_mode(root, expected):
    target = _rclone_store_target(root=root)

    assert target.argv_target == expected


def test_rclone_store_target_uses_name_only_when_credential_ref_is_absent():
    fallback = _rclone_store_target(remote=None)
    empty = _data_store(
        StoreKind.ONEDRIVE,
        "experiments",
        name="lab",
        credential_ref="",
    )

    assert fallback.remote.value == "lab"
    assert fallback.argv_target == "lab:experiments/001/x.fcs"
    assert (
        store_relative_reference(
            empty,
            path="001/x.fcs",
            content_hash=_sha256(b"x"),
        )
        is None
    )


def test_rclone_store_target_factory_rejects_mismatched_logical_identity():
    store = _data_store(
        StoreKind.ONEDRIVE,
        "experiments",
        name="lab",
        credential_ref="lab-onedrive",
    )
    locator = PortableStorePath.parse_decoded("001/x.fcs")
    assert locator is not None
    reference = ExternalArtifactReference(
        source_system="store",
        uri="store://other/001/x.fcs",
        content_hash=_sha256(b"x"),
        store_name="other",
        locator=locator.path,
    )

    assert (
        rclone_store_resolution_target(
            store,
            locator=locator,
            logical_reference=reference,
        )
        is None
    )


def test_rclone_store_target_cannot_bypass_validated_factory():
    locator = PortableStorePath.parse_decoded("001/x.fcs")
    remote = RcloneRemoteName.parse("lab-onedrive")
    root = RegisteredRcloneRoot.parse_decoded("experiments")
    assert locator is not None
    assert remote is not None
    assert root is not None
    reference = ExternalArtifactReference(
        source_system="store",
        uri="store://lab/001/x.fcs",
        content_hash=_sha256(b"x"),
        store_name="lab",
        locator=locator.path,
    )

    with pytest.raises(TypeError, match="validated factory"):
        RcloneStoreResolutionTarget(
            logical_reference=reference,
            remote=remote,
            registered_root=root,
            locator=locator,
            _factory_token=object(),
        )


def test_store_relative_reference_http_builds_scoped_logical_target():
    store = _data_store(StoreKind.HTTP, "https://files.example/base", name="web")
    target = store_relative_reference(
        store,
        path="nested/x.bin",
        content_hash=_sha256(b"x"),
    )

    assert isinstance(target, HttpStoreResolutionTarget)
    assert target.locator.components == ("nested", "x.bin")
    assert target.registered_prefix.canonical_url == "https://files.example/base/"
    assert target.logical_reference.source_system == "store"
    assert target.logical_reference.uri == "store://web/nested/x.bin"
    assert target.logical_reference.store_name == "web"
    assert target.logical_reference.locator == "nested/x.bin"


def test_http_store_target_factory_uses_endpoint_before_root():
    store = _data_store(
        StoreKind.HTTP,
        "https://root.example/ignored",
        endpoint="https://files.example/base",
        name="web",
    )
    target = store_relative_reference(
        store,
        path="x.bin",
        content_hash=_sha256(b"x"),
    )

    assert isinstance(target, HttpStoreResolutionTarget)
    assert target.registered_prefix.canonical_url == "https://files.example/base/"

    store.endpoint = "https://files.example/base?invalid=secret"
    assert (
        store_relative_reference(
            store,
            path="x.bin",
            content_hash=_sha256(b"x"),
        )
        is None
    )

    store.endpoint = ""
    assert (
        store_relative_reference(
            store,
            path="x.bin",
            content_hash=_sha256(b"x"),
        )
        is None
    )


def test_http_store_target_factory_rejects_mismatched_logical_identity():
    store = _data_store(
        StoreKind.HTTP,
        "https://files.example/base",
        name="web",
    )
    locator = PortableStorePath.parse_decoded("nested/x.bin")
    assert locator is not None
    reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator=locator.path,
        content_hash=_sha256(b"x"),
    )

    assert (
        http_store_resolution_target(
            store,
            locator=locator,
            logical_reference=reference.model_copy(
                update={
                    "store_name": "other",
                    "uri": "store://other/nested/x.bin",
                }
            ),
        )
        is None
    )
    assert (
        http_store_resolution_target(
            store,
            locator=PortableStorePath(("other.bin",)),
            logical_reference=reference,
        )
        is None
    )
    assert (
        http_store_resolution_target(
            store,
            locator=locator,
            logical_reference=reference.model_copy(
                update={"uri": "store://web/other.bin"}
            ),
        )
        is None
    )


def test_http_store_target_cannot_be_constructed_without_validated_factory():
    locator = PortableStorePath(("artifact.bin",))
    prefix = RegisteredHttpPrefix.parse("https://files.example/base")
    assert prefix is not None
    reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator=locator.path,
        content_hash=_sha256(b"x"),
    )

    with pytest.raises(TypeError, match="validated factory"):
        HttpStoreResolutionTarget(
            logical_reference=reference,
            registered_prefix=prefix,
            locator=locator,
            _factory_token=object(),
        )


@pytest.mark.parametrize(
    ("base", "locator"),
    (
        ("https://files.example/base?token=secret", "artifact.bin"),
        ("https://user:secret@files.example/base", "artifact.bin"),
        ("https://files.example/base", "../secret.bin"),
        ("https://files.example/base", "nested/%2e%2e/secret.bin"),
        ("https://files.example/base", "nested\\secret.bin"),
    ),
)
def test_invalid_http_store_materialization_returns_none_without_network_io(
    base: str,
    locator: str,
    monkeypatch,
):
    store = _data_store(StoreKind.HTTP, base, name="web")

    def unexpected_network_operation(*_args, **_kwargs):
        raise AssertionError("invalid HTTP store materialization performed network I/O")

    monkeypatch.setattr(
        artifact_resolution.OutboundHttpPolicy,
        "authorize",
        unexpected_network_operation,
    )

    assert (
        store_relative_reference(
            store,
            path=locator,
            content_hash=_sha256(b"x"),
        )
        is None
    )


def test_store_relative_reference_unsupported_kind_returns_none():
    store = _data_store(StoreKind.DATABASE, "postgresql://lims", name="lims")
    assert store_relative_reference(store, path="q", content_hash=_sha256(b"x")) is None


def test_store_relative_reference_git_builds_scoped_logical_target():
    store = _data_store(
        StoreKind.GIT, "https://example.com/org/repo.git", name="repo"
    )
    commit = "a" * 40
    target = store_relative_reference(
        store, path=f"analysis/run.py@{commit}", content_hash=_sha256(b"x")
    )

    assert isinstance(target, GitStoreResolutionTarget)
    assert target.remote.subprocess_value == _GIT_REMOTE
    assert target.pin.path.components == ("analysis", "run.py")
    assert target.pin.object_id.value == commit
    assert target.pin.object_id.object_format == "sha1"
    assert target.logical_reference.source_system == "store"
    assert target.logical_reference.store_name == "repo"
    assert target.logical_reference.locator == f"analysis/run.py@{commit}"
    assert target.logical_reference.uri == f"store://repo/analysis/run.py@{commit}"


def test_store_relative_reference_git_retains_internal_at_and_sha256_pin():
    object_id = "b" * 64

    target = store_relative_reference(
        _data_store(StoreKind.GIT, _GIT_REMOTE, name="repo"),
        path=f"analysis/model@v2.py@{object_id}",
        content_hash=_sha256(b"x"),
    )

    assert isinstance(target, GitStoreResolutionTarget)
    assert target.pin.path.path == "analysis/model@v2.py"
    assert target.pin.object_id.value == object_id
    assert target.pin.object_id.object_format == "sha256"
    assert target.logical_reference.uri == (
        f"store://repo/analysis/model@v2.py@{object_id}"
    )


def test_store_relative_reference_git_without_commit_returns_none():
    store = _data_store(
        StoreKind.GIT, "https://example.com/org/repo.git", name="repo"
    )
    assert (
        store_relative_reference(store, path="analysis/run.py", content_hash=_sha256(b"x"))
        is None
    )


@pytest.mark.parametrize(
    ("root", "locator"),
    (
        ("https://user:secret@example.com/repo.git", f"analysis/run.py@{'a' * 40}"),
        ("../local-repo.git", f"analysis/run.py@{'a' * 40}"),
        (_GIT_REMOTE, "analysis/run.py@HEAD"),
        (_GIT_REMOTE, f"analysis/run.py@{'A' * 40}"),
        (_GIT_REMOTE, f"analysis/run.py@{'0' * 40}"),
        (_GIT_REMOTE, "analysis/run.py@abcdef1"),
        (_GIT_REMOTE, f"/analysis/run.py@{'a' * 40}"),
        (_GIT_REMOTE, f"analysis/../secret.py@{'a' * 40}"),
        (_GIT_REMOTE, f"analysis/run:1.py@{'a' * 40}"),
        (_GIT_REMOTE, f"analysis\\run.py@{'a' * 40}"),
        (_GIT_REMOTE, f"analysis/%72un.py@{'a' * 40}"),
    ),
    ids=(
        "credentialed-remote",
        "local-remote",
        "symbolic-ref",
        "uppercase-object-id",
        "zero-object-id",
        "abbreviated-object-id",
        "absolute-path",
        "traversal",
        "windows-ads",
        "backslash",
        "encoded-path-alias",
    ),
)
def test_store_relative_reference_rejects_invalid_git_target_without_host_io(
    root,
    locator,
    monkeypatch,
):
    def unexpected_cache_creation(*_args, **_kwargs):
        raise AssertionError("invalid registered Git target touched the cache")

    monkeypatch.setattr(artifact_resolution.os, "makedirs", unexpected_cache_creation)

    target = store_relative_reference(
        _data_store(StoreKind.GIT, root, name="repo"),
        path=locator,
        content_hash=_sha256(b"x"),
    )

    assert target is None


def test_git_store_target_factory_rejects_mismatched_logical_identity():
    store = _data_store(StoreKind.GIT, _GIT_REMOTE, name="repo")
    pin = PinnedGitPath.parse_decoded(f"analysis/run.py@{'a' * 40}")
    other_pin = PinnedGitPath.parse_decoded(f"analysis/other.py@{'a' * 40}")
    assert pin is not None
    assert other_pin is not None
    reference = ExternalArtifactReference.for_git_store(
        store_name=store.name,
        repository_path=pin.path.path,
        object_id=pin.object_id.value,
        content_hash=_sha256(b"x"),
    )

    assert (
        git_store_resolution_target(
            store,
            pin=pin,
            logical_reference=reference.model_copy(
                update={
                    "store_name": "other",
                    "uri": f"store://other/{pin.uri_path}",
                }
            ),
        )
        is None
    )
    assert (
        git_store_resolution_target(
            store,
            pin=other_pin,
            logical_reference=reference,
        )
        is None
    )
    assert (
        git_store_resolution_target(
            store,
            pin=pin,
            logical_reference=reference.model_copy(
                update={"uri": f"store://repo/{other_pin.uri_path}"}
            ),
        )
        is None
    )
    assert (
        git_store_resolution_target(
            store,
            pin=pin,
            logical_reference=reference.model_copy(
                update={"locator": other_pin.locator}
            ),
        )
        is None
    )


def test_git_store_target_cannot_bypass_validated_factory():
    pin = PinnedGitPath.parse_decoded(f"analysis/run.py@{'a' * 40}")
    remote = parse_git_remote_address(_GIT_REMOTE)
    assert pin is not None
    assert remote is not None
    reference = ExternalArtifactReference.for_git_store(
        store_name="repo",
        repository_path=pin.path.path,
        object_id=pin.object_id.value,
        content_hash=_sha256(b"x"),
    )

    with pytest.raises(TypeError, match="validated factory"):
        GitStoreResolutionTarget(
            logical_reference=reference,
            remote=remote,
            pin=pin,
            _factory_token=object(),
        )


def test_store_relative_reference_git_resolves_end_to_end(tmp_path):
    # The typed target must reach GitResolver without becoming a generic git+ URI.
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

    result = registry.resolve_prepared(ref)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.uri == f"store://repo/src/model.py@{commit}"


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


@pytest.mark.parametrize("root", ("relative/store", "~/store", "C:store"))
def test_check_store_health_rejects_invalid_local_root_before_probe(
    root, monkeypatch
):
    store = _data_store(StoreKind.LOCAL_FS, root)

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("invalid local store root reached a filesystem probe")

    monkeypatch.setattr(artifact_resolution.os.path, "realpath", unexpected_probe)
    monkeypatch.setattr(artifact_resolution.os.path, "isdir", unexpected_probe)

    health = check_store_health(store)

    assert health == artifact_resolution.StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        "Local store root is invalid.",
    )


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


def test_legacy_check_store_health_http_fails_closed_without_client_seam():
    secret = "legacy-http-target-must-not-escape"
    store = _data_store(
        StoreKind.HTTP,
        f"https://user:{secret}@files.example/base",
        name="web",
    )

    health = check_store_health(store)

    assert health.status is StoreHealthStatus.UNREACHABLE
    assert health.detail == "HTTP store health check failed."
    assert secret not in str(health.to_json_dict())
    with pytest.raises(TypeError):
        check_store_health(store, http_client=object())  # type: ignore[call-arg]


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


class _RecordingPreparedResolver(
    ArtifactResolver,
    ScopedLocalStoreResolver,
    ScopedHttpStoreResolver,
    ScopedRcloneStoreResolver,
    ScopedGitStoreResolver,
):
    def __init__(self) -> None:
        self.can_resolve_references: list[ExternalArtifactReference] = []
        self.calls: list[
            tuple[
                str,
                object,
                int,
                tuple[int, int] | None,
            ]
        ] = []

    def can_resolve(self, ref: ExternalArtifactReference) -> bool:
        self.can_resolve_references.append(ref)
        return True

    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        self.calls.append(("ordinary", ref, max_bytes, byte_range))
        return self._result(ref)

    def resolve_within_root(
        self,
        target: LocalStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        self.calls.append(("local", target, max_bytes, byte_range))
        return self._result(target.logical_reference)

    def resolve_within_http_store(
        self,
        target: HttpStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        self.calls.append(("http", target, max_bytes, byte_range))
        return self._result(target.logical_reference)

    def resolve_within_rclone_store(
        self,
        target: RcloneStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        self.calls.append(("rclone", target, max_bytes, byte_range))
        return self._result(target.logical_reference)

    def resolve_within_git_store(
        self,
        target: GitStoreResolutionTarget,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        self.calls.append(("git", target, max_bytes, byte_range))
        return self._result(target.logical_reference)

    @staticmethod
    def _result(ref: ExternalArtifactReference) -> ResolvedArtifact:
        return ResolvedArtifact(
            status=ResolutionStatus.UNRESOLVED,
            source_system=ref.source_system,
            uri=ref.uri,
            expected_hash=ref.content_hash,
            fetched_at=datetime.now(timezone.utc),
            detail="recorded",
        )


def test_registry_returns_precomputed_prepared_result_without_resolver_work():
    resolver = _RecordingPreparedResolver()
    precomputed = ResolvedArtifact(
        status=ResolutionStatus.UNRESOLVED,
        source_system="store",
        uri="store://[redacted]",
        expected_hash=_sha256(b"artifact"),
        fetched_at=datetime.now(timezone.utc),
        detail="already resolved",
    )

    result = ResolverRegistry([resolver]).resolve_prepared(
        precomputed,
        max_bytes=17,
        byte_range=(2, 5),
    )

    assert result is precomputed
    assert resolver.can_resolve_references == []
    assert resolver.calls == []


def test_registry_dispatches_prepared_external_reference_to_ordinary_resolver():
    resolver = _RecordingPreparedResolver()
    reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"artifact"),
    )

    result = ResolverRegistry([resolver]).resolve_prepared(
        reference,
        max_bytes=17,
        byte_range=(2, 5),
    )

    assert result.uri == reference.uri
    assert resolver.can_resolve_references == [reference]
    assert resolver.calls == [("ordinary", reference, 17, (2, 5))]


def test_registry_dispatches_prepared_local_target_to_scoped_resolver(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    target = _local_store_target(
        _data_store(StoreKind.LOCAL_FS, str(store_root)),
        "artifact.bin",
        _sha256(b"artifact"),
    )
    resolver = _RecordingPreparedResolver()

    result = ResolverRegistry([resolver]).resolve_prepared(
        target,
        max_bytes=17,
        byte_range=(2, 5),
    )

    assert result.uri == target.logical_reference.uri
    assert resolver.can_resolve_references == []
    assert resolver.calls == [("local", target, 17, (2, 5))]


def test_registry_dispatches_prepared_http_target_to_scoped_resolver():
    target = _registered_http_target()
    resolver = _RecordingPreparedResolver()

    result = ResolverRegistry([resolver]).resolve_prepared(
        target,
        max_bytes=17,
        byte_range=(2, 5),
    )

    assert result.uri == target.logical_reference.uri
    assert resolver.can_resolve_references == []
    assert resolver.calls == [("http", target, 17, (2, 5))]


def test_registry_dispatches_prepared_rclone_target_to_scoped_resolver():
    target = _rclone_store_target()
    resolver = _RecordingPreparedResolver()

    result = ResolverRegistry([resolver]).resolve_prepared(
        target,
        max_bytes=17,
        byte_range=(2, 5),
    )

    assert result.uri == target.logical_reference.uri
    assert resolver.can_resolve_references == []
    assert resolver.calls == [("rclone", target, 17, (2, 5))]


def test_registry_dispatches_prepared_git_target_to_scoped_resolver():
    target = _git_store_target()
    resolver = _RecordingPreparedResolver()

    result = ResolverRegistry([resolver]).resolve_prepared(
        target,
        max_bytes=17,
        byte_range=(2, 5),
    )

    assert result.uri == target.logical_reference.uri
    assert resolver.can_resolve_references == []
    assert resolver.calls == [("git", target, 17, (2, 5))]


def test_registry_does_not_fall_back_to_generic_rclone_for_registered_target():
    target = _rclone_store_target()

    class OrdinaryOnlyResolver(ArtifactResolver):
        def can_resolve(self, _ref):
            raise AssertionError("registered target reached generic dispatch")

        def resolve(self, _ref, *, max_bytes=DEFAULT_MAX_BYTES, byte_range=None):
            raise AssertionError("registered target reached generic dispatch")

    result = ResolverRegistry([OrdinaryOnlyResolver()]).resolve_prepared(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == target.logical_reference.uri
    assert result.detail == "No scoped rclone-store resolver is registered."


def test_registry_does_not_fall_back_to_generic_git_for_registered_target():
    target = _git_store_target()

    class OrdinaryOnlyResolver(ArtifactResolver):
        def can_resolve(self, _ref):
            raise AssertionError("registered target reached generic dispatch")

        def resolve(self, _ref, *, max_bytes=DEFAULT_MAX_BYTES, byte_range=None):
            raise AssertionError("registered target reached generic dispatch")

    result = ResolverRegistry([OrdinaryOnlyResolver()]).resolve_prepared(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == target.logical_reference.uri
    assert result.detail == "No scoped Git-store resolver is registered."


def test_registry_rejects_unsupported_prepared_runtime_type_without_reflection():
    class SensitiveTarget:
        def __str__(self) -> str:
            raise AssertionError("unsupported target was stringified")

        def __repr__(self) -> str:
            raise AssertionError("unsupported target was represented")

    resolver = _RecordingPreparedResolver()

    result = ResolverRegistry([resolver]).resolve_prepared(
        SensitiveTarget(),  # type: ignore[arg-type]
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.source_system == "prepared"
    assert result.uri == "prepared://[redacted]"
    assert result.expected_hash == "unavailable"
    assert result.observed_hash is None
    assert result.content is None
    assert result.detail == "Prepared artifact resolution target is unsupported."
    assert resolver.can_resolve_references == []
    assert resolver.calls == []


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


def test_registry_never_falls_back_to_ordinary_resolve_for_local_store(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, "artifact.bin", _sha256(b"x"))

    class OrdinaryOnlyResolver(ArtifactResolver):
        def can_resolve(self, ref):
            return True

        def resolve(self, ref, *, max_bytes=DEFAULT_MAX_BYTES, byte_range=None):
            raise AssertionError("scoped dispatch fell back to ordinary resolution")

    result = ResolverRegistry([OrdinaryOnlyResolver()]).resolve_prepared(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == target.logical_reference.uri
    assert result.detail == "No scoped local-store resolver is registered."


def test_registry_never_falls_back_to_ordinary_resolve_for_http_store():
    target = _registered_http_target()

    class OrdinaryOnlyResolver(ArtifactResolver):
        def can_resolve(self, ref):
            return True

        def resolve(self, ref, *, max_bytes=DEFAULT_MAX_BYTES, byte_range=None):
            raise AssertionError("scoped dispatch fell back to ordinary resolution")

    result = ResolverRegistry([OrdinaryOnlyResolver()]).resolve_prepared(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == target.logical_reference.uri
    assert result.detail == "No scoped HTTP-store resolver is registered."


def test_local_store_scope_rejects_static_link_escape_but_allows_safe_link(
    tmp_path,
):
    store_root = tmp_path / "store"
    store_root.mkdir()
    in_store = _write(store_root, "in-store.bin", b"inside")
    outside = _write(tmp_path, "outside.bin", b"outside")
    safe_link = store_root / "safe-link.bin"
    escape_link = store_root / "escape-link.bin"
    try:
        safe_link.symlink_to(in_store)
        escape_link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    resolver = ResolverRegistry([LocalFilesystemResolver(allowed_roots=[tmp_path])])

    safe = resolver.resolve_local_store(
        _local_store_target(store, safe_link.name, _sha256(b"inside"))
    )
    escaped = resolver.resolve_local_store(
        _local_store_target(store, escape_link.name, _sha256(b"outside"))
    )

    assert safe.status is ResolutionStatus.VERIFIED
    assert safe.content == b"inside"
    assert safe.uri == f"store://store/{safe_link.name}"
    assert escaped.status is ResolutionStatus.UNRESOLVED
    assert escaped.content is None
    assert str(outside) not in (escaped.detail or "")


def test_local_store_recovery_never_searches_global_sibling(tmp_path):
    store_root = tmp_path / "store"
    sibling_root = tmp_path / "sibling"
    store_root.mkdir()
    sibling_root.mkdir()
    data = b"same content outside registered store"
    _write(sibling_root, "moved.bin", data)
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, "missing.bin", _sha256(data))
    registry = ResolverRegistry(
        [
            LocalFilesystemResolver(
                allowed_roots=[tmp_path],
                recovery=RecoveryPolicy(enabled=True),
            )
        ]
    )

    result = registry.resolve_local_store(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


def test_local_store_recovery_can_find_match_inside_same_store(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    data = b"same content moved within registered store"
    moved = _write(store_root, "moved.bin", data)
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, "missing.bin", _sha256(data))
    registry = ResolverRegistry(
        [
            LocalFilesystemResolver(
                allowed_roots=[tmp_path],
                recovery=RecoveryPolicy(enabled=True),
            )
        ]
    )

    result = registry.resolve_local_store(target)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.uri == "store://store/missing.bin"
    assert result.source_system == "store"
    assert result.detail == (
        "Recovered within registered local store "
        "(differs from reference locator)."
    )
    assert str(moved) not in (result.detail or "")


def test_scoped_reader_uses_exact_store_policy_and_opened_stream_identity(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    recorded = b"descriptor-owned store bytes"
    replacement = b"replacement pathname bytes"
    path = _write(store_root, "artifact.bin", recorded)
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, path.name, _sha256(recorded))
    seen_policies: list[LocalPathPolicy] = []

    class SwappingReader:
        @contextmanager
        def open_regular_file(self, requested_path):
            assert os.fspath(requested_path) == os.path.realpath(path)
            stream = BytesIO(recorded)
            path.write_bytes(replacement)
            try:
                yield OpenedLocalFile(
                    stream=stream,
                    display_path=str(path),
                    size_hint_bytes=len(recorded),
                )
            finally:
                stream.close()

    def reader_factory(policy):
        seen_policies.append(policy)
        return SwappingReader()

    registry = ResolverRegistry(
        [
            LocalFilesystemResolver(
                allowed_roots=[tmp_path],
                file_reader_factory=reader_factory,
            )
        ]
    )

    result = registry.resolve_local_store(target)

    assert len(seen_policies) == 2
    assert seen_policies[0].canonical_roots == (os.path.realpath(tmp_path),)
    assert seen_policies[1].canonical_roots == (os.path.realpath(store_root),)
    assert path.read_bytes() == replacement
    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == recorded
    assert result.source_system == "store"
    assert result.uri == "store://store/artifact.bin"
    assert result.detail is None


def test_scoped_root_second_canonicalization_failure_is_opaque_and_never_reads(
    tmp_path, monkeypatch
):
    store_root = tmp_path / "store"
    store_root.mkdir()
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, "artifact.bin", _sha256(b"x"))
    reader_policies: list[LocalPathPolicy] = []

    class UnexpectedReader:
        @contextmanager
        def open_regular_file(self, _requested_path):
            raise AssertionError("failed store scoping reached the byte reader")
            yield  # pragma: no cover

    def reader_factory(policy):
        reader_policies.append(policy)
        return UnexpectedReader()

    resolver = LocalFilesystemResolver(
        allowed_roots=[tmp_path],
        file_reader_factory=reader_factory,
    )
    original_realpath = artifact_resolution.os.path.realpath
    calls = 0

    def fail_second_store_realpath(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("store root changed")
        return original_realpath(path)

    monkeypatch.setattr(
        artifact_resolution.os.path,
        "realpath",
        fail_second_store_realpath,
    )

    result = resolver.resolve_within_root(target)

    assert calls == 2
    assert len(reader_policies) == 1
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == "store://store/artifact.bin"
    assert result.detail == "Local store artifact is not authorized."
    assert result.content is None


def test_scoped_root_retarget_between_canonicalizations_never_changes_store(
    tmp_path, monkeypatch
):
    store_root = tmp_path / "store"
    sibling_root = tmp_path / "sibling"
    store_root.mkdir()
    sibling_root.mkdir()
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, "artifact.bin", _sha256(b"sibling"))
    reader_policies: list[LocalPathPolicy] = []

    class UnexpectedReader:
        @contextmanager
        def open_regular_file(self, _requested_path):
            raise AssertionError("retargeted store root reached the byte reader")
            yield  # pragma: no cover

    def reader_factory(policy):
        reader_policies.append(policy)
        return UnexpectedReader()

    resolver = LocalFilesystemResolver(
        allowed_roots=[tmp_path],
        file_reader_factory=reader_factory,
    )
    original_realpath = artifact_resolution.os.path.realpath
    sibling_canonical = original_realpath(sibling_root)
    calls = 0

    def retarget_store_root(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            return sibling_canonical
        return original_realpath(path)

    monkeypatch.setattr(
        artifact_resolution.os.path,
        "realpath",
        retarget_store_root,
    )

    result = resolver.resolve_within_root(target)

    assert calls == 2
    assert len(reader_policies) == 1
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == "store://store/artifact.bin"
    assert result.detail == "Local store artifact is not authorized."
    assert result.observed_hash is None
    assert result.content is None


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


def test_recovery_starts_after_missing_reader_context_has_closed(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    data = b"non-reentrant reader recovery"
    candidate = root / "moved.bin"
    candidate.write_bytes(data)
    missing = root / "gone.bin"

    class NonReentrantReader:
        def __init__(self) -> None:
            self.active = False

        @contextmanager
        def open_regular_file(self, requested_path):
            assert self.active is False
            self.active = True
            requested = os.fspath(requested_path)
            stream = BytesIO(data)
            try:
                if requested == str(missing):
                    yield LocalOpenFailure(LocalOpenFailureReason.MISSING)
                else:
                    assert requested == str(candidate)
                    yield OpenedLocalFile(
                        stream=stream,
                        display_path=requested,
                        size_hint_bytes=len(data),
                    )
            finally:
                stream.close()
                self.active = False

    reader = NonReentrantReader()
    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True),
        file_reader_factory=lambda _policy: reader,
    )

    result = resolver.resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert reader.active is False


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
def test_windows_local_store_direct_safe_junction_verifies(tmp_path):
    store_root = tmp_path / "store"
    target_parent = store_root / "target-parent"
    target_parent.mkdir(parents=True)
    data = b"safe bytes through in-store junction"
    _write(target_parent, "artifact.bin", data)
    junction = store_root / "safe-junction"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target_parent)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(
        store,
        "safe-junction/artifact.bin",
        _sha256(data),
    )

    result = ResolverRegistry(
        [LocalFilesystemResolver(allowed_roots=[tmp_path])]
    ).resolve_local_store(target)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.uri == "store://store/safe-junction/artifact.bin"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_local_store_direct_parent_swap_never_hashes_global_sibling(
    tmp_path, monkeypatch
):
    store_root = tmp_path / "store"
    parent = store_root / "victim-parent"
    parent.mkdir(parents=True)
    inside = _write(parent, "artifact.bin", b"in-store decoy")
    moved_parent = store_root / "original-parent"
    sibling_parent = tmp_path / "global-sibling"
    sibling_parent.mkdir()
    sibling_data = b"matching bytes outside registered store"
    sibling = _write(sibling_parent, inside.name, sibling_data)
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(
        store,
        "victim-parent/artifact.bin",
        _sha256(sibling_data),
    )
    resolver = LocalFilesystemResolver(allowed_roots=[tmp_path])
    real_open = local_file_access._open_windows_descriptor
    swapped = False

    def swap_candidate_parent(planned_path):
        nonlocal swapped
        if not swapped and os.path.basename(planned_path) == inside.name:
            parent.rename(moved_parent)
            completed = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(parent),
                    str(sibling_parent),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            assert completed.returncode == 0, (
                f"mklink /J failed: stdout={completed.stdout!r} "
                f"stderr={completed.stderr!r}"
            )
            swapped = True
        return real_open(planned_path)

    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        swap_candidate_parent,
    )

    result = ResolverRegistry([resolver]).resolve_local_store(target)

    assert swapped is True
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.observed_hash is None
    assert result.detail == (
        "Local artifact is not an authorized readable regular file."
    )
    assert str(sibling) not in (result.detail or "")


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


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_recovery_parent_swap_never_hashes_outside_target(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    parent = root / "victim-parent"
    parent.mkdir(parents=True)
    candidate = parent / "moved.bin"
    candidate.write_bytes(b"scan-visible inside decoy")
    moved_parent = root / "original-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    outside = outside_parent / candidate.name
    outside_data = b"matching outside bytes"
    outside.write_bytes(outside_data)
    missing = root / "gone.bin"
    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True),
    )
    real_open = local_file_access._open_windows_descriptor
    swapped = False

    def swap_candidate_parent(planned_path):
        nonlocal swapped
        if not swapped and os.path.basename(planned_path) == candidate.name:
            parent.rename(moved_parent)
            completed = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(parent),
                    str(outside_parent),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            assert completed.returncode == 0, (
                f"mklink /J failed: stdout={completed.stdout!r} "
                f"stderr={completed.stderr!r}"
            )
            swapped = True
        return real_open(planned_path)

    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        swap_candidate_parent,
    )

    result = resolver.resolve(_local_ref(missing, _sha256(outside_data)))

    assert swapped is True
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_local_store_recovery_final_handle_uses_store_not_global_scope(
    tmp_path, monkeypatch
):
    store_root = tmp_path / "store"
    parent = store_root / "victim-parent"
    parent.mkdir(parents=True)
    candidate = parent / "moved.bin"
    candidate.write_bytes(b"scan-visible in-store decoy")
    moved_parent = store_root / "original-parent"
    sibling_parent = tmp_path / "global-sibling"
    sibling_parent.mkdir()
    sibling = sibling_parent / candidate.name
    sibling_data = b"matching bytes in global sibling"
    sibling.write_bytes(sibling_data)
    store = _data_store(StoreKind.LOCAL_FS, str(store_root))
    target = _local_store_target(store, "gone.bin", _sha256(sibling_data))
    resolver = LocalFilesystemResolver(
        allowed_roots=[tmp_path],
        recovery=RecoveryPolicy(enabled=True),
    )
    real_open = local_file_access._open_windows_descriptor
    swapped = False

    def swap_candidate_parent(planned_path):
        nonlocal swapped
        if not swapped and os.path.basename(planned_path) == candidate.name:
            parent.rename(moved_parent)
            completed = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(parent),
                    str(sibling_parent),
                ],
                capture_output=True,
                check=False,
                text=True,
            )
            assert completed.returncode == 0, (
                f"mklink /J failed: stdout={completed.stdout!r} "
                f"stderr={completed.stderr!r}"
            )
            swapped = True
        return real_open(planned_path)

    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        swap_candidate_parent,
    )

    result = ResolverRegistry([resolver]).resolve_local_store(target)

    assert swapped is True
    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert str(sibling) not in (result.detail or "")


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


def test_recovery_enforces_remaining_budget_when_open_file_grows(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    root.mkdir()
    candidate = root / "moved.bin"
    candidate.write_bytes(b"scan-visible placeholder")
    missing = root / "gone.bin"
    grown = b"g" * 64

    class GrowingReader:
        @contextmanager
        def open_regular_file(self, requested_path):
            requested = os.fspath(requested_path)
            if requested == str(missing):
                yield LocalOpenFailure(LocalOpenFailureReason.MISSING)
                return
            assert requested == str(candidate)
            stream = BytesIO(grown)
            try:
                yield OpenedLocalFile(
                    stream=stream,
                    display_path=str(candidate),
                    size_hint_bytes=1,
                )
            finally:
                stream.close()

    def unexpected_pathname_size(_path):
        raise AssertionError("recovery used a pathname size preflight")

    monkeypatch.setattr(
        "lab_tracker.artifact_resolution.os.path.getsize",
        unexpected_pathname_size,
    )
    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True, max_bytes=8),
        file_reader_factory=lambda _policy: GrowingReader(),
    )

    result = resolver.resolve(_local_ref(missing, _sha256(grown)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


def test_recovery_debits_partial_reads_that_end_in_io_failure(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    first = root / "first.bin"
    second = root / "second.bin"
    first.touch()
    second.touch()
    missing = root / "gone.bin"
    opened_candidates: list[str] = []

    class PartialFailureStream(BytesIO):
        def __init__(self) -> None:
            super().__init__(b"x" * 8)
            self._completed_read = False

        def read(self, size=-1):
            if self._completed_read:
                raise OSError("candidate failed after a partial read")
            self._completed_read = True
            return super().read(size)

    class PartialFailureReader:
        @contextmanager
        def open_regular_file(self, requested_path):
            requested = os.fspath(requested_path)
            if requested == str(missing):
                yield LocalOpenFailure(LocalOpenFailureReason.MISSING)
                return
            opened_candidates.append(requested)
            stream = PartialFailureStream()
            try:
                yield OpenedLocalFile(
                    stream=stream,
                    display_path=requested,
                    size_hint_bytes=0,
                )
            finally:
                stream.close()

    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True, max_files=2, max_bytes=8),
        file_reader_factory=lambda _policy: PartialFailureReader(),
    )

    result = resolver.resolve(_local_ref(missing, _sha256(b"not present")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert opened_candidates == [str(first)]


def test_recovery_hashes_opened_stream_without_reopening_candidate(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    recorded = b"recorded recovery bytes"
    replacement = b"replacement candidate bytes"
    candidate = root / "moved.bin"
    candidate.write_bytes(recorded)
    missing = root / "gone.bin"

    class SwappingRecoveryReader:
        @contextmanager
        def open_regular_file(self, requested_path):
            requested = os.fspath(requested_path)
            if requested == str(missing):
                yield LocalOpenFailure(LocalOpenFailureReason.MISSING)
                return
            assert requested == str(candidate)
            stream = BytesIO(recorded)
            candidate.write_bytes(replacement)
            try:
                yield OpenedLocalFile(
                    stream=stream,
                    display_path=str(candidate),
                    size_hint_bytes=len(recorded),
                )
            finally:
                stream.close()

    resolver = LocalFilesystemResolver(
        allowed_roots=[root],
        recovery=RecoveryPolicy(enabled=True),
        file_reader_factory=lambda _policy: SwappingRecoveryReader(),
    )

    result = resolver.resolve(_local_ref(missing, _sha256(recorded)))

    assert candidate.read_bytes() == replacement
    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == recorded


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

    http_client = FakeSafeHttpClient(())
    registry = registry_from_env(
        http_policy=OutboundHttpPolicy(
            address_resolver=FakeAddressResolver(),
        ),
        http_client=http_client,
    )

    http_resolver = next(
        resolver for resolver in registry._resolvers if isinstance(resolver, HttpResolver)
    )
    assert http_resolver._deadline_seconds == 2.5
    assert http_resolver._client is http_client


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


def test_scoped_git_resolver_uses_exact_object_format_and_separate_caches(
    tmp_path,
):
    sha1_data = b"sha1 registered artifact"
    sha256_data = b"sha256 registered artifact"
    sha1_id = "a" * 40
    sha256_id = "b" * 64
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (f"{len(sha1_data)}\n".encode(),), b""),
            (0, (sha1_data,), b""),
            (0, (), b""),
            (0, (), b""),
            (0, (f"{len(sha256_data)}\n".encode(),), b""),
            (0, (sha256_data,), b""),
        ]
    )
    resolver = _git_resolver(executor=executor, cache_root=tmp_path)
    sha1_target = _git_store_target(
        object_id=sha1_id,
        content_hash=_sha256(sha1_data),
    )
    sha256_target = _git_store_target(
        object_id=sha256_id,
        content_hash=_sha256(sha256_data),
    )

    sha1_result = resolver.resolve_within_git_store(
        sha1_target,
        byte_range=(2, 9),
    )
    sha256_result = resolver.resolve_within_git_store(sha256_target)

    assert sha1_result.status is ResolutionStatus.VERIFIED
    assert sha1_result.content == sha1_data[2:9]
    assert sha1_result.uri == sha1_target.logical_reference.uri
    assert sha256_result.status is ResolutionStatus.VERIFIED
    assert sha256_result.content == sha256_data
    assert sha256_result.uri == sha256_target.logical_reference.uri

    sha1_calls = executor.calls[:5]
    sha256_calls = executor.calls[5:]
    sha1_init = next(call for call in sha1_calls if "init" in call["command"])
    sha256_init = next(call for call in sha256_calls if "init" in call["command"])
    assert sha1_init["command"][-3:] == [
        "init",
        "-q",
        "--object-format=sha1",
    ]
    assert sha256_init["command"][-3:] == [
        "init",
        "-q",
        "--object-format=sha256",
    ]
    assert next(
        call["command"] for call in sha1_calls if "fetch" in call["command"]
    )[-3:] == ["--", _GIT_REMOTE, sha1_id]
    assert next(
        call["command"] for call in sha256_calls if "fetch" in call["command"]
    )[-3:] == ["--", _GIT_REMOTE, sha256_id]

    def expected_cache(object_format: str) -> str:
        identity = f"{object_format}\0{_GIT_REMOTE}"
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return os.fspath(tmp_path / f"{object_format}-{digest}")

    sha1_cwd = expected_cache("sha1")
    sha256_cwd = expected_cache("sha256")
    assert sha1_cwd != sha256_cwd
    assert {call["cwd"] for call in sha1_calls} == {sha1_cwd}
    assert {call["cwd"] for call in sha256_calls} == {sha256_cwd}
    assert Path(sha1_cwd).is_dir()
    assert Path(sha256_cwd).is_dir()
    assert len({id(call["deadline"]) for call in sha1_calls}) == 1
    assert len({id(call["deadline"]) for call in sha256_calls}) == 1
    assert sha1_calls[0]["deadline"] is not sha256_calls[0]["deadline"]


def test_scoped_git_resolver_reports_drift_with_logical_identity(tmp_path):
    target = _git_store_target(content_hash=_sha256(b"recorded"))
    resolver = _git_resolver(
        runner=_fake_git_runner(blob=b"actual"),
        cache_root=tmp_path,
    )

    result = resolver.resolve_within_git_store(target)

    assert result.status is ResolutionStatus.DRIFTED
    assert result.uri == target.logical_reference.uri
    assert result.observed_hash == _sha256(b"actual")
    assert result.content is None
    assert _GIT_REMOTE not in str(result.to_json_dict())


def test_scoped_git_resolver_uses_one_total_deadline(tmp_path):
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
    target = _git_store_target(content_hash=_sha256(b"x"))
    result = _git_resolver(
        executor=executor,
        cache_root=tmp_path,
        deadline_seconds=1.0,
        clock=clock,
    ).resolve_within_git_store(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == target.logical_reference.uri
    assert result.detail == "Git artifact resolution failed."
    assert len(executor.calls) == 4
    assert len({id(call["deadline"]) for call in executor.calls}) == 1


def test_scoped_git_resolver_never_parses_a_generic_git_locator(
    tmp_path,
    monkeypatch,
):
    data = b"typed target"
    target = _git_store_target(content_hash=_sha256(data))

    def unexpected_generic_parse(_uri):
        raise AssertionError("registered target reached generic Git URI parsing")

    monkeypatch.setattr(
        artifact_resolution,
        "_parse_git_locator",
        unexpected_generic_parse,
    )

    result = _git_resolver(
        runner=_fake_git_runner(blob=data),
        cache_root=tmp_path,
    ).resolve_within_git_store(target)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.uri == target.logical_reference.uri


def test_scoped_git_policy_denial_precedes_executor_and_cache_construction(
    tmp_path,
    monkeypatch,
):
    target = _git_store_target(content_hash=_sha256(b"x"))
    cache_root = tmp_path / "must-not-exist"

    def unexpected_executor():
        raise AssertionError("policy denial constructed a process executor")

    def unexpected_cache_creation(*_args, **_kwargs):
        raise AssertionError("policy denial touched the Git cache")

    monkeypatch.setattr(
        artifact_resolution,
        "BoundedSubprocessExecutor",
        unexpected_executor,
    )
    monkeypatch.setattr(artifact_resolution.os, "makedirs", unexpected_cache_creation)
    resolver = GitResolver(
        cache_root=cache_root,
        remote_policy=_git_policy("https://allowed.example/"),
    )

    result = resolver.resolve_within_git_store(target)

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.uri == target.logical_reference.uri
    assert result.detail == "Remote is not in the git resolver allowlist."
    assert not cache_root.exists()


def test_direct_generic_head_retains_legacy_init_and_cache_identity(tmp_path):
    data = b"legacy mutable direct reference"
    executor = _FakeProcessExecutor(
        [
            (0, (), b""),
            (0, (), b""),
            (0, (f"{len(data)}\n".encode(),), b""),
            (0, (data,), b""),
        ]
    )

    result = _git_resolver(executor=executor, cache_root=tmp_path).resolve(
        _git_ref(_sha256(data), commit="HEAD")
    )

    assert result.status is ResolutionStatus.VERIFIED
    init = next(call for call in executor.calls if "init" in call["command"])
    assert init["command"][-2:] == ["init", "-q"]
    assert not any(
        argument.startswith("--object-format=")
        for argument in init["command"]
    )
    fetch = next(call for call in executor.calls if "fetch" in call["command"])
    assert fetch["command"][-3:] == ["--", _GIT_REMOTE, "HEAD"]
    legacy_digest = hashlib.sha256(_GIT_REMOTE.encode()).hexdigest()[:16]
    legacy_cache = os.fspath(tmp_path / legacy_digest)
    assert {call["cwd"] for call in executor.calls} == {legacy_cache}
    assert Path(legacy_cache).is_dir()


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
