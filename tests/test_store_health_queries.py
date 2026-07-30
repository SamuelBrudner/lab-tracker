from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from lab_tracker.application.store_health_queries import StoreHealthQueries
from lab_tracker.auth import AuthContext, Role
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.errors import OpaqueTargetNotFoundError
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.rclone_store_definition import RCLONE_BACKED_STORE_KINDS
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    GroupStoreScope,
    ProjectStoreScope,
    StoreAuthorityRegistry,
)
from lab_tracker.store_health import (
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    CachedStoreHealthProbe,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)


class _StoreHealthAccess:
    def __init__(
        self,
        *,
        store: DataStore | None = None,
        error: BaseException | None = None,
        events: list[str],
    ) -> None:
        self.store = store
        self.error = error
        self.events = events
        self.calls: list[tuple[UUID, AuthContext | None]] = []

    def get_data_store_for_read(
        self,
        store_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> DataStore:
        self.events.append("authorize")
        self.calls.append((store_id, actor))
        if self.error is not None:
            raise self.error
        assert self.store is not None
        return self.store


class _StoreHealthChecker:
    def __init__(
        self,
        *,
        health: StoreHealth,
        events: list[str],
        error: BaseException | None = None,
    ) -> None:
        self.health = health
        self.events = events
        self.error = error
        self.targets: list[StoreProbeTarget] = []

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        self.events.append("check")
        self.targets.append(target)
        if self.error is not None:
            raise self.error
        return self.health


class _RecordingProvider:
    def __init__(
        self,
        registry: StoreAuthorityRegistry,
        *,
        events: list[str],
        next_registry: StoreAuthorityRegistry | None = None,
    ) -> None:
        self.registry = registry
        self.next_registry = next_registry
        self.events = events
        self.calls = 0

    def __call__(self) -> StoreAuthorityRegistry:
        self.events.append("snapshot")
        self.calls += 1
        snapshot = self.registry
        if self.next_registry is not None:
            self.registry = self.next_registry
            self.next_registry = None
        return snapshot


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.VIEWER)


def _definition_for_kind(kind: StoreKind) -> ValidatedDataStoreDefinition:
    if kind is StoreKind.HTTP:
        return ValidatedDataStoreDefinition.create(
            name="analysis-http",
            kind=kind,
            root="https://files.example.test/data/",
        )
    if kind is StoreKind.GIT:
        return ValidatedDataStoreDefinition.create(
            name="analysis-git",
            kind=kind,
            root="ssh://git@example.test/org/repository",
        )
    if kind in RCLONE_BACKED_STORE_KINDS:
        return ValidatedDataStoreDefinition.create(
            name="analysis-rclone",
            kind=kind,
            root="/approved/data",
            credential_ref="approved-remote",
        )
    raise AssertionError(f"unsupported test store kind: {kind}")


def _registry_for(
    *,
    grant_id: str,
    definition: ValidatedDataStoreDefinition,
    project_id: UUID | None = None,
    group_id: UUID | None = None,
    extra_capability: bool = False,
) -> StoreAuthorityRegistry:
    assert (project_id is None) != (group_id is None)
    scope = (
        {"project_id": str(project_id)}
        if project_id is not None
        else {"group_id": str(group_id)}
    )
    grant_capabilities = [StoreCapability.BYTES_BY_PATH.value]
    if extra_capability:
        grant_capabilities.append(StoreCapability.BYTE_RANGE.value)
    grant: dict[str, object] = {
        "grant_id": grant_id,
        "scope": scope,
        "kind": definition.kind.value,
        "root": definition.root,
        "capabilities": grant_capabilities,
    }
    if definition.kind in RCLONE_BACKED_STORE_KINDS:
        grant.update(
            {
                "remote": definition.credential_ref or definition.name,
                "credential_mode": (
                    "credential_ref"
                    if definition.credential_ref is not None
                    else "name_fallback"
                ),
            }
        )
    return StoreAuthorityRegistry.from_json(
        json.dumps(
            {
                "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
                "grants": [grant],
            },
            separators=(",", ":"),
        )
    )


def _registered_store(
    kind: StoreKind = StoreKind.HTTP,
    *,
    grant_id: str = "health-grant",
    group_scoped: bool = False,
) -> tuple[DataStore, StoreAuthorityRegistry]:
    scope_id = uuid4()
    project_id = None if group_scoped else scope_id
    group_id = scope_id if group_scoped else None
    definition = _definition_for_kind(kind)
    capabilities = [StoreCapability.BYTES_BY_PATH]
    registry = _registry_for(
        grant_id=grant_id,
        project_id=project_id,
        group_id=group_id,
        definition=definition,
    )
    registry_proof = registry.authorize(
        grant_id=grant_id,
        scope=(
            GroupStoreScope(group_id)
            if group_id is not None
            else ProjectStoreScope(project_id)
        ),
        candidate=definition,
        capabilities=capabilities,
    )
    assert registry_proof is not None
    return (
        DataStore(
            store_id=uuid4(),
            project_id=project_id,
            group_id=group_id,
            name=definition.name,
            kind=definition.kind,
            capabilities=capabilities,
            root=definition.root,
            endpoint=definition.endpoint,
            credential_ref=definition.credential_ref,
            authority_grant_id=grant_id,
            authority_grant_fingerprint=registry_proof.fingerprint,
        ),
        registry,
    )


