from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from lab_tracker.application.context_queries import (
    ContextQueries,
    PreparedExternalArtifactResolution,
)
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
    MAX_ARTIFACT_BYTE_OFFSET,
    MAX_INLINE_ARTIFACT_BYTES,
)
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    DataStore,
    ExternalArtifactReference,
    StoreKind,
)


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _http_store_fixture(
    *,
    locator: str = "artifact.bin",
) -> tuple[UUID, ExternalArtifactReference, DataStore]:
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="web",
        locator=locator,
        content_hash=_sha256(b"ok"),
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="web",
        kind=StoreKind.HTTP,
        root="https://files.example/base",
    )
    return project_id, reference, store


class _ContextApi:
    def __init__(self, *, project_id: UUID, reference: ExternalArtifactReference) -> None:
        self.project_id = project_id
        self.reference = reference
        self.calls: list[str] = []
        self.raise_on_read = False

    def get_dataset_for_read(self, _dataset_id: UUID, *, actor: AuthContext):
        return self._authorized_entity(actor, "commit_manifest")

    def get_analysis_for_read(self, _analysis_id: UUID, *, actor: AuthContext):
        return self._authorized_entity(actor, "external_artifacts")

    def get_claim_for_read(self, _claim_id: UUID, *, actor: AuthContext):
        return self._authorized_entity(actor, "external_citations")

    def _authorized_entity(self, actor: AuthContext, artifact_field: str):
        if self.raise_on_read:
            raise AssertionError("resolve must not read through the API")
        self.calls.append(f"authorized:{actor.user_id}")
        fields = {"project_id": self.project_id}
        if artifact_field == "commit_manifest":
            fields[artifact_field] = SimpleNamespace(
                external_artifacts=[self.reference]
            )
        else:
            fields[artifact_field] = [self.reference]
        return SimpleNamespace(**fields)


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


def test_direct_reference_denial_releases_scope_without_resolver_work():
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
    assert isinstance(prepared.target, ResolvedArtifact)
    assert calls == ["release"]
    assert registry.prepared_targets == []
    assert registry.parameters == []
    assert registry.references == []
    assert result["status"] == "unresolved"
    assert result["source_system"] == "store"
    assert result["uri"] == "store://[redacted]"
    assert result["observed_hash"] is None
    assert result["returned_bytes"] == 0
    assert result["detail"] == "Store artifact could not be resolved."
    assert result["entity_type"] == "dataset"
    assert result["entity_id"] == str(dataset_id)
    assert result["artifact_index"] == 0
    assert result["content_base64"] is None
    assert "prepared" not in str(result)


@pytest.mark.parametrize("entity_type", ("dataset", "analysis", "claim"))
@pytest.mark.parametrize(
    ("source_system", "uri"),
    (
        ("file", "file:///operator/private/secret.bin"),
        ("http", "https://private.example/secret.bin"),
        ("rclone", "rclone://operator-private/secret.bin"),
        ("git", "git+https://git.example/private/repository.git#secret.bin"),
    ),
)
def test_contributor_direct_reference_bogus_hash_sequence_is_blocked_before_dispatch(
    entity_type,
    source_system,
    uri,
):
    secret = b"operator bytes outside project authority"
    for content_hash in (_sha256(b"bogus"), _sha256(secret)):
        reference = ExternalArtifactReference(
            source_system=source_system,
            uri=uri,
            content_hash=content_hash,
        )
        api = _ContextApi(project_id=uuid4(), reference=reference)
        lookup = _DataStoreLookup()
        calls: list[str] = []
        registry = _ResolverRegistry(calls)
        queries = ContextQueries(
            api=api,
            repository=_ContextRepository(lookup),
            session=object(),
            release_read_scope=lambda calls=calls: calls.append("release"),
            resolver_registry=registry,
        )

        prepared = queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type=entity_type,
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )
        result = queries.resolve_prepared_external_artifact(prepared)

        assert api.calls == ["authorized:reader"]
        assert lookup.calls == []
        assert isinstance(prepared.target, ResolvedArtifact)
        assert calls == ["release"]
        assert registry.prepared_targets == []
        assert result["status"] == "unresolved"
        assert result["source_system"] == "store"
        assert result["uri"] == "store://[redacted]"
        assert result["expected_hash"] == content_hash
        assert result["observed_hash"] is None
        assert result["content_base64"] is None
        assert result["returned_bytes"] == 0
        assert result["detail"] == "Store artifact could not be resolved."
        assert "secret.bin" not in str(result)


