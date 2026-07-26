from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from lab_tracker.application.context_queries import ContextQueries
from lab_tracker.artifact_resolution import (
    GitStoreResolutionTarget,
    HttpStoreResolutionTarget,
    LocalStoreResolutionTarget,
    PreparedArtifactResolutionTarget,
    RcloneStoreResolutionTarget,
    ResolutionStatus,
    ResolvedArtifact,
)
from lab_tracker.artifact_resolution_limits import (
    DEFAULT_MAX_BYTES,
    MAX_ARTIFACT_BYTE_OFFSET,
    MAX_INLINE_ARTIFACT_BYTES,
)
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    DataStore,
    ExternalArtifactReference,
    StoreKind,
)


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


class _ContextApi:
    def __init__(self, *, project_id: UUID, reference: ExternalArtifactReference) -> None:
        self.project_id = project_id
        self.reference = reference
        self.calls: list[str] = []
        self.raise_on_read = False

    def get_dataset_for_read(self, _dataset_id: UUID, *, actor: AuthContext):
        if self.raise_on_read:
            raise AssertionError("resolve must not read through the API")
        self.calls.append(f"authorized:{actor.user_id}")
        return SimpleNamespace(
            project_id=self.project_id,
            commit_manifest=SimpleNamespace(external_artifacts=[self.reference]),
        )


class _DataStoreLookup:
    def __init__(self, store: DataStore | None = None) -> None:
        self.store = store
        self.calls: list[str] = []
        self.names: list[str] = []

    def get_by_name(self, _project_id: UUID, name: str):
        self.calls.append("store")
        self.names.append(name)
        return self.store


class _ExactDataStoreLookup:
    def __init__(self, *stores: DataStore) -> None:
        self.stores = {store.name: store for store in stores}
        self.calls: list[str] = []
        self.names: list[str] = []

    def get_by_name(self, _project_id: UUID, name: str):
        self.calls.append("store")
        self.names.append(name)
        return self.stores.get(name)


class _ContextRepository:
    def __init__(self, lookup: _DataStoreLookup) -> None:
        self.data_stores = lookup


class _ResolverRegistry:
    def __init__(
        self,
        calls: list[str],
        *,
        error: BaseException | None = None,
    ) -> None:
        self.calls = calls
        self.error = error
        self.prepared_targets: list[PreparedArtifactResolutionTarget] = []
        self.parameters: list[tuple[int, tuple[int, int] | None]] = []
        self.precomputed_targets: list[ResolvedArtifact] = []
        self.references: list[ExternalArtifactReference] = []
        self.local_targets: list[LocalStoreResolutionTarget] = []
        self.http_targets: list[HttpStoreResolutionTarget] = []
        self.rclone_targets: list[RcloneStoreResolutionTarget] = []
        self.git_targets: list[GitStoreResolutionTarget] = []

    def resolve_prepared(
        self,
        target: PreparedArtifactResolutionTarget,
        *,
        max_bytes: int,
        byte_range: tuple[int, int] | None,
    ) -> ResolvedArtifact:
        self.calls.append("resolve-prepared")
        self.prepared_targets.append(target)
        self.parameters.append((max_bytes, byte_range))
        if isinstance(target, ResolvedArtifact):
            self.precomputed_targets.append(target)
            return target
        if isinstance(target, ExternalArtifactReference):
            self.references.append(target)
            reference = target
        elif isinstance(target, LocalStoreResolutionTarget):
            self.local_targets.append(target)
            reference = target.logical_reference
        elif isinstance(target, HttpStoreResolutionTarget):
            self.http_targets.append(target)
            reference = target.logical_reference
        elif isinstance(target, RcloneStoreResolutionTarget):
            self.rclone_targets.append(target)
            reference = target.logical_reference
        elif isinstance(target, GitStoreResolutionTarget):
            self.git_targets.append(target)
            reference = target.logical_reference
        else:
            raise AssertionError("unsupported prepared target reached test registry")
        if self.error is not None:
            raise self.error
        full_content = b"ok"
        if byte_range is None:
            content = full_content[:max_bytes]
        else:
            start, end = byte_range
            content = full_content[start : min(end, start + max_bytes)]
        return ResolvedArtifact(
            status=ResolutionStatus.VERIFIED,
            source_system=reference.source_system,
            uri=reference.uri,
            expected_hash=reference.content_hash,
            observed_hash=reference.content_hash,
            size_bytes=len(full_content),
            content=content,
            truncated=len(content) < len(full_content),
            fetched_at=datetime.now(timezone.utc),
        )


def test_prepared_resolution_releases_before_resolving_detached_reference():
    project_id = uuid4()
    dataset_id = uuid4()
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"ok"),
        metadata={"nested": {"value": "prepared"}},
    )
    api = _ContextApi(project_id=project_id, reference=source_reference)
    lookup = _DataStoreLookup()
    repository = _ContextRepository(lookup)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=repository,
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=dataset_id,
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=1,
        byte_end=3,
    )
    source_reference.metadata["nested"]["value"] = "mutated"
    api.raise_on_read = True

    result = queries.resolve_prepared_external_artifact(prepared)

    assert api.calls == ["authorized:reader"]
    assert lookup.calls == []
    assert calls == ["release", "resolve-prepared"]
    assert registry.prepared_targets == [prepared.target]
    assert registry.parameters == [(64, (1, 3))]
    assert registry.references[0].metadata == {"nested": {"value": "prepared"}}
    assert result["entity_type"] == "dataset"
    assert result["entity_id"] == str(dataset_id)
    assert result["artifact_index"] == 0
    assert result["content_base64"] == "aw=="


