from __future__ import annotations

import ctypes
import io
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest

from lab_tracker._windows_job import (
    CREATE_SUSPENDED,
    CtypesWindowsJobApi,
    WindowsJobCleanupError,
    WindowsJobOperationError,
    WindowsJobUnavailableError,
    _IoCounters,
    _JobObjectBasicLimitInformation,
    _JobObjectExtendedLimitInformation,
    _ThreadEntry32,
    spawn_suspended_in_job,
)
from lab_tracker.bounded_subprocess import (
    BoundedSubprocessExecutor,
    ProcessDeadline,
    ProcessExecutionError,
)

_JOB_HANDLE = 101
_PROCESS_HANDLE = 102
_THREAD_HANDLE = 103
_PROCESS_ID = 104


class FakeWinFunction:
    def __init__(self, callback: Callable[..., object]) -> None:
        self._callback = callback
        self.argtypes: list[object] = []
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        return self._callback(*args)


class FakeKernel32:
    def __init__(self) -> None:
        self.closed_handles: list[int] = []
        self.open_thread_calls = 0
        success = FakeWinFunction(lambda *_args: 1)
        self.CreateJobObjectW = success
        self.SetInformationJobObject = success
        self.OpenProcess = success
        self.AssignProcessToJobObject = success
        self.IsProcessInJob = success
        self.CreateToolhelp32Snapshot = FakeWinFunction(
            lambda *_args: _JOB_HANDLE
        )
        self.Thread32First = FakeWinFunction(self._thread_first)
        self.Thread32Next = FakeWinFunction(lambda *_args: 0)
        self.OpenThread = FakeWinFunction(self._open_thread)
        self.ResumeThread = success
        self.TerminateJobObject = success
        self.CloseHandle = FakeWinFunction(self._close_handle)

    def _thread_first(self, _snapshot: object, raw_entry: object) -> int:
        entry = ctypes.cast(
            raw_entry,
            ctypes.POINTER(_ThreadEntry32),
        ).contents
        entry.th32OwnerProcessID = _PROCESS_ID
        entry.th32ThreadID = _THREAD_HANDLE
        return 1

    def _open_thread(self, *_args: object) -> int:
        self.open_thread_calls += 1
        return _THREAD_HANDLE

    def _close_handle(self, raw_handle: object) -> int:
        value = getattr(raw_handle, "value", raw_handle)
        assert isinstance(value, int)
        self.closed_handles.append(value)
        return 1


def test_win32_ctypes_layout_matches_sdk_for_current_pointer_width() -> None:
    assert ctypes.sizeof(_ThreadEntry32) == 28
    assert _field_offsets(_ThreadEntry32) == {
        "dwSize": 0,
        "cntUsage": 4,
        "th32ThreadID": 8,
        "th32OwnerProcessID": 12,
        "tpBasePri": 16,
        "tpDeltaPri": 20,
        "dwFlags": 24,
    }
    assert ctypes.sizeof(_IoCounters) == 48
    assert _field_offsets(_IoCounters) == {
        "ReadOperationCount": 0,
        "WriteOperationCount": 8,
        "OtherOperationCount": 16,
        "ReadTransferCount": 24,
        "WriteTransferCount": 32,
        "OtherTransferCount": 40,
    }

    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    if pointer_size == 8:
        assert ctypes.sizeof(_JobObjectBasicLimitInformation) == 64
        assert _field_offsets(_JobObjectBasicLimitInformation) == {
            "PerProcessUserTimeLimit": 0,
            "PerJobUserTimeLimit": 8,
            "LimitFlags": 16,
            "MinimumWorkingSetSize": 24,
            "MaximumWorkingSetSize": 32,
            "ActiveProcessLimit": 40,
            "Affinity": 48,
            "PriorityClass": 56,
            "SchedulingClass": 60,
        }
        assert ctypes.sizeof(_JobObjectExtendedLimitInformation) == 144
        assert _field_offsets(_JobObjectExtendedLimitInformation) == {
            "BasicLimitInformation": 0,
            "IoInfo": 64,
            "ProcessMemoryLimit": 112,
            "JobMemoryLimit": 120,
            "PeakProcessMemoryUsed": 128,
            "PeakJobMemoryUsed": 136,
        }
    else:
        assert pointer_size == 4
        assert ctypes.sizeof(_JobObjectBasicLimitInformation) == 48
        assert _field_offsets(_JobObjectBasicLimitInformation) == {
            "PerProcessUserTimeLimit": 0,
            "PerJobUserTimeLimit": 8,
            "LimitFlags": 16,
            "MinimumWorkingSetSize": 20,
            "MaximumWorkingSetSize": 24,
            "ActiveProcessLimit": 28,
            "Affinity": 32,
            "PriorityClass": 36,
            "SchedulingClass": 40,
        }
        assert ctypes.sizeof(_JobObjectExtendedLimitInformation) == 112
        assert _field_offsets(_JobObjectExtendedLimitInformation) == {
            "BasicLimitInformation": 0,
            "IoInfo": 48,
            "ProcessMemoryLimit": 96,
            "JobMemoryLimit": 100,
            "PeakProcessMemoryUsed": 104,
            "PeakJobMemoryUsed": 108,
        }


