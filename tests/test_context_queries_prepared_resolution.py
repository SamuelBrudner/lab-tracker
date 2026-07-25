from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from lab_tracker.application.context_queries import ContextQueries
from lab_tracker.artifact_resolution import (
    LocalStoreResolutionTarget,
    ResolutionStatus,
    ResolvedArtifact,
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
        self.references: list[ExternalArtifactReference] = []
        self.local_targets: list[LocalStoreResolutionTarget] = []

    def resolve(
        self,
        reference: ExternalArtifactReference,
        *,
        max_bytes: int,
        byte_range: tuple[int, int] | None,
    ) -> ResolvedArtifact:
        self.calls.append("resolve")
        self.references.append(reference)
        assert max_bytes == 64
        assert byte_range == (1, 3)
        if self.error is not None:
            raise self.error
        content = b"ok"
        return ResolvedArtifact(
            status=ResolutionStatus.VERIFIED,
            source_system=reference.source_system,
            uri=reference.uri,
            expected_hash=reference.content_hash,
            observed_hash=reference.content_hash,
            content=content,
            fetched_at=datetime.now(timezone.utc),
        )

    def resolve_local_store(
        self,
        target: LocalStoreResolutionTarget,
        *,
        max_bytes: int,
        byte_range: tuple[int, int] | None,
    ) -> ResolvedArtifact:
        self.calls.append("resolve-local-store")
        self.local_targets.append(target)
        assert max_bytes == 64
        assert byte_range == (1, 3)
        if self.error is not None:
            raise self.error
        reference = target.logical_reference
        return ResolvedArtifact(
            status=ResolutionStatus.VERIFIED,
            source_system=reference.source_system,
            uri=reference.uri,
            expected_hash=reference.content_hash,
            observed_hash=reference.content_hash,
            content=b"ok",
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
    assert calls == ["release", "resolve"]
    assert registry.references[0].metadata == {"nested": {"value": "prepared"}}
    assert result["entity_type"] == "dataset"
    assert result["entity_id"] == str(dataset_id)
    assert result["artifact_index"] == 0
    assert result["content_base64"] == "b2s="


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
    api.raise_on_read = True

    result = queries.resolve_prepared_external_artifact(prepared)

    assert lookup.calls == ["store"]
    assert calls == ["release"]
    assert registry.references == []
    assert result["status"] == "unresolved"
    assert result["content_base64"] is None


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
    assert calls == ["release", "resolve-local-store"]
    assert registry.references == []
    assert len(registry.local_targets) == 1
    assert registry.local_targets[0].logical_reference.metadata == {
        "nested": {"value": "prepared"}
    }
    assert result["status"] == "verified"
    assert result["uri"] == "store://lab-fs/nested/artifact.txt"
    assert result["content_base64"] == "b2s="


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

    assert isinstance(prepared.target, ExternalArtifactReference)
    assert prepared.target.uri == (
        f"git+https://git.example/lab/model.git#{commit}:src/model.py"
    )
    assert lookup.names == ["analysis-repo"]


def test_git_store_materialization_preserves_backend_legal_punctuation():
    project_id = uuid4()
    commit = "b" * 40
    locator = f"src/run:1.py@{commit}"
    source_reference = ExternalArtifactReference.for_store(
        store_name="analysis-repo",
        locator=locator,
        content_hash=_sha256(b"model"),
        source_system="legacy-git",
    ).model_copy(
        update={
            "uri": "legacy-git://display-only",
        }
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

    assert isinstance(prepared.target, ExternalArtifactReference)
    assert prepared.target.uri == (
        f"git+https://git.example/lab/model.git#{commit}:src/run:1.py"
    )
    assert lookup.names == ["analysis-repo"]


def test_uri_only_git_store_reference_preserves_custom_source_label():
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

    assert isinstance(prepared.target, ExternalArtifactReference)
    assert prepared.target.uri == (
        f"git+https://git.example/lab/model.git#{commit}:src/run.py"
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
    assert calls == ["release"]
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
    ("source_system", "uri", "store_name", "locator", "expected_store_lookups"),
    [
        ("store", "store://wrong/path.txt", "lab-fs", "path.txt", 1),
        ("store", "store://lab-fs//secret.txt", None, None, 1),
        ("store", "store://lab-fs/path.txt?download=1", None, None, 1),
        ("store", "store://lab-fs/path.txt#fragment", None, None, 1),
        ("store", "store://user@lab-fs/path.txt", None, None, 1),
        ("store", "store://lab-fs/%2e%2e/secret.txt", None, None, 1),
        ("store", "store://lab-fs/%70ath.txt", None, None, 1),
        ("local", "store://[secret", None, None, 0),
        ("local", " STORE://[secret", None, None, 0),
    ],
)
def test_invalid_store_identity_fails_closed_before_resolver_work(
    source_system,
    uri,
    store_name,
    locator,
    expected_store_lookups,
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
    assert calls == ["release"]
    assert registry.references == []
    assert registry.local_targets == []
    assert result["status"] == "unresolved"
    assert result["uri"] == "store://[redacted]"
    assert result["detail"] == "Store artifact reference is invalid."
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
    assert calls == ["release", "resolve"]
    assert len(registry.references) == 1
