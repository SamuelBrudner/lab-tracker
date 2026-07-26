from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from os import PathLike
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import lab_tracker.local_filesystem_authority as authority_module
import lab_tracker.local_filesystem_operations as operations_module
from lab_tracker.bounded_subprocess import (
    BoundedSubprocessExecutor,
    ProcessDeadline,
    ProcessResult,
    StdoutConsumer,
)
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthority
from lab_tracker.local_filesystem_operations import (
    LOCAL_FILESYSTEM_PROTOCOL_VERSION,
    LOCAL_FILESYSTEM_REQUEST_ENV,
    MAX_LOCAL_FILESYSTEM_REQUEST_BYTES,
    MAX_LOCAL_RECOVERY_RESPONSE_BYTES,
    BoundedLocalFilesystemOperations,
)
from lab_tracker.local_filesystem_ports import (
    DirectLocalRecoveryScope,
    DirectLocalRegularFileTarget,
    EnumeratedLocalRegularFileTarget,
    LocalDirectoryInspection,
    LocalRecoveryEnumerationOutcome,
    LocalRegularFileReadOutcome,
    RegisteredLocalRecoveryScope,
    RegisteredLocalRegularFileTarget,
)
from lab_tracker.local_resolution_budget import (
    LocalResolutionBudget,
    LocalResolutionLimits,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FatalOperationFailure(BaseException):
    pass


class RecordingExecutor:
    def __init__(
        self,
        outcomes: Sequence[ProcessResult | BaseException],
        *,
        after_run: Callable[[], None] | None = None,
        stdout_chunks: Sequence[Sequence[bytes]] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self.after_run = after_run
        self._stdout_chunks = (
            [tuple(chunks) for chunks in stdout_chunks]
            if stdout_chunks is not None
            else [()] * len(self._outcomes)
        )
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
        chunks = self._stdout_chunks.pop(0)
        if stdout_consumer is not None:
            for chunk in chunks:
                stdout_consumer(chunk)
        if self.after_run is not None:
            self.after_run()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _result(
    returncode: int,
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


def _enumeration_stdout(
    *,
    status: str = "complete",
    directories: int = 1,
    candidates: list[dict[str, object]] | None = None,
) -> bytes:
    return json.dumps(
        {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "status": status,
            "directories": directories,
            "candidates": [] if candidates is None else candidates,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _operations(
    root: Path,
    executor: RecordingExecutor,
) -> BoundedLocalFilesystemOperations:
    return BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([root]),
        executor=executor,
    )


def _deadline(clock: FakeClock | None = None) -> ProcessDeadline:
    return ProcessDeadline.after(5.0, clock=clock or FakeClock())


def test_regular_file_broker_streams_direct_bytes_under_the_exact_budget(
    tmp_path: Path,
) -> None:
    root = tmp_path / "operator-secret-root"
    candidate = root / "artifact.bin"
    payload = b"\x00raw\r\n\x1a\xff"
    executor = RecordingExecutor(
        [_result(0, stdout_bytes=len(payload))],
        stdout_chunks=[(payload[:3], payload[3:])],
    )
    operations = _operations(root, executor)
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=32, deadline_seconds=5.0),
        clock=FakeClock(),
    )
    received = bytearray()

    result = operations.read_regular_file(
        DirectLocalRegularFileTarget(str(candidate)),
        budget=budget,
        stdout_consumer=received.extend,
    )

    assert result.outcome is LocalRegularFileReadOutcome.COMPLETE
    assert result.bytes_read == len(payload)
    assert bytes(received) == payload
    assert budget.remaining_bytes == 32 - len(payload)
    assert budget.terminal is False
    call = executor.calls[0]
    assert call["deadline"] is budget.deadline
    assert call["stdout_limit_bytes"] == 33
    assert call["stderr_limit_bytes"] == 0
    assert call["stdout_consumer"] is not None
    request = json.loads(call["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request == {
        "candidate": str(candidate),
        "max_bytes": 32,
        "op": "read-file",
        "roots": [str(root)],
        "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
    }


def test_regular_file_broker_keeps_registered_root_and_locator_separate(
    tmp_path: Path,
) -> None:
    operator_root = tmp_path / "operator"
    store_root = operator_root / "store"
    executor = RecordingExecutor([_result(0)])
    operations = _operations(operator_root, executor)
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=17, deadline_seconds=5.0),
        clock=FakeClock(),
    )

    result = operations.read_regular_file(
        RegisteredLocalRegularFileTarget(
            str(store_root),
            ("nested", "artifact.bin"),
        ),
        budget=budget,
        stdout_consumer=lambda _chunk: None,
    )

    assert result.outcome is LocalRegularFileReadOutcome.COMPLETE
    request = json.loads(executor.calls[0]["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request == {
        "locator": ["nested", "artifact.bin"],
        "max_bytes": 17,
        "op": "read-registered-file",
        "roots": [str(operator_root)],
        "store_root": str(store_root),
        "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
    }
    assert "candidate" not in request


def test_recovery_broker_returns_path_free_direct_targets_under_exact_budget(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-secret-root"
    second_root = tmp_path / "second-secret-root"
    stdout = _enumeration_stdout(
        directories=3,
        candidates=[
            {"root_index": 1, "locator": ["nested", "result.csv"]},
            {"root_index": 0, "locator": ["fallback.bin"]},
        ],
    )
    executor = RecordingExecutor([_result(0, stdout=stdout)])
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([first_root, second_root]),
        executor=executor,
    )
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=17, deadline_seconds=5.0),
        clock=FakeClock(),
    )

    result = operations.enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name="result.csv",
        max_files=2,
        max_directories=7,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.COMPLETE
    assert result.directories_visited == 3
    assert [candidate.name for candidate in result.candidates] == [
        "result.csv",
        "fallback.bin",
    ]
    assert [
        (candidate.target.root_index, candidate.target.locator)
        for candidate in result.candidates
        if isinstance(candidate.target, EnumeratedLocalRegularFileTarget)
    ] == [
        (1, ("nested", "result.csv")),
        (0, ("fallback.bin",)),
    ]
    assert budget.remaining_bytes == 17
    assert budget.terminal is False
    call = executor.calls[0]
    assert call["deadline"] is budget.deadline
    assert call["stdout_limit_bytes"] == MAX_LOCAL_RECOVERY_RESPONSE_BYTES
    assert call["stderr_limit_bytes"] == 0
    assert call["stdout_consumer"] is None
    request = json.loads(call["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request == {
        "max_directories": 7,
        "max_files": 2,
        "op": "enumerate-files",
        "roots": [str(first_root), str(second_root)],
        "target_name": "result.csv",
        "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
    }


def test_recovery_broker_keeps_registered_enumeration_nested(
    tmp_path: Path,
) -> None:
    operator_root = tmp_path / "operator"
    store_root = operator_root / "store"
    stdout = _enumeration_stdout(
        directories=2,
        candidates=[{"root_index": 0, "locator": ["nested", "artifact.bin"]}]
    )
    executor = RecordingExecutor([_result(0, stdout=stdout)])
    operations = _operations(operator_root, executor)
    budget = LocalResolutionBudget(clock=FakeClock())

    result = operations.enumerate_recovery_candidates(
        RegisteredLocalRecoveryScope(str(store_root)),
        target_name=None,
        max_files=4,
        max_directories=5,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.COMPLETE
    candidate = result.candidates[0]
    assert candidate.name == "artifact.bin"
    assert type(candidate.target) is RegisteredLocalRegularFileTarget
    assert candidate.target.store_root == str(store_root)
    assert candidate.target.locator == ("nested", "artifact.bin")
    request = json.loads(executor.calls[0]["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request == {
        "max_directories": 5,
        "max_files": 4,
        "op": "enumerate-registered-files",
        "roots": [str(operator_root)],
        "store_root": str(store_root),
        "target_name": None,
        "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
    }


def test_windows_registered_recovery_preserves_raw_store_identity_for_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_store_root = "C:/grant/store/"
    executor = RecordingExecutor(
        [
            _result(
                0,
                stdout=_enumeration_stdout(
                    directories=2,
                    candidates=[
                        {
                            "root_index": 0,
                            "locator": ["nested", "artifact.bin"],
                        }
                    ]
                ),
            )
        ]
    )
    monkeypatch.setattr(operations_module.os, "name", "nt")
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([r"C:\grant"]),
        executor=executor,
    )

    result = operations.enumerate_recovery_candidates(
        RegisteredLocalRecoveryScope(raw_store_root),
        target_name=None,
        max_files=1,
        max_directories=2,
        budget=LocalResolutionBudget(clock=FakeClock()),
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.COMPLETE
    target = result.candidates[0].target
    assert type(target) is RegisteredLocalRegularFileTarget
    assert target.store_root == raw_store_root
    request = json.loads(executor.calls[0]["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request["roots"] == [r"C:\grant"]
    assert request["store_root"] == r"C:\grant\store"


def test_enumerated_direct_target_reads_through_retained_root_boundary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "operator"
    executor = RecordingExecutor([_result(0)])
    operations = _operations(root, executor)
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=9),
        clock=FakeClock(),
    )

    result = operations.read_regular_file(
        EnumeratedLocalRegularFileTarget(0, ("nested", "artifact.bin")),
        budget=budget,
        stdout_consumer=lambda _chunk: None,
    )

    assert result.outcome is LocalRegularFileReadOutcome.COMPLETE
    request = json.loads(executor.calls[0]["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request == {
        "locator": ["nested", "artifact.bin"],
        "max_bytes": 9,
        "op": "read-registered-file",
        "roots": [str(root)],
        "store_root": str(root),
        "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
    }


@pytest.mark.parametrize(
    "stdout",
    (
        b"",
        b"{}",
        b'{"candidates":[],"directories":0,"status":"complete","v":2}',
        b'{"candidates":[],"directories":0,"extra":1,"status":"complete","v":1}',
        _enumeration_stdout(directories=0),
        _enumeration_stdout(
            directories=0,
            candidates=[{"root_index": 0, "locator": ["artifact.bin"]}],
        ),
        _enumeration_stdout(
            directories=1,
            candidates=[
                {
                    "root_index": 0,
                    "locator": ["nested", "artifact.bin"],
                }
            ],
        ),
        _enumeration_stdout(directories=3),
        _enumeration_stdout(
            candidates=[
                {"root_index": 0, "locator": ["fallback.bin"]},
                {"root_index": 0, "locator": ["result.csv"]},
            ]
        ),
        _enumeration_stdout(
            candidates=[{"root_index": 1, "locator": ["artifact.bin"]}]
        ),
        _enumeration_stdout(
            candidates=[{"root_index": 0, "locator": ["..", "artifact.bin"]}]
        ),
        _enumeration_stdout(
            candidates=[
                {"root_index": 0, "locator": ["artifact.bin"]},
                {"root_index": 0, "locator": ["artifact.bin"]},
            ]
        ),
    ),
)
def test_malformed_recovery_output_is_all_or_nothing_and_terminal(
    tmp_path: Path,
    stdout: bytes,
) -> None:
    executor = RecordingExecutor([_result(0, stdout=stdout)])
    budget = LocalResolutionBudget(clock=FakeClock())

    result = _operations(tmp_path, executor).enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name="result.csv",
        max_files=2,
        max_directories=2,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.FAILED
    assert result.candidates == ()
    assert result.directories_visited == 0
    assert budget.terminal is True
    assert budget.remaining_bytes == 0


@pytest.mark.parametrize(
    "process_result",
    (
        _result(3, stdout=_enumeration_stdout()),
        _result(
            0,
            stdout=_enumeration_stdout(),
            stdout_bytes=len(_enumeration_stdout()) - 1,
        ),
        _result(0, stdout=_enumeration_stdout(), stderr_bytes=1),
        cast(ProcessResult, object()),
    ),
)
def test_recovery_process_metadata_uncertainty_discards_every_candidate(
    tmp_path: Path,
    process_result: ProcessResult,
) -> None:
    budget = LocalResolutionBudget(clock=FakeClock())

    result = _operations(
        tmp_path,
        RecordingExecutor([process_result]),
    ).enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=1,
        max_directories=1,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.FAILED
    assert result.candidates == ()
    assert result.directories_visited == 0
    assert budget.terminal is True
    assert budget.remaining_bytes == 0


@pytest.mark.parametrize(
    "component",
    ("CON", "artifact.", "artifact ", "bad:name", "bad\ud800name"),
)
def test_windows_recovery_output_rejects_unsupported_components_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    stdout = _enumeration_stdout(
        candidates=[{"root_index": 0, "locator": [component]}]
    )
    budget = LocalResolutionBudget(clock=FakeClock())
    operations = _operations(
        tmp_path,
        RecordingExecutor([_result(0, stdout=stdout)]),
    )
    monkeypatch.setattr(operations_module.os, "name", "nt")

    result = operations.enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=1,
        max_directories=1,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.FAILED
    assert result.candidates == ()
    assert budget.terminal is True


def test_windows_recovery_locator_uses_utf16_authority_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations_module.os, "name", "nt")
    exact_total = (*(("a" * 255,) * 63), "b" * 127, "c" * 128)
    over_total = (*(("a" * 255,) * 63), "b" * 127, "c" * 129)

    assert operations_module._valid_recovery_locator(("é" * 128,))
    assert operations_module._valid_recovery_locator(("a" * 255,))
    assert not operations_module._valid_recovery_locator(("a" * 256,))
    assert operations_module._valid_recovery_locator(exact_total)
    assert not operations_module._valid_recovery_locator(over_total)


def test_recovery_limit_result_remains_usable_until_candidates_are_read(
    tmp_path: Path,
) -> None:
    stdout = _enumeration_stdout(
        status="limit",
        directories=2,
        candidates=[{"root_index": 0, "locator": ["artifact.bin"]}],
    )
    budget = LocalResolutionBudget(clock=FakeClock())

    result = _operations(
        tmp_path,
        RecordingExecutor([_result(0, stdout=stdout)]),
    ).enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=1,
        max_directories=2,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.LIMIT_REACHED
    assert len(result.candidates) == 1
    assert result.directories_visited == 2
    assert budget.terminal is False


def test_zero_recovery_caps_and_unscoped_roots_spawn_no_process(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor([])
    operations = _operations(tmp_path, executor)

    zero_files = operations.enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=0,
        max_directories=3,
        budget=LocalResolutionBudget(clock=FakeClock()),
    )
    zero_directories = operations.enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=3,
        max_directories=0,
        budget=LocalResolutionBudget(clock=FakeClock()),
    )
    unscoped = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.for_unscoped_library_compatibility(),
        executor=executor,
    ).enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=3,
        max_directories=3,
        budget=LocalResolutionBudget(clock=FakeClock()),
    )

    assert zero_files.outcome is LocalRecoveryEnumerationOutcome.COMPLETE
    assert zero_directories.outcome is LocalRecoveryEnumerationOutcome.LIMIT_REACHED
    assert unscoped.outcome is LocalRecoveryEnumerationOutcome.COMPLETE
    assert executor.calls == []