def test_preparation_rejects_reversed_byte_range_before_entity_or_resolver_work():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    with pytest.raises(
        ValidationError,
        match="byte_end must be greater than or equal to byte_start",
    ):
        queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=64,
            byte_start=5,
            byte_end=2,
        )

    assert api.calls == []
    assert calls == []
    assert registry.references == []


@pytest.mark.parametrize(
    ("max_bytes", "byte_start", "byte_end"),
    [
        (True, None, None),
        (False, None, None),
        (1.0, None, None),
        ("1", None, None),
        (0, None, None),
        (-1, None, None),
        (MAX_INLINE_ARTIFACT_BYTES + 1, None, None),
        (64, True, 1),
        (64, 1.0, 2),
        (64, "1", 2),
        (64, -1, 2),
        (64, MAX_ARTIFACT_BYTE_OFFSET + 1, MAX_ARTIFACT_BYTE_OFFSET + 1),
        (64, 0, MAX_ARTIFACT_BYTE_OFFSET + 1),
        (64, 1, None),
        (64, None, 1),
        (64, 2, 1),
    ],
)
def test_preparation_rejects_invalid_content_bounds_before_entity_or_resolver_work(
    max_bytes,
    byte_start,
    byte_end,
):
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    lookup = _DataStoreLookup()
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    with pytest.raises(ValidationError):
        queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=max_bytes,
            byte_start=byte_start,
            byte_end=byte_end,
        )

    assert api.calls == []
    assert lookup.calls == []
    assert calls == []
    assert registry.references == []


def test_resolution_rejects_a_mutated_missing_limit_before_release_or_dispatch():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"ok"),
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=uuid4(), reference=source_reference),
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=None,
        byte_end=None,
    )
    object.__setattr__(prepared, "max_bytes", None)

    with pytest.raises(ValidationError, match="max_bytes must be an integer"):
        queries.resolve_prepared_external_artifact(prepared)

    assert calls == []
    assert registry.prepared_targets == []


def test_prepared_resolution_releases_before_returning_materialized_unresolved_result():
    project_id = uuid4()
    source_reference = ExternalArtifactReference.for_store(
        store_name="missing",
        locator="artifact.txt",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=project_id, reference=source_reference)
    lookup = _DataStoreLookup()
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    assert prepared.max_bytes == DEFAULT_MAX_BYTES
    api.raise_on_read = True

    result = queries.resolve_prepared_external_artifact(prepared)

    assert lookup.calls == ["store"]
    assert calls == ["release", "resolve-prepared"]
    assert len(registry.precomputed_targets) == 1
    assert registry.precomputed_targets[0] is prepared.target
    assert registry.references == []
    assert registry.local_targets == []
    assert registry.http_targets == []
    assert result["status"] == "unresolved"
    assert result["content_base64"] is None


def test_prepared_resolution_rejects_oversized_custom_resolver_output_before_base64():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://secret-locator",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    calls: list[str] = []

    class OversizedRegistry(_ResolverRegistry):
        def resolve_prepared(self, *args, **kwargs):
            result = super().resolve_prepared(*args, **kwargs)
            object.__setattr__(result, "content", b"ok")
            return result

    registry = OversizedRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=1,
        byte_start=None,
        byte_end=None,
    )

    result = queries.resolve_prepared_external_artifact(prepared)

    assert result["status"] == "unresolved"
    assert result["source_system"] == "artifact"
    assert result["uri"] == "artifact://[redacted]"
    assert result["expected_hash"] == "unavailable"
    assert result["observed_hash"] is None
    assert result["content_base64"] is None
    assert result["returned_bytes"] == 0
    assert result["detail"] == "Artifact resolver result could not be returned safely."
    assert "secret-locator" not in str(result)


def test_prepared_resolution_rejects_under_cap_substitution_from_injected_registry():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://logical-reference",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    calls: list[str] = []

    class SubstitutingRegistry:
        def resolve_prepared(self, target, **_kwargs):
            calls.append("resolve-prepared")
            substituted = b"x"
            substituted_hash = _sha256(substituted)
            object.__setattr__(target, "source_system", "secret-source")
            object.__setattr__(target, "uri", "secret://different-artifact")
            object.__setattr__(target, "content_hash", substituted_hash)
            return ResolvedArtifact(
                status=ResolutionStatus.VERIFIED,
                source_system=target.source_system,
                uri=target.uri,
                expected_hash=substituted_hash,
                observed_hash=substituted_hash,
                size_bytes=len(substituted),
                content=substituted,
                fetched_at=datetime.now(timezone.utc),
            )

    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=SubstitutingRegistry(),
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=None,
        byte_end=None,
    )

    result = queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release", "resolve-prepared"]
    assert result["status"] == "unresolved"
    assert result["source_system"] == "artifact"
    assert result["uri"] == "artifact://[redacted]"
    assert result["content_base64"] is None
    assert "secret" not in str(result)


def test_prepared_resolution_sanitizes_unsupported_custom_resolver_output():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://logical-reference",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    calls: list[str] = []

    class UnsupportedRegistry:
        def resolve_prepared(self, *_args, **_kwargs):
            calls.append("resolve-prepared")
            return {"uri": "test://secret-resolver-output", "content": b"secret"}

    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=UnsupportedRegistry(),
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=None,
        byte_end=None,
    )

    result = queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release", "resolve-prepared"]
    assert result["status"] == "unresolved"
    assert result["source_system"] == "artifact"
    assert result["uri"] == "artifact://[redacted]"
    assert result["content_base64"] is None
    assert result["returned_bytes"] == 0
    assert "secret-resolver-output" not in str(result)


