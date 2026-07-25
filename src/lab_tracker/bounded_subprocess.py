"""Bounded, redacting subprocess execution for external artifact adapters.

The executor in this module is deliberately narrower than ``subprocess.run``:
one absolute monotonic deadline covers process execution and concurrent pipe
drainage, stdout and stderr have independent hard caps, and failure cleanup
targets the whole POSIX process group.  Raw stderr and command arguments never
cross the boundary.

The execution deadline does not include process-group cleanup.  Cleanup has its
own small, fixed upper bound (terminate grace plus kill/reap grace), so a command
can exceed its execution deadline only by that documented bound.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from os import PathLike
from typing import IO, Protocol

DEFAULT_PROCESS_DEADLINE_SECONDS = 30.0
MAX_PROCESS_DEADLINE_SECONDS = 86_400.0
DEFAULT_PROCESS_STDERR_LIMIT_BYTES = 64 * 1024

_DEFAULT_CHUNK_SIZE = 64 * 1024
_DEFAULT_TERMINATE_GRACE_SECONDS = 0.25
_DEFAULT_KILL_GRACE_SECONDS = 1.0
_MAX_CLEANUP_PHASE_SECONDS = 5.0
_MINIMUM_CLEANUP_SECONDS = 0.10
_POLL_INTERVAL_SECONDS = 0.01

_GENERIC_EXECUTION_DETAIL = "Subprocess execution failed."
_GENERIC_DEADLINE_DETAIL = "Subprocess execution deadline exceeded."
_GENERIC_OUTPUT_DETAIL = "Subprocess output limit exceeded."
_GENERIC_CONSUMER_DETAIL = "Subprocess output consumer failed."
_GENERIC_CLEANUP_DETAIL = "Subprocess cleanup failed."
_GENERIC_PLATFORM_DETAIL = "Subprocess execution is unavailable on this platform."

Clock = Callable[[], float]
StdoutConsumer = Callable[[bytes], None]


class ProcessExecutionError(RuntimeError):
    """A child failed without exposing its command, target, or raw stderr."""


class ProcessDeadlineExceeded(ProcessExecutionError):
    """The absolute process execution deadline expired."""


class ProcessOutputLimitExceeded(ProcessExecutionError):
    """A child exceeded either independent pipe limit."""


class ProcessConsumerError(ProcessExecutionError):
    """The trusted streaming stdout consumer failed."""


class ProcessCleanupError(ProcessExecutionError):
    """A failed child could not be killed and reaped within the cleanup bound."""


class ProcessUnsupportedPlatformError(ProcessExecutionError):
    """The platform cannot provide the required descendant containment."""


@dataclass(frozen=True)
class ProcessDeadline:
    """One immutable monotonic deadline shared across logical commands."""

    expires_at: float
    clock: Clock = field(default=time.monotonic, repr=False, compare=False)

    @classmethod
    def after(
        cls,
        seconds: float,
        *,
        clock: Clock = time.monotonic,
    ) -> ProcessDeadline:
        """Create a deadline after a finite positive duration of at most one day."""

        _validate_deadline_seconds(seconds)
        now = clock()
        if not math.isfinite(now) or not math.isfinite(now + seconds):
            raise ValueError("Process deadline clock must be finite.")
        return cls(expires_at=now + seconds, clock=clock)

    def remaining(self) -> float:
        """Return remaining execution seconds, clamped to zero after expiry."""

        remaining = self.expires_at - self.clock()
        return remaining if math.isfinite(remaining) and remaining > 0 else 0.0

    def check(self) -> None:
        """Raise a redacted error once the absolute deadline has expired."""

        if self.remaining() <= 0:
            raise ProcessDeadlineExceeded(_GENERIC_DEADLINE_DETAIL)


@dataclass(frozen=True)
class ProcessResult:
    """A completed child result containing no raw stderr.

    ``stdout`` contains captured, bounded metadata output.  When a
    ``stdout_consumer`` was supplied, bytes are delivered only to that consumer
    and ``stdout`` is empty.  The byte counters remain available in both modes.
    """

    returncode: int
    stdout: bytes
    stdout_bytes: int
    stderr_bytes: int


class ProcessExecutor(Protocol):
    """Port for one bounded subprocess invocation."""

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
    ) -> ProcessResult: ...


@dataclass
class _PipeState:
    limit: int
    consumer: StdoutConsumer | None = None
    count: int = 0
    captured: bytearray = field(default_factory=bytearray)


class _FailureState:
    """Thread-safe first-failure slot; all stored exceptions are redacted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failure: ProcessExecutionError | None = None
        self.event = threading.Event()

    def set(self, failure: ProcessExecutionError) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = failure
                self.event.set()

    def get(self) -> ProcessExecutionError | None:
        with self._lock:
            return self._failure