def test_entity_authorization_failure_precedes_direct_reference_denial():
    reference = ExternalArtifactReference(
        source_system="http",
        uri="https://private.example/secret.bin",
        content_hash=_sha256(b"secret"),
    )

    class DeniedApi(_ContextApi):
        def get_dataset_for_read(
            self,
            _dataset_id: UUID,
            *,
            actor: AuthContext,
        ):
            self.calls.append(f"authorized:{actor.user_id}")
            raise NotFoundError("Dataset does not exist.")

    api = DeniedApi(project_id=uuid4(), reference=reference)
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

    with pytest.raises(NotFoundError, match="Dataset does not exist"):
        queries.prepare_external_artifact_resolution(
            actor=AuthContext(user_id="reader", role=Role.VIEWER),
            entity_type="dataset",
            entity_id=uuid4(),
            artifact_index=0,
            content_hash=None,
            max_bytes=None,
            byte_start=None,
            byte_end=None,
        )

    assert api.calls == ["authorized:reader"]
    assert lookup.calls == []
    assert calls == []
    assert registry.prepared_targets == []


def test_forged_prepared_raw_reference_fails_closed_before_resolver_dispatch():
    raw_reference = ExternalArtifactReference(
        source_system="file",
        uri="file:///operator/private/secret.bin",
        content_hash=_sha256(b"secret"),
    )
    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=uuid4(), reference=raw_reference),
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )
    forged = PreparedExternalArtifactResolution(
        entity_type="dataset",
        entity_id=uuid4(),
        artifact_index=0,
        target=raw_reference,
        max_bytes=64,
        byte_range=None,
    )

    result = queries.resolve_prepared_external_artifact(forged)

    assert calls == ["release"]
    assert registry.prepared_targets == []
    assert registry.parameters == []
    assert result["status"] == "unresolved"
    assert result["source_system"] == "artifact"
    assert result["uri"] == "artifact://[redacted]"
    assert result["expected_hash"] == "unavailable"
    assert result["observed_hash"] is None
    assert result["content_base64"] is None
    assert result["returned_bytes"] == 0
    assert result["detail"] == "Artifact resolver result could not be returned safely."
    assert "secret.bin" not in str(result)


def test_forged_custom_prepared_handle_fails_closed_without_reflection():
    class SensitiveHandle:
        @property
        def _authorization(self):
            raise AssertionError("forged handle authorization was inspected")

        def __getattr__(self, _name):
            raise AssertionError("forged handle field was inspected")

        def __str__(self):
            raise AssertionError("forged handle was stringified")

    calls: list[str] = []
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(
            project_id=uuid4(),
            reference=ExternalArtifactReference(
                source_system="file",
                uri="file:///operator/private/secret.bin",
                content_hash=_sha256(b"secret"),
            ),
        ),
        repository=_ContextRepository(_DataStoreLookup()),
        session=object(),
        release_read_scope=lambda: calls.append("release"),
        resolver_registry=registry,
    )

    result = queries.resolve_prepared_external_artifact(
        SensitiveHandle(),  # type: ignore[arg-type]
    )

    assert calls == ["release"]
    assert registry.prepared_targets == []
    assert result["status"] == "unresolved"
    assert result["source_system"] == "artifact"
    assert result["uri"] == "artifact://[redacted]"
    assert result["content_base64"] is None