def test_prepared_local_store_resolution_is_frozen_and_uses_scoped_dispatch(
    tmp_path,
):
    project_id = uuid4()
    dataset_id = uuid4()
    store_root = tmp_path / "registered-store"
    source_reference = ExternalArtifactReference.for_store(
        store_name="lab-fs",
        locator="nested/artifact.txt",
        content_hash=_sha256(b"ok"),
        metadata={"nested": {"value": "prepared"}},
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="lab-fs",
        kind=StoreKind.LOCAL_FS,
        root=str(store_root),
    )
    api = _ContextApi(project_id=project_id, reference=source_reference)
    lookup = _DataStoreLookup(store)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=dataset_id,
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=1,
        byte_end=3,
    )
    source_reference.metadata["nested"]["value"] = "mutated"
    store.root = str(tmp_path / "mutated-after-preparation")
    api.raise_on_read = True

    assert isinstance(prepared.target, LocalStoreResolutionTarget)
    assert prepared.target.store_root == str(store_root)
    assert prepared.target.locator.path == "nested/artifact.txt"

    result = queries.resolve_prepared_external_artifact(prepared)

    assert api.calls == ["authorized:reader"]
    assert lookup.calls == ["store"]
    assert lookup.names == ["lab-fs"]
    assert calls == ["release", "resolve-prepared"]
    assert registry.prepared_targets == [prepared.target]
    assert registry.parameters == [(64, (1, 3))]
    assert registry.references == []
    assert len(registry.local_targets) == 1
    assert registry.local_targets[0].logical_reference.metadata == {
        "nested": {"value": "prepared"}
    }
    assert result["status"] == "verified"
    assert result["uri"] == "store://lab-fs/nested/artifact.txt"
    assert result["content_base64"] == "aw=="


def test_prepared_http_store_resolution_is_frozen_and_uses_scoped_dispatch():
    project_id = uuid4()
    dataset_id = uuid4()
    source_reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator="nested/artifact.txt",
        content_hash=_sha256(b"ok"),
        source_system="legacy-http",
        metadata={"nested": {"value": "prepared"}},
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="web",
        kind=StoreKind.HTTP,
        root="https://root.example/ignored",
        endpoint="https://files.example/base",
    )
    api = _ContextApi(project_id=project_id, reference=source_reference)
    lookup = _DataStoreLookup(store)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=dataset_id,
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=1,
        byte_end=3,
    )
    source_reference.metadata["nested"]["value"] = "mutated"
    store.endpoint = "https://mutated.example/outside"
    api.raise_on_read = True

    assert isinstance(prepared.target, HttpStoreResolutionTarget)
    assert prepared.target.registered_prefix.canonical_url == (
        "https://files.example/base/"
    )
    assert prepared.target.locator.path == "nested/artifact.txt"
    assert prepared.target.logical_reference.source_system == "store"

    result = queries.resolve_prepared_external_artifact(prepared)

    assert api.calls == ["authorized:reader"]
    assert lookup.calls == ["store"]
    assert lookup.names == ["web"]
    assert calls == ["release", "resolve-prepared"]
    assert registry.prepared_targets == [prepared.target]
    assert registry.parameters == [(64, (1, 3))]
    assert registry.references == []
    assert registry.local_targets == []
    assert len(registry.http_targets) == 1
    assert registry.http_targets[0].logical_reference.metadata == {
        "nested": {"value": "prepared"}
    }
    assert result["status"] == "verified"
    assert result["uri"] == "store://web/nested/artifact.txt"
    assert result["content_base64"] == "aw=="


def test_structured_and_uri_http_store_references_prepare_equal_targets():
    project_id = uuid4()
    store_name = "user@web store:one"
    logical_uri = "store://user%40web%20store%3Aone/caf%C3%A9/file%20name.bin"
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=store_name,
        kind=StoreKind.HTTP,
        root="https://files.example/base",
    )
    structured = ExternalArtifactReference.for_http_store(
        store_name=store_name,
        locator="café/file name.bin",
        content_hash=_sha256(b"ok"),
    )
    uri_only = ExternalArtifactReference(
        source_system="legacy-http",
        uri=logical_uri,
        content_hash=_sha256(b"ok"),
    )

    def prepare(reference: ExternalArtifactReference) -> HttpStoreResolutionTarget:
        queries = ContextQueries(
            api=_ContextApi(project_id=project_id, reference=reference),
            repository=_ContextRepository(_DataStoreLookup(store)),
            session=object(),
            release_read_scope=lambda: None,
        )
        prepared = queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )
        assert isinstance(prepared.target, HttpStoreResolutionTarget)
        return prepared.target

    structured_target = prepare(structured)
    uri_target = prepare(uri_only)

    assert structured_target == uri_target
    assert structured_target.logical_reference.source_system == "store"
    assert structured_target.logical_reference.store_name == store_name
    assert structured_target.logical_reference.uri == logical_uri
    assert structured_target.locator.components == ("café", "file name.bin")
    assert structured_target.logical_reference.locator == "café/file name.bin"


def test_uri_http_store_reference_preserves_unique_legacy_literal_percent_name():
    project_id = uuid4()
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="legacy%20remote",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
    )
    lookup = _ExactDataStoreLookup(store)
    reference = ExternalArtifactReference(
        source_system="legacy-http",
        uri="store://legacy%20remote/nested/artifact.bin",
        content_hash=_sha256(b"ok"),
    )
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert isinstance(prepared.target, HttpStoreResolutionTarget)
    assert lookup.names == ["legacy remote", "legacy%20remote"]
    assert prepared.target.logical_reference.store_name == "legacy%20remote"
    assert prepared.target.logical_reference.uri == (
        "store://legacy%2520remote/nested/artifact.bin"
    )


