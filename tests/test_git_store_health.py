from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, fields
from os import PathLike
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from store_authority_fakes import unsafe_probe_target_for_adapter_test

from lab_tracker.bounded_subprocess import (
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
    ProcessResult,
    StdoutConsumer,
)
from lab_tracker.git_process import (
    GIT_GENERIC_HTTP_REDIRECT_CONFIG,
    GIT_PROCESS_METADATA_LIMIT_BYTES,
    GIT_PROCESS_STDERR_LIMIT_BYTES,
)
from lab_tracker.git_remote_policy import GitRemotePolicy
from lab_tracker.git_store_health import GitStoreHealthProbe
from lab_tracker.models import StoreKind
from lab_tracker.store_health import (
    GIT_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

_REMOTE = "https://example.com/org/repo.git"


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
        after_calls: Mapping[int, Callable[[], None]] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._after_calls = dict(after_calls or {})
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
        callback = self._after_calls.get(len(self.calls))
        if callback is not None:
            callback()
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
    root: str = _REMOTE,
    kind: StoreKind = StoreKind.GIT,
) -> StoreProbeTarget:
    return unsafe_probe_target_for_adapter_test(
        store_id=UUID(int=2),
        name="repository",
        kind=kind,
        root=root,
        endpoint=None,
        credential_ref=None,
    )


def _probe(
    executor: RecordingExecutor,
    workdir: Path,
    *,
    grants: str = _REMOTE,
    clock: FakeClock | None = None,
    **kwargs: object,
) -> GitStoreHealthProbe:
    return GitStoreHealthProbe(
        policy=GitRemotePolicy.from_config(grants),
        executor=executor,
        workdir=workdir,
        clock=clock or FakeClock(),
        **kwargs,
    )


def _healthy_outcomes(remote: str = _REMOTE) -> tuple[ProcessResult, ProcessResult]:
    return (
        _result(stdout=remote.encode() + b"\n"),
        _result(stdout=b"a" * 40 + b"\tHEAD\n"),
    )


def _assert_static_failure(result: StoreHealth) -> None:
    assert result == StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        GIT_STORE_HEALTH_FAILURE_DETAIL,
    )
    assert result.to_json_dict() == {
        "status": "unreachable",
        "detail": GIT_STORE_HEALTH_FAILURE_DETAIL,
    }


def test_git_probe_is_an_immutable_slotted_dependency_container(
    tmp_path: Path,
) -> None:
    probe = _probe(RecordingExecutor(_healthy_outcomes()), tmp_path)

    assert [item.name for item in fields(GitStoreHealthProbe)] == [
        "policy",
        "executor",
        "workdir",
        "binary",
        "allow_protocol",
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
        ("binary", "git\0other", ValueError),
        ("allow_protocol", 1, TypeError),
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
def test_git_probe_rejects_invalid_or_unbounded_configuration(
    tmp_path: Path,
    field_name: str,
    value: object,
    error_type: type[Exception],
) -> None:
    kwargs: dict[str, object] = {
        "policy": GitRemotePolicy.deny_all(),
        "executor": RecordingExecutor(()),
        "workdir": tmp_path,
        field_name: value,
    }

    with pytest.raises(error_type):
        GitStoreHealthProbe(**kwargs)  # type: ignore[arg-type]


def test_non_git_kind_fails_without_process_or_cwd_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RecordingExecutor(())
    filesystem_calls: list[str] = []
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda value: filesystem_calls.append(os.fspath(value)) or os.fspath(value),
    )

    result = _probe(executor, tmp_path)(_target(kind=StoreKind.RCLONE))

    _assert_static_failure(result)
    assert executor.calls == []
    assert filesystem_calls == []


