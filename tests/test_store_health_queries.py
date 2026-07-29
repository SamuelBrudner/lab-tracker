from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from lab_tracker.application.store_health_queries import StoreHealthQueries
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import OpaqueTargetNotFoundError
from lab_tracker.models import DataStore, StoreKind
from lab_tracker.store_health import (
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
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


def _store() -> DataStore:
    return DataStore(
        store_id=uuid4(),
        project_id=uuid4(),
        name="analysis-http",
        kind=StoreKind.HTTP,
        root="https://files.example.test/original",
        endpoint="https://cdn.example.test/original",
        credential_ref="http-credential",
    )


def _actor() -> AuthContext:
    return AuthContext(user_id=uuid4(), role=Role.VIEWER)


def test_store_health_query_authorizes_snapshots_releases_then_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _store()
    actor = _actor()
    events: list[str] = []
    access = _StoreHealthAccess(store=source, events=events)
    expected_health = StoreHealth(StoreHealthStatus.HEALTHY)
    checker = _StoreHealthChecker(health=expected_health, events=events)

    def reject_target_construction(
        _target_type: type[StoreProbeTarget],
        _store: DataStore,
    ) -> StoreProbeTarget:
        raise AssertionError("fail-closed health constructed a probe target")

    monkeypatch.setattr(
        StoreProbeTarget,
        "from_store",
        classmethod(reject_target_construction),
    )

    def release_read_scope() -> None:
        events.append("release")
        source.name = "mutated-name"
        source.root = "https://mutated.example.test/root"
        source.endpoint = "https://mutated.example.test/endpoint"
        source.credential_ref = "mutated-credential"

    query = StoreHealthQueries(
        api=access,
        checker=checker,
        release_read_scope=release_read_scope,
    )

    result = query.check(source.store_id, actor=actor)

    assert events == ["authorize", "release"]
    assert access.calls == [(source.store_id, actor)]
    assert checker.targets == []
    assert result.store_id == source.store_id
    assert result.kind is StoreKind.HTTP
    assert result.health == StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    )
    with pytest.raises(FrozenInstanceError):
        result.kind = StoreKind.GIT  # type: ignore[misc]


@pytest.mark.parametrize("store_id", [uuid4(), uuid4()])
def test_hidden_and_missing_stores_never_release_or_invoke_checker(
    store_id: UUID,
) -> None:
    events: list[str] = []
    error = OpaqueTargetNotFoundError("Data store does not exist.")
    access = _StoreHealthAccess(error=error, events=events)
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    query = StoreHealthQueries(
        api=access,
        checker=checker,
        release_read_scope=lambda: events.append("release"),
    )

    with pytest.raises(
        OpaqueTargetNotFoundError,
        match=r"^Data store does not exist\.$",
    ):
        query.check(store_id, actor=_actor())

    assert events == ["authorize"]
    assert checker.targets == []


def test_scope_release_base_exception_prevents_checker_work() -> None:
    source = _store()
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )

    def interrupt_release() -> None:
        events.append("release")
        raise KeyboardInterrupt("request cancelled")

    query = StoreHealthQueries(
        api=_StoreHealthAccess(store=source, events=events),
        checker=checker,
        release_read_scope=interrupt_release,
    )

    with pytest.raises(KeyboardInterrupt, match="request cancelled"):
        query.check(source.store_id, actor=_actor())

    assert events == ["authorize", "release"]
    assert checker.targets == []


def test_checker_base_exception_is_never_reached_after_scope_release() -> None:
    source = _store()
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
    )

    result = query.check(source.store_id, actor=_actor())

    assert events == ["authorize", "release"]
    assert checker.targets == []
    assert result.health.status is StoreHealthStatus.UNSUPPORTED
    assert result.health.detail == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE


def test_every_authorized_store_returns_the_same_static_safe_health_result() -> None:
    first = _store()
    second = DataStore(
        store_id=uuid4(),
        project_id=uuid4(),
        name="secret-local-name",
        kind=StoreKind.LOCAL_FS,
        root="/secret/local/root",
        endpoint=None,
        credential_ref="secret-local-credential",
    )
    events: list[str] = []
    checker = _StoreHealthChecker(
        health=StoreHealth(StoreHealthStatus.HEALTHY),
        events=events,
    )
    access = _StoreHealthAccess(store=first, events=events)
    query = StoreHealthQueries(
        api=access,
        checker=checker,
        release_read_scope=lambda: events.append("release"),
    )

    first_result = query.check(first.store_id, actor=_actor())
    access.store = second
    second_result = query.check(second.store_id, actor=_actor())

    assert first_result.health is second_result.health
    assert first_result.health.to_json_dict() == {
        "status": "unsupported",
        "detail": STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    }
    assert events == ["authorize", "release", "authorize", "release"]
    assert checker.targets == []