def test_ambiguous_encoded_and_legacy_http_store_names_fail_closed():
    project_id = uuid4()
    decoded_store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="legacy remote",
        kind=StoreKind.HTTP,
        root="https://files.example/decoded",
    )
    literal_store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="legacy%20remote",
        kind=StoreKind.HTTP,
        root="https://files.example/literal",
    )
    lookup = _ExactDataStoreLookup(decoded_store, literal_store)
    reference = ExternalArtifactReference(
        source_system="legacy-http",
        uri="store://legacy%20remote/nested/artifact.bin",
        content_hash=_sha256(b"ok"),
    )
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert isinstance(prepared.target, ResolvedArtifact)
    assert lookup.names == ["legacy remote", "legacy%20remote"]
    assert prepared.target.status is ResolutionStatus.UNRESOLVED
    assert prepared.target.uri == "store://[redacted]"


@pytest.mark.parametrize(
    ("locator", "canonical_uri"),
    (
        (
            "nested/file name.bin",
            "store://web/nested/file%20name.bin",
        ),
        (
            "Müller/測定.bin",
            "store://web/M%C3%BCller/%E6%B8%AC%E5%AE%9A.bin",
        ),
        (
            "nested/a+b.bin",
            "store://web/nested/a%2Bb.bin",
        ),
        (
            "nested/result (final).bin",
            "store://web/nested/result%20%28final%29.bin",
        ),
    ),
)
def test_legacy_for_store_http_reference_is_canonicalized_during_preparation(
    locator: str,
    canonical_uri: str,
) -> None:
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator=locator,
        content_hash=_sha256(b"ok"),
        source_system="legacy-http",
    )
    assert reference.uri == f"store://web/{locator}"
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
    )
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert isinstance(prepared.target, HttpStoreResolutionTarget)
    assert prepared.target.locator.path == locator
    assert prepared.target.logical_reference.source_system == "store"
    assert prepared.target.logical_reference.uri == canonical_uri


@pytest.mark.parametrize("uri_only", (False, True))
def test_http_store_legacy_compatibility_does_not_accept_mismatched_identity(
    uri_only: bool,
) -> None:
    project_id = uuid4()
    legacy = ExternalArtifactReference.for_store(
        store_name="web",
        locator="nested/file name.bin",
        content_hash=_sha256(b"ok"),
    )
    reference = (
        ExternalArtifactReference(
            source_system="store",
            uri=legacy.uri,
            content_hash=legacy.content_hash,
        )
        if uri_only
        else legacy.model_copy(
            update={"uri": "store://web/nested/different name.bin"}
        )
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
    )
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert isinstance(prepared.target, ResolvedArtifact)
    assert prepared.target.status is ResolutionStatus.UNRESOLVED
    assert prepared.target.uri == "store://[redacted]"
    assert prepared.target.detail == "Store artifact reference is invalid."


@pytest.mark.parametrize(
    "store_name",
    (
        "[not-ip]",
        "web：name",
        "web／name",
    ),
)
def test_non_round_trippable_http_store_names_fail_in_both_reference_forms(
    store_name: str,
) -> None:
    project_id = uuid4()
    logical_uri = f"store://{store_name}/nested/artifact.bin"
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=store_name,
        kind=StoreKind.HTTP,
        root="https://files.example/base",
    )
    references = (
        ExternalArtifactReference(
            source_system="store",
            uri=logical_uri,
            content_hash=_sha256(b"ok"),
            store_name=store_name,
            locator="nested/artifact.bin",
        ),
        ExternalArtifactReference(
            source_system="store",
            uri=logical_uri,
            content_hash=_sha256(b"ok"),
        ),
    )
    outcomes: list[tuple[ResolutionStatus, str, str | None]] = []

    for reference in references:
        queries = ContextQueries(
            api=_ContextApi(project_id=project_id, reference=reference),
            repository=_ContextRepository(_DataStoreLookup(store)),
            session=object(),
            release_read_scope=lambda: None,
        )
        prepared = queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )

        assert isinstance(prepared.target, ResolvedArtifact)
        outcomes.append(
            (
                prepared.target.status,
                prepared.target.uri,
                prepared.target.detail,
            )
        )

    assert outcomes == [
        (
            ResolutionStatus.UNRESOLVED,
            "store://[redacted]",
            "Store artifact reference is invalid.",
        ),
        (
            ResolutionStatus.UNRESOLVED,
            "store://[redacted]",
            "Store artifact reference is invalid.",
        ),
    ]


def test_precomputed_failure_does_not_construct_default_resolver_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid4()
    reference = ExternalArtifactReference(
        source_system="store",
        uri="store://missing/artifact.bin",
        content_hash=_sha256(b"secret"),
    )
    calls: list[str] = []
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    assert isinstance(prepared.target, ResolvedArtifact)

    def unexpected_registry_factory():
        calls.append("registry-from-env")
        raise AssertionError("static resolution loaded resolver configuration")

    monkeypatch.setattr(
        "lab_tracker.artifact_resolution.registry_from_env",
        unexpected_registry_factory,
    )

    result = queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release"]
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == "Store artifact could not be resolved."