def _registered_store_fixture(
    tmp_path,
    store_kind: StoreKind,
) -> tuple[UUID, ExternalArtifactReference, DataStore]:
    project_id = uuid4()
    store_name = f"registered-{store_kind.value}"
    roots = {
        StoreKind.LOCAL_FS: str(tmp_path / "operator-secret-local-root"),
        StoreKind.HTTP: "https://operator-secret.example/base",
        StoreKind.RCLONE: "/operator-secret/rclone-root",
        StoreKind.GIT: "https://operator-secret.example/repository.git",
    }
    reference = ExternalArtifactReference.for_store(
        store_name=store_name,
        locator=(
            "artifact.bin@"
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            if store_kind is StoreKind.GIT
            else "artifact.bin"
        ),
        content_hash=_sha256(b"ok"),
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=store_name,
        kind=store_kind,
        root=roots[store_kind],
        endpoint=(
            "https://operator-secret-endpoint.example/health"
            if store_kind is StoreKind.HTTP
            else None
        ),
        credential_ref=(
            "operator-secret-credential"
            if store_kind in {StoreKind.HTTP, StoreKind.RCLONE, StoreKind.GIT}
            else None
        ),
    )
    return project_id, reference, store


def _prepare_registered_store(
    tmp_path,
    store_kind: StoreKind,
    *,
    release_read_scope=None,
):
    project_id, reference, store = _registered_store_fixture(tmp_path, store_kind)
    calls: list[str] = []
    lookup = _DataStoreLookup(store)
    registry = _ResolverRegistry(calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=(
            release_read_scope
            if release_read_scope is not None
            else lambda: calls.append("release")
        ),
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
    return queries, prepared, calls, registry, lookup, reference, store


def _assert_registered_store_is_statically_unavailable(
    prepared: PreparedExternalArtifactResolution,
) -> None:
    assert type(prepared.target) is ResolvedArtifact
    assert prepared.target.status is ResolutionStatus.UNRESOLVED
    assert prepared.target.source_system == "store"
    assert prepared.target.uri == "store://[redacted]"
    assert prepared.target.observed_hash is None
    assert prepared.target.content is None
    assert prepared.target.detail == "Store artifact could not be resolved."
    assert not isinstance(
        prepared.target,
        (
            LocalStoreResolutionTarget,
            HttpStoreResolutionTarget,
            RcloneStoreResolutionTarget,
            GitStoreResolutionTarget,
        ),
    )


@pytest.mark.parametrize(
    "store_kind",
    (StoreKind.LOCAL_FS, StoreKind.HTTP, StoreKind.RCLONE, StoreKind.GIT),
)
def test_every_registered_store_kind_fails_closed_before_target_or_resolver_work(
    tmp_path,
    store_kind: StoreKind,
):
    queries, prepared, calls, registry, lookup, reference, store = (
        _prepare_registered_store(tmp_path, store_kind)
    )

    _assert_registered_store_is_statically_unavailable(prepared)
    assert lookup.names == [store.name]

    reference.metadata["operator-secret"] = "mutated"
    store.name = "mutated-after-preparation"
    store.root = "mutated-after-preparation"
    result = queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release"]
    assert registry.prepared_targets == []
    assert registry.parameters == []
    assert registry.local_targets == []
    assert registry.http_targets == []
    assert registry.rclone_targets == []
    assert registry.git_targets == []
    assert result["status"] == "unresolved"
    assert result["source_system"] == "store"
    assert result["uri"] == "store://[redacted]"
    assert result["expected_hash"] == reference.content_hash
    assert result["observed_hash"] is None
    assert result["content_base64"] is None
    assert result["returned_bytes"] == 0
    assert result["detail"] == "Store artifact could not be resolved."
    assert "operator-secret" not in str(result)
    with pytest.raises(FrozenInstanceError):
        prepared.target.detail = "changed"  # type: ignore[misc]


def test_selected_store_definition_is_not_read_while_use_gate_is_closed():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="sealed-store",
        locator="secret.bin",
        content_hash=_sha256(b"ok"),
    )

    class SelectedStoreWithoutTargetAccess:
        store_id = uuid4()
        name = "sealed-store"

        def __getattr__(self, field: str):
            raise AssertionError(f"registered-store target field was read: {field}")

    lookup = _DataStoreLookup()
    lookup.store = SelectedStoreWithoutTargetAccess()  # type: ignore[assignment]
    registry_calls: list[str] = []
    registry = _ResolverRegistry(registry_calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: registry_calls.append("release"),
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
    result = queries.resolve_prepared_external_artifact(prepared)

    _assert_registered_store_is_statically_unavailable(prepared)
    assert lookup.names == ["sealed-store"]
    assert registry_calls == ["release"]
    assert registry.prepared_targets == []
    assert result["detail"] == "Store artifact could not be resolved."


def test_missing_registered_store_preserves_exact_candidate_lookup_and_fails_opaquely():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="missing-store",
        locator="secret.bin",
        content_hash=_sha256(b"ok"),
    )
    lookup = _DataStoreLookup()
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

    _assert_registered_store_is_statically_unavailable(prepared)
    assert lookup.names == ["missing-store"]


