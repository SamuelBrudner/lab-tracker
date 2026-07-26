"""Private Windows Job Object containment for bounded subprocesses.

This module deliberately exposes a small, high-level Win32 protocol so the
security-sensitive spawn order can be tested without importing or invoking
``kernel32`` on non-Windows hosts.  Public callers should use
``BoundedSubprocessExecutor`` instead.
"""

from __future__ import annotations

import ctypes
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from os import PathLike
from typing import Protocol, cast

CREATE_SUSPENDED = 0x00000004

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_ERROR_NO_MORE_FILES = 18

_GENERIC_UNAVAILABLE_DETAIL = "Windows process containment is unavailable."
_GENERIC_OPERATION_DETAIL = "Windows process containment failed."
_GENERIC_CLEANUP_DETAIL = "Windows process cleanup failed."

DeadlineCheck = Callable[[], None]
PopenFactory = Callable[..., subprocess.Popen[bytes]]


class WindowsJobUnavailableError(RuntimeError):
    """The required Windows containment API is unavailable."""


class WindowsJobOperationError(RuntimeError):
    """A redacted Windows containment operation failed."""


class WindowsJobCleanupError(RuntimeError):
    """A contained Windows process could not be killed and reaped."""


class WindowsJobApi(Protocol):
    """Minimal Win32 surface required by the suspended-spawn protocol."""

    def create_kill_on_close_job(self) -> int:
        """Create a private, non-inheritable kill-on-close job."""

    def open_process_for_assignment(self, pid: int) -> int:
        """Open a temporary process handle with assignment rights."""

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        """Assign the process to the private job."""

    def is_process_in_job(self, process_handle: int, job_handle: int) -> bool:
        """Return whether the process is a member of this exact job."""

    def open_sole_thread(
        self,
        pid: int,
        *,
        deadline_check: DeadlineCheck,
    ) -> int:
        """Open the only thread owned by a newly suspended process."""

    def resume_thread(self, thread_handle: int) -> int:
        """Resume a thread and return its previous suspend count."""

    def terminate_job(self, job_handle: int) -> None:
        """Terminate every active process in the job."""

    def close_handle(self, handle: int) -> None:
        """Close one owned Win32 handle."""


class _WinFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> object: ...


_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int32
_LONG = ctypes.c_int32
_ULONG_PTR = ctypes.c_size_t
_SIZE_T = ctypes.c_size_t
_HANDLE = ctypes.c_void_p
_LARGE_INTEGER = ctypes.c_int64


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _ULONG_PTR),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", _DWORD),
        ("cntUsage", _DWORD),
        ("th32ThreadID", _DWORD),
        ("th32OwnerProcessID", _DWORD),
        ("tpBasePri", _LONG),
        ("tpDeltaPri", _LONG),
        ("dwFlags", _DWORD),
    ]