@pytest.mark.parametrize(
    ("root", "uri", "store_name", "locator", "expected_detail"),
    (
        (
            "https://user:secret@files.example/base",
            "store://web/artifact.bin",
            "web",
            "artifact.bin",
            "Store artifact could not be resolved.",
        ),
        (
            "https://files.example/base?token=secret",
            "store://web/artifact.bin",
            "web",
            "artifact.bin",
            "Store artifact could not be resolved.",
        ),
        (
            "https://files.example/base",
            "store://web/%2e%2e/secret.bin",
            None,
            None,
            "Store artifact reference is invalid.",
        ),
        (
            "https://files.example/base",
            "store://web/nested%5Csecret.bin",
            None,
            None,
            "Store artifact reference is invalid.",
        ),
    ),
)
def test_invalid_http_store_definition_or_locator_never_reaches_resolver_io(
    root: str,
    uri: str,
    store_name: str | None,
    locator: str | None,
    expected_detail: str,
):
    project_id = uuid4()
    reference = ExternalArtifactReference(
        source_system="store",
        uri=uri,
        content_hash=_sha256(b"secret"),
        store_name=store_name,
        locator=locator,
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="web",
        kind=StoreKind.HTTP,
        root=root,
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    result = queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release", "resolve-prepared"]
    assert len(registry.precomputed_targets) == 1
    assert registry.precomputed_targets[0] is prepared.target
    assert registry.references == []
    assert registry.local_targets == []
    assert registry.http_targets == []
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == expected_detail
    assert "secret" not in str(result)


def test_structured_and_uri_rclone_store_references_prepare_equal_targets():
    project_id = uuid4()
    store_name = "cloud store"
    logical_uri = "store://cloud%20store/caf%C3%A9/file%20name.bin"
    metadata = {"nested": {"value": "prepared"}}
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=store_name,
        kind=StoreKind.RCLONE,
        root="/Lab Data",
        credential_ref="lab remote",
    )
    structured = ExternalArtifactReference.for_store(
        store_name=store_name,
        locator="café/file name.bin",
        content_hash=_sha256(b"ok"),
        source_system="legacy-rclone",
        metadata=metadata,
    )
    uri_only = ExternalArtifactReference(
        source_system="legacy-rclone",
        uri=logical_uri,
        content_hash=_sha256(b"ok"),
        metadata=metadata,
    )

    def prepare(reference: ExternalArtifactReference) -> RcloneStoreResolutionTarget:
        queries = ContextQueries(
            api=_ContextApi(project_id=project_id, reference=reference),
            repository=_ContextRepository(_DataStoreLookup(store)),
            session=object(),
            release_read_scope=lambda: None,
        )
        prepared = queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )
        assert isinstance(prepared.target, RcloneStoreResolutionTarget)
        return prepared.target

    structured_target = prepare(structured)
    uri_target = prepare(uri_only)

    assert structured.uri == "store://cloud store/café/file name.bin"
    assert structured_target == uri_target
    assert structured_target.logical_reference.source_system == "store"
    assert structured_target.logical_reference.store_name == store_name
    assert structured_target.logical_reference.uri == logical_uri
    assert structured_target.logical_reference.locator == "café/file name.bin"
    assert structured_target.remote.value == "lab remote"
    assert structured_target.registered_root.rooted is True
    assert structured_target.registered_root.components == ("Lab Data",)
    assert structured_target.locator.components == ("café", "file name.bin")
    with pytest.raises(FrozenInstanceError):
        structured_target.locator = uri_target.locator


def test_rclone_preparation_preserves_relative_rooted_and_remote_root_modes():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="archive",
        locator="artifact.bin",
        content_hash=_sha256(b"ok"),
    )

    def prepare(root: str) -> RcloneStoreResolutionTarget:
        store = DataStore(
            store_id=uuid4(),
            project_id=project_id,
            name="archive",
            kind=StoreKind.ONEDRIVE,
            root=root,
            credential_ref="lab-onedrive",
        )
        queries = ContextQueries(
            api=_ContextApi(project_id=project_id, reference=reference),
            repository=_ContextRepository(_DataStoreLookup(store)),
            session=object(),
            release_read_scope=lambda: None,
        )
        prepared = queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )
        assert isinstance(prepared.target, RcloneStoreResolutionTarget)
        return prepared.target

    relative = prepare("experiments")
    rooted = prepare("/experiments")
    remote_root = prepare("/")

    assert relative != rooted
    assert relative.registered_root.rooted is False
    assert relative.registered_root.components == ("experiments",)
    assert relative.argv_target == "lab-onedrive:experiments/artifact.bin"
    assert rooted.registered_root.rooted is True
    assert rooted.registered_root.components == ("experiments",)
    assert rooted.argv_target == "lab-onedrive:/experiments/artifact.bin"
    assert remote_root.registered_root.rooted is True
    assert remote_root.registered_root.components == ()
    assert remote_root.argv_target == "lab-onedrive:/artifact.bin"


def test_prepared_rclone_store_resolution_is_detached_and_uses_scoped_dispatch():
    project_id = uuid4()
    dataset_id = uuid4()
    source_reference = ExternalArtifactReference.for_store(
        store_name="archive",
        locator="nested/artifact.txt",
        content_hash=_sha256(b"ok"),
        source_system="legacy-rclone",
        metadata={"nested": {"value": "prepared"}},
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="archive",
        kind=StoreKind.ONEDRIVE,
        root="/OneDrive/experiments",
        credential_ref="lab-onedrive",
    )
    api = _ContextApi(project_id=project_id, reference=source_reference)
    lookup = _DataStoreLookup(store)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=dataset_id,
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=1,
        byte_end=3,
    )
    assert isinstance(prepared.target, RcloneStoreResolutionTarget)
    source_reference.metadata["nested"]["value"] = "mutated"
    store.root = "/mutated/outside"
    store.credential_ref = "mutated-remote"
    api.raise_on_read = True

    assert prepared.target.argv_target == (
        "lab-onedrive:/OneDrive/experiments/nested/artifact.txt"
    )
    assert prepared.target.logical_reference.metadata == {
        "nested": {"value": "prepared"}
    }

    result = queries.resolve_prepared_external_artifact(prepared)

    assert api.calls == ["authorized:reader"]
    assert lookup.calls == ["store"]
    assert lookup.names == ["archive"]
    assert calls == ["release", "resolve-prepared"]
    assert registry.prepared_targets == [prepared.target]
    assert registry.parameters == [(64, (1, 3))]
    assert registry.references == []
    assert registry.local_targets == []
    assert registry.http_targets == []
    assert registry.rclone_targets == [prepared.target]
    assert result["status"] == "verified"
    assert result["uri"] == "store://archive/nested/artifact.txt"
    assert result["content_base64"] == "aw=="
    assert "lab-onedrive" not in str(result)
    assert "OneDrive" not in str(result)


