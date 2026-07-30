from __future__ import annotations

import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, replace
from uuid import UUID

import pytest
from store_authority_fakes import bound_authority_proof

from lab_tracker.artifact_resolution import check_store_health
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    ProjectStoreScope,
    StoreAuthorityRegistry,
)
from lab_tracker.store_authority_use import (
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)
from lab_tracker.store_health import (
    MAX_STORE_HEALTH_CACHE_MAX_ENTRIES,
    MAX_STORE_HEALTH_CACHE_TTL_SECONDS,
    MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    CachedStoreHealthProbe,
    StoreHealth,
    StoreHealthProbeUnavailable,
    StoreHealthStatus,
    StoreProbeTarget,
)


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self._now = now
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def set(self, now: float) -> None:
        with self._lock:
            self._now = now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class _RecordingProbe:
    def __init__(
        self,
        result: StoreHealth | None = None,
    ) -> None:
        self.result = result or StoreHealth(StoreHealthStatus.HEALTHY)
        self.targets: list[StoreProbeTarget] = []
        self._lock = threading.Lock()

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        with self._lock:
            self.targets.append(target)
        return self.result


class _ObservedCache(CachedStoreHealthProbe):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.follower_waiting = threading.Event()

    def _await_follower(self, future: Future[StoreHealth]) -> StoreHealth:
        self.follower_waiting.set()
        return super()._await_follower(future)


def _target(number: int = 1) -> StoreProbeTarget:
    proof = bound_authority_proof(
        store_id=UUID(int=number),
        name=f"store-{number}",
        kind=StoreKind.HTTP,
        root=f"https://store-{number}.example/data/",
        project_id=UUID(int=10_000 + number),
    )
    return StoreProbeTarget.from_authority_proof(proof)


