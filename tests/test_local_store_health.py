from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import FrozenInstanceError, fields
from uuid import UUID

import pytest

from lab_tracker.bounded_subprocess import (
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
)
from lab_tracker.local_filesystem_ports import LocalDirectoryInspection
from lab_tracker.local_store_health import LocalStoreHealthProbe
from lab_tracker.models import StoreKind
from lab_tracker.store_health import (
    LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FatalProbeFailure(BaseException):
    pass


class UnexpectedClock:
    def __call__(self) -> float:
        raise FatalProbeFailure(
            "a non-local target must not create a process deadline"
        )


class RecordingInspector:
    def __init__(
        self,
        outcomes: Sequence[LocalDirectoryInspection | BaseException],
        *,
        after_inspection: Callable[[], None] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self.after_inspection = after_inspection
        self.calls: list[tuple[str, ProcessDeadline]] = []

    def inspect_directory(
        self,
        candidate: str,
        *,
        deadline: ProcessDeadline,
    ) -> LocalDirectoryInspection:
        self.calls.append((candidate, deadline))
        outcome = self._outcomes.pop(0)
        if self.after_inspection is not None:
            self.after_inspection()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _target(
    root: str = "/raw/operator/store",
    *,
    kind: StoreKind = StoreKind.LOCAL_FS,
) -> StoreProbeTarget:
    return StoreProbeTarget(
        store_id=UUID(int=3),
        name="local-store",
        kind=kind,
        root=root,
        endpoint=None,
        credential_ref=None,
    )


def _probe(
    inspector: RecordingInspector,
    *,
    clock: Callable[[], float] | None = None,
    **kwargs: object,
) -> LocalStoreHealthProbe:
    return LocalStoreHealthProbe(
        inspector=inspector,
        clock=clock or FakeClock(),
        **kwargs,
    )


def _assert_static_failure(result: StoreHealth) -> None:
    assert result == StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    )
    assert result.to_json_dict() == {
        "status": "unreachable",
        "detail": LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    }


def test_probe_is_an_immutable_slotted_dependency_container() -> None:
    probe = _probe(RecordingInspector((LocalDirectoryInspection.ACCESSIBLE,)))

    assert [item.name for item in fields(LocalStoreHealthProbe)] == [
        "inspector",
        "deadline_seconds",
        "clock",
    ]
    assert not hasattr(probe, "__dict__")
    with pytest.raises(FrozenInstanceError):
        probe.deadline_seconds = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    (
        ("deadline_seconds", True, TypeError),
        ("deadline_seconds", "1", TypeError),
        ("deadline_seconds", 0.0, ValueError),
        ("deadline_seconds", -1.0, ValueError),
        ("deadline_seconds", float("nan"), ValueError),
        ("deadline_seconds", float("inf"), ValueError),
        ("deadline_seconds", MAX_PROCESS_DEADLINE_SECONDS + 1.0, ValueError),
        ("clock", None, TypeError),
    ),
)
def test_probe_rejects_invalid_or_unbounded_configuration(
    field_name: str,
    value: object,
    error_type: type[Exception],
) -> None:
    kwargs: dict[str, object] = {
        "inspector": RecordingInspector(()),
        field_name: value,
    }

    with pytest.raises(error_type):
        LocalStoreHealthProbe(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind",
    tuple(kind for kind in StoreKind if kind is not StoreKind.LOCAL_FS),
)
def test_non_local_kinds_fail_before_deadline_or_inspection(kind: StoreKind) -> None:
    inspector = RecordingInspector(())

    result = _probe(inspector, clock=UnexpectedClock())(_target(kind=kind))

    _assert_static_failure(result)
    assert inspector.calls == []


@pytest.mark.parametrize(
    ("inspection", "expected_status"),
    (
        (LocalDirectoryInspection.ACCESSIBLE, StoreHealthStatus.HEALTHY),
        (LocalDirectoryInspection.DENIED, StoreHealthStatus.UNREACHABLE),
        (LocalDirectoryInspection.FAILED, StoreHealthStatus.UNREACHABLE),
    ),
)
def test_probe_maps_only_accessible_to_healthy(
    inspection: LocalDirectoryInspection,
    expected_status: StoreHealthStatus,
) -> None:
    root = "/raw/operator/private-store"
    inspector = RecordingInspector((inspection,))

    result = _probe(inspector)(_target(root))

    assert result.status is expected_status
    assert inspector.calls[0][0] == root
    assert isinstance(inspector.calls[0][1], ProcessDeadline)
    if expected_status is StoreHealthStatus.UNREACHABLE:
        _assert_static_failure(result)
        assert root not in str(result.to_json_dict())


def test_probe_creates_deadline_before_inspector_and_passes_raw_candidate() -> None:
    clock = FakeClock()
    root = "/raw/operator/link/../store"

    class DeadlineAssertingInspector(RecordingInspector):
        def inspect_directory(
            self,
            candidate: str,
            *,
            deadline: ProcessDeadline,
        ) -> LocalDirectoryInspection:
            assert deadline.clock is clock
            assert deadline.expires_at == pytest.approx(101.5)
            assert deadline.remaining() == pytest.approx(1.5)
            return super().inspect_directory(candidate, deadline=deadline)

    inspector = DeadlineAssertingInspector(
        (LocalDirectoryInspection.ACCESSIBLE,)
    )

    result = _probe(
        inspector,
        clock=clock,
        deadline_seconds=1.5,
    )(_target(root))

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert inspector.calls[0][0] == root


def test_deadline_expiration_after_inspection_is_static() -> None:
    clock = FakeClock()
    inspector = RecordingInspector(
        (LocalDirectoryInspection.ACCESSIBLE,),
        after_inspection=lambda: clock.advance(2.0),
    )

    result = _probe(
        inspector,
        clock=clock,
        deadline_seconds=1.0,
    )(_target())

    _assert_static_failure(result)


def test_each_probe_receives_a_fresh_deadline() -> None:
    inspector = RecordingInspector(
        (
            LocalDirectoryInspection.ACCESSIBLE,
            LocalDirectoryInspection.ACCESSIBLE,
        )
    )
    probe = _probe(inspector)

    assert probe(_target()) == StoreHealth(StoreHealthStatus.HEALTHY)
    assert probe(_target()) == StoreHealth(StoreHealthStatus.HEALTHY)

    assert inspector.calls[0][1] is not inspector.calls[1][1]


def test_ordinary_inspector_failure_is_static_and_redacted() -> None:
    root = "/raw/operator/secret-store"
    inspector = RecordingInspector(
        (RuntimeError("private local path diagnostic"),)
    )

    result = _probe(inspector)(_target(root))

    _assert_static_failure(result)
    assert root not in str(result.to_json_dict())
    assert "private local path diagnostic" not in str(result.to_json_dict())


@pytest.mark.parametrize(
    "failure",
    (
        KeyboardInterrupt("operator interruption"),
        SystemExit("operator exit"),
        FatalProbeFailure("fatal probe failure"),
    ),
    ids=("keyboard-interrupt", "system-exit", "custom-base-exception"),
)
def test_base_exception_propagates_unchanged(failure: BaseException) -> None:
    inspector = RecordingInspector((failure,))

    with pytest.raises(type(failure)) as caught:
        _probe(inspector)(_target())

    assert caught.value is failure