def test_recovery_executor_failure_and_late_deadline_abort_budget(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    budget = LocalResolutionBudget(
        LocalResolutionLimits(deadline_seconds=1),
        clock=clock,
    )
    executor = RecordingExecutor(
        [_result(0, stdout=_enumeration_stdout())],
        after_run=lambda: clock.advance(2),
    )

    result = _operations(tmp_path, executor).enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=1,
        max_directories=1,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.FAILED
    assert budget.terminal is True


def test_recovery_deadline_covers_response_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    budget = LocalResolutionBudget(
        LocalResolutionLimits(deadline_seconds=1),
        clock=clock,
    )
    original_parser = operations_module._parse_enumeration_result

    def parse_after_deadline(*args: Any, **kwargs: Any):
        parsed = original_parser(*args, **kwargs)
        clock.advance(2)
        return parsed

    monkeypatch.setattr(
        operations_module,
        "_parse_enumeration_result",
        parse_after_deadline,
    )

    result = _operations(
        tmp_path,
        RecordingExecutor([_result(0, stdout=_enumeration_stdout())]),
    ).enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=1,
        max_directories=1,
        budget=budget,
    )

    assert result.outcome is LocalRecoveryEnumerationOutcome.FAILED
    assert result.candidates == ()
    assert budget.terminal is True


