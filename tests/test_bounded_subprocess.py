from __future__ import annotations

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
    ProcessConsumerError,
    ProcessDeadline,
    ProcessDeadlineExceeded,
    ProcessExecutionError,
    ProcessExecutor,
    ProcessOutputLimitExceeded,
    ProcessUnsupportedPlatformError,
)


@pytest.fixture(autouse=True)
def _require_posix_except_for_fail_closed_test(request: pytest.FixtureRequest) -> None:
    if (
        os.name != "posix"
        and request.node.name != "test_non_posix_fails_closed_before_spawn"
    ):
        pytest.skip(
            "The security boundary intentionally fails closed without POSIX groups."
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


def test_reader_start_failure_cleans_child_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "thread-resource-secret"
    spawned: dict[str, subprocess.Popen[bytes]] = {}
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        spawned["process"] = process
        return process

    def fail_start(_thread: threading.Thread) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(bounded_subprocess.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(threading.Thread, "start", fail_start)

    with pytest.raises(ProcessExecutionError) as raised:
        _run("import time; time.sleep(60)")

    assert str(raised.value) == "Subprocess execution failed."
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


def test_popen_uses_noninteractive_closed_descriptor_process_group_flags(
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


def test_deadline_kills_and_reaps_process_group() -> None:
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
            deadline=ProcessDeadline.after(0.25),
        )

    pid = int(descendant_pid.strip())
    _assert_pid_disappears(pid)


def test_leader_exit_with_inherited_pipe_is_still_bounded_by_group_deadline() -> None:
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
            deadline=ProcessDeadline.after(0.25),
        )

    pid = int(descendant_pid.strip())
    _assert_pid_disappears(pid)


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
    _assert_pid_disappears(pid)


def test_cleanup_uses_one_advertised_absolute_bound() -> None:
    executor = BoundedSubprocessExecutor(
        terminate_grace_seconds=0.05,
        kill_grace_seconds=0.10,
    )
    execution_seconds = 0.10
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


def test_non_posix_fails_closed_before_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bounded_subprocess.os, "name", "nt")

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("spawn must not be called")

    monkeypatch.setattr(bounded_subprocess.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(ProcessUnsupportedPlatformError) as raised:
        BoundedSubprocessExecutor().run(
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


def _assert_pid_disappears(pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"child process {pid} was not reaped")