class CtypesWindowsJobApi:
    """ctypes-backed implementation of the private Windows Job Object port."""

    def __init__(self) -> None:
        try:
            loader = cast(
                Callable[..., object],
                getattr(ctypes, "WinDLL"),  # noqa: B009
            )
            self._kernel32 = loader("kernel32", use_last_error=True)
        except (AttributeError, OSError):
            raise WindowsJobUnavailableError(_GENERIC_UNAVAILABLE_DETAIL) from None

        self._create_job_object = self._bind(
            "CreateJobObjectW",
            [ctypes.c_void_p, ctypes.c_wchar_p],
            _HANDLE,
        )
        self._set_information_job_object = self._bind(
            "SetInformationJobObject",
            [_HANDLE, ctypes.c_int32, ctypes.c_void_p, _DWORD],
            _BOOL,
        )
        self._open_process = self._bind(
            "OpenProcess",
            [_DWORD, _BOOL, _DWORD],
            _HANDLE,
        )
        self._assign_process_to_job = self._bind(
            "AssignProcessToJobObject",
            [_HANDLE, _HANDLE],
            _BOOL,
        )
        self._is_process_in_job = self._bind(
            "IsProcessInJob",
            [_HANDLE, _HANDLE, ctypes.POINTER(_BOOL)],
            _BOOL,
        )
        self._create_toolhelp_snapshot = self._bind(
            "CreateToolhelp32Snapshot",
            [_DWORD, _DWORD],
            _HANDLE,
        )
        self._thread32_first = self._bind(
            "Thread32First",
            [_HANDLE, ctypes.POINTER(_ThreadEntry32)],
            _BOOL,
        )
        self._thread32_next = self._bind(
            "Thread32Next",
            [_HANDLE, ctypes.POINTER(_ThreadEntry32)],
            _BOOL,
        )
        self._open_thread = self._bind(
            "OpenThread",
            [_DWORD, _BOOL, _DWORD],
            _HANDLE,
        )
        self._resume_thread = self._bind(
            "ResumeThread",
            [_HANDLE],
            _DWORD,
        )
        self._terminate_job_object = self._bind(
            "TerminateJobObject",
            [_HANDLE, ctypes.c_uint32],
            _BOOL,
        )
        self._close_handle = self._bind(
            "CloseHandle",
            [_HANDLE],
            _BOOL,
        )

    def create_kill_on_close_job(self) -> int:
        raw_job = self._create_job_object(None, None)
        try:
            job_handle = _required_handle(raw_job)
        except WindowsJobOperationError:
            raise WindowsJobUnavailableError(_GENERIC_UNAVAILABLE_DETAIL) from None
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        configured = _as_int(
            self._set_information_job_object(
                _HANDLE(job_handle),
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
        )
        if configured == 0:
            with suppress(WindowsJobOperationError):
                self.close_handle(job_handle)
            raise WindowsJobUnavailableError(_GENERIC_UNAVAILABLE_DETAIL)
        return job_handle

    def open_process_for_assignment(self, pid: int) -> int:
        desired_access = (
            _PROCESS_SET_QUOTA
            | _PROCESS_TERMINATE
            | _PROCESS_QUERY_LIMITED_INFORMATION
        )
        return _required_handle(
            self._open_process(desired_access, 0, _DWORD(pid))
        )

    def assign_process(self, job_handle: int, process_handle: int) -> None:
        if (
            _as_int(
                self._assign_process_to_job(
                    _HANDLE(job_handle),
                    _HANDLE(process_handle),
                )
            )
            == 0
        ):
            raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)

    def is_process_in_job(self, process_handle: int, job_handle: int) -> bool:
        in_job = _BOOL()
        if (
            _as_int(
                self._is_process_in_job(
                    _HANDLE(process_handle),
                    _HANDLE(job_handle),
                    ctypes.byref(in_job),
                )
            )
            == 0
        ):
            raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
        return bool(in_job.value)

    def open_sole_thread(
        self,
        pid: int,
        *,
        deadline_check: DeadlineCheck,
    ) -> int:
        deadline_check()
        snapshot = _required_snapshot_handle(
            self._create_toolhelp_snapshot(_TH32CS_SNAPTHREAD, 0)
        )
        thread_ids: list[int] = []
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(_ThreadEntry32)
            has_entry = _as_int(
                self._thread32_first(_HANDLE(snapshot), ctypes.byref(entry))
            )
            while has_entry != 0:
                deadline_check()
                if int(entry.th32OwnerProcessID) == pid:
                    thread_ids.append(int(entry.th32ThreadID))
                    if len(thread_ids) > 1:
                        break
                entry.dwSize = ctypes.sizeof(_ThreadEntry32)
                _set_windows_last_error(0)
                has_entry = _as_int(
                    self._thread32_next(_HANDLE(snapshot), ctypes.byref(entry))
                )
                if has_entry == 0 and _windows_last_error() != _ERROR_NO_MORE_FILES:
                    raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
        finally:
            self.close_handle(snapshot)

        deadline_check()
        if len(thread_ids) != 1:
            raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
        return _required_handle(
            self._open_thread(_THREAD_SUSPEND_RESUME, 0, _DWORD(thread_ids[0]))
        )

    def resume_thread(self, thread_handle: int) -> int:
        previous_suspend_count = _as_int(
            self._resume_thread(_HANDLE(thread_handle))
        )
        if previous_suspend_count == 0xFFFFFFFF:
            raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
        return previous_suspend_count

    def terminate_job(self, job_handle: int) -> None:
        if (
            _as_int(
                self._terminate_job_object(_HANDLE(job_handle), 1)
            )
            == 0
        ):
            raise WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)

    def close_handle(self, handle: int) -> None:
        if _as_int(self._close_handle(_HANDLE(handle))) == 0:
            raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)

    def _bind(
        self,
        name: str,
        argtypes: list[object],
        restype: object,
    ) -> _WinFunction:
        try:
            function = cast(_WinFunction, getattr(self._kernel32, name))
        except AttributeError:
            raise WindowsJobUnavailableError(_GENERIC_UNAVAILABLE_DETAIL) from None
        function.argtypes = argtypes
        function.restype = restype
        return function