class _ProcessLifecycle:
    """Idempotent, race-safe ownership of process termination and pipes."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        terminate_grace_seconds: float,
        kill_grace_seconds: float,
    ) -> None:
        self._process = process
        self._terminate_grace_seconds = terminate_grace_seconds
        self._kill_grace_seconds = kill_grace_seconds
        self._lock = threading.Lock()
        self._stopped = False
        self._pipes_closed = False

    def stop(self, *, cleanup_expires_at: float) -> None:
        """Terminate, kill, and reap the full POSIX process group once."""

        with self._lock:
            if self._stopped:
                return
            self._stopped = True

        if os.name != "posix":
            raise ProcessUnsupportedPlatformError(_GENERIC_PLATFORM_DETAIL)

        # The leader may already have exited while a descendant still owns a
        # pipe.  Always target the group created by start_new_session.
        _signal_process_group(self._process.pid, signal.SIGTERM)
        terminate_expires_at = min(
            cleanup_expires_at,
            time.monotonic() + self._terminate_grace_seconds,
        )
        _wait_for_process_group_exit(
            self._process.pid,
            expires_at=terminate_expires_at,
            process=self._process,
        )
        if _process_group_exists(self._process.pid):
            _signal_process_group(self._process.pid, signal.SIGKILL)

        try:
            self._process.wait(timeout=_remaining_cleanup(cleanup_expires_at))
        except subprocess.TimeoutExpired:
            _signal_process_group(self._process.pid, signal.SIGKILL)
            raise ProcessCleanupError(_GENERIC_CLEANUP_DETAIL) from None
        except (OSError, subprocess.SubprocessError):
            raise ProcessCleanupError(_GENERIC_CLEANUP_DETAIL) from None
        _wait_for_process_group_exit(
            self._process.pid,
            expires_at=cleanup_expires_at,
            process=self._process,
        )
        if _process_group_exists(self._process.pid):
            raise ProcessCleanupError(_GENERIC_CLEANUP_DETAIL)

    def close_pipes(self) -> None:
        """Close both owned pipe objects once, suppressing redacted cleanup noise."""

        with self._lock:
            if self._pipes_closed:
                return
            self._pipes_closed = True
        for pipe in (self._process.stdout, self._process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()


class BoundedSubprocessExecutor:
    """Execute commands with bounded time, memory, and POSIX descendants.

    Non-POSIX platforms fail closed before spawning.  Python's Windows process
    group flag does not provide kill-on-close descendant containment; a future
    Windows implementation must use a Job Object rather than pretending that
    terminating only the group leader is sufficient.
    """

    def __init__(
        self,
        *,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        terminate_grace_seconds: float = _DEFAULT_TERMINATE_GRACE_SECONDS,
        kill_grace_seconds: float = _DEFAULT_KILL_GRACE_SECONDS,
    ) -> None:
        _validate_positive_int(chunk_size, name="Process pipe chunk size")
        _validate_cleanup_duration(
            terminate_grace_seconds, name="Process terminate grace"
        )
        _validate_cleanup_duration(kill_grace_seconds, name="Process kill grace")
        self._chunk_size = chunk_size
        self._terminate_grace_seconds = terminate_grace_seconds
        self._kill_grace_seconds = kill_grace_seconds

    @property
    def maximum_cleanup_seconds(self) -> float:
        """Maximum configured cleanup overrun after execution ends or fails."""

        return max(
            _MINIMUM_CLEANUP_SECONDS,
            self._terminate_grace_seconds + self._kill_grace_seconds,
        )

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
        """Run one argument-vector command, redacting all operational failures."""

        _validate_command(command)
        _validate_nonnegative_int(stdout_limit_bytes, name="stdout byte limit")
        _validate_nonnegative_int(stderr_limit_bytes, name="stderr byte limit")
        if os.name != "posix":
            raise ProcessUnsupportedPlatformError(_GENERIC_PLATFORM_DETAIL)
        deadline.check()

        try:
            process = subprocess.Popen(  # noqa: S603 - never invokes a shell
                list(command),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                cwd=cwd,
                env=None if env is None else dict(env),
                start_new_session=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ProcessExecutionError(_GENERIC_EXECUTION_DETAIL) from None

        lifecycle = _ProcessLifecycle(
            process,
            terminate_grace_seconds=self._terminate_grace_seconds,
            kill_grace_seconds=self._kill_grace_seconds,
        )
        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        if stdout_pipe is None or stderr_pipe is None:
            lifecycle.stop(
                cleanup_expires_at=time.monotonic() + self.maximum_cleanup_seconds
            )
            lifecycle.close_pipes()
            raise ProcessExecutionError(_GENERIC_EXECUTION_DETAIL)

        stdout_state = _PipeState(
            limit=stdout_limit_bytes,
            consumer=stdout_consumer,
        )
        stderr_state = _PipeState(
            limit=stderr_limit_bytes,
            consumer=_discard_output,
        )
        failure = _FailureState()
        threads = (
            self._reader_thread(
                name="lab-tracker-process-stdout",
                pipe=stdout_pipe,
                state=stdout_state,
                deadline=deadline,
                failure=failure,
            ),
            self._reader_thread(
                name="lab-tracker-process-stderr",
                pipe=stderr_pipe,
                state=stderr_state,
                deadline=deadline,
                failure=failure,
            ),
        )

        try:
            for thread in threads:
                thread.start()
            returncode = self._wait_for_completion(
                process,
                threads=threads,
                deadline=deadline,
                failure=failure,
            )
            for thread in threads:
                thread.join()
            if _process_group_exists(process.pid):
                cleanup_expires_at = (
                    time.monotonic() + self.maximum_cleanup_seconds
                )
                lifecycle.stop(cleanup_expires_at=cleanup_expires_at)
            deadline.check()
        except ProcessExecutionError as primary:
            self._cleanup_failure(lifecycle, threads=threads, primary=primary)
            raise
        except Exception:
            generic_failure = ProcessExecutionError(_GENERIC_EXECUTION_DETAIL)
            self._cleanup_failure(
                lifecycle,
                threads=threads,
                primary=generic_failure,
            )
            raise generic_failure from None
        except BaseException:
            # KeyboardInterrupt and other control-flow exceptions must not leave
            # an external command or its descendants behind.
            self._cleanup_failure(
                lifecycle,
                threads=threads,
                primary=ProcessExecutionError(_GENERIC_EXECUTION_DETAIL),
            )
            raise
        finally:
            lifecycle.close_pipes()

        return ProcessResult(
            returncode=returncode,
            stdout=bytes(stdout_state.captured),
            stdout_bytes=stdout_state.count,
            stderr_bytes=stderr_state.count,
        )

    def _reader_thread(
        self,
        *,
        name: str,
        pipe: IO[bytes],
        state: _PipeState,
        deadline: ProcessDeadline,
        failure: _FailureState,
    ) -> threading.Thread:
        return threading.Thread(
            target=self._drain_pipe,
            kwargs={
                "pipe": pipe,
                "state": state,
                "deadline": deadline,
                "failure": failure,
            },
            name=name,
            daemon=False,
        )

    def _drain_pipe(
        self,
        *,
        pipe: IO[bytes],
        state: _PipeState,
        deadline: ProcessDeadline,
        failure: _FailureState,
    ) -> None:
        try:
            while True:
                deadline.check()
                # Never allocate more than the remaining allowance plus the one
                # byte needed to detect overflow.
                read_size = min(self._chunk_size, state.limit - state.count + 1)
                try:
                    chunk = os.read(pipe.fileno(), read_size)
                except OSError:
                    failure.set(ProcessExecutionError(_GENERIC_EXECUTION_DETAIL))
                    return
                if not chunk:
                    return
                next_count = state.count + len(chunk)
                if next_count > state.limit:
                    failure.set(ProcessOutputLimitExceeded(_GENERIC_OUTPUT_DETAIL))
                    return
                state.count = next_count
                if state.consumer is None:
                    try:
                        state.captured.extend(chunk)
                    except BaseException:
                        failure.set(
                            ProcessExecutionError(_GENERIC_EXECUTION_DETAIL)
                        )
                        return
                else:
                    try:
                        state.consumer(chunk)
                    except BaseException:
                        failure.set(ProcessConsumerError(_GENERIC_CONSUMER_DETAIL))
                        return
                deadline.check()
        except ProcessDeadlineExceeded as exc:
            failure.set(exc)
        except BaseException:
            failure.set(ProcessExecutionError(_GENERIC_EXECUTION_DETAIL))

    def _wait_for_completion(
        self,
        process: subprocess.Popen[bytes],
        *,
        threads: Sequence[threading.Thread],
        deadline: ProcessDeadline,
        failure: _FailureState,
    ) -> int:
        while True:
            current_failure = failure.get()
            if current_failure is not None:
                raise current_failure
            try:
                returncode = process.poll()
            except (OSError, subprocess.SubprocessError):
                raise ProcessExecutionError(_GENERIC_EXECUTION_DETAIL) from None
            if returncode is not None and all(not thread.is_alive() for thread in threads):
                deadline.check()
                current_failure = failure.get()
                if current_failure is not None:
                    raise current_failure
                return returncode
            remaining = deadline.remaining()
            if remaining <= 0:
                raise ProcessDeadlineExceeded(_GENERIC_DEADLINE_DETAIL)
            failure.event.wait(min(_POLL_INTERVAL_SECONDS, remaining))

    def _cleanup_failure(
        self,
        lifecycle: _ProcessLifecycle,
        *,
        threads: Sequence[threading.Thread],
        primary: ProcessExecutionError,
    ) -> None:
        cleanup_expires_at = time.monotonic() + self.maximum_cleanup_seconds
        try:
            lifecycle.stop(cleanup_expires_at=cleanup_expires_at)
        except ProcessExecutionError as cleanup_error:
            lifecycle.close_pipes()
            self._join_readers(threads, cleanup_expires_at=cleanup_expires_at)
            raise cleanup_error from primary
        lifecycle.close_pipes()
        self._join_readers(threads, cleanup_expires_at=cleanup_expires_at)

    def _join_readers(
        self,
        threads: Sequence[threading.Thread],
        *,
        cleanup_expires_at: float,
    ) -> None:
        for thread in threads:
            if thread.ident is None:
                continue
            thread.join(timeout=_remaining_cleanup(cleanup_expires_at))
        if any(thread.ident is not None and thread.is_alive() for thread in threads):
            raise ProcessCleanupError(_GENERIC_CLEANUP_DETAIL)


def _validate_deadline_seconds(seconds: float) -> None:
    if (
        not math.isfinite(seconds)
        or seconds <= 0
        or seconds > MAX_PROCESS_DEADLINE_SECONDS
    ):
        raise ValueError(
            "Process deadline must be finite, positive, and no more than 86400 seconds."
        )


def _validate_cleanup_duration(value: float, *, name: str) -> None:
    if not math.isfinite(value) or value < 0 or value > _MAX_CLEANUP_PHASE_SECONDS:
        raise ValueError(f"{name} must be finite and between 0 and 5 seconds.")


def _validate_positive_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def _validate_nonnegative_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")


def _validate_command(command: Sequence[str]) -> None:
    if (
        not command
        or isinstance(command, (str, bytes))
        or not isinstance(command[0], str)
        or not command[0]
        or any(not isinstance(part, str) or "\x00" in part for part in command)
    ):
        raise ValueError(
            "Process command must have a non-empty executable and NUL-free string arguments."
        )


def _signal_process_group(process_group_id: int, signum: int) -> None:
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        return
    except OSError:
        raise ProcessCleanupError(_GENERIC_CLEANUP_DETAIL) from None


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _wait_for_process_group_exit(
    process_group_id: int,
    *,
    expires_at: float,
    process: subprocess.Popen[bytes],
) -> None:
    while True:
        try:
            process.poll()
        except (OSError, subprocess.SubprocessError):
            raise ProcessCleanupError(_GENERIC_CLEANUP_DETAIL) from None
        if not _process_group_exists(process_group_id):
            return
        remaining = expires_at - time.monotonic()
        if remaining <= 0:
            return
        threading.Event().wait(min(_POLL_INTERVAL_SECONDS, remaining))


def _remaining_cleanup(expires_at: float) -> float:
    return max(0.0, expires_at - time.monotonic())


def _discard_output(_chunk: bytes) -> None:
    """Drain a bounded stream without retaining potentially sensitive bytes."""
