import hashlib
from pathlib import Path

import pytest

from lab_tracker.artifact_resolution import (
    DEFAULT_MAX_BYTES,
    ArtifactResolver,
    LocalFilesystemResolver,
    ResolutionStatus,
    ResolvedArtifact,
    ResolverRegistry,
    default_registry,
    is_verifiable_hash,
    parse_content_hash,
)
from lab_tracker.models import ExternalArtifactReference


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
    assert payload["observed_hash"] == _sha256(data)
