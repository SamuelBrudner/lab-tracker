from __future__ import annotations

import ctypes
import gc
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from lab_tracker import bounded_subprocess
from lab_tracker.bounded_subprocess import (
    MAX_PROCESS_DEADLINE_SECONDS,
    BoundedSubprocessExecutor,
    ProcessCleanupError,
    ProcessConsumerError,
    ProcessDeadline,
    ProcessDeadlineExceeded,
    ProcessExecutionError,
    ProcessExecutor,
    ProcessOutputLimitExceeded,
    ProcessUnsupportedPlatformError,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _python(source: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source)


def _deadline(seconds: float = 5.0) -> ProcessDeadline:
    return ProcessDeadline.after(seconds)


def _run(
    source: str,
    *,
    stdout_limit: int = 64 * 1024,
    stderr_limit: int = 64 * 1024,
    consumer: Callable[[bytes], None] | None = None,
    deadline: ProcessDeadline | None = None,
) -> bounded_subprocess.ProcessResult:
    return BoundedSubprocessExecutor().run(
        _python(source),
        deadline=deadline or _deadline(),
        stdout_limit_bytes=stdout_limit,
        stderr_limit_bytes=stderr_limit,
        stdout_consumer=consumer,
    )


@pytest.mark.parametrize(
    "seconds",
    [
        0.0,
        -1.0,
        float("inf"),
        float("-inf"),
        float("nan"),
        MAX_PROCESS_DEADLINE_SECONDS + 0.001,
    ],
)
def test_process_deadline_rejects_invalid_duration(seconds: float) -> None:
    with pytest.raises(ValueError, match="finite, positive"):
        ProcessDeadline.after(seconds)


def test_process_deadline_is_absolute_immutable_and_clamped() -> None:
    clock = FakeClock()
    deadline = ProcessDeadline.after(4.0, clock=clock)

    assert deadline.expires_at == 104.0
    assert deadline.remaining() == 4.0
    clock.value = 102.5
    assert deadline.remaining() == 1.5
    clock.value = 200.0
    assert deadline.remaining() == 0.0
    with pytest.raises(ProcessDeadlineExceeded) as raised:
        deadline.check()
    assert str(raised.value) == "Subprocess execution deadline exceeded."
    with pytest.raises(AttributeError):
        deadline.expires_at = 300.0  # type: ignore[misc]


def test_process_deadline_rejects_nonfinite_clock() -> None:
    with pytest.raises(ValueError, match="clock must be finite"):
        ProcessDeadline.after(1.0, clock=lambda: float("inf"))


def test_executor_satisfies_typed_port_and_exposes_cleanup_bound() -> None:
    executor: ProcessExecutor = BoundedSubprocessExecutor(
        terminate_grace_seconds=0.02,
        kill_grace_seconds=0.03,
    )

    assert isinstance(executor, BoundedSubprocessExecutor)
    assert executor.maximum_cleanup_seconds == pytest.approx(0.10)


def test_capture_preserves_return_code_and_bounded_stdout_but_not_stderr() -> None:
    result = _run(
        "import sys; sys.stdout.buffer.write(b'meta'); "
        "sys.stderr.buffer.write(b'credential=secret'); sys.exit(7)"
    )

    assert result.returncode == 7
    assert result.stdout == b"meta"
    assert result.stdout_bytes == 4
    assert result.stderr_bytes == len(b"credential=secret")
    assert not hasattr(result, "stderr")


def test_streaming_consumer_receives_stdout_without_buffering_it() -> None:
    consumed = bytearray()

    result = _run(
        "import sys; sys.stdout.buffer.write(b'a' * 131073)",
        stdout_limit=131073,
        consumer=consumed.extend,
    )

    assert consumed == b"a" * 131073
    assert result.stdout == b""
    assert result.stdout_bytes == 131073
    assert result.stderr_bytes == 0


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_exact_independent_pipe_limit_is_allowed(stream: str) -> None:
    target = "stdout" if stream == "stdout" else "stderr"
    result = _run(
        f"import sys; sys.{target}.buffer.write(b'x' * 4096)",
        stdout_limit=4096 if stream == "stdout" else 1,
        stderr_limit=4096 if stream == "stderr" else 1,
    )

    assert getattr(result, f"{stream}_bytes") == 4096


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_one_byte_over_independent_pipe_limit_kills_and_redacts(stream: str) -> None:
    secret = "do-not-leak-token"
    target = "stdout" if stream == "stdout" else "stderr"

    with pytest.raises(ProcessOutputLimitExceeded) as raised:
        _run(
            f"import sys, time; sys.{target}.buffer.write(b'x' * 4097); "
            f"sys.{target}.buffer.flush(); "
            f"sys.stderr.write('{secret}') if '{target}' == 'stderr' else None; "
            "time.sleep(30)",
            stdout_limit=4096 if stream == "stdout" else 64 * 1024,
            stderr_limit=4096 if stream == "stderr" else 64 * 1024,
        )

    assert str(raised.value) == "Subprocess output limit exceeded."
    assert secret not in str(raised.value)


def test_zero_limit_allows_empty_output_and_rejects_first_byte() -> None:
    result = _run("", stdout_limit=0, stderr_limit=0)
    assert result.stdout == b""

    with pytest.raises(ProcessOutputLimitExceeded):
        _run("print('x', end='')", stdout_limit=0)


def test_process_group_signal_reaps_fast_exit_before_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def zombie_then_vanished(_process_group_id: int, _signum: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("unreaped zombie")
        raise ProcessLookupError("reaped")

    class ExitedProcess:
        def __init__(self) -> None:
            self.polls = 0

        def poll(self) -> int:
            self.polls += 1
            return 0

    process = ExitedProcess()
    monkeypatch.setattr(
        bounded_subprocess,
        "_kill_process_group",
        zombie_then_vanished,
    )

    bounded_subprocess._signal_process_group(123, 15, process=process)  # type: ignore[arg-type]

    assert attempts == 2
    assert process.polls == 1


def test_process_group_signal_falls_back_to_owned_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def always_denied(_process_group_id: int, _signum: int) -> None:
        raise PermissionError("private cleanup detail")

    class RunningProcess:
        def __init__(self) -> None:
            self.signals: list[int] = []

        def poll(self) -> None:
            return None

        def send_signal(self, signum: int) -> None:
            self.signals.append(signum)

    process = RunningProcess()
    monkeypatch.setattr(
        bounded_subprocess,
        "_kill_process_group",
        always_denied,
    )

    bounded_subprocess._signal_process_group(123, 15, process=process)  # type: ignore[arg-type]

    assert process.signals == [15]


def test_process_group_signal_preserves_leader_permission_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def always_denied(_process_group_id: int, _signum: int) -> None:
        raise PermissionError("private group detail")

    class DeniedProcess:
        def poll(self) -> None:
            return None

        def send_signal(self, _signum: int) -> None:
            raise PermissionError("private leader detail")

    monkeypatch.setattr(
        bounded_subprocess,
        "_kill_process_group",
        always_denied,
    )

    with pytest.raises(ProcessCleanupError) as raised:
        bounded_subprocess._signal_process_group(123, 15, process=DeniedProcess())  # type: ignore[arg-type]

    assert str(raised.value) == "Subprocess cleanup failed."
    assert "private group detail" not in str(raised.value)
    assert "private leader detail" not in str(raised.value)


def test_process_lifecycle_fails_when_group_survives_leader_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def group_signal_denied(_process_group_id: int, _signum: int) -> None:
        raise PermissionError("private group detail")

    class ExitedLeaderWithLiveDescendant:
        pid = 123
        stdout = None
        stderr = None

        def __init__(self) -> None:
            self.signals: list[int] = []

        def poll(self) -> int:
            return 0

        def send_signal(self, signum: int) -> None:
            self.signals.append(signum)

        def wait(self, *, timeout: float) -> int:
            del timeout
            return 0

    process = ExitedLeaderWithLiveDescendant()
    monkeypatch.setattr(
        bounded_subprocess,
        "_kill_process_group",
        group_signal_denied,
    )
    lifecycle = bounded_subprocess._PosixProcessLifecycle(  # type: ignore[arg-type]
        process,
        terminate_grace_seconds=0,
        kill_grace_seconds=0,
    )

    with pytest.raises(ProcessCleanupError):
        lifecycle.stop(cleanup_expires_at=time.monotonic())

    assert process.signals == [15, bounded_subprocess._POSIX_SIGKILL]


def test_concurrent_readers_prevent_cross_pipe_deadlock() -> None:
    size = 1024 * 1024
    result = _run(
        "import sys, threading; "
        f"size={size}; "
        "a=threading.Thread(target=lambda: "
        "sys.stdout.buffer.write(b'o' * size)); "
        "b=threading.Thread(target=lambda: "
        "sys.stderr.buffer.write(b'e' * size)); "
        "a.start(); b.start(); a.join(); b.join()",
        stdout_limit=size,
        stderr_limit=size,
    )

    assert result.stdout == b"o" * size
    assert result.stdout_bytes == size
    assert result.stderr_bytes == size


def test_callback_failure_is_generic_and_stops_child() -> None:
    secret = "callback-secret"

    def fail(_chunk: bytes) -> None:
        raise RuntimeError(secret)

    started = time.monotonic()
    with pytest.raises(ProcessConsumerError) as raised:
        _run(
            "import sys, time; print('chunk', flush=True); time.sleep(30)",
            consumer=fail,
        )

    assert time.monotonic() - started < 3.0
    assert str(raised.value) == "Subprocess output consumer failed."
    assert secret not in str(raised.value)


def test_callback_base_exception_is_converted_to_generic_failure() -> None:
    def stop_thread(_chunk: bytes) -> None:
        raise KeyboardInterrupt("must-not-escape-worker")

    with pytest.raises(ProcessConsumerError) as raised:
        _run(
            "import sys, time; print('chunk', flush=True); time.sleep(30)",
            consumer=stop_thread,
        )

    assert str(raised.value) == "Subprocess output consumer failed."


def test_spawn_failure_redacts_command_and_os_detail() -> None:
    secret = "/missing/private-token-command"

    with pytest.raises(ProcessExecutionError) as raised:
        BoundedSubprocessExecutor().run(
            (secret,),
            deadline=_deadline(),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )

    assert str(raised.value) == "Subprocess execution failed."
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("failure_type", "expected_type", "expected_detail"),
    [
        (RuntimeError, ProcessExecutionError, "Subprocess execution failed."),
        (SystemExit, ProcessExecutionError, "Subprocess execution failed."),
        (KeyboardInterrupt, KeyboardInterrupt, ""),
    ],
    ids=["ordinary-exception", "control-flow-exception", "keyboard-interrupt"],
)
def test_reader_start_failure_cleans_child_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
    expected_type: type[BaseException],
    expected_detail: str,
) -> None:
    secret = "thread-resource-secret"
    spawned: dict[str, subprocess.Popen[bytes]] = {}
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned["process"] = process
        return process

    def fail_start(_thread: threading.Thread) -> None:
        raise failure_type(secret)

    monkeypatch.setattr(bounded_subprocess.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(expected_type) as raised:
        _run("import time; time.sleep(60)")

    assert str(raised.value) == expected_detail
    assert secret not in str(raised.value)
    assert spawned["process"].poll() is not None


def test_expired_deadline_does_not_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    deadline = ProcessDeadline.after(1.0, clock=clock)
    clock.value += 2.0

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("spawn must not be called")

    monkeypatch.setattr(bounded_subprocess.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(ProcessDeadlineExceeded):
        BoundedSubprocessExecutor().run(
            ("redacted",),
            deadline=deadline,
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )


def test_popen_uses_noninteractive_closed_descriptor_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    observed: dict[str, object] = {}

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        observed.update(kwargs)
        return real_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(bounded_subprocess.subprocess, "Popen", recording_popen)
    result = _run("print('ok', end='')")

    assert result.stdout == b"ok"
    assert observed["shell"] is False
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["stdout"] is subprocess.PIPE
    assert observed["stderr"] is subprocess.PIPE
    assert observed["close_fds"] is True
    if os.name == "posix":
        assert observed["start_new_session"] is True


def test_cwd_and_explicit_environment_are_forwarded(tmp_path: Path) -> None:
    result = BoundedSubprocessExecutor().run(
        _python(
            "import os, pathlib; "
            "print(pathlib.Path.cwd().name + ':' + os.environ['BOUND_VALUE'], end='')"
        ),
        deadline=_deadline(),
        stdout_limit_bytes=100,
        stderr_limit_bytes=100,
        cwd=tmp_path,
        env={"BOUND_VALUE": "present"},
    )

    assert result.stdout == f"{tmp_path.name}:present".encode()


def test_deadline_kills_and_reaps_contained_descendants() -> None:
    descendant_pid = bytearray()
    source = """
import signal
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

def stop(_signum, _frame):
    child.wait(timeout=5)
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
print(child.pid, flush=True)
time.sleep(60)
"""

    with pytest.raises(ProcessDeadlineExceeded):
        _run(
            source,
            consumer=descendant_pid.extend,
            deadline=ProcessDeadline.after(1.0 if os.name == "nt" else 0.25),
        )

    pid = int(descendant_pid.strip())
    _assert_pid_terminated(pid)


def test_leader_exit_with_inherited_pipe_is_still_bounded_by_deadline() -> None:
    descendant_pid = bytearray()
    source = """
import subprocess
import sys

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
"""

    with pytest.raises(ProcessDeadlineExceeded):
        _run(
            source,
            consumer=descendant_pid.extend,
            deadline=ProcessDeadline.after(1.0 if os.name == "nt" else 0.25),
        )

    pid = int(descendant_pid.strip())
    _assert_pid_terminated(pid)


def test_successful_leader_exit_still_cleans_descendant_with_closed_pipes() -> None:
    source = """
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
"""

    result = _run(source)

    assert result.returncode == 0
    pid = int(result.stdout.strip())
    _assert_pid_terminated(pid)


def test_cleanup_uses_one_advertised_absolute_bound() -> None:
    executor = BoundedSubprocessExecutor(
        terminate_grace_seconds=0.05,
        kill_grace_seconds=0.10,
    )
    execution_seconds = 1.0 if os.name == "nt" else 0.10
    started = time.monotonic()

    with pytest.raises(ProcessDeadlineExceeded):
        executor.run(
            _python(
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            ),
            deadline=ProcessDeadline.after(execution_seconds),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )

    elapsed = time.monotonic() - started
    # Process startup and scheduler jitter get a small allowance; cleanup does
    # not receive a fresh full timeout at each phase.
    assert elapsed < execution_seconds + executor.maximum_cleanup_seconds + 0.50


def test_successful_containment_cleanup_is_outside_execution_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    deadline = ProcessDeadline.after(1.0, clock=clock)
    process = subprocess.Popen(  # noqa: S603 - fixed Python test command
        _python("print('ok', end='')"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )

    class AdvancingCleanupLifecycle:
        def __init__(self) -> None:
            self.process = process

        def finish(self, *, cleanup_expires_at: float) -> None:
            assert cleanup_expires_at > time.monotonic()
            clock.value = deadline.expires_at + 10.0

        def stop(self, *, cleanup_expires_at: float) -> None:
            assert cleanup_expires_at > time.monotonic()
            if process.poll() is None:
                process.kill()
            process.wait()

        def close_pipes(self) -> None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

        def close_containment(self) -> None:
            pass

    executor = BoundedSubprocessExecutor()
    lifecycle = AdvancingCleanupLifecycle()
    monkeypatch.setattr(
        executor,
        "_spawn_lifecycle",
        lambda *_args, **_kwargs: lifecycle,
    )

    result = executor.run(
        ("ignored",),
        deadline=deadline,
        stdout_limit_bytes=10,
        stderr_limit_bytes=10,
    )

    assert result.returncode == 0
    assert result.stdout == b"ok"
    assert deadline.remaining() == 0.0


def test_unknown_platform_fails_closed_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("spawn must not be called")

    monkeypatch.setattr(bounded_subprocess.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(ProcessUnsupportedPlatformError) as raised:
        BoundedSubprocessExecutor(_platform_name="unsupported").run(
            ("redacted",),
            deadline=_deadline(),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )
    assert str(raised.value) == "Subprocess execution is unavailable on this platform."


def test_invalid_commands_limits_and_cleanup_configuration_fail_before_spawn() -> None:
    with pytest.raises(ValueError, match="non-empty executable"):
        BoundedSubprocessExecutor().run(
            (),
            deadline=_deadline(),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )
    with pytest.raises(ValueError, match="non-empty executable"):
        BoundedSubprocessExecutor().run(
            "not-an-argv",  # type: ignore[arg-type]
            deadline=_deadline(),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )
    with pytest.raises(ValueError, match="NUL-free"):
        BoundedSubprocessExecutor().run(
            ("unused", "bad\x00argument"),
            deadline=_deadline(),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )
    with pytest.raises(ValueError, match="non-negative"):
        BoundedSubprocessExecutor().run(
            ("unused",),
            deadline=_deadline(),
            stdout_limit_bytes=-1,
            stderr_limit_bytes=1,
        )
    with pytest.raises(ValueError, match="positive integer"):
        BoundedSubprocessExecutor(chunk_size=0)
    with pytest.raises(ValueError, match="between 0 and 5"):
        BoundedSubprocessExecutor(terminate_grace_seconds=float("inf"))


def test_reader_threads_are_named_non_daemon_and_joined() -> None:
    before = {thread.ident for thread in threading.enumerate()}
    _run("print('ok')")
    leaked = [
        thread
        for thread in threading.enumerate()
        if thread.ident not in before and thread.name.startswith("lab-tracker-process-")
    ]
    assert leaked == []


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows Job Objects")
def test_windows_failure_before_assignment_never_executes_child(
    tmp_path: Path,
) -> None:
    from lab_tracker._windows_job import (
        CtypesWindowsJobApi,
        WindowsJobOperationError,
    )

    marker = tmp_path / "must-not-run"

    class RejectBeforeAssignment:
        def __init__(self) -> None:
            self._delegate = CtypesWindowsJobApi()

        def create_kill_on_close_job(self) -> int:
            return self._delegate.create_kill_on_close_job()

        def open_process_for_assignment(self, _pid: int) -> int:
            raise WindowsJobOperationError(
                "private assignment failure must be redacted"
            )

        def close_handle(self, handle: int) -> None:
            self._delegate.close_handle(handle)

    with pytest.raises(ProcessExecutionError) as raised:
        BoundedSubprocessExecutor(
            _windows_api=RejectBeforeAssignment(),  # type: ignore[arg-type]
        ).run(
            _python(
                "from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('executed')"
            ),
            deadline=_deadline(),
            stdout_limit_bytes=1,
            stderr_limit_bytes=1,
        )

    assert str(raised.value) == "Subprocess execution failed."
    assert "private assignment failure" not in str(raised.value)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows handles")
def test_windows_executor_does_not_leak_process_job_or_pipe_handles() -> None:
    before = _windows_handle_count()
    source = """
import subprocess
import sys

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
print(child.pid, flush=True)
"""

    for _ in range(6):
        result = _run(source)
        _assert_pid_terminated(int(result.stdout.strip()))
    gc.collect()

    assert _windows_handle_count() <= before + 2


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows Job Objects")
@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [
        ("stdout", ProcessOutputLimitExceeded),
        ("stderr", ProcessOutputLimitExceeded),
        ("consumer", ProcessConsumerError),
    ],
)
def test_windows_failure_kills_descendant_tree(
    tmp_path: Path,
    failure_mode: str,
    expected_error: type[ProcessExecutionError],
) -> None:
    child_pid_path = tmp_path / f"{failure_mode}-child-pid"
    source = f"""
import pathlib
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))
if {failure_mode!r} == "stdout":
    sys.stdout.buffer.write(b"x" * 4097)
    sys.stdout.buffer.flush()
elif {failure_mode!r} == "stderr":
    sys.stderr.buffer.write(b"x" * 4097)
    sys.stderr.buffer.flush()
else:
    print("consumer-trigger", flush=True)
time.sleep(60)
"""

    def fail_consumer(_chunk: bytes) -> None:
        raise RuntimeError("consumer-private-detail")

    with pytest.raises(expected_error):
        _run(
            source,
            stdout_limit=4096,
            stderr_limit=4096,
            consumer=fail_consumer if failure_mode == "consumer" else None,
        )

    child_pid = int(child_pid_path.read_text())
    _assert_pid_terminated(child_pid)


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows Job Objects")
def test_windows_job_close_is_tree_wide_backstop_when_terminate_fails() -> None:
    from lab_tracker._windows_job import (
        CtypesWindowsJobApi,
        WindowsJobCleanupError,
    )

    descendant_pid = bytearray()

    class FailExplicitTermination:
        def __init__(self) -> None:
            self._delegate = CtypesWindowsJobApi()

        def __getattr__(self, name: str) -> object:
            return getattr(self._delegate, name)

        def terminate_job(self, _job_handle: int) -> None:
            raise WindowsJobCleanupError(
                "private termination failure must be redacted"
            )

    def capture_then_fail(chunk: bytes) -> None:
        descendant_pid.extend(chunk)
        raise RuntimeError("consumer-private-detail")

    source = """
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""
    with pytest.raises(ProcessCleanupError) as raised:
        BoundedSubprocessExecutor(
            _windows_api=FailExplicitTermination(),  # type: ignore[arg-type]
        ).run(
            _python(source),
            deadline=_deadline(),
            stdout_limit_bytes=100,
            stderr_limit_bytes=100,
            stdout_consumer=capture_then_fail,
        )

    assert str(raised.value) == "Subprocess cleanup failed."
    assert "private termination failure" not in str(raised.value)
    _assert_pid_terminated(int(descendant_pid.strip()))


def _assert_pid_terminated(pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return
        time.sleep(0.01)
    pytest.fail(f"child process {pid} was not terminated")


def _pid_is_running(pid: int) -> bool:
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _windows_pid_is_running(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.c_uint32,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int32
    process_handle = kernel32.OpenProcess(0x00100000, 0, pid)
    if not process_handle:
        return False
    try:
        return kernel32.WaitForSingleObject(process_handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(process_handle)


def _windows_handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int32
    count = ctypes.c_uint32()
    assert kernel32.GetProcessHandleCount(
        kernel32.GetCurrentProcess(),
        ctypes.byref(count),
    )
    return int(count.value)