def _query(
    *,
    store: DataStore,
    registry: StoreAuthorityRegistry,
    events: list[str],
) -> tuple[StoreHealthQueries, _StoreHealthChecker, _RecordingProvider]:
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    provider = _RecordingProvider(registry, events=events)
    return (
        StoreHealthQueries(
            api=_StoreHealthAccess(store=store, events=events),
            checker=checker,
            release_read_scope=lambda: events.append("release"),
            store_authority_snapshot_provider=provider,
        ),
        checker,
        provider,
    )


def test_remote_health_detaches_releases_revalidates_then_checks() -> None:
    source, registry = _registered_store()
    original_name = source.name
    original_root = source.root
    actor = _actor()
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY, "reachable"),
        events=events,
    )
    provider = _RecordingProvider(registry, events=events)

    def release_read_scope() -> None:
        events.append("release")
        source.name = "mutated-name"
        source.root = "not a valid registered root"
        source.authority_grant_id = "mutated-grant"
        source.authority_grant_fingerprint = f"sag-v1-sha256:{'f' * 64}"

    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=release_read_scope,
        store_authority_snapshot_provider=provider,
    )

    result = query.check(source.store_id, actor=actor)

    assert events == ["authorize", "release", "snapshot", "check"]
    assert provider.calls == 1
    assert result.store_id == source.store_id
    assert result.kind is StoreKind.HTTP
    assert result.health == StoreHealth(StoreHealthStatus.HEALTHY, "reachable")
    assert len(checker.targets) == 1
    target = checker.targets[0]
    assert target.name == original_name
    assert target.root == original_root
    assert target.authority_binding_identity is not None
    with pytest.raises(FrozenInstanceError):
        result.kind = StoreKind.GIT  # type: ignore[misc]


@pytest.mark.parametrize(
    "kind",
    [StoreKind.HTTP, StoreKind.GIT, *sorted(RCLONE_BACKED_STORE_KINDS, key=str)],
)
def test_every_remote_backed_kind_can_revalidate_and_probe(kind: StoreKind) -> None:
    source, registry = _registered_store(kind)
    events: list[str] = []
    query, checker, provider = _query(
        store=source,
        registry=registry,
        events=events,
    )

    result = query.check(source.store_id, actor=_actor())

    assert result.health.status is StoreHealthStatus.HEALTHY
    assert events == ["authorize", "release", "snapshot", "check"]
    assert provider.calls == 1
    assert checker.targets[0].kind is kind


def test_group_scoped_remote_store_revalidates_against_its_exact_scope() -> None:
    source, registry = _registered_store(group_scoped=True)
    events: list[str] = []
    query, checker, provider = _query(
        store=source,
        registry=registry,
        events=events,
    )

    result = query.check(source.store_id, actor=_actor())

    assert result.health.status is StoreHealthStatus.HEALTHY
    assert provider.calls == 1
    assert len(checker.targets) == 1
    identity = checker.targets[0].authority_binding_identity
    assert identity is not None
    assert identity.store_id == source.store_id


@pytest.mark.parametrize("store_id", [uuid4(), uuid4()])
def test_hidden_and_missing_stores_never_release_or_invoke_provider_or_checker(
    store_id: UUID,
) -> None:
    events: list[str] = []
    error = OpaqueTargetNotFoundError("Data store does not exist.")
    access = _StoreHealthAccess(error=error, events=events)
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    provider = _RecordingProvider(
        StoreAuthorityRegistry.deny_all(),
        events=events,
    )
    query = StoreHealthQueries(
        api=access,
        checker=checker,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=provider,
    )

    with pytest.raises(
        OpaqueTargetNotFoundError,
        match=r"^Data store does not exist\.$",
    ):
        query.check(store_id, actor=_actor())

    assert events == ["authorize"]
    assert provider.calls == 0
    assert checker.targets == []


def test_scope_release_base_exception_prevents_provider_and_checker_work() -> None:
    source, registry = _registered_store()
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    provider = _RecordingProvider(registry, events=events)

    def interrupt_release() -> None:
        events.append("release")
        raise KeyboardInterrupt("request cancelled")

    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=interrupt_release,
        store_authority_snapshot_provider=provider,
    )

    with pytest.raises(KeyboardInterrupt, match="request cancelled"):
        query.check(source.store_id, actor=_actor())

    assert events == ["authorize", "release"]
    assert provider.calls == 0
    assert checker.targets == []


def test_checker_base_exception_is_preserved_after_successful_revalidation() -> None:
    source, registry = _registered_store()
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
        error=KeyboardInterrupt("probe cancelled"),
    )
    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=_RecordingProvider(
            registry,
            events=events,
        ),
    )

    with pytest.raises(KeyboardInterrupt, match="probe cancelled"):
        query.check(source.store_id, actor=_actor())

    assert events == ["authorize", "release", "snapshot", "check"]
    assert len(checker.targets) == 1