def test_recovery_fatal_executor_failure_aborts_before_propagating(
    tmp_path: Path,
) -> None:
    fatal = FatalOperationFailure("operator interruption")
    budget = LocalResolutionBudget(clock=FakeClock())

    with pytest.raises(FatalOperationFailure) as exc_info:
        _operations(
            tmp_path,
            RecordingExecutor([fatal]),
        ).enumerate_recovery_candidates(
            DirectLocalRecoveryScope(),
            target_name=None,
            max_files=1,
            max_directories=1,
            budget=budget,
        )

    assert exc_info.value is fatal
    assert budget.terminal is True


def test_enabled_recovery_configuration_must_fit_one_helper_request() -> None:
    separator = "\\" if os.name == "nt" else "/"
    anchor = "C:\\" if os.name == "nt" else "/"
    long_roots = [
        f"{anchor}{'a' * 200}{separator}{index:02d}{'b' * 190}"
        for index in range(64)
    ]
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots(long_roots),
        executor=RecordingExecutor([]),
    )

    with pytest.raises(
        ValueError,
        match="roots exceed the bounded helper request limit",
    ):
        operations.validate_direct_recovery_configuration(
            max_files=4096,
            max_directories=4096,
        )

    operations.validate_direct_recovery_configuration(
        max_files=0,
        max_directories=4096,
    )