def test_missing_win32_api_fails_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingKernel32:
        pass

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: MissingKernel32(),
        raising=False,
    )
    spawn_calls = 0

    def unexpected_spawn(*_args: object, **_kwargs: object) -> FakeProcess:
        nonlocal spawn_calls
        spawn_calls += 1
        raise AssertionError("spawn must not be called")

    with pytest.raises(
        WindowsJobUnavailableError,
        match="Windows process containment is unavailable",
    ):
        spawn_suspended_in_job(
            ("redacted",),
            cwd=None,
            env=None,
            deadline_check=lambda: None,
            cleanup_timeout_seconds=0.25,
            popen_factory=unexpected_spawn,  # type: ignore[arg-type]
        )

    assert spawn_calls == 0


@pytest.mark.parametrize(
    ("next_last_error", "should_succeed"),
    [(18, True), (5, False), (None, False)],
)
def test_thread_snapshot_accepts_only_real_end_of_enumeration(
    monkeypatch: pytest.MonkeyPatch,
    next_last_error: int | None,
    should_succeed: bool,
) -> None:
    kernel32 = FakeKernel32()
    last_error = 18
    observed_before_next: list[int] = []
    cleared_errors: list[int] = []

    def thread_next(*_args: object) -> int:
        nonlocal last_error
        observed_before_next.append(last_error)
        if next_last_error is not None:
            last_error = next_last_error
        return 0

    def set_last_error(value: int) -> int:
        nonlocal last_error
        cleared_errors.append(value)
        last_error = value
        return value

    kernel32.Thread32Next = FakeWinFunction(thread_next)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: last_error,
        raising=False,
    )
    monkeypatch.setattr(
        ctypes,
        "set_last_error",
        set_last_error,
        raising=False,
    )
    api = CtypesWindowsJobApi()

    if should_succeed:
        assert api.open_sole_thread(
            _PROCESS_ID,
            deadline_check=lambda: None,
        ) == _THREAD_HANDLE
        assert kernel32.open_thread_calls == 1
    else:
        with pytest.raises(
            WindowsJobOperationError,
            match="Windows process containment failed",
        ):
            api.open_sole_thread(
                _PROCESS_ID,
                deadline_check=lambda: None,
            )
        assert kernel32.open_thread_calls == 0

    assert kernel32.closed_handles == [_JOB_HANDLE]
    assert cleared_errors == [0]
    assert observed_before_next == [0]


class FakeProcess:
    def __init__(self, events: list[str]) -> None:
        self.pid = _PROCESS_ID
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.alive = True
        self.events = events
        self.kill_calls = 0
        self.wait_calls = 0
        self.wait_error: BaseException | None = None

    def kill(self) -> None:
        self.events.append("kill_leader")
        self.kill_calls += 1
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.events.append("wait_leader")
        self.wait_calls += 1
        if self.wait_error is not None:
            raise self.wait_error
        if self.alive:
            raise subprocess.TimeoutExpired(("redacted",), timeout)
        return 1