def test_present_empty_rclone_credential_fails_closed_without_scoped_dispatch():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="archive",
        locator="nested/artifact.bin",
        content_hash=_sha256(b"secret"),
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="archive",
        kind=StoreKind.RCLONE,
        root="/experiments",
        credential_ref="",
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    result = queries.resolve_prepared_external_artifact(prepared)

    assert isinstance(prepared.target, ResolvedArtifact)
    assert calls == ["release", "resolve-prepared"]
    assert registry.references == []
    assert registry.rclone_targets == []
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == "Store artifact could not be resolved."
    assert "archive" not in str(result)


@pytest.mark.parametrize(
    ("reference", "root", "credential_ref", "forbidden"),
    (
        (
            ExternalArtifactReference(
                source_system="store",
                uri="store://archive/nested/artifact.bin?download=secret",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "download",
        ),
        (
            ExternalArtifactReference(
                source_system="store",
                uri="store://archive/nested/artifact.bin#secret-fragment",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "fragment",
        ),
        (
            ExternalArtifactReference(
                source_system="store",
                uri="store://archive/nested%2Fsecret.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "secret.bin",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="../secret.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "lab-remote",
            "secret.bin",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="artifact.bin",
                content_hash=_sha256(b"secret"),
            ),
            "../outside",
            "lab-remote",
            "outside",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="artifact.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            ":s3,env_auth=true",
            "env_auth",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="archive",
                locator="artifact.bin",
                content_hash=_sha256(b"secret"),
            ),
            "/experiments",
            "-config",
            "config",
        ),
    ),
)
def test_invalid_rclone_identity_locator_root_or_remote_fails_opaquely(
    reference: ExternalArtifactReference,
    root: str,
    credential_ref: str,
    forbidden: str,
) -> None:
    project_id = uuid4()
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="archive",
        kind=StoreKind.RCLONE,
        root=root,
        credential_ref=credential_ref,
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    result = queries.resolve_prepared_external_artifact(prepared)

    assert isinstance(prepared.target, ResolvedArtifact)
    assert calls == ["release", "resolve-prepared"]
    assert registry.references == []
    assert registry.rclone_targets == []
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert forbidden not in str(result)


def test_structured_and_uri_git_store_references_prepare_equal_targets():
    project_id = uuid4()
    object_id = "d" * 64
    store_name = "user@analysis store"
    canonical_uri = (
        "store://user%40analysis%20store/"
        f"M%C3%BCller/@generated%20model.py@{object_id}"
    )
    metadata = {"nested": {"value": "prepared"}}
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=store_name,
        kind=StoreKind.GIT,
        root="HTTPS://GIT.EXAMPLE:443/lab/model.git",
    )
    structured = ExternalArtifactReference.for_store(
        store_name=store_name,
        locator=f"Müller/@generated model.py@{object_id}",
        content_hash=_sha256(b"model"),
        source_system="legacy-git",
        metadata=metadata,
    )
    uri_only = ExternalArtifactReference(
        source_system="legacy-git",
        uri=canonical_uri,
        content_hash=_sha256(b"model"),
        metadata=metadata,
    )

    def prepare(reference: ExternalArtifactReference) -> GitStoreResolutionTarget:
        queries = ContextQueries(
            api=_ContextApi(project_id=project_id, reference=reference),
            repository=_ContextRepository(_DataStoreLookup(store)),
            session=object(),
            release_read_scope=lambda: None,
        )
        prepared = queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )
        assert isinstance(prepared.target, GitStoreResolutionTarget)
        return prepared.target

    structured_target = prepare(structured)
    uri_target = prepare(uri_only)

    assert structured_target == uri_target
    assert structured_target.logical_reference.source_system == "store"
    assert structured_target.logical_reference.uri == canonical_uri
    assert structured_target.logical_reference.store_name == store_name
    assert structured_target.logical_reference.locator == (
        f"Müller/@generated model.py@{object_id}"
    )
    assert structured_target.remote.subprocess_value == (
        "https://git.example/lab/model.git"
    )
    assert structured_target.pin.path.components == (
        "Müller",
        "@generated model.py",
    )
    assert structured_target.pin.object_id.object_format == "sha256"
    with pytest.raises(FrozenInstanceError):
        structured_target.pin = uri_target.pin