def test_enabled_recovery_configuration_requires_a_readable_candidate_envelope() -> None:
    separator = "\\" if os.name == "nt" else "/"
    anchor = "C:\\" if os.name == "nt" else "/"
    root = anchor + separator.join(["a" * 245] * 50)
    executor = RecordingExecutor([])
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([root]),
        executor=executor,
    )

    with pytest.raises(
        ValueError,
        match="roots exceed the bounded helper request limit",
    ):
        operations.validate_direct_recovery_configuration(
            max_files=4096,
            max_directories=4096,
        )

    assert executor.calls == []


@pytest.mark.parametrize(
    ("platform_name", "root", "component"),
    (
        ("posix", "/grant", "é" * 127),
        ("nt", r"C:\grant", "é" * 255),
    ),
)
def test_candidate_read_envelope_overflow_is_a_limit_not_a_terminal_failure(
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    root: str,
    component: str,
) -> None:
    monkeypatch.setattr(operations_module.os, "name", platform_name)
    long_locator = (component,) * 64
    executor = RecordingExecutor(
        [
            _result(
                0,
                stdout=_enumeration_stdout(
                    directories=64,
                    candidates=[
                        {
                            "root_index": 0,
                            "locator": list(long_locator),
                        },
                        {
                            "root_index": 0,
                            "locator": ["match.bin"],
                        },
                    ],
                ),
            ),
            _result(0),
        ]
    )
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([root]),
        executor=executor,
    )
    budget = LocalResolutionBudget(clock=FakeClock())

    enumeration = operations.enumerate_recovery_candidates(
        DirectLocalRecoveryScope(),
        target_name=None,
        max_files=2,
        max_directories=64,
        budget=budget,
    )

    assert enumeration.outcome is LocalRecoveryEnumerationOutcome.LIMIT_REACHED
    assert len(enumeration.candidates) == 1
    candidate = enumeration.candidates[0]
    assert candidate.name == "match.bin"
    assert budget.terminal is False

    read = operations.read_regular_file(
        candidate.target,
        budget=budget,
        stdout_consumer=lambda _chunk: None,
    )

    assert read.outcome is LocalRegularFileReadOutcome.COMPLETE
    assert budget.terminal is False
    request = json.loads(executor.calls[1]["env"][LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request["locator"] == ["match.bin"]


@pytest.mark.parametrize(
    ("returncode", "expected"),
    (
        (4, LocalRegularFileReadOutcome.MISSING),
        (2, LocalRegularFileReadOutcome.DENIED),
    ),
)
def test_clean_zero_pre_read_outcomes_release_the_reservation(
    tmp_path: Path,
    returncode: int,
    expected: LocalRegularFileReadOutcome,
) -> None:
    executor = RecordingExecutor([_result(returncode)])
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=11, deadline_seconds=5.0),
        clock=FakeClock(),
    )

    result = _operations(tmp_path, executor).read_regular_file(
        DirectLocalRegularFileTarget(str(tmp_path / "artifact.bin")),
        budget=budget,
        stdout_consumer=lambda _chunk: None,
    )

    assert result.outcome is expected
    assert result.bytes_read == 0
    assert budget.remaining_bytes == 11
    assert budget.terminal is False