@pytest.mark.parametrize(
    ("root", "grants"),
    (
        ("", _REMOTE),
        ("not a remote", _REMOTE),
        ("https://unlisted.example/org/repo.git", _REMOTE),
        ("file:///private/secret", _REMOTE),
        ("ext::sh -c exploit", _REMOTE),
    ),
)
def test_invalid_or_unlisted_remote_fails_before_cwd_or_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root: str,
    grants: str,
) -> None:
    executor = RecordingExecutor(())
    filesystem_calls: list[str] = []
    monkeypatch.setattr(
        os.path,
        "realpath",
        lambda value: filesystem_calls.append(os.fspath(value)) or os.fspath(value),
    )

    result = _probe(executor, tmp_path, grants=grants)(_target(root=root))

    _assert_static_failure(result)
    assert executor.calls == []
    assert filesystem_calls == []


def test_authorization_precedes_an_unusable_pathlike_workdir() -> None:
    class ExplosivePath:
        def __fspath__(self) -> str:
            raise AssertionError("denied remote inspected the workdir")

    executor = RecordingExecutor(())
    probe = GitStoreHealthProbe(
        policy=GitRemotePolicy.deny_all(),
        executor=executor,
        workdir=ExplosivePath(),  # type: ignore[arg-type]
    )

    result = probe(_target())

    _assert_static_failure(result)
    assert executor.calls == []


def test_missing_workdir_fails_without_process(tmp_path: Path) -> None:
    executor = RecordingExecutor(())

    result = _probe(executor, tmp_path / "missing")(_target())

    _assert_static_failure(result)
    assert executor.calls == []