def test_prepared_git_store_resolution_is_detached_and_uses_scoped_dispatch():
    project_id = uuid4()
    dataset_id = uuid4()
    object_id = "e" * 40
    source_reference = ExternalArtifactReference.for_store(
        store_name="analysis-repo",
        locator=f"src/model.py@{object_id}",
        content_hash=_sha256(b"ok"),
        source_system="legacy-git",
        metadata={"nested": {"value": "prepared"}},
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
    )
    api = _ContextApi(project_id=project_id, reference=source_reference)
    lookup = _DataStoreLookup(store)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=dataset_id,
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=1,
        byte_end=3,
    )
    assert isinstance(prepared.target, GitStoreResolutionTarget)
    source_reference.metadata["nested"]["value"] = "mutated"
    store.root = "https://git.example/outside/repo.git"
    api.raise_on_read = True

    assert prepared.target.remote.subprocess_value == (
        "https://git.example/lab/model.git"
    )
    assert prepared.target.logical_reference.metadata == {
        "nested": {"value": "prepared"}
    }

    result = queries.resolve_prepared_external_artifact(prepared)

    assert api.calls == ["authorized:reader"]
    assert lookup.calls == ["store"]
    assert calls == ["release", "resolve-prepared"]
    assert registry.prepared_targets == [prepared.target]
    assert registry.parameters == [(64, (1, 3))]
    assert registry.references == []
    assert registry.git_targets == [prepared.target]
    assert result["status"] == "verified"
    assert result["uri"] == f"store://analysis-repo/src/model.py@{object_id}"
    assert result["content_base64"] == "aw=="
    assert "git.example" not in str(result)


