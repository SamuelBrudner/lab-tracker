from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from lab_tracker.application.context_queries import ContextQueries
from lab_tracker.artifact_resolution import ResolutionStatus, ResolvedArtifact
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import ExternalArtifactReference


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
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_by_name(self, _project_id: UUID, _name: str):
        self.calls.append("store")
        return None


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
