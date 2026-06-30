import hashlib
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV,
    LAB_TRACKER_RESOLVER_RECOVERY_ENV,
    ArtifactResolver,
    HttpResolver,
    LocalFilesystemResolver,
    RcloneCompleted,
    RcloneResolver,
    ResolutionStatus,
    ResolvedArtifact,
    ResolverRegistry,
    StoreHealthStatus,
    check_store_health,
    default_registry,
    is_verifiable_hash,
    parse_content_hash,
    registry_from_env,
    store_relative_reference,
)
from lab_tracker.models import DataStore, ExternalArtifactReference, StoreKind


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


def test_local_resolver_recovers_missing_file_by_hash_when_enabled(tmp_path):
    allowed = tmp_path / "store"
    recovered_dir = allowed / "moved"
    recovered_dir.mkdir(parents=True)
    data = b"survived under a new parent"
    recovered_path = _write(recovered_dir, "artifact.txt", data)
    missing = allowed / "original" / "artifact.txt"
    ref = _local_ref(missing, _sha256(data))

    result = LocalFilesystemResolver(
        allowed_roots=[allowed],
        recover_by_content_hash=True,
    ).resolve(ref)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data
    assert result.uri == ref.uri
    assert str(recovered_path) in (result.detail or "")
    assert "recovered" in (result.detail or "").lower()


def test_local_resolver_recovery_is_disabled_by_default(tmp_path):
    allowed = tmp_path / "store"
    recovered_dir = allowed / "moved"
    recovered_dir.mkdir(parents=True)
    data = b"present but recovery is off"
    _write(recovered_dir, "artifact.txt", data)
    missing = allowed / "original" / "artifact.txt"

    result = LocalFilesystemResolver(allowed_roots=[allowed]).resolve(
        _local_ref(missing, _sha256(data))
    )

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.detail == "Local artifact not found."


def test_local_resolver_recovery_budget_exhaustion_is_unresolved(tmp_path):
    allowed = tmp_path / "store"
    allowed.mkdir()
    data = b"matching bytes beyond the file budget"
    _write(allowed, "a.bin", b"first non-match")
    _write(allowed, "z.bin", data)
    missing = allowed / "missing.bin"

    result = LocalFilesystemResolver(
        allowed_roots=[allowed],
        recover_by_content_hash=True,
        recovery_max_files=1,
    ).resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None
    assert result.detail == "Local artifact not found."


def test_local_resolver_recovery_ignores_matches_outside_allowed_roots(tmp_path):
    allowed = tmp_path / "store"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    data = b"outside the configured root"
    _write(outside, "artifact.txt", data)
    missing = allowed / "artifact.txt"

    result = LocalFilesystemResolver(
        allowed_roots=[allowed],
        recover_by_content_hash=True,
    ).resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