class FakeWindowsJobApi:
    def __init__(
        self,
        *,
        fail_at: str | None = None,
        in_job: bool = True,
        resume_count: int = 1,
    ) -> None:
        self.events: list[str] = []
        self.fail_at = fail_at
        self.in_job = in_job
        self.resume_count = resume_count
        self.process: FakeProcess | None = None
        self.assigned = False
        self.resume_succeeded = False
        self.closed_handles: list[int] = []

    def create_kill_on_close_job(self) -> int:
        self.events.append("create_job")
        if self.fail_at == "create_job":
            raise WindowsJobUnavailableError(
                "Windows process containment is unavailable."
            )
        return _JOB_HANDLE

    def open_process_for_assignment(self, pid: int) -> int:
        assert pid == _PROCESS_ID
        self.events.append("open_process")
        self._fail_operation("open_process")
        return _PROCESS_HANDLE

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        assert (job_handle, process_handle) == (_JOB_HANDLE, _PROCESS_HANDLE)
        self.events.append("assign_process")
        self._fail_operation("assign_process")
        self.assigned = True

    def is_process_in_job(self, process_handle: int, job_handle: int) -> bool:
        assert (process_handle, job_handle) == (_PROCESS_HANDLE, _JOB_HANDLE)
        self.events.append("verify_membership")
        self._fail_operation("verify_membership")
        return self.in_job

    def open_sole_thread(
        self,
        pid: int,
        *,
        deadline_check: Callable[[], None],
    ) -> int:
        assert pid == _PROCESS_ID
        self.events.append("open_thread")
        deadline_check()
        self._fail_operation("open_thread")
        return _THREAD_HANDLE

    def resume_thread(self, thread_handle: int) -> int:
        assert thread_handle == _THREAD_HANDLE
        self.events.append("resume_thread")
        self._fail_operation("resume_thread")
        self.resume_succeeded = self.resume_count == 1
        return self.resume_count

    def terminate_job(self, job_handle: int) -> None:
        assert job_handle == _JOB_HANDLE
        self.events.append("terminate_job")
        if self.fail_at == "terminate_job":
            raise WindowsJobCleanupError("Windows process cleanup failed.")
        if self.process is not None and self.assigned:
            self.process.alive = False

    def close_handle(self, handle: int) -> None:
        self.events.append(f"close_handle:{handle}")
        self.closed_handles.append(handle)
        if self.fail_at == f"close_handle:{handle}":
            raise WindowsJobOperationError("Windows process containment failed.")
        if handle == _JOB_HANDLE and self.process is not None:
            # KILL_ON_JOB_CLOSE is the final tree-wide backstop.
            self.process.alive = False

    def _fail_operation(self, operation: str) -> None:
        if self.fail_at == operation:
            raise WindowsJobOperationError(
                "Windows process containment failed."
            )


class RecordingPopenFactory:
    def __init__(
        self,
        api: FakeWindowsJobApi,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.api = api
        self.failure = failure
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self,
        command: Sequence[str],
        **kwargs: Any,
    ) -> FakeProcess:
        self.api.events.append("spawn_suspended")
        self.calls.append((list(command), kwargs))
        if self.failure is not None:
            raise self.failure
        process = FakeProcess(self.api.events)
        self.api.process = process
        return process


def _spawn(
    api: FakeWindowsJobApi,
    *,
    factory: RecordingPopenFactory | None = None,
    deadline_check: Callable[[], None] = lambda: None,
) -> tuple[object, RecordingPopenFactory]:
    popen_factory = factory or RecordingPopenFactory(api)
    lifecycle = spawn_suspended_in_job(
        ("resolver-secret-command", "private-target"),
        cwd=None,
        env={"VISIBLE_ONLY_TO_CHILD": "credential"},
        deadline_check=deadline_check,
        cleanup_timeout_seconds=0.25,
        api=api,
        popen_factory=popen_factory,  # type: ignore[arg-type]
    )
    return lifecycle, popen_factory