@pytest.mark.parametrize(
    ("reference", "root", "forbidden"),
    (
        (
            ExternalArtifactReference(
                source_system="store",
                uri=f"store://analysis-repo/src/model.py@{'a' * 40}?secret=query",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "query",
        ),
        (
            ExternalArtifactReference(
                source_system="store",
                uri=f"store://analysis-repo/src/model.py%40{'a' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "model.py",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator="src/model.py@HEAD",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "HEAD",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator=f"src/model.py@{'A' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "AAAA",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator=f"src/../secret.py@{'a' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "https://git.example/lab/model.git",
            "secret.py",
        ),
        (
            ExternalArtifactReference.for_store(
                store_name="analysis-repo",
                locator=f"src/model.py@{'a' * 40}",
                content_hash=_sha256(b"secret"),
            ),
            "../legacy-secret-repo",
            "legacy-secret",
        ),
    ),
)
def test_invalid_git_identity_pin_path_or_remote_fails_opaquely(
    reference: ExternalArtifactReference,
    root: str,
    forbidden: str,
) -> None:
    project_id = uuid4()
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="analysis-repo",
        kind=StoreKind.GIT,
        root=root,
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    result = queries.resolve_prepared_external_artifact(prepared)

    assert isinstance(prepared.target, ResolvedArtifact)
    assert calls == ["release", "resolve-prepared"]
    assert registry.references == []
    assert registry.git_targets == []
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert forbidden not in str(result)


def test_preexisting_raw_at_git_pin_keeps_one_canonical_store_identity():
    project_id = uuid4()
    commit = "a" * 40
    locator = f"src/model.py@{commit}"
    source_reference = ExternalArtifactReference(
        source_system="store",
        uri=f"store://analysis-repo/{locator}",
        content_hash=_sha256(b"model"),
        store_name="analysis-repo",
        locator=locator,
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
    )
    lookup = _DataStoreLookup(store)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=source_reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert isinstance(prepared.target, GitStoreResolutionTarget)
    assert prepared.target.logical_reference.uri == (
        f"store://analysis-repo/src/model.py@{commit}"
    )
    assert prepared.target.logical_reference.locator == locator
    assert prepared.target.remote.subprocess_value == (
        "https://git.example/lab/model.git"
    )
    assert prepared.target.pin.path.path == "src/model.py"
    assert prepared.target.pin.object_id.value == commit
    assert lookup.names == ["analysis-repo"]


def test_git_store_materialization_rejects_nonportable_ads_punctuation():
    project_id = uuid4()
    commit = "b" * 40
    locator = f"src/run:1.py@{commit}"
    source_reference = ExternalArtifactReference.for_store(
        store_name="analysis-repo",
        locator=locator,
        content_hash=_sha256(b"model"),
        source_system="legacy-git",
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="analysis-repo",
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
    )
    lookup = _DataStoreLookup(store)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=source_reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    result = queries.resolve_prepared_external_artifact(prepared)

    assert isinstance(prepared.target, ResolvedArtifact)
    assert registry.git_targets == []
    assert registry.references == []
    assert calls == ["release", "resolve-prepared"]
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == "Store artifact reference is invalid."
    assert lookup.names == ["analysis-repo"]


def test_uri_only_git_store_reference_canonicalizes_custom_store_identity():
    project_id = uuid4()
    commit = "c" * 40
    store_name = "user@analysis-repo"
    source_reference = ExternalArtifactReference(
        source_system="legacy-git",
        uri=f"store://{store_name}/src/run.py@{commit}",
        content_hash=_sha256(b"model"),
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=store_name,
        kind=StoreKind.GIT,
        root="https://git.example/lab/model.git",
    )
    lookup = _DataStoreLookup(store)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=source_reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert isinstance(prepared.target, GitStoreResolutionTarget)
    assert prepared.target.logical_reference.source_system == "store"
    assert prepared.target.logical_reference.uri == (
        f"store://user%40analysis-repo/src/run.py@{commit}"
    )
    assert prepared.target.logical_reference.store_name == store_name
    assert prepared.target.logical_reference.locator == f"src/run.py@{commit}"
    assert prepared.target.remote.subprocess_value == (
        "https://git.example/lab/model.git"
    )
    assert lookup.names == [store_name]


def test_same_backend_punctuation_is_invalid_for_local_store(tmp_path):
    project_id = uuid4()
    locator = f"src/run:1.py@{'b' * 40}"
    source_reference = ExternalArtifactReference.for_store(
        store_name="lab-fs",
        locator=locator,
        content_hash=_sha256(b"secret"),
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="lab-fs",
        kind=StoreKind.LOCAL_FS,
        root=str(tmp_path),
    )
    lookup = _DataStoreLookup(store)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=source_reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    result = queries.resolve_prepared_external_artifact(prepared)

    assert lookup.names == ["lab-fs"]
    assert registry.references == []
    assert registry.local_targets == []
    assert calls == ["release", "resolve-prepared"]
    assert len(registry.precomputed_targets) == 1
    assert registry.precomputed_targets[0] is prepared.target
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == "Store artifact reference is invalid."


def test_legacy_local_source_system_is_canonicalized_on_detached_target(tmp_path):
    project_id = uuid4()
    source_reference = ExternalArtifactReference.for_store(
        store_name="lab-fs",
        locator="/nested/artifact.bin",
        content_hash=_sha256(b"artifact"),
        source_system="legacy-local",
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="lab-fs",
        kind=StoreKind.LOCAL_FS,
        root=str(tmp_path),
    )
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=source_reference),
        repository=_ContextRepository(_DataStoreLookup(store)),
        session=object(),
        release_read_scope=lambda: None,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    assert source_reference.source_system == "legacy-local"
    assert source_reference.locator == "nested/artifact.bin"
    assert isinstance(prepared.target, LocalStoreResolutionTarget)
    assert prepared.target.logical_reference.source_system == "store"
    assert prepared.target.logical_reference.uri == (
        "store://lab-fs/nested/artifact.bin"
    )


@pytest.mark.parametrize(
    (
        "source_system",
        "uri",
        "store_name",
        "locator",
        "expected_store_lookups",
        "expected_detail",
    ),
    [
        (
            "store",
            "store://wrong/path.txt",
            "lab-fs",
            "path.txt",
            1,
            "Store artifact reference is invalid.",
        ),
        (
            "store",
            "store://lab-fs//secret.txt",
            None,
            None,
            1,
            "Store artifact reference is invalid.",
        ),
        (
            "store",
            "store://lab-fs/path.txt?download=1",
            None,
            None,
            1,
            "Store artifact reference is invalid.",
        ),
        (
            "store",
            "store://lab-fs/path.txt#fragment",
            None,
            None,
            1,
            "Store artifact reference is invalid.",
        ),
        (
            "store",
            "store://user@lab-fs/path.txt",
            None,
            None,
            1,
            "Store artifact could not be resolved.",
        ),
        (
            "store",
            "store://lab-fs/%2e%2e/secret.txt",
            None,
            None,
            1,
            "Store artifact reference is invalid.",
        ),
        (
            "store",
            "store://lab-fs/%70ath.txt",
            None,
            None,
            1,
            "Store artifact reference is invalid.",
        ),
        (
            "local",
            "store://[secret",
            None,
            None,
            0,
            "Store artifact reference is invalid.",
        ),
        (
            "local",
            " STORE://[secret",
            None,
            None,
            0,
            "Store artifact reference is invalid.",
        ),
    ],
)
def test_invalid_store_identity_fails_closed_before_resolver_work(
    source_system,
    uri,
    store_name,
    locator,
    expected_store_lookups,
    expected_detail,
    tmp_path,
):
    project_id = uuid4()
    reference = ExternalArtifactReference(
        source_system=source_system,
        uri=uri,
        content_hash=_sha256(b"secret"),
        store_name=store_name,
        locator=locator,
    )
    api = _ContextApi(project_id=project_id, reference=reference)
    lookup = _DataStoreLookup(
        DataStore(
            store_id=uuid4(),
            project_id=project_id,
            name="lab-fs",
            kind=StoreKind.LOCAL_FS,
            root=str(tmp_path),
        )
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )
    result = queries.resolve_prepared_external_artifact(prepared)

    assert lookup.calls == ["store"] * expected_store_lookups
    assert calls == ["release", "resolve-prepared"]
    assert len(registry.precomputed_targets) == 1
    assert registry.precomputed_targets[0] is prepared.target
    assert registry.references == []
    assert registry.local_targets == []
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == expected_detail
    assert result["content_base64"] is None
    assert "secret.txt" not in str(result)


def test_prepared_resolution_does_not_resolve_when_scope_release_raises_base_exception():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    calls: list[str] = []
    registry = _ResolverRegistry(calls)

    def interrupt_release() -> None:
        calls.append("release")
        raise KeyboardInterrupt("cancel")

    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=interrupt_release,
        resolver_registry=registry,
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=None,
        byte_start=None,
        byte_end=None,
    )

    with pytest.raises(KeyboardInterrupt, match="cancel"):
        queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release"]
    assert registry.references == []


def test_prepared_resolution_releases_before_resolver_base_exception():
    source_reference = ExternalArtifactReference(
        source_system="test",
        uri="test://artifact",
        content_hash=_sha256(b"ok"),
    )
    api = _ContextApi(project_id=uuid4(), reference=source_reference)
    calls: list[str] = []
    registry = _ResolverRegistry(calls, error=KeyboardInterrupt("resolver cancelled"))
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )
    prepared = queries.prepare_external_artifact_resolution(
        actor=AuthContext(user_id="reader", role=Role.VIEWER),
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        content_hash=None,
        max_bytes=64,
        byte_start=1,
        byte_end=3,
    )
    api.raise_on_read = True

    with pytest.raises(KeyboardInterrupt, match="resolver cancelled"):
        queries.resolve_prepared_external_artifact(prepared)

    assert api.calls == ["authorized:reader"]
    assert calls == ["release", "resolve-prepared"]
    assert registry.parameters == [(64, (1, 3))]
    assert len(registry.references) == 1