def test_snapshot_provider_base_exception_is_preserved_after_scope_release() -> None:
    source, _registry = _registered_store()
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )

    def interrupt_snapshot() -> StoreAuthorityRegistry:
        events.append("snapshot")
        raise KeyboardInterrupt("snapshot cancelled")

    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=interrupt_snapshot,
    )

    with pytest.raises(KeyboardInterrupt, match="snapshot cancelled"):
        query.check(source.store_id, actor=_actor())

    assert events == ["authorize", "release", "snapshot"]
    assert checker.targets == []


@pytest.mark.parametrize(
    "store",
    [
        DataStore(
            store_id=uuid4(),
            project_id=uuid4(),
            name="legacy-http",
            kind=StoreKind.HTTP,
            capabilities=[StoreCapability.BYTES_BY_PATH],
            root="https://legacy.example.test/data/",
        ),
        DataStore(
            store_id=uuid4(),
            project_id=uuid4(),
            name="invalid-http",
            kind=StoreKind.HTTP,
            capabilities=[StoreCapability.BYTES_BY_PATH],
            root="not a URL",
            authority_grant_id="health-grant",
            authority_grant_fingerprint=f"sag-v1-sha256:{'a' * 64}",
        ),
        DataStore(
            store_id=uuid4(),
            project_id=uuid4(),
            name="local-store",
            kind=StoreKind.LOCAL_FS,
            capabilities=[StoreCapability.BYTES_BY_PATH],
            root="/not-opened",
        ),
        DataStore(
            store_id=uuid4(),
            project_id=uuid4(),
            name="database-store",
            kind=StoreKind.DATABASE,
            capabilities=[StoreCapability.QUERY],
            root="opaque-database-reference",
        ),
    ],
    ids=["legacy", "invalid", "local", "unsupported-kind"],
)
def test_legacy_invalid_local_and_static_paths_never_snapshot_or_construct_target(
    store: DataStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    provider = _RecordingProvider(
        StoreAuthorityRegistry.deny_all(),
        events=events,
    )

    def reject_target_construction(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("denied health path constructed a probe target")

    monkeypatch.setattr(
        StoreProbeTarget,
        "from_authority_proof",
        classmethod(reject_target_construction),
    )
    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=store, events=events),
        checker=checker,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=provider,
    )

    result = query.check(store.store_id, actor=_actor())

    assert events == ["authorize", "release"]
    assert provider.calls == 0
    assert checker.targets == []
    assert result.health == StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    )


@pytest.mark.parametrize("revocation", ["missing", "changed"])
def test_revoked_or_changed_grant_fails_closed_before_target_or_checker(
    revocation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, original_registry = _registered_store()
    assert source.project_id is not None
    changed_registry = (
        StoreAuthorityRegistry.deny_all()
        if revocation == "missing"
        else _registry_for(
            grant_id=source.authority_grant_id or "",
            project_id=source.project_id,
            definition=_definition_for_kind(source.kind),
            extra_capability=True,
        )
    )
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    provider = _RecordingProvider(changed_registry, events=events)

    def reject_target_construction(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("revoked health path constructed a probe target")

    monkeypatch.setattr(
        StoreProbeTarget,
        "from_authority_proof",
        classmethod(reject_target_construction),
    )
    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=provider,
    )

    result = query.check(source.store_id, actor=_actor())

    assert original_registry.grant_count == 1
    assert provider.calls == 1
    assert events == ["authorize", "release", "snapshot"]
    assert checker.targets == []
    assert result.health == StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    )


def test_each_request_uses_one_point_in_time_snapshot() -> None:
    source, registry = _registered_store()
    events: list[str] = []
    provider = _RecordingProvider(
        registry,
        events=events,
        next_registry=StoreAuthorityRegistry.deny_all(),
    )
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=provider,
    )

    first = query.check(source.store_id, actor=_actor())
    second = query.check(source.store_id, actor=_actor())

    assert first.health.status is StoreHealthStatus.HEALTHY
    assert second.health == StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    )
    assert provider.calls == 2
    assert len(checker.targets) == 1
    assert events == [
        "authorize",
        "release",
        "snapshot",
        "check",
        "authorize",
        "release",
        "snapshot",
    ]


def test_warm_probe_cache_cannot_bypass_per_request_revalidation() -> None:
    source, registry = _registered_store()
    events: list[str] = []
    raw_probe = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    cached_probe = CachedStoreHealthProbe(raw_probe)
    provider = _RecordingProvider(registry, events=events)
    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=cached_probe,
        release_read_scope=lambda: events.append("release"),
        store_authority_snapshot_provider=provider,
    )

    first = query.check(source.store_id, actor=_actor())
    provider.registry = StoreAuthorityRegistry.deny_all()
    second = query.check(source.store_id, actor=_actor())

    assert first.health.status is StoreHealthStatus.HEALTHY
    assert second.health == StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    )
    assert provider.calls == 2
    assert len(raw_probe.targets) == 1
    assert cached_probe.entry_count == 1