@pytest.mark.parametrize(
    "result",
    (
        _result(0, stdout=b"captured", stdout_bytes=0),
        _result(0, stdout_bytes=True),
        _result(0, stdout_bytes=-1),
        _result(0, stderr_bytes=1),
        _result(True),
        _result(1),
        _result(3),
        _result(4, stdout_bytes=1),
    ),
)
def test_malformed_or_failed_regular_file_result_consumes_budget_terminally(
    tmp_path: Path,
    result: ProcessResult,
) -> None:
    executor = RecordingExecutor([result])
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=11, deadline_seconds=5.0),
        clock=FakeClock(),
    )

    read = _operations(tmp_path, executor).read_regular_file(
        DirectLocalRegularFileTarget(str(tmp_path / "artifact.bin")),
        budget=budget,
        stdout_consumer=lambda _chunk: None,
    )

    assert read.outcome is LocalRegularFileReadOutcome.FAILED
    assert budget.remaining_bytes == 0
    assert budget.terminal is True


def test_partial_executor_failure_discards_bytes_and_consumes_budget(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor(
        [RuntimeError("secret helper failure")],
        stdout_chunks=[(b"partial",)],
    )
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=11, deadline_seconds=5.0),
        clock=FakeClock(),
    )
    received = bytearray()

    read = _operations(tmp_path, executor).read_regular_file(
        DirectLocalRegularFileTarget(str(tmp_path / "artifact.bin")),
        budget=budget,
        stdout_consumer=received.extend,
    )

    assert bytes(received) == b"partial"
    assert read.outcome is LocalRegularFileReadOutcome.FAILED
    assert read.bytes_read == 0
    assert budget.terminal is True
    assert budget.remaining_bytes == 0


def test_fatal_executor_failure_consumes_budget_before_propagating(
    tmp_path: Path,
) -> None:
    fatal = FatalOperationFailure("operator interruption")
    executor = RecordingExecutor(
        [fatal],
        stdout_chunks=[(b"partial",)],
    )
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=11, deadline_seconds=5.0),
        clock=FakeClock(),
    )

    with pytest.raises(FatalOperationFailure) as exc_info:
        _operations(tmp_path, executor).read_regular_file(
            DirectLocalRegularFileTarget(str(tmp_path / "artifact.bin")),
            budget=budget,
            stdout_consumer=lambda _chunk: None,
        )

    assert exc_info.value is fatal
    assert budget.terminal is True
    assert budget.remaining_bytes == 0


def test_denied_regular_file_target_never_spawns_or_debits_budget(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside.bin"
    executor = RecordingExecutor([])
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=11, deadline_seconds=5.0),
        clock=FakeClock(),
    )

    result = _operations(allowed, executor).read_regular_file(
        DirectLocalRegularFileTarget(str(outside)),
        budget=budget,
        stdout_consumer=lambda _chunk: None,
    )

    assert result.outcome is LocalRegularFileReadOutcome.DENIED
    assert executor.calls == []
    assert budget.remaining_bytes == 11
    assert budget.terminal is False