def test_fake_abi_contains_before_resume_and_closes_every_handle_once() -> None:
    api = FakeWindowsJobApi()

    lifecycle, factory = _spawn(api)

    assert api.events == [
        "create_job",
        "spawn_suspended",
        "open_process",
        "assign_process",
        "verify_membership",
        f"close_handle:{_PROCESS_HANDLE}",
        "open_thread",
        "resume_thread",
        f"close_handle:{_THREAD_HANDLE}",
    ]
    assert len(factory.calls) == 1
    command, kwargs = factory.calls[0]
    assert command == ["resolver-secret-command", "private-target"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["close_fds"] is True
    assert kwargs["creationflags"] == CREATE_SUSPENDED
    assert kwargs["env"] == {"VISIBLE_ONLY_TO_CHILD": "credential"}
    assert "resume_thread" not in api.events[: api.events.index("verify_membership")]

    lifecycle.finish(cleanup_expires_at=time.monotonic() + 0.25)  # type: ignore[attr-defined]
    lifecycle.close_pipes()  # type: ignore[attr-defined]
    lifecycle.close_containment()  # type: ignore[attr-defined]

    assert api.events[-3:] == [
        "terminate_job",
        "wait_leader",
        f"close_handle:{_JOB_HANDLE}",
    ]
    assert api.closed_handles == [
        _PROCESS_HANDLE,
        _THREAD_HANDLE,
        _JOB_HANDLE,
    ]
    assert api.process is not None
    assert api.process.wait_calls == 1
    assert api.process.stdout.closed
    assert api.process.stderr.closed


def test_fake_abi_unavailable_job_fails_before_spawn() -> None:
    api = FakeWindowsJobApi(fail_at="create_job")
    factory = RecordingPopenFactory(api)

    with pytest.raises(
        WindowsJobUnavailableError,
        match="Windows process containment is unavailable",
    ):
        _spawn(api, factory=factory)

    assert api.events == ["create_job"]
    assert factory.calls == []
    assert api.closed_handles == []


@pytest.mark.parametrize("fail_on_check", range(1, 9))
def test_fake_abi_deadline_failure_never_resumes_and_cleans_owned_handles(
    fail_on_check: int,
) -> None:
    class DeadlineFailure(RuntimeError):
        pass

    checks = 0

    def check_deadline() -> None:
        nonlocal checks
        checks += 1
        if checks == fail_on_check:
            raise DeadlineFailure("private deadline detail")

    api = FakeWindowsJobApi()

    with pytest.raises(DeadlineFailure, match="private deadline detail"):
        _spawn(api, deadline_check=check_deadline)

    assert "resume_thread" not in api.events
    assert not api.resume_succeeded
    if fail_on_check == 1:
        assert api.events == []
        return
    assert api.closed_handles.count(_JOB_HANDLE) == 1
    if fail_on_check == 2:
        assert "spawn_suspended" not in api.events
        return
    assert api.process is not None
    assert not api.process.alive
    assert api.process.wait_calls == 1
    if fail_on_check in {3, 4, 5}:
        assert api.process.kill_calls == 1
        assert "terminate_job" not in api.events
    else:
        assert api.process.kill_calls == 0
        assert api.events.count("terminate_job") == 1
    if fail_on_check >= 4:
        assert api.closed_handles.count(_PROCESS_HANDLE) == 1
    if fail_on_check == 8:
        assert api.closed_handles.count(_THREAD_HANDLE) == 1


def test_fake_abi_spawn_failure_closes_job_without_resume() -> None:
    secret = "private-popen-detail"
    api = FakeWindowsJobApi()
    factory = RecordingPopenFactory(api, failure=OSError(secret))

    with pytest.raises(OSError, match=secret):
        _spawn(api, factory=factory)

    assert api.events == [
        "create_job",
        "spawn_suspended",
        f"close_handle:{_JOB_HANDLE}",
    ]
    assert api.closed_handles == [_JOB_HANDLE]


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
@pytest.mark.parametrize(
    "failure_type",
    [RuntimeError, SystemExit, GeneratorExit],
    ids=["ordinary-exception", "system-exit", "generator-exit"],
)
def test_arbitrary_spawn_exception_is_redacted_at_public_boundary(
    platform_name: str,
    failure_type: type[BaseException],
) -> None:
    secret = "argv-and-audit-hook-private-detail"
    api = FakeWindowsJobApi()
    factory = RecordingPopenFactory(api, failure=failure_type(secret))
    executor = BoundedSubprocessExecutor(
        _platform_name=platform_name,
        _windows_api=api,
        _popen_factory=factory,  # type: ignore[arg-type]
    )

    with pytest.raises(ProcessExecutionError) as raised:
        executor.run(
            ("private-command", "private-target"),
            deadline=ProcessDeadline.after(1.0),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
            env={"PRIVATE_TOKEN": "secret"},
        )

    assert str(raised.value) == "Subprocess execution failed."
    assert secret not in str(raised.value)
    assert "private-command" not in str(raised.value)
    assert "private-target" not in str(raised.value)
    if platform_name == "nt":
        assert api.closed_handles == [_JOB_HANDLE]


@pytest.mark.parametrize("platform_name", ["posix", "nt"])
def test_spawn_keyboard_interrupt_is_sanitized_at_public_boundary(
    platform_name: str,
) -> None:
    secret = "audit-hook-private-keyboard-interrupt-detail"
    api = FakeWindowsJobApi()
    factory = RecordingPopenFactory(api, failure=KeyboardInterrupt(secret))
    executor = BoundedSubprocessExecutor(
        _platform_name=platform_name,
        _windows_api=api,
        _popen_factory=factory,  # type: ignore[arg-type]
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        executor.run(
            ("private-command", "private-target"),
            deadline=ProcessDeadline.after(1.0),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
            env={"PRIVATE_TOKEN": "secret"},
        )

    assert str(raised.value) == ""
    assert secret not in str(raised.value)
    if platform_name == "nt":
        assert api.closed_handles == [_JOB_HANDLE]


@pytest.mark.parametrize("failure", ["open_process", "assign_process"])
def test_fake_abi_precontainment_failure_kills_suspended_leader(
    failure: str,
) -> None:
    api = FakeWindowsJobApi(fail_at=failure)

    with pytest.raises(
        WindowsJobOperationError,
        match="Windows process containment failed",
    ):
        _spawn(api)

    assert "resume_thread" not in api.events
    assert api.process is not None
    assert api.process.kill_calls == 1
    assert api.process.wait_calls == 1
    assert not api.process.alive
    assert api.events[-2:] == [
        "wait_leader",
        f"close_handle:{_JOB_HANDLE}",
    ]
    assert api.closed_handles.count(_JOB_HANDLE) == 1


@pytest.mark.parametrize(
    ("failure", "in_job", "resume_count"),
    [
        ("verify_membership", True, 1),
        (None, False, 1),
        (f"close_handle:{_PROCESS_HANDLE}", True, 1),
        ("open_thread", True, 1),
        ("resume_thread", True, 1),
        (None, True, 0),
        (f"close_handle:{_THREAD_HANDLE}", True, 1),
    ],
)
def test_fake_abi_failure_never_escapes_containment_and_closes_handles(
    failure: str | None,
    in_job: bool,
    resume_count: int,
) -> None:
    api = FakeWindowsJobApi(
        fail_at=failure,
        in_job=in_job,
        resume_count=resume_count,
    )

    with pytest.raises(
        WindowsJobOperationError,
        match="Windows process containment failed",
    ):
        _spawn(api)

    assert api.process is not None
    assert not api.process.alive
    assert api.process.wait_calls == 1
    assert api.closed_handles.count(_JOB_HANDLE) == 1
    assert api.closed_handles.count(_PROCESS_HANDLE) <= 1
    assert api.closed_handles.count(_THREAD_HANDLE) <= 1
    if failure in {
        "verify_membership",
        f"close_handle:{_PROCESS_HANDLE}",
        "open_thread",
    } or not in_job:
        assert "resume_thread" not in api.events
        assert not api.resume_succeeded
    if failure in {"open_thread", "resume_thread"} or resume_count == 0:
        assert api.events.index("verify_membership") < api.events.index(
            "terminate_job"
        )


def test_fake_abi_terminate_failure_uses_job_close_backstop_and_reports_cleanup() -> None:
    api = FakeWindowsJobApi(fail_at="terminate_job")
    lifecycle, _ = _spawn(api)

    with pytest.raises(
        WindowsJobCleanupError,
        match="Windows process cleanup failed",
    ):
        lifecycle.stop(  # type: ignore[attr-defined]
            cleanup_expires_at=time.monotonic() + 0.25
        )

    assert api.events[-3:] == [
        "terminate_job",
        f"close_handle:{_JOB_HANDLE}",
        "wait_leader",
    ]
    assert api.process is not None
    assert not api.process.alive
    assert api.process.wait_calls == 1
    assert api.closed_handles.count(_JOB_HANDLE) == 1


def test_fake_abi_wait_failure_still_closes_job_and_reports_cleanup() -> None:
    api = FakeWindowsJobApi()
    lifecycle, _ = _spawn(api)
    assert api.process is not None
    api.process.wait_error = OSError("sensitive wait detail")

    with pytest.raises(
        WindowsJobCleanupError,
        match="Windows process cleanup failed",
    ) as raised:
        lifecycle.stop(  # type: ignore[attr-defined]
            cleanup_expires_at=time.monotonic() + 0.25
        )

    assert "sensitive wait detail" not in str(raised.value)
    assert api.events[-3:] == [
        "terminate_job",
        "wait_leader",
        f"close_handle:{_JOB_HANDLE}",
    ]
    assert api.closed_handles.count(_JOB_HANDLE) == 1


def _field_offsets(structure: type[ctypes.Structure]) -> dict[str, int]:
    return {
        field_name: int(getattr(structure, field_name).offset)
        for field_name, _field_type in structure._fields_
    }