def test_probe_uses_fixed_argv_shared_deadline_caps_and_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workdir = tmp_path / "parent" / "health"
    workdir.mkdir(parents=True)
    executor = RecordingExecutor(_healthy_outcomes())
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        monkeypatch.setenv(variable, f"private-{variable}")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ext")

    result = _probe(
        executor,
        workdir,
        binary="/opt/bin/git",
        allow_protocol="https:ssh",
    )(_target())

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert len(executor.calls) == 2
    canonical_workdir = os.path.realpath(workdir)
    config = [
        "-c",
        GIT_GENERIC_HTTP_REDIRECT_CONFIG,
        "-c",
        f"http.{_REMOTE}.followRedirects=false",
    ]
    assert executor.calls[0]["command"] == [
        "/opt/bin/git",
        *config,
        "ls-remote",
        "--get-url",
        "--",
        _REMOTE,
    ]
    assert executor.calls[1]["command"] == [
        "/opt/bin/git",
        *config,
        "ls-remote",
        "--",
        _REMOTE,
        "HEAD",
    ]
    assert executor.calls[0]["deadline"] is executor.calls[1]["deadline"]
    assert isinstance(executor.calls[0]["deadline"], ProcessDeadline)
    for call in executor.calls:
        assert call["stdout_limit_bytes"] == GIT_PROCESS_METADATA_LIMIT_BYTES
        assert call["stderr_limit_bytes"] == GIT_PROCESS_STDERR_LIMIT_BYTES
        assert call["stdout_consumer"] is None
        assert call["cwd"] == canonical_workdir
        assert call["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert call["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh"
        assert call["env"]["GIT_CEILING_DIRECTORIES"] == os.path.realpath(workdir.parent)
        assert all(
            variable not in call["env"]
            for variable in (
                "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR",
                "GIT_DIR",
                "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY",
                "GIT_WORK_TREE",
            )
        )
    assert executor.calls[0]["env"] is executor.calls[1]["env"]


@pytest.mark.parametrize(
    "preflight",
    (
        _result(returncode=1),
        _result(stdout=_REMOTE.encode()),
        _result(stdout=_REMOTE.encode() + b"\n\n"),
        _result(stdout=b"https://attacker.invalid/rewrite.git\n"),
        _result(stdout=b"\xff\n"),
    ),
)
def test_preflight_failure_or_exact_mismatch_prevents_second_call(
    tmp_path: Path,
    preflight: ProcessResult,
) -> None:
    executor = RecordingExecutor((preflight,))

    result = _probe(executor, tmp_path)(_target())

    _assert_static_failure(result)
    assert len(executor.calls) == 1
    assert "--get-url" in executor.calls[0]["command"]


def test_crlf_preflight_is_accepted(tmp_path: Path) -> None:
    executor = RecordingExecutor(
        (
            _result(stdout=_REMOTE.encode() + b"\r\n"),
            _result(),
        )
    )

    result = _probe(executor, tmp_path)(_target())

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert len(executor.calls) == 2


def test_nonzero_head_exit_is_static_and_redacted(tmp_path: Path) -> None:
    secret = "git-private-diagnostic"
    executor = RecordingExecutor(
        (
            _result(stdout=_REMOTE.encode() + b"\n"),
            _result(
                returncode=128,
                stdout=secret.encode(),
                stderr_bytes=len(secret),
            ),
        )
    )

    result = _probe(executor, tmp_path)(_target())

    _assert_static_failure(result)
    assert secret not in str(result.to_json_dict())


@pytest.mark.parametrize("failing_call", (1, 2))
def test_ordinary_executor_exception_is_static_and_redacted(
    tmp_path: Path,
    failing_call: int,
) -> None:
    secret = "executor-private-diagnostic"
    outcomes: tuple[ProcessResult | BaseException, ...]
    if failing_call == 1:
        outcomes = (RuntimeError(secret),)
    else:
        outcomes = (
            _result(stdout=_REMOTE.encode() + b"\n"),
            RuntimeError(secret),
        )
    executor = RecordingExecutor(outcomes)

    result = _probe(executor, tmp_path)(_target())

    _assert_static_failure(result)
    assert secret not in str(result.to_json_dict())
    assert len(executor.calls) == failing_call


def test_deadline_expiration_after_head_is_static(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = RecordingExecutor(
        _healthy_outcomes(),
        after_calls={2: lambda: clock.advance(2.0)},
    )

    result = _probe(
        executor,
        tmp_path,
        clock=clock,
        deadline_seconds=1.0,
    )(_target())

    _assert_static_failure(result)
    assert executor.calls[0]["deadline"] is executor.calls[1]["deadline"]


def test_deadline_expiration_after_preflight_prevents_head(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = RecordingExecutor(
        _healthy_outcomes(),
        after_calls={1: lambda: clock.advance(2.0)},
    )

    result = _probe(
        executor,
        tmp_path,
        clock=clock,
        deadline_seconds=1.0,
    )(_target())

    _assert_static_failure(result)
    assert len(executor.calls) == 1


def test_each_logical_probe_gets_a_fresh_shared_deadline(tmp_path: Path) -> None:
    executor = RecordingExecutor((*_healthy_outcomes(), *_healthy_outcomes()))
    probe = _probe(executor, tmp_path)

    assert probe(_target()) == StoreHealth(StoreHealthStatus.HEALTHY)
    assert probe(_target()) == StoreHealth(StoreHealthStatus.HEALTHY)

    assert len(executor.calls) == 4
    assert executor.calls[0]["deadline"] is executor.calls[1]["deadline"]
    assert executor.calls[2]["deadline"] is executor.calls[3]["deadline"]
    assert executor.calls[0]["deadline"] is not executor.calls[2]["deadline"]


@pytest.mark.parametrize("failing_call", (1, 2))
@pytest.mark.parametrize(
    "failure",
    (
        KeyboardInterrupt("operator interruption"),
        SystemExit("operator exit"),
        FatalProbeFailure("fatal probe failure"),
    ),
    ids=("keyboard-interrupt", "system-exit", "custom-base-exception"),
)
def test_base_exception_propagates_unchanged(
    tmp_path: Path,
    failing_call: int,
    failure: BaseException,
) -> None:
    outcomes: tuple[ProcessResult | BaseException, ...]
    if failing_call == 1:
        outcomes = (failure,)
    else:
        outcomes = (
            _result(stdout=_REMOTE.encode() + b"\n"),
            failure,
        )
    executor = RecordingExecutor(outcomes)

    with pytest.raises(type(failure)) as caught:
        _probe(executor, tmp_path)(_target())

    assert caught.value is failure
    assert len(executor.calls) == failing_call
