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
    BoundedLocalFilesystemOperations,
)
from lab_tracker.local_filesystem_ports import (
    DirectLocalRegularFileTarget,
    LocalDirectoryInspection,
    LocalRegularFileReadOutcome,
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
