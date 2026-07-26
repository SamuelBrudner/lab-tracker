from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import FrozenInstanceError, fields
from os import PathLike
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import lab_tracker._local_store_health_helper as local_health_helper
import lab_tracker.local_store_health as local_store_health
from lab_tracker.bounded_subprocess import (
    MAX_PROCESS_DEADLINE_SECONDS,
    BoundedSubprocessExecutor,
    ProcessCleanupError,
    ProcessDeadline,
    ProcessDeadlineExceeded,
    ProcessOutputLimitExceeded,
    ProcessResult,
    StdoutConsumer,
)
from lab_tracker.local_path_policy import LocalPathPolicy
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
            "a rejected target must not create a process deadline"
        )


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
    root: str,
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
    policy: LocalPathPolicy,
    executor: RecordingExecutor,
    *,
    clock: Callable[[], float] | None = None,
    **kwargs: object,
) -> LocalStoreHealthProbe:
    return LocalStoreHealthProbe(
        policy=policy,
        executor=executor,
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


def _run_helper(root: Path, *extra_args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and packaged helper path
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            os.fspath(local_store_health._HELPER_PATH),
            *extra_args,
        ],
        check=False,
        capture_output=True,
        env=local_store_health._helper_environment(os.fspath(root)),
    )


def _create_windows_junction(junction: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )


def test_helper_accepts_only_a_plain_directory_without_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "store"
    directory.mkdir()
    regular_file = tmp_path / "artifact.bin"
    regular_file.write_bytes(b"not a directory")

    assert local_health_helper.main(
        ("helper",),
        {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: str(directory)},
    ) == 0
    assert local_health_helper.main(
        ("helper",),
        {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: str(regular_file)},
    ) != 0
    assert local_health_helper.main(
        ("helper",),
        {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: str(tmp_path / "missing")},
    ) != 0
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("argv", "environment"),
    (
        (("helper", "unexpected"), {}),
        ((), {}),
        (("helper",), {}),
        (("helper",), {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: ""}),
        (("helper",), {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: "bad\0root"}),
    ),
)
def test_helper_rejects_malformed_protocol_without_output(
    argv: Sequence[str],
    environment: Mapping[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert local_health_helper.main(argv, environment) != 0
    assert capsys.readouterr() == ("", "")


def test_helper_collapses_even_control_flow_stat_failures_without_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_stat(*_args: object, **_kwargs: object) -> object:
        raise KeyboardInterrupt("private path diagnostic")

    monkeypatch.setattr(local_health_helper.os, "stat", fail_stat)

    assert local_health_helper.main(
        ("helper",),
        {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: "/private/store"},
    ) != 0
    assert capsys.readouterr() == ("", "")


def test_helper_rejects_windows_reparse_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reparse_flag = 0x400
    monkeypatch.setattr(
        local_health_helper.stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_flag,
        raising=False,
    )
    monkeypatch.setattr(
        local_health_helper.os,
        "stat",
        lambda *_args, **_kwargs: SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=reparse_flag,
        ),
    )

    assert local_health_helper.main(
        ("helper",),
        {local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV: "/approved/store"},
    ) != 0


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symbolic links")
def test_real_helper_rejects_a_final_symbolic_link(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")

    completed = _run_helper(link)

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_real_helper_rejects_a_final_windows_junction(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    _create_windows_junction(junction, target)

    completed = _run_helper(junction)

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_probe_is_an_immutable_slotted_dependency_container(tmp_path: Path) -> None:
    probe = _probe(LocalPathPolicy([tmp_path]), RecordingExecutor((_result(),)))

    assert [item.name for item in fields(LocalStoreHealthProbe)] == [
        "policy",
        "executor",
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
        "policy": LocalPathPolicy([]),
        "executor": RecordingExecutor(()),
        field_name: value,
    }

    with pytest.raises(error_type):
        LocalStoreHealthProbe(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kind",
    tuple(kind for kind in StoreKind if kind is not StoreKind.LOCAL_FS),
)
def test_non_local_kinds_fail_before_policy_deadline_or_process(
    tmp_path: Path,
    kind: StoreKind,
) -> None:
    executor = RecordingExecutor(())

    result = _probe(
        LocalPathPolicy([tmp_path]),
        executor,
        clock=UnexpectedClock(),
    )(_target(str(tmp_path), kind=kind))

    _assert_static_failure(result)
    assert executor.calls == []


@pytest.mark.parametrize(
    "root",
    (
        "",
        "relative/store",
        "../escape",
        "//server/share",
        r"\\server\share",
        r"\\?\C:\private",
        "control\tpath",
    ),
)
def test_invalid_registered_roots_fail_before_deadline_or_process(
    tmp_path: Path,
    root: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = RecordingExecutor(())
    policy = LocalPathPolicy([tmp_path])

    def unexpected_realpath(_path: object) -> str:
        raise FatalProbeFailure("lexically invalid root reached canonicalization")

    monkeypatch.setattr(local_store_health.os.path, "realpath", unexpected_realpath)

    result = _probe(
        policy,
        executor,
        clock=UnexpectedClock(),
    )(_target(root))

    _assert_static_failure(result)
    assert executor.calls == []


def test_deny_all_disjoint_and_sibling_prefix_roots_never_start_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "store"
    allowed.mkdir()
    disjoint = tmp_path / "private"
    disjoint.mkdir()
    sibling = tmp_path / "store-private"
    sibling.mkdir()
    cases = (
        (LocalPathPolicy([]), allowed),
        (LocalPathPolicy([allowed]), disjoint),
        (LocalPathPolicy([allowed]), sibling),
    )

    def unexpected_realpath(_path: object) -> str:
        raise FatalProbeFailure("lexically denied root reached canonicalization")

    monkeypatch.setattr(local_store_health.os.path, "realpath", unexpected_realpath)

    for policy, root in cases:
        executor = RecordingExecutor(())
        result = _probe(policy, executor, clock=UnexpectedClock())(_target(str(root)))
        _assert_static_failure(result)
        assert executor.calls == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symbolic links")
def test_static_symbolic_link_escape_is_rejected_before_helper(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "mounted-outside"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    executor = RecordingExecutor(())

    result = _probe(
        LocalPathPolicy([allowed]),
        executor,
        clock=UnexpectedClock(),
    )(_target(str(link)))

    _assert_static_failure(result)
    assert executor.calls == []


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symbolic links")
def test_symbolic_link_parent_traversal_escape_is_rejected_before_helper(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside_parent = tmp_path / "outside"
    outside_target = outside_parent / "nested"
    outside_target.mkdir(parents=True)
    link = allowed / "mounted-outside"
    try:
        link.symlink_to(outside_target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    registered_root = os.path.join(os.fspath(link), os.pardir)
    executor = RecordingExecutor(())

    result = _probe(
        LocalPathPolicy([allowed]),
        executor,
        clock=UnexpectedClock(),
    )(_target(registered_root))

    _assert_static_failure(result)
    assert executor.calls == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_static_windows_junction_escape_is_rejected_before_helper(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = allowed / "mounted-outside"
    _create_windows_junction(junction, outside)
    executor = RecordingExecutor(())

    result = _probe(
        LocalPathPolicy([allowed]),
        executor,
        clock=UnexpectedClock(),
    )(_target(str(junction)))

    _assert_static_failure(result)
    assert executor.calls == []


def test_probe_uses_fixed_isolated_command_minimal_environment_and_zero_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "must-not-reach-helper")
    if os.name == "posix":
        monkeypatch.setenv("LANG", "C")
        monkeypatch.setenv("LC_CTYPE", "C")
        monkeypatch.setenv("LC_PRIVATE_VALUE", "must-not-reach-helper")
    elif os.name == "nt":
        monkeypatch.setenv("SystemRoot", r"C:\Windows")
        monkeypatch.setenv("WINDIR", r"C:\Windows")
    executor = RecordingExecutor((_result(),))

    result = _probe(LocalPathPolicy([tmp_path]), executor)(_target(str(root)))

    assert result == StoreHealth(StoreHealthStatus.HEALTHY)
    assert len(executor.calls) == 1
    call = executor.calls[0]
    canonical_root = os.path.realpath(root)
    assert call["command"] == [
        sys.executable,
        "-I",
        "-S",
        "-B",
        os.fspath(local_store_health._HELPER_PATH),
    ]
    assert canonical_root not in call["command"]
    assert call["stdout_limit_bytes"] == 0
    assert call["stderr_limit_bytes"] == 0
    assert call["stdout_consumer"] is None
    assert call["cwd"] is None
    assert call["env"][local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV] == canonical_root
    assert "UNRELATED_PRIVATE_VALUE" not in call["env"]
    if os.name == "posix":
        assert call["env"]["LANG"] == "C"
        assert call["env"]["LC_CTYPE"] == "C"
        assert "LC_PRIVATE_VALUE" not in call["env"]
        assert all(
            name == local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV
            or name in {"LANG", "LC_ALL", "LC_CTYPE"}
            for name in call["env"]
        )
    elif os.name == "nt":
        normalized_environment = {
            name.upper(): value for name, value in call["env"].items()
        }
        assert normalized_environment["SYSTEMROOT"] == r"C:\Windows"
        assert normalized_environment["WINDIR"] == r"C:\Windows"
        assert all(
            name == local_health_helper.LOCAL_STORE_HEALTH_ROOT_ENV
            or name.upper() in {"SYSTEMROOT", "WINDIR"}
            for name in call["env"]
        )
    assert isinstance(call["deadline"], ProcessDeadline)


@pytest.mark.parametrize(
    "outcome",
    (
        _result(returncode=1),
        _result(stdout=b"unexpected"),
        _result(stdout_bytes=1),
        _result(stderr_bytes=1),
        ProcessDeadlineExceeded("private local path diagnostic"),
        ProcessOutputLimitExceeded("private local path diagnostic"),
        ProcessCleanupError("private local path diagnostic"),
        RuntimeError("private local path diagnostic"),
    ),
    ids=(
        "nonzero-exit",
        "captured-stdout",
        "stdout-overflow",
        "stderr-overflow",
        "deadline",
        "output-limit",
        "cleanup",
        "ordinary-error",
    ),
)
def test_nonclean_exit_or_executor_failure_is_static_and_redacted(
    tmp_path: Path,
    outcome: ProcessResult | BaseException,
) -> None:
    root = tmp_path / "store-secret-name"
    root.mkdir()
    executor = RecordingExecutor((outcome,))

    result = _probe(LocalPathPolicy([tmp_path]), executor)(_target(str(root)))

    _assert_static_failure(result)
    assert str(root) not in str(result.to_json_dict())
    assert "private local path diagnostic" not in str(result.to_json_dict())


def test_deadline_expiration_after_helper_is_static(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    clock = FakeClock()
    executor = RecordingExecutor((_result(),), after_run=lambda: clock.advance(2.0))

    result = _probe(
        LocalPathPolicy([tmp_path]),
        executor,
        clock=clock,
        deadline_seconds=1.0,
    )(_target(str(root)))

    _assert_static_failure(result)


def test_each_probe_receives_a_fresh_deadline(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    executor = RecordingExecutor((_result(), _result()))
    probe = _probe(LocalPathPolicy([tmp_path]), executor)

    assert probe(_target(str(root))) == StoreHealth(StoreHealthStatus.HEALTHY)
    assert probe(_target(str(root))) == StoreHealth(StoreHealthStatus.HEALTHY)

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
def test_base_exception_propagates_unchanged(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    executor = RecordingExecutor((failure,))

    with pytest.raises(type(failure)) as caught:
        _probe(LocalPathPolicy([tmp_path]), executor)(_target(str(root)))

    assert caught.value is failure


@pytest.mark.parametrize(
    ("root_kind", "expected_status"),
    (
        ("directory", StoreHealthStatus.HEALTHY),
        ("file", StoreHealthStatus.UNREACHABLE),
        ("missing", StoreHealthStatus.UNREACHABLE),
    ),
)
def test_real_bounded_helper_reports_directory_file_and_missing(
    tmp_path: Path,
    root_kind: str,
    expected_status: StoreHealthStatus,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    root = allowed / root_kind
    if root_kind == "directory":
        root.mkdir()
    elif root_kind == "file":
        root.write_bytes(b"artifact")
    probe = LocalStoreHealthProbe(
        policy=LocalPathPolicy([allowed]),
        executor=BoundedSubprocessExecutor(),
        deadline_seconds=5.0,
    )

    result = probe(_target(str(root)))

    assert result.status is expected_status
    if expected_status is StoreHealthStatus.UNREACHABLE:
        _assert_static_failure(result)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symbolic links")
def test_in_root_symbolic_link_uses_canonical_target_with_real_helper(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    target = allowed / "target"
    target.mkdir(parents=True)
    link = allowed / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    probe = LocalStoreHealthProbe(
        policy=LocalPathPolicy([allowed]),
        executor=BoundedSubprocessExecutor(),
        deadline_seconds=5.0,
    )

    assert probe(_target(str(link))) == StoreHealth(StoreHealthStatus.HEALTHY)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_in_root_windows_junction_uses_canonical_target_with_real_helper(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    target = allowed / "target"
    target.mkdir(parents=True)
    junction = allowed / "junction"
    _create_windows_junction(junction, target)
    probe = LocalStoreHealthProbe(
        policy=LocalPathPolicy([allowed]),
        executor=BoundedSubprocessExecutor(),
        deadline_seconds=5.0,
    )

    assert probe(_target(str(junction))) == StoreHealth(StoreHealthStatus.HEALTHY)