def test_broker_uses_fixed_isolated_command_and_exact_compact_request(
    tmp_path: Path,
) -> None:
    root = tmp_path / "operator-secret-root"
    candidate = root / "candidate-secret"
    executor = RecordingExecutor([_result(0)])
    operations = _operations(root, executor)
    deadline = _deadline()

    result = operations.inspect_directory(str(candidate), deadline=deadline)

    assert result is LocalDirectoryInspection.ACCESSIBLE
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["command"] == [
        sys.executable,
        "-I",
        "-S",
        "-B",
        os.fspath(operations_module._HELPER_PATH),
    ]
    assert str(root) not in call["command"]
    assert str(candidate) not in call["command"]
    assert call["deadline"] is deadline
    assert call["stdout_limit_bytes"] == 0
    assert call["stderr_limit_bytes"] == 0
    assert call["stdout_consumer"] is None
    assert call["cwd"] is None
    environment = call["env"]
    assert isinstance(environment, dict)
    request = environment[LOCAL_FILESYSTEM_REQUEST_ENV]
    assert request == json.dumps(
        {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "op": "inspect-directory",
            "candidate": str(candidate),
            "roots": [str(root)],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert request.isascii()
    assert len(request.encode("utf-8")) <= MAX_LOCAL_FILESYSTEM_REQUEST_BYTES


def test_environment_is_minimal_and_does_not_inherit_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_UNRELATED_SECRET", "do-not-inherit")
    monkeypatch.setenv("PATH", "do-not-inherit")
    executor = RecordingExecutor([_result(0)])
    operations = _operations(tmp_path, executor)

    operations.inspect_directory(str(tmp_path / "store"), deadline=_deadline())

    environment = executor.calls[0]["env"]
    assert isinstance(environment, dict)
    assert "LAB_TRACKER_UNRELATED_SECRET" not in environment
    assert "PATH" not in environment
    allowed = {LOCAL_FILESYSTEM_REQUEST_ENV}
    if os.name == "nt":
        allowed.update(name for name in environment if name.upper() in {"SYSTEMROOT", "WINDIR"})
    elif os.name == "posix":
        allowed.update({"LANG", "LC_ALL", "LC_CTYPE"} & environment.keys())
    assert environment.keys() <= allowed


def test_broker_preserves_unresolved_alias_parent_suffix_for_helper(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    candidate = f"{root}/link/.."
    expected_candidate = str(root / "link" / "..") if os.name == "nt" else candidate
    executor = RecordingExecutor([_result(0)])

    result = _operations(root, executor).inspect_directory(
        candidate,
        deadline=_deadline(),
    )

    assert result is LocalDirectoryInspection.ACCESSIBLE
    environment = executor.calls[0]["env"]
    assert isinstance(environment, dict)
    request = json.loads(environment[LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request["candidate"] == expected_candidate
    assert request["roots"] == [str(root)]


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("directory", LocalDirectoryInspection.ACCESSIBLE),
        ("file", LocalDirectoryInspection.DENIED),
        ("missing", LocalDirectoryInspection.DENIED),
    ),
)
def test_real_bounded_helper_maps_directory_file_and_missing(
    tmp_path: Path,
    kind: str,
    expected: LocalDirectoryInspection,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    candidate = allowed / kind
    if kind == "directory":
        candidate.mkdir()
    elif kind == "file":
        candidate.write_bytes(b"not a directory")
    operations = BoundedLocalFilesystemOperations(
        LocalFilesystemAuthority.from_roots([allowed]),
        BoundedSubprocessExecutor(),
    )

    assert (
        operations.inspect_directory(
            str(candidate),
            deadline=ProcessDeadline.after(5.0),
        )
        is expected
    )


@pytest.mark.skipif(sys.platform != "linux", reason="requires Linux bytes paths")
def test_real_bounded_helper_preserves_surrogateescaped_directory_names(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    raw_candidate = os.fsencode(allowed) + b"/non-utf8-\xff"
    os.mkdir(raw_candidate)
    candidate = os.fsdecode(raw_candidate)
    operations = BoundedLocalFilesystemOperations(
        LocalFilesystemAuthority.from_roots([allowed]),
        BoundedSubprocessExecutor(),
    )

    assert (
        operations.inspect_directory(
            candidate,
            deadline=ProcessDeadline.after(5.0),
        )
        is LocalDirectoryInspection.ACCESSIBLE
    )


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symbolic links")
def test_real_helper_preserves_native_alias_parent_traversal(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    target = allowed / "target" / "nested"
    target.mkdir(parents=True)
    link = allowed / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    operations = BoundedLocalFilesystemOperations(
        LocalFilesystemAuthority.from_roots([allowed]),
        BoundedSubprocessExecutor(),
    )

    assert (
        operations.inspect_directory(
            f"{link}/..",
            deadline=ProcessDeadline.after(5.0),
        )
        is LocalDirectoryInspection.ACCESSIBLE
    )


@pytest.mark.parametrize(
    ("returncode", "expected"),
    (
        (0, LocalDirectoryInspection.ACCESSIBLE),
        (2, LocalDirectoryInspection.DENIED),
        (3, LocalDirectoryInspection.FAILED),
        (1, LocalDirectoryInspection.FAILED),
        (4, LocalDirectoryInspection.FAILED),
        (-9, LocalDirectoryInspection.FAILED),
    ),
)
def test_helper_exit_codes_map_to_exact_finite_results(
    tmp_path: Path,
    returncode: int,
    expected: LocalDirectoryInspection,
) -> None:
    executor = RecordingExecutor([_result(returncode)])

    result = _operations(tmp_path, executor).inspect_directory(
        str(tmp_path / "store"),
        deadline=_deadline(),
    )

    assert result is expected


@pytest.mark.parametrize(
    "result",
    (
        _result(0, stdout=b"x"),
        _result(0, stdout_bytes=1),
        _result(0, stderr_bytes=1),
        _result(2, stdout=b"x"),
        _result(3, stderr_bytes=1),
    ),
)
def test_any_helper_output_is_a_failed_protocol(
    tmp_path: Path,
    result: ProcessResult,
) -> None:
    executor = RecordingExecutor([result])

    assert (
        _operations(tmp_path, executor).inspect_directory(
            str(tmp_path / "store"),
            deadline=_deadline(),
        )
        is LocalDirectoryInspection.FAILED
    )


def test_malformed_executor_result_maps_to_finite_failed_outcome(
    tmp_path: Path,
) -> None:
    executor = RecordingExecutor([cast(ProcessResult, object())])

    assert (
        _operations(tmp_path, executor).inspect_directory(
            str(tmp_path / "store"),
            deadline=_deadline(),
        )
        is LocalDirectoryInspection.FAILED
    )


def test_deny_all_disjoint_sibling_and_ambiguous_candidates_never_spawn(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    candidates = (
        tmp_path / "other",
        tmp_path / "allowed-sibling",
        f"{root}/../escape",
    )
    authorities = (
        LocalFilesystemAuthority.from_roots([]),
        LocalFilesystemAuthority.from_roots([root]),
        LocalFilesystemAuthority.from_roots([root]),
        LocalFilesystemAuthority.from_roots([root]),
    )
    executor = RecordingExecutor([])

    deny_all_result = BoundedLocalFilesystemOperations(
        authorities[0],
        executor,
    ).inspect_directory(str(root), deadline=_deadline())
    candidate_results = [
        BoundedLocalFilesystemOperations(authority, executor).inspect_directory(
            str(candidate),
            deadline=_deadline(),
        )
        for authority, candidate in zip(authorities[1:], candidates, strict=True)
    ]

    assert deny_all_result is LocalDirectoryInspection.DENIED
    assert candidate_results == [LocalDirectoryInspection.DENIED] * 3
    assert executor.calls == []


def test_oversized_candidate_is_denied_by_bounded_admission_without_spawning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    oversized_component = "x" * MAX_LOCAL_FILESYSTEM_REQUEST_BYTES
    executor = RecordingExecutor([])

    result = _operations(root, executor).inspect_directory(
        f"{root}/{oversized_component}",
        deadline=_deadline(),
    )

    assert result is LocalDirectoryInspection.DENIED
    assert executor.calls == []


@pytest.mark.parametrize(
    "candidate",
    (
        "//host/share",
        "/allowed/bad\0name",
        "/allowed/bad\nname",
        "/allowed/../escape",
    ),
)
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX path semantics")
def test_malformed_or_provably_escaping_posix_candidates_do_not_spawn(
    candidate: str,
) -> None:
    authority = LocalFilesystemAuthority.from_roots(["/allowed"])
    executor = RecordingExecutor([])

    result = BoundedLocalFilesystemOperations(
        authority,
        executor,
    ).inspect_directory(candidate, deadline=_deadline())

    assert result is LocalDirectoryInspection.DENIED
    assert executor.calls == []


def test_oversized_candidate_component_is_denied_without_spawning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    oversized_component = "x" * (authority_module.MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES + 1)
    executor = RecordingExecutor([])

    result = _operations(root, executor).inspect_directory(
        f"{root}/{oversized_component}",
        deadline=_deadline(),
    )

    assert result is LocalDirectoryInspection.DENIED
    assert executor.calls == []


def test_malformed_windows_candidates_do_not_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_os = SimpleNamespace(
        name="nt",
        fspath=os.fspath,
        getcwd=lambda: r"C:\startup",
    )
    monkeypatch.setattr(authority_module, "os", windows_os)
    authority = LocalFilesystemAuthority.from_roots([r"C:\Allowed"])
    executor = RecordingExecutor([])
    operations = BoundedLocalFilesystemOperations(authority, executor)
    invalid = (
        "/",
        r"\\server\share",
        r"\\?\C:\Allowed\store",
        r"C:\Allowed\artifact:stream",
        r"C:\Allowed\trailing.",
        "C:\\Allowed\\trailing ",
        r"C:\Allowed\CON",
        r"C:\Allowed\..\escape",
    )

    results = [
        operations.inspect_directory(candidate, deadline=_deadline()) for candidate in invalid
    ]

    assert results == [LocalDirectoryInspection.DENIED] * len(invalid)
    assert executor.calls == []


def test_safe_windows_separator_aliases_are_normalized_before_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_os = SimpleNamespace(
        name="nt",
        fspath=os.fspath,
        getcwd=lambda: r"C:\startup",
    )
    monkeypatch.setattr(authority_module, "os", windows_os)
    authority = LocalFilesystemAuthority.from_roots(["c:/Allowed//"])
    executor = RecordingExecutor([_result(0)])
    operations = BoundedLocalFilesystemOperations(authority, executor)

    result = operations.inspect_directory(
        "C:\\Allowed\\store\\",
        deadline=_deadline(),
    )

    assert result is LocalDirectoryInspection.ACCESSIBLE
    environment = executor.calls[0]["env"]
    assert isinstance(environment, dict)
    request = json.loads(environment[LOCAL_FILESYSTEM_REQUEST_ENV])
    assert request["candidate"] == r"C:\Allowed\store"
    assert request["roots"] == [r"C:\Allowed"]


def test_expired_deadline_fails_before_authority_selection_or_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = LocalFilesystemAuthority.from_roots([tmp_path])
    executor = RecordingExecutor([])
    operations = BoundedLocalFilesystemOperations(authority, executor)
    clock = FakeClock()
    deadline = ProcessDeadline.after(1.0, clock=clock)
    clock.advance(2.0)

    def unexpected_selection(
        _authority: LocalFilesystemAuthority,
        _candidate: str,
    ) -> object:
        raise AssertionError("expired deadline reached authority selection")

    monkeypatch.setattr(
        LocalFilesystemAuthority,
        "select_directory",
        unexpected_selection,
    )

    assert (
        operations.inspect_directory(str(tmp_path), deadline=deadline)
        is LocalDirectoryInspection.FAILED
    )
    assert executor.calls == []


def test_deadline_expiry_after_helper_is_a_failed_operation(tmp_path: Path) -> None:
    clock = FakeClock()
    executor = RecordingExecutor(
        [_result(0)],
        after_run=lambda: clock.advance(6.0),
    )

    result = _operations(tmp_path, executor).inspect_directory(
        str(tmp_path / "store"),
        deadline=_deadline(clock),
    )

    assert result is LocalDirectoryInspection.FAILED


def test_executor_exceptions_are_redacted_but_base_exceptions_propagate(
    tmp_path: Path,
) -> None:
    secret = "candidate-secret"
    failed_executor = RecordingExecutor([RuntimeError(secret)])

    result = _operations(tmp_path, failed_executor).inspect_directory(
        str(tmp_path / secret),
        deadline=_deadline(),
    )

    assert result is LocalDirectoryInspection.FAILED
    assert secret not in repr(result)

    fatal = FatalOperationFailure(secret)
    fatal_executor = RecordingExecutor([fatal])
    with pytest.raises(FatalOperationFailure) as exc_info:
        _operations(tmp_path, fatal_executor).inspect_directory(
            str(tmp_path / secret),
            deadline=_deadline(),
        )
    assert exc_info.value is fatal


@pytest.mark.parametrize("executable", ("", "python", "/bad\0python"))
def test_invalid_python_executable_fails_without_spawning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable: str,
) -> None:
    executor = RecordingExecutor([])
    monkeypatch.setattr(operations_module.sys, "executable", executable)

    result = _operations(tmp_path, executor).inspect_directory(
        str(tmp_path / "store"),
        deadline=_deadline(),
    )

    assert result is LocalDirectoryInspection.FAILED
    assert executor.calls == []


@pytest.mark.parametrize(
    "failure",
    (
        KeyboardInterrupt("operator interruption"),
        SystemExit("operator exit"),
        FatalOperationFailure("fatal operation failure"),
    ),
)
def test_authority_base_exceptions_propagate_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    authority = LocalFilesystemAuthority.from_roots([tmp_path])
    operations = BoundedLocalFilesystemOperations(
        authority,
        RecordingExecutor([]),
    )

    def fail_selection(
        _authority: LocalFilesystemAuthority,
        _candidate: str,
    ) -> object:
        raise failure

    monkeypatch.setattr(
        LocalFilesystemAuthority,
        "select_directory",
        fail_selection,
    )

    with pytest.raises(type(failure)) as exc_info:
        operations.inspect_directory(str(tmp_path), deadline=_deadline())
    assert exc_info.value is failure