@pytest.mark.parametrize("selected_name", ("legacy%name", "legacy%25name"))
def test_uri_candidate_fallback_keeps_unique_selection_but_never_builds_a_target(
    selected_name: str,
):
    project_id = uuid4()
    reference = ExternalArtifactReference(
        source_system="store",
        uri="store://legacy%25name/artifact.bin",
        content_hash=_sha256(b"ok"),
    )
    store = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name=selected_name,
        kind=StoreKind.HTTP,
        root="https://operator-secret.example/base",
    )
    lookup = _ExactDataStoreLookup(store)
    registry_calls: list[str] = []
    registry = _ResolverRegistry(registry_calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(lookup),  # type: ignore[arg-type]
        session=object(),
        release_read_scope=lambda: registry_calls.append("release"),
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

    assert lookup.names == ["legacy%name", "legacy%25name"]
    _assert_registered_store_is_statically_unavailable(prepared)
    assert registry_calls == ["release"]
    assert registry.prepared_targets == []
    assert result["detail"] == "Store artifact could not be resolved."


def test_distinct_uri_candidate_matches_remain_ambiguously_invalid():
    project_id = uuid4()
    reference = ExternalArtifactReference(
        source_system="store",
        uri="store://legacy%25name/artifact.bin",
        content_hash=_sha256(b"ok"),
    )
    stores = [
        DataStore(
            store_id=uuid4(),
            project_id=project_id,
            name=name,
            kind=StoreKind.HTTP,
            root="https://operator-secret.example/base",
        )
        for name in ("legacy%name", "legacy%25name")
    ]
    lookup = _ExactDataStoreLookup(*stores)
    registry_calls: list[str] = []
    registry = _ResolverRegistry(registry_calls)
    queries = ContextQueries(
        api=_ContextApi(project_id=project_id, reference=reference),
        repository=_ContextRepository(lookup),  # type: ignore[arg-type]
        session=object(),
        release_read_scope=lambda: registry_calls.append("release"),
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

    assert lookup.names == ["legacy%name", "legacy%25name"]
    assert type(prepared.target) is ResolvedArtifact
    assert prepared.target.detail == "Store artifact reference is invalid."
    assert registry_calls == ["release"]
    assert registry.prepared_targets == []
    assert result["detail"] == "Store artifact reference is invalid."
    assert result["uri"] == "store://[redacted]"


def test_mismatched_lookup_row_is_treated_as_unavailable():
    project_id = uuid4()
    reference = ExternalArtifactReference.for_store(
        store_name="project-store",
        locator="secret.bin",
        content_hash=_sha256(b"ok"),
    )
    wrong_row = DataStore(
        store_id=uuid4(),
        project_id=project_id,
        name="group-fallback-store",
        kind=StoreKind.HTTP,
        root="https://operator-secret.example/base",
    )
    lookup = _DataStoreLookup(wrong_row)
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

    assert lookup.names == ["project-store"]
    _assert_registered_store_is_statically_unavailable(prepared)


def test_prepared_fail_closed_result_is_single_use_and_ignores_public_replacement(
    tmp_path,
):
    queries, prepared, calls, registry, _, _, _ = _prepare_registered_store(
        tmp_path,
        StoreKind.HTTP,
    )
    object.__setattr__(
        prepared,
        "target",
        ExternalArtifactReference(
            source_system="http",
            uri="https://operator-secret.example/private",
            content_hash=_sha256(b"secret"),
        ),
    )

    accepted = queries.resolve_prepared_external_artifact(prepared)
    replay = queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release", "release"]
    assert registry.prepared_targets == []
    assert accepted["source_system"] == "store"
    assert accepted["detail"] == "Store artifact could not be resolved."
    assert replay["source_system"] == "artifact"
    assert replay["expected_hash"] == "unavailable"
    assert "operator-secret" not in str(accepted)
    assert "operator-secret" not in str(replay)


def test_prepared_fail_closed_result_has_one_concurrent_record_consumer(tmp_path):
    queries, prepared, calls, registry, _, _, _ = _prepare_registered_store(
        tmp_path,
        StoreKind.RCLONE,
    )
    start = Barrier(2)

    def resolve() -> dict[str, object]:
        start.wait()
        return queries.resolve_prepared_external_artifact(prepared)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            future.result()
            for future in (executor.submit(resolve), executor.submit(resolve))
        ]

    assert sorted(result["source_system"] for result in results) == [
        "artifact",
        "store",
    ]
    assert {result["status"] for result in results} == {"unresolved"}
    assert calls == ["release", "release"]
    assert registry.prepared_targets == []


@pytest.mark.parametrize(
    ("max_bytes", "byte_start", "byte_end", "message"),
    (
        (True, None, None, "max_bytes must be an integer"),
        (0, None, None, f"max_bytes must be between 1 and {MAX_INLINE_ARTIFACT_BYTES}"),
        (
            None,
            -1,
            1,
            f"byte_start must be between 0 and {MAX_ARTIFACT_BYTE_OFFSET}",
        ),
        (
            None,
            2,
            1,
            "byte_end must be greater than or equal to byte_start",
        ),
    ),
)
def test_invalid_content_bounds_fail_before_entity_or_store_lookup(
    max_bytes,
    byte_start,
    byte_end,
    message,
):
    project_id, reference, store = _http_store_fixture()
    api = _ContextApi(project_id=project_id, reference=reference)
    lookup = _DataStoreLookup(store)
    queries = ContextQueries(
        api=api,
        repository=_ContextRepository(lookup),
        session=object(),
        release_read_scope=lambda: None,
    )

    with pytest.raises(ValidationError, match=message):
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


def test_issued_bounds_are_inert_for_static_unavailable_result(tmp_path):
    queries, prepared, calls, registry, _, _, _ = _prepare_registered_store(
        tmp_path,
        StoreKind.GIT,
    )

    assert prepared.max_bytes == 64
    object.__setattr__(prepared, "max_bytes", MAX_INLINE_ARTIFACT_BYTES)
    object.__setattr__(prepared, "byte_range", (2, 1))
    result = queries.resolve_prepared_external_artifact(prepared)

    assert result["status"] == "unresolved"
    assert result["content_base64"] is None
    assert calls == ["release"]
    assert registry.prepared_targets == []


def test_scope_release_base_exception_prevents_result_and_resolver_work(tmp_path):
    calls: list[str] = []

    def interrupt_release() -> None:
        calls.append("release")
        raise KeyboardInterrupt("request cancelled")

    queries, prepared, _, registry, _, _, _ = _prepare_registered_store(
        tmp_path,
        StoreKind.HTTP,
        release_read_scope=interrupt_release,
    )

    with pytest.raises(KeyboardInterrupt, match="request cancelled"):
        queries.resolve_prepared_external_artifact(prepared)

    assert calls == ["release"]
    assert registry.prepared_targets == []