def _proof_backed_target(*, grant_id: str) -> StoreProbeTarget:
    project_id = UUID(int=700)
    store_id = UUID(int=701)
    definition = ValidatedDataStoreDefinition.create(
        name="proof-backed",
        kind=StoreKind.HTTP,
        root="https://proof.example.test/data/",
    )
    capabilities = [StoreCapability.BYTES_BY_PATH]
    registry = StoreAuthorityRegistry.from_json(
        json.dumps(
            {
                "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
                "grants": [
                    {
                        "grant_id": grant_id,
                        "scope": {"project_id": str(project_id)},
                        "kind": StoreKind.HTTP.value,
                        "root": definition.root,
                        "capabilities": [
                            StoreCapability.BYTES_BY_PATH.value,
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        )
    )
    registry_proof = registry.authorize(
        grant_id=grant_id,
        scope=ProjectStoreScope(project_id),
        candidate=definition,
        capabilities=capabilities,
    )
    assert registry_proof is not None
    store = DataStore(
        store_id=store_id,
        project_id=project_id,
        name=definition.name,
        kind=definition.kind,
        capabilities=capabilities,
        root=definition.root,
        endpoint=definition.endpoint,
        credential_ref=definition.credential_ref,
        authority_grant_id=grant_id,
        authority_grant_fingerprint=registry_proof.fingerprint,
    )
    binding = detach_store_authority_binding(store)
    assert binding is not None
    use_proof = revalidate_store_authority_binding(registry, binding)
    assert use_proof is not None
    return StoreProbeTarget.from_authority_proof(use_proof)


def test_probe_target_has_exact_immutable_hashable_snapshot_fields() -> None:
    target = _target()
    matching = _target()

    assert [field.name for field in fields(StoreProbeTarget)] == [
        "store_id",
        "name",
        "kind",
        "root",
        "endpoint",
        "credential_ref",
        "authority_binding_identity",
    ]
    assert target == matching
    assert hash(target) == hash(matching)
    assert not hasattr(target, "__dict__")
    with pytest.raises(FrozenInstanceError):
        target.name = "changed"  # type: ignore[misc]


def test_probe_target_detaches_only_probe_fields_from_the_authority_proof() -> None:
    proof = bound_authority_proof(
        name="archive",
        kind=StoreKind.S3,
        root="/root",
        credential_ref="archive-remote",
        project_id=UUID(int=8),
        store_id=UUID(int=7),
    )

    target = StoreProbeTarget.from_authority_proof(proof)

    assert target.store_id == UUID(int=7)
    assert target.name == "archive"
    assert target.kind is StoreKind.S3
    assert target.root == "/root"
    assert target.endpoint is None
    assert target.credential_ref == "archive-remote"
    assert target.authority_binding_identity is proof.binding_identity
    for persistence_only in ("project_id", "group_id", "is_default", "created_by"):
        assert not hasattr(target, persistence_only)


def test_probe_target_exposes_only_the_authority_proof_factory() -> None:
    """Keep every normal construction path bound to a complete use-time proof.

    ``from_store`` built a probe target straight from a registered row, with no
    proof that the operator still authorizes it. The dataclass constructor is
    deliberately incompatible with the target fields as well, so a sealed
    identity cannot be copied onto unrelated adapter inputs.
    """

    factories = {
        name
        for name, attribute in vars(StoreProbeTarget).items()
        if isinstance(attribute, classmethod)
    }
    assert factories == {"from_authority_proof"}


def test_probe_target_cannot_be_directly_built_or_retargeted() -> None:
    proof = bound_authority_proof(
        name="archive",
        kind=StoreKind.S3,
        root="/root",
        credential_ref="archive-remote",
        project_id=UUID(int=8),
        store_id=UUID(int=7),
    )
    target = StoreProbeTarget.from_authority_proof(proof)
    probe_fields: dict[str, object] = {
        "store_id": UUID(int=7),
        "name": "archive",
        "kind": StoreKind.S3,
        "root": "/root",
        "endpoint": None,
        "credential_ref": "archive-remote",
        "authority_binding_identity": proof.binding_identity,
    }

    with pytest.raises(TypeError):
        StoreProbeTarget(**probe_fields)  # type: ignore[arg-type]
    with pytest.raises(
        TypeError,
        match=r"^Store probe target requires an authority use proof\.$",
    ):
        StoreProbeTarget(proof, _factory_token=object())
    with pytest.raises(TypeError):
        replace(target)
    with pytest.raises(TypeError):
        replace(
            target,
            kind=StoreKind.GIT,
            root="ssh://git@unapproved.example/repository",
        )


def test_probe_target_factory_carries_authority_identity_and_redacts_repr() -> None:
    target = _proof_backed_target(grant_id="secret-grant-a")

    assert target.authority_binding_identity is not None
    rendered = repr(target)
    assert "secret-grant-a" not in rendered
    assert "sag-v1-sha256:" not in rendered
    assert "https://proof.example.test/data/" not in rendered
    # Boundness is now a type invariant rather than a repr flag: every probe
    # target carries a sealed identity, so the repr shows only classification.
    assert rendered == (
        f"StoreProbeTarget(store_id={target.store_id!r}, kind={target.kind!r})"
    )


def test_probe_target_factory_rejects_unsealed_proof_like_objects() -> None:
    class FakeProof:
        store_id = UUID(int=701)
        definition = object()
        binding_identity = object()

    with pytest.raises(
        TypeError,
        match=r"^Store probe target requires an authority use proof\.$",
    ):
        StoreProbeTarget.from_authority_proof(FakeProof())  # type: ignore[arg-type]


def test_store_health_is_immutable_hashable_and_serializes_canonically() -> None:
    result = StoreHealth(StoreHealthStatus.UNREACHABLE, "not reachable")

    assert result.is_healthy is False
    assert result.to_json_dict() == {
        "status": "unreachable",
        "detail": "not reachable",
    }
    assert hash(result) == hash(StoreHealth(StoreHealthStatus.UNREACHABLE, "not reachable"))
    with pytest.raises(FrozenInstanceError):
        result.detail = "changed"  # type: ignore[misc]


def test_legacy_adapter_accepts_the_detached_structural_target() -> None:
    store = DataStore(
        store_id=UUID(int=55),
        project_id=UUID(int=56),
        name="database-reference",
        kind=StoreKind.DATABASE,
        root="database-reference",
        capabilities=[StoreCapability.QUERY],
    )

    assert check_store_health(store) == StoreHealth(
        StoreHealthStatus.UNSUPPORTED,
        "Health checks for 'database' stores are not supported yet.",
    )


@pytest.mark.parametrize(
    ("keyword", "value", "expected_error"),
    [
        ("max_entries", True, TypeError),
        ("max_entries", 1.0, TypeError),
        ("max_entries", 0, ValueError),
        ("max_entries", -1, ValueError),
        ("max_entries", MAX_STORE_HEALTH_CACHE_MAX_ENTRIES + 1, ValueError),
        ("ttl_seconds", True, TypeError),
        ("ttl_seconds", "1", TypeError),
        ("ttl_seconds", 0.0, ValueError),
        ("ttl_seconds", -1.0, ValueError),
        ("ttl_seconds", float("nan"), ValueError),
        ("ttl_seconds", float("inf"), ValueError),
        ("ttl_seconds", MAX_STORE_HEALTH_CACHE_TTL_SECONDS + 0.1, ValueError),
        ("singleflight_wait_seconds", True, TypeError),
        ("singleflight_wait_seconds", "1", TypeError),
        ("singleflight_wait_seconds", 0.0, ValueError),
        ("singleflight_wait_seconds", -1.0, ValueError),
        ("singleflight_wait_seconds", float("nan"), ValueError),
        ("singleflight_wait_seconds", float("inf"), ValueError),
        (
            "singleflight_wait_seconds",
            MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS + 0.1,
            ValueError,
        ),
    ],
)
def test_cache_configuration_rejects_invalid_or_unbounded_values(
    keyword: str,
    value: object,
    expected_error: type[Exception],
) -> None:
    with pytest.raises(expected_error):
        CachedStoreHealthProbe(_RecordingProbe(), **{keyword: value})  # type: ignore[arg-type]


def test_cache_configuration_accepts_each_hard_boundary() -> None:
    cache = CachedStoreHealthProbe(
        _RecordingProbe(),
        max_entries=MAX_STORE_HEALTH_CACHE_MAX_ENTRIES,
        ttl_seconds=MAX_STORE_HEALTH_CACHE_TTL_SECONDS,
        singleflight_wait_seconds=MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
    )

    assert cache.entry_count == 0
    assert cache.in_flight_count == 0
    assert cache.max_entries == MAX_STORE_HEALTH_CACHE_MAX_ENTRIES
    assert cache.ttl_seconds == MAX_STORE_HEALTH_CACHE_TTL_SECONDS
    assert (
        cache.waiter_timeout_seconds
        == MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS
    )


def test_exact_target_cache_hit_returns_same_result_and_makes_no_second_probe() -> None:
    result = StoreHealth(StoreHealthStatus.HEALTHY)
    probe = _RecordingProbe(result)
    cache = CachedStoreHealthProbe(probe)
    target = _target()

    assert cache(target) is result
    assert cache(target) is result
    assert probe.targets == [target]
    assert cache.entry_count == 1
    assert cache.in_flight_count == 0


def test_distinct_proof_bound_targets_do_not_share_cache_entries() -> None:
    probe = _RecordingProbe()
    cache = CachedStoreHealthProbe(probe)
    original = _target(1)
    changed = _target(99)

    cache(original)
    cache(changed)

    assert probe.targets == [original, changed]
    assert cache.entry_count == 2


def test_authority_binding_identity_participates_in_cache_and_flight_keys() -> None:
    first = _proof_backed_target(grant_id="grant-a")
    second = _proof_backed_target(grant_id="grant-b")
    probe = _RecordingProbe()
    cache = CachedStoreHealthProbe(probe)

    assert first.store_id == second.store_id
    assert first.name == second.name
    assert first.kind is second.kind
    assert first.root == second.root
    assert first.authority_binding_identity != second.authority_binding_identity

    cache(first)
    cache(second)

    assert probe.targets == [first, second]
    assert cache.entry_count == 2


def test_different_authority_bindings_do_not_coalesce_in_flight() -> None:
    first = _proof_backed_target(grant_id="grant-a")
    second = _proof_backed_target(grant_id="grant-b")
    both_entered = threading.Barrier(3)
    targets: list[StoreProbeTarget] = []
    targets_lock = threading.Lock()

    def probe(target: StoreProbeTarget) -> StoreHealth:
        with targets_lock:
            targets.append(target)
        both_entered.wait(timeout=10.0)
        return StoreHealth(StoreHealthStatus.HEALTHY)

    cache = CachedStoreHealthProbe(probe, singleflight_wait_seconds=2.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_probe = pool.submit(cache, first)
        second_probe = pool.submit(cache, second)
        both_entered.wait(timeout=10.0)
        assert first_probe.result(timeout=10.0).is_healthy
        assert second_probe.result(timeout=10.0).is_healthy

    assert set(targets) == {first, second}
    assert cache.entry_count == 2
    assert cache.in_flight_count == 0


def test_ttl_starts_at_probe_completion_and_expires_at_exact_boundary() -> None:
    clock = _FakeClock(10.0)
    target = _target()
    calls = 0

    def probe(_: StoreProbeTarget) -> StoreHealth:
        nonlocal calls
        calls += 1
        clock.advance(7.0)
        return StoreHealth(StoreHealthStatus.HEALTHY, str(calls))

    cache = CachedStoreHealthProbe(probe, ttl_seconds=5.0, clock=clock)

    first = cache(target)
    clock.set(21.999)
    assert cache(target) is first
    clock.set(22.0)
    second = cache(target)

    assert second != first
    assert second.detail == "2"
    assert calls == 2


def test_cache_is_hard_bounded_lru_and_a_hit_promotes_to_mru() -> None:
    probe = _RecordingProbe()
    cache = CachedStoreHealthProbe(probe, max_entries=2)
    first = _target(1)
    second = _target(2)
    third = _target(3)

    cache(first)
    cache(second)
    cache(first)
    cache(third)
    assert cache.entry_count == 2

    cache(first)
    cache(second)

    assert probe.targets == [first, second, third, second]
    assert cache.entry_count == 2


def test_cache_bound_holds_when_distinct_leaders_complete_concurrently() -> None:
    leader_count = 8
    completion_barrier = threading.Barrier(leader_count + 1)

    def probe(_: StoreProbeTarget) -> StoreHealth:
        completion_barrier.wait(timeout=10.0)
        return StoreHealth(StoreHealthStatus.HEALTHY)

    cache = CachedStoreHealthProbe(probe, max_entries=3)
    with ThreadPoolExecutor(max_workers=leader_count) as pool:
        leaders = [pool.submit(cache, _target(index + 1)) for index in range(leader_count)]
        completion_barrier.wait(timeout=10.0)
        for leader in leaders:
            assert leader.result(timeout=10.0).is_healthy

    assert cache.entry_count == 3
    assert cache.in_flight_count == 0


@pytest.mark.parametrize(
    "result",
    [
        StoreHealth(StoreHealthStatus.UNREACHABLE, "expected operational result"),
        StoreHealth(StoreHealthStatus.UNSUPPORTED, "adapter unavailable"),
    ],
)
def test_legitimate_nonhealthy_results_are_cached(result: StoreHealth) -> None:
    probe = _RecordingProbe(result)
    cache = CachedStoreHealthProbe(probe)

    assert cache(_target()) is result
    assert cache(_target()) is result
    assert len(probe.targets) == 1


def test_same_key_concurrent_calls_use_one_leader_probe() -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    result = StoreHealth(StoreHealthStatus.HEALTHY)

    def probe(_: StoreProbeTarget) -> StoreHealth:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=10.0)
        return result

    cache = _ObservedCache(probe, singleflight_wait_seconds=2.0)
    target = _target()
    with ThreadPoolExecutor(max_workers=8) as pool:
        leader = pool.submit(cache, target)
        assert entered.wait(timeout=10.0)
        followers = [pool.submit(cache, target) for _ in range(7)]
        assert cache.follower_waiting.wait(timeout=10.0)
        assert cache.in_flight_count == 1
        release.set()
        observed = [leader.result(timeout=10.0)]
        observed.extend(future.result(timeout=10.0) for future in followers)

    assert observed == [result] * 8
    assert calls == 1
    assert cache.in_flight_count == 0
    assert cache.entry_count == 1


def test_different_keys_are_never_serialized_behind_a_blocked_probe() -> None:
    blocked_target = _target(1)
    fast_target = _target(2)
    entered = threading.Event()
    release = threading.Event()

    def probe(target: StoreProbeTarget) -> StoreHealth:
        if target == blocked_target:
            entered.set()
            assert release.wait(timeout=10.0)
        return StoreHealth(StoreHealthStatus.HEALTHY, target.name)

    cache = _ObservedCache(probe, singleflight_wait_seconds=2.0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        blocked = pool.submit(cache, blocked_target)
        assert entered.wait(timeout=10.0)
        follower = pool.submit(cache, blocked_target)
        assert cache.follower_waiting.wait(timeout=10.0)

        fast_result = cache(fast_target)
        assert fast_result.detail == fast_target.name
        assert cache.in_flight_count == 1

        release.set()
        assert blocked.result(timeout=10.0).detail == blocked_target.name
        assert follower.result(timeout=10.0).detail == blocked_target.name


class _ProbeExplosion(BaseException):
    pass


class _CompletionClockExplosion(BaseException):
    pass


def test_leader_failure_is_original_but_follower_is_sanitized_and_retryable() -> None:
    entered = threading.Event()
    release = threading.Event()
    explosion = _ProbeExplosion("secret backend detail")
    calls = 0

    def probe(_: StoreProbeTarget) -> StoreHealth:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=10.0)
            raise explosion
        return StoreHealth(StoreHealthStatus.HEALTHY)

    cache = _ObservedCache(probe, singleflight_wait_seconds=2.0)
    target = _target()
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(cache, target)
        assert entered.wait(timeout=10.0)
        follower = pool.submit(cache, target)
        assert cache.follower_waiting.wait(timeout=10.0)
        release.set()

        with pytest.raises(_ProbeExplosion) as leader_error:
            leader.result(timeout=10.0)
        with pytest.raises(StoreHealthProbeUnavailable) as follower_error:
            follower.result(timeout=10.0)

    assert leader_error.value is explosion
    assert str(follower_error.value) == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    assert follower_error.value.__cause__ is None
    assert follower_error.value.__context__ is None
    assert "secret backend detail" not in repr(follower_error.value)
    assert cache.entry_count == 0
    assert cache.in_flight_count == 0

    assert cache(target).status is StoreHealthStatus.HEALTHY
    assert calls == 2


def test_completion_clock_failure_wakes_followers_and_leaves_key_retryable() -> None:
    entered = threading.Event()
    release = threading.Event()
    clock_calls = 0
    probe_calls = 0
    explosion = _CompletionClockExplosion("secret clock detail")

    def clock() -> float:
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 3:
            raise explosion
        return float(clock_calls)

    def probe(_: StoreProbeTarget) -> StoreHealth:
        nonlocal probe_calls
        probe_calls += 1
        if probe_calls == 1:
            entered.set()
            assert release.wait(timeout=10.0)
        return StoreHealth(StoreHealthStatus.HEALTHY)

    cache = _ObservedCache(
        probe,
        clock=clock,
        singleflight_wait_seconds=2.0,
    )
    target = _target()
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(cache, target)
        assert entered.wait(timeout=10.0)
        follower = pool.submit(cache, target)
        assert cache.follower_waiting.wait(timeout=10.0)
        release.set()

        with pytest.raises(_CompletionClockExplosion) as leader_error:
            leader.result(timeout=10.0)
        with pytest.raises(StoreHealthProbeUnavailable) as follower_error:
            follower.result(timeout=10.0)

    assert leader_error.value is explosion
    assert str(follower_error.value) == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
    assert "secret clock detail" not in repr(follower_error.value)
    assert cache.entry_count == 0
    assert cache.in_flight_count == 0

    assert cache(target).status is StoreHealthStatus.HEALTHY
    assert probe_calls == 2


def test_timed_out_follower_does_not_cancel_or_remove_the_leader() -> None:
    entered = threading.Event()
    release = threading.Event()
    result = StoreHealth(StoreHealthStatus.HEALTHY)
    calls = 0

    def probe(_: StoreProbeTarget) -> StoreHealth:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=10.0)
        return result

    cache = CachedStoreHealthProbe(
        probe,
        singleflight_wait_seconds=0.01,
    )
    target = _target()
    with ThreadPoolExecutor(max_workers=1) as pool:
        leader = pool.submit(cache, target)
        assert entered.wait(timeout=10.0)

        with pytest.raises(StoreHealthProbeUnavailable) as timeout_error:
            cache(target)
        assert str(timeout_error.value) == STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE
        assert timeout_error.value.__cause__ is None
        assert timeout_error.value.__context__ is None
        assert cache.in_flight_count == 1
        assert cache.entry_count == 0

        release.set()
        assert leader.result(timeout=10.0) is result

    assert cache.in_flight_count == 0
    assert cache.entry_count == 1
    assert cache(target) is result
    assert calls == 1


def test_stale_failure_cleanup_cannot_remove_a_newer_exact_key_future() -> None:
    cache = CachedStoreHealthProbe(_RecordingProbe())
    target = _target()
    stale: Future[StoreHealth] = Future()
    current: Future[StoreHealth] = Future()
    cache._in_flight[target] = current

    cache._remove_in_flight(target, stale)
    assert cache.in_flight_count == 1

    cache._remove_in_flight(target, current)
    assert cache.in_flight_count == 0


def test_observation_counts_are_read_only() -> None:
    cache = CachedStoreHealthProbe(_RecordingProbe())

    with pytest.raises(AttributeError):
        cache.entry_count = 10  # type: ignore[misc]
    with pytest.raises(AttributeError):
        cache.in_flight_count = 10  # type: ignore[misc]
    with pytest.raises(AttributeError):
        cache.max_entries = 10  # type: ignore[misc]
    with pytest.raises(AttributeError):
        cache.ttl_seconds = 10.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        cache.waiter_timeout_seconds = 10.0  # type: ignore[misc]