def test_local_resolver_recovery_rejects_same_name_wrong_bytes(tmp_path):
    allowed = tmp_path / "store"
    recovered_dir = allowed / "moved"
    recovered_dir.mkdir(parents=True)
    _write(recovered_dir, "artifact.txt", b"same name, wrong bytes")
    missing = allowed / "original" / "artifact.txt"

    result = LocalFilesystemResolver(
        allowed_roots=[allowed],
        recover_by_content_hash=True,
    ).resolve(_local_ref(missing, _sha256(b"expected bytes")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert result.content is None


def test_local_resolver_recovery_prioritizes_same_basename(tmp_path):
    allowed = tmp_path / "store"
    recovered_dir = allowed / "moved"
    recovered_dir.mkdir(parents=True)
    data = b"same basename wins within a one-file budget"
    _write(allowed, "a.bin", b"would exhaust budget if hashed first")
    _write(recovered_dir, "target.bin", data)
    missing = allowed / "original" / "target.bin"

    result = LocalFilesystemResolver(
        allowed_roots=[allowed],
        recover_by_content_hash=True,
        recovery_max_files=1,
    ).resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


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


def _http_ref(
    url: str, content_hash: str, *, source_system: str = "http"
) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system=source_system, uri=url, content_hash=content_hash
    )


def _mock_http_client(
    *, body: bytes = b"", status: int = 200, content_type: str = "application/octet-stream"
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_resolver_verifies_matching_body():
    body = b"flow cytometry export"
    client = _mock_http_client(body=body, content_type="text/csv; charset=utf-8")
    resolver = HttpResolver(client=client)

    result = resolver.resolve(_http_ref("https://store.example/sample.csv", _sha256(body)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == body
    assert result.size_bytes == len(body)
    assert result.content_type == "text/csv"


def test_http_resolver_reports_drift_on_mismatch():
    resolver = HttpResolver(client=_mock_http_client(body=b"actual bytes"))

    result = resolver.resolve(_http_ref("https://store.example/x", _sha256(b"recorded bytes")))

    assert result.status is ResolutionStatus.DRIFTED
    assert result.observed_hash == _sha256(b"actual bytes")


def test_http_resolver_http_error_status_is_unresolved():
    resolver = HttpResolver(client=_mock_http_client(body=b"", status=404))

    result = resolver.resolve(_http_ref("https://store.example/missing", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "404" in (result.detail or "")


def test_http_resolver_refuses_oversized_fetch():
    body = b"0123456789" * 10  # 100 bytes
    resolver = HttpResolver(client=_mock_http_client(body=body), max_fetch_bytes=16)

    result = resolver.resolve(_http_ref("https://store.example/big.bin", _sha256(body)))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "limit" in (result.detail or "").lower()
    assert result.content is None


def test_http_resolver_truncates_payload_but_verifies():
    body = b"abcdefghij"
    resolver = HttpResolver(client=_mock_http_client(body=body))

    result = resolver.resolve(_http_ref("https://store.example/x", _sha256(body)), max_bytes=4)

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == b"abcd"
    assert result.truncated is True
    assert result.size_bytes == 10


def test_http_resolver_connection_error_is_unresolved():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resolver = HttpResolver(client=client)

    result = resolver.resolve(_http_ref("https://store.example/x", _sha256(b"x")))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert "fetch" in (result.detail or "").lower()


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


def test_default_registry_dispatches_http(monkeypatch):
    body = b"served over http"
    # default_registry's HttpResolver creates its own client; patch it to a mock.
    import lab_tracker.artifact_resolution as ar

    registry = ar.ResolverRegistry(
        [ar.HttpResolver(client=_mock_http_client(body=body))]
    )
    result = registry.resolve(_http_ref("https://store.example/x", _sha256(body)))
    assert result.status is ResolutionStatus.VERIFIED


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


def test_store_relative_reference_local_builds_file_uri(tmp_path):
    store = _data_store(StoreKind.LOCAL_FS, str(tmp_path / "store"))
    ref = store_relative_reference(store, path="exp/001/x.txt", content_hash=_sha256(b"x"))
    assert ref is not None
    assert ref.source_system == "local"
    assert ref.uri == (tmp_path / "store" / "exp" / "001" / "x.txt").as_uri()


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


def test_registry_from_env_enables_local_recovery(monkeypatch, tmp_path):
    allowed = tmp_path / "store"
    recovered_dir = allowed / "moved"
    recovered_dir.mkdir(parents=True)
    data = b"env-enabled recovery"
    _write(recovered_dir, "artifact.txt", data)
    missing = allowed / "original" / "artifact.txt"
    monkeypatch.setenv(LAB_TRACKER_RESOLVER_ALLOWED_ROOTS_ENV, str(allowed))
    monkeypatch.setenv(LAB_TRACKER_RESOLVER_RECOVERY_ENV, "true")

    result = registry_from_env().resolve(_local_ref(missing, _sha256(data)))

    assert result.status is ResolutionStatus.VERIFIED
    assert result.content == data


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
    assert payload["observed_hash"] == _sha256(data)
