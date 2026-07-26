from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, fields
from os import PathLike
from typing import Any
from uuid import UUID

import pytest

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
    ProcessResult,
    StdoutConsumer,
)
from lab_tracker.models import StoreKind
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_health import (
    RCLONE_HEALTH_OUTPUT_LIMIT_BYTES,
    RcloneStoreHealthProbe,
)
from lab_tracker.store_health import (
    RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

_RCLONE_BACKED_KINDS = (
    StoreKind.SSH,
    StoreKind.S3,
    StoreKind.GCS,
    StoreKind.AZURE_BLOB,
    StoreKind.DROPBOX,
    StoreKind.GDRIVE,
    StoreKind.BOX,
    StoreKind.ONEDRIVE,
    StoreKind.RCLONE,
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


class RecordingExecutor:
    def __init__(
        self,
        outcomes: Sequence[ProcessResult | BaseException],
        *,
        after_run: Callable[[], None] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self.after_run = after_run
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        command: Sequence[str],
        *,
        deadline: ProcessDeadline,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        stdout_consumer: StdoutConsumer | None = None,
        cwd: str | PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        self.calls.append(
            {
                "command": list(command),
                "deadline": deadline,
                "stdout_limit_bytes": stdout_limit_bytes,
                "stderr_limit_bytes": stderr_limit_bytes,
                "stdout_consumer": stdout_consumer,
                "cwd": cwd,
                "env": env,
            }
        )
        outcome = self._outcomes.pop(0)
        if self.after_run is not None:
            self.after_run()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _result(
    returncode: int = 0,
    *,
    stdout: bytes = b"",
    stdout_bytes: int | None = None,
    stderr_bytes: int = 0,
) -> ProcessResult:
    return ProcessResult(
        returncode=returncode,
        stdout=stdout,
        stdout_bytes=len(stdout) if stdout_bytes is None else stdout_bytes,
        stderr_bytes=stderr_bytes,
    )


def _target(
    *,
    kind: StoreKind = StoreKind.RCLONE,
    name: str = "fallback",
    root: str = "experiments",
    credential_ref: str | None = "lab-remote",
) -> StoreProbeTarget:
    return StoreProbeTarget(
        store_id=UUID(int=1),
        name=name,
        kind=kind,
        root=root,
        endpoint=None,
        credential_ref=credential_ref,
    )


def _probe(
    executor: RecordingExecutor,
    *,
    grants: str = "lab-remote",
    clock: FakeClock | None = None,
    **kwargs: object,
) -> RcloneStoreHealthProbe:
    return RcloneStoreHealthProbe(
        policy=RcloneRemotePolicy.from_config(grants),
        executor=executor,
        clock=clock or FakeClock(),
        **kwargs,
    )


def _assert_static_failure(result: StoreHealth) -> None:
    assert result == StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    )
    assert result.to_json_dict() == {
        "status": "unreachable",
        "detail": RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    }


def test_rclone_probe_is_an_immutable_slotted_dependency_container() -> None:
    probe = _probe(RecordingExecutor((_result(),)))

    assert [item.name for item in fields(RcloneStoreHealthProbe)] == [
        "policy",
        "executor",
        "binary",
        "deadline_seconds",
        "clock",
    ]
    assert not hasattr(probe, "__dict__")
    with pytest.raises(FrozenInstanceError):
        probe.binary = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    (
        ("binary", None, TypeError),
        ("binary", "", ValueError),
        ("binary", "rclone\0other", ValueError),
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
def test_rclone_probe_rejects_invalid_or_unbounded_configuration(
    field_name: str,
    value: object,
    error_type: type[Exception],
) -> None:
    kwargs: dict[str, object] = {
        "policy": RcloneRemotePolicy.deny_all(),
        "executor": RecordingExecutor(()),
        field_name: value,
    }

    with pytest.raises(error_type):
        RcloneStoreHealthProbe(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind",
    tuple(kind for kind in StoreKind if kind not in _RCLONE_BACKED_KINDS),
)
def test_non_rclone_store_kinds_fail_without_process(kind: StoreKind) -> None:
    executor = RecordingExecutor(())

    result = _probe(executor)(_target(kind=kind))

    _assert_static_failure(result)
    assert executor.calls == []


@pytest.mark.parametrize("kind", _RCLONE_BACKED_KINDS)
def test_every_rclone_backed_kind_uses_the_bounded_probe(kind: StoreKind) -> None:
    executor = RecordingExecutor((_result(),))

    result = _probe(executor)(_target(kind=kind))

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("target", "grants"),
    (
        (_target(credential_ref="missing"), "lab-remote"),
        (_target(credential_ref=""), "fallback"),
        (_target(credential_ref=" bad"), "fallback"),
        (_target(credential_ref=None, name=""), "lab-remote"),
        (_target(root=""), "lab-remote"),
        (_target(root="relative//double"), "lab-remote"),
        (_target(root="../escape"), "lab-remote"),
        (_target(root="/CON"), "lab-remote"),
        (_target(root="/tab\tpath"), "lab-remote"),
    ),
)
def test_invalid_unlisted_or_blank_target_fails_without_process(
    target: StoreProbeTarget,
    grants: str,
) -> None:
    executor = RecordingExecutor(())

    result = _probe(executor, grants=grants)(target)

    _assert_static_failure(result)
    assert executor.calls == []


def test_non_none_credential_ref_is_authoritative_without_name_fallback() -> None:
    executor = RecordingExecutor(())

    result = _probe(executor, grants="fallback")(
        _target(name="fallback", credential_ref="unlisted")
    )

    _assert_static_failure(result)
    assert executor.calls == []


@pytest.mark.parametrize(
    ("root", "expected_target"),
    (
        ("experiments/2026", "lab-remote:experiments/2026"),
        ("/experiments/2026", "lab-remote:/experiments/2026"),
        ("/", "lab-remote:/"),
    ),
)
def test_probe_builds_one_fixed_bounded_command_for_every_root_mode(
    root: str,
    expected_target: str,
) -> None:
    executor = RecordingExecutor((_result(stdout=b"entry/\n"),))

    result = _probe(executor, binary="/opt/bin/rclone")(_target(root=root))

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert len(executor.calls) == 1
    assert executor.calls[0] == {
        "command": [
            "/opt/bin/rclone",
            "lsf",
            "--max-depth",
            "1",
            expected_target,
        ],
        "deadline": executor.calls[0]["deadline"],
        "stdout_limit_bytes": RCLONE_HEALTH_OUTPUT_LIMIT_BYTES,
        "stderr_limit_bytes": DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
        "stdout_consumer": None,
        "cwd": None,
        "env": None,
    }
    assert isinstance(executor.calls[0]["deadline"], ProcessDeadline)


def test_name_is_used_only_when_credential_ref_is_none() -> None:
    executor = RecordingExecutor((_result(),))

    result = _probe(executor, grants="fallback")(
        _target(name="fallback", credential_ref=None, root="/")
    )

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert executor.calls[0]["command"][-1] == "fallback:/"


def test_nonzero_exit_and_executor_exception_are_static_and_redacted() -> None:
    secret = "rclone-private-diagnostic"
    for outcome in (
        _result(returncode=17, stdout=secret.encode(), stderr_bytes=len(secret)),
        RuntimeError(secret),
    ):
        executor = RecordingExecutor((outcome,))

        result = _probe(executor)(_target())

        _assert_static_failure(result)
        assert secret not in str(result.to_json_dict())


def test_deadline_expiration_after_process_is_static_and_redacted() -> None:
    clock = FakeClock()
    executor = RecordingExecutor((_result(),), after_run=lambda: clock.advance(2.0))

    result = _probe(
        executor,
        clock=clock,
        deadline_seconds=1.0,
    )(_target())

    _assert_static_failure(result)


def test_each_logical_probe_receives_a_fresh_deadline() -> None:
    executor = RecordingExecutor((_result(), _result()))
    probe = _probe(executor)

    assert probe(_target()) == StoreHealth(StoreHealthStatus.HEALTHY)
    assert probe(_target()) == StoreHealth(StoreHealthStatus.HEALTHY)

    assert len(executor.calls) == 2
    assert executor.calls[0]["deadline"] is not executor.calls[1]["deadline"]


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
    executor = RecordingExecutor((failure,))

    with pytest.raises(type(failure)) as caught:
        _probe(executor)(_target())

    assert caught.value is failure