class WindowsJobLifecycle:
    """Own a suspended-spawned process and its kill-on-close Job Object."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        job_handle: int,
        api: WindowsJobApi,
    ) -> None:
        self.process = process
        self._job_handle: int | None = job_handle
        self._api = api
        self._lock = threading.Lock()
        self._contained = False
        self._stopped = False
        self._pipes_closed = False

    def mark_contained(self) -> None:
        """Record verified membership before the suspended thread is resumed."""

        with self._lock:
            if self._stopped:
                raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
            self._contained = True

    def finish(self, *, cleanup_expires_at: float) -> None:
        """Close a successful invocation while killing any descendants.

        ``ActiveProcesses`` is intentionally not used as a completion gate:
        Windows job accounting retains processes while any process handle is
        open, including the handle owned internally by ``Popen``.
        """

        self.stop(cleanup_expires_at=cleanup_expires_at)

    def stop(self, *, cleanup_expires_at: float) -> None:
        """Terminate the job, reap the leader, and close containment once."""

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            job_handle = self._job_handle
            contained = self._contained

        if job_handle is None:
            raise WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)

        cleanup_failure: WindowsJobCleanupError | None = None
        if contained:
            try:
                self._api.terminate_job(job_handle)
            except (WindowsJobCleanupError, WindowsJobOperationError):
                cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)
        else:
            # Before membership has been verified, TerminateJobObject cannot
            # be trusted to reach the still-suspended leader.
            try:
                self.process.kill()
            except (OSError, subprocess.SubprocessError):
                cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)

        if cleanup_failure is not None:
            # Closing a KILL_ON_CLOSE job is the tree-wide backstop when the
            # explicit termination call fails.
            with suppress(WindowsJobCleanupError):
                self.close_containment()

        try:
            self.process.wait(timeout=_remaining(cleanup_expires_at))
        except (OSError, subprocess.SubprocessError):
            cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)

        try:
            self.close_containment()
        except WindowsJobCleanupError:
            cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)

        if cleanup_failure is not None:
            raise cleanup_failure from None

    def close_pipes(self) -> None:
        """Close both owned pipe objects once."""

        with self._lock:
            if self._pipes_closed:
                return
            self._pipes_closed = True
        for pipe in (self.process.stdout, self.process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()

    def close_containment(self) -> None:
        """Close the job handle once; KILL_ON_CLOSE remains the final backstop."""

        with self._lock:
            job_handle = self._job_handle
            if job_handle is None:
                return
            try:
                self._api.close_handle(job_handle)
            except WindowsJobOperationError:
                raise WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL) from None
            self._job_handle = None


def spawn_suspended_in_job(
    command: Sequence[str],
    *,
    cwd: str | PathLike[str] | None,
    env: Mapping[str, str] | None,
    deadline_check: DeadlineCheck,
    cleanup_timeout_seconds: float,
    api: WindowsJobApi | None = None,
    popen_factory: PopenFactory | None = None,
) -> WindowsJobLifecycle:
    """Spawn suspended, contain and verify it, then resume its sole thread."""

    windows_api = api if api is not None else CtypesWindowsJobApi()
    factory = popen_factory if popen_factory is not None else subprocess.Popen
    deadline_check()
    job_handle = windows_api.create_kill_on_close_job()
    process: subprocess.Popen[bytes] | None = None
    lifecycle: WindowsJobLifecycle | None = None
    try:
        deadline_check()
        process = factory(  # noqa: S603 - never invokes a shell
            list(command),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            cwd=cwd,
            env=None if env is None else dict(env),
            creationflags=CREATE_SUSPENDED,
        )
        lifecycle = WindowsJobLifecycle(
            process,
            job_handle=job_handle,
            api=windows_api,
        )
        deadline_check()
        process_handle = windows_api.open_process_for_assignment(process.pid)
        try:
            deadline_check()
            windows_api.assign_process(job_handle, process_handle)
            deadline_check()
            if not windows_api.is_process_in_job(process_handle, job_handle):
                raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
            lifecycle.mark_contained()
        finally:
            windows_api.close_handle(process_handle)

        deadline_check()
        thread_handle = windows_api.open_sole_thread(
            process.pid,
            deadline_check=deadline_check,
        )
        try:
            deadline_check()
            if windows_api.resume_thread(thread_handle) != 1:
                raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
        finally:
            windows_api.close_handle(thread_handle)
        deadline_check()
        return lifecycle
    except BaseException:
        cleanup_expires_at = time.monotonic() + cleanup_timeout_seconds
        cleanup_failure: WindowsJobCleanupError | None = None
        if lifecycle is not None:
            try:
                lifecycle.stop(cleanup_expires_at=cleanup_expires_at)
            except WindowsJobCleanupError as exc:
                cleanup_failure = exc
            lifecycle.close_pipes()
            try:
                lifecycle.close_containment()
            except WindowsJobCleanupError as exc:
                cleanup_failure = exc
        elif process is not None:
            cleanup_failure = _kill_uncontained_process(
                process,
                windows_api=windows_api,
                job_handle=job_handle,
                cleanup_expires_at=cleanup_expires_at,
            )
        else:
            try:
                windows_api.close_handle(job_handle)
            except WindowsJobOperationError:
                cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)
        if cleanup_failure is not None:
            raise cleanup_failure from None
        raise


def _required_handle(raw_handle: object) -> int:
    handle = _as_optional_int(raw_handle)
    if handle is None or handle == 0:
        raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
    return handle


def _required_snapshot_handle(raw_handle: object) -> int:
    handle = _required_handle(raw_handle)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise WindowsJobOperationError(_GENERIC_OPERATION_DETAIL)
    return handle


def _as_int(value: object) -> int:
    converted = _as_optional_int(value)
    if converted is None:
        return 0
    return converted


def _as_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    raw_value = getattr(value, "value", None)
    return raw_value if isinstance(raw_value, int) else None


def _remaining(expires_at: float) -> float:
    return max(0.0, expires_at - time.monotonic())


def _windows_last_error() -> int:
    get_last_error = cast(
        Callable[[], int],
        getattr(ctypes, "get_last_error"),  # noqa: B009
    )
    return get_last_error()


def _set_windows_last_error(value: int) -> None:
    set_last_error = cast(
        Callable[[int], int],
        getattr(ctypes, "set_last_error"),  # noqa: B009
    )
    set_last_error(value)


def _kill_uncontained_process(
    process: subprocess.Popen[bytes],
    *,
    windows_api: WindowsJobApi,
    job_handle: int,
    cleanup_expires_at: float,
) -> WindowsJobCleanupError | None:
    """Best-effort fallback if lifecycle allocation failed after suspended spawn."""

    cleanup_failure: WindowsJobCleanupError | None = None
    try:
        process.kill()
    except (OSError, subprocess.SubprocessError):
        cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)
    try:
        process.wait(timeout=_remaining(cleanup_expires_at))
    except (OSError, subprocess.SubprocessError):
        cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            with suppress(OSError):
                pipe.close()
    try:
        windows_api.close_handle(job_handle)
    except WindowsJobOperationError:
        cleanup_failure = WindowsJobCleanupError(_GENERIC_CLEANUP_DETAIL)
    return cleanup_failure
