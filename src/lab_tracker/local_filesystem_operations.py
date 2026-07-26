"""Bounded broker for pre-follow-safe host-local filesystem operations."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lab_tracker.bounded_subprocess import (
    ProcessDeadline,
    ProcessExecutor,
    ProcessResult,
    StdoutConsumer,
)
from lab_tracker.local_filesystem_authority import (
    MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS,
    LocalFilesystemAuthority,
)
from lab_tracker.local_filesystem_ports import (
    DirectLocalRegularFileTarget,
    LocalDirectoryInspection,
    LocalRegularFileReadOutcome,
    LocalRegularFileReadResult,
    LocalRegularFileTarget,
    RegisteredLocalRegularFileTarget,
)
from lab_tracker.local_resolution_budget import LocalResolutionBudget

LOCAL_FILESYSTEM_REQUEST_ENV: Final = "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
LOCAL_FILESYSTEM_PROTOCOL_VERSION: Final = 1
MAX_LOCAL_FILESYSTEM_REQUEST_BYTES: Final = 24 * 1024
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS: Final = MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS

_INSPECT_DIRECTORY_OPERATION: Final = "inspect-directory"
_READ_FILE_OPERATION: Final = "read-file"
_READ_REGISTERED_FILE_OPERATION: Final = "read-registered-file"
_ACCESSIBLE_EXIT: Final = 0
_DENIED_EXIT: Final = 2
_FAILED_EXIT: Final = 3
_MISSING_EXIT: Final = 4
_HELPER_OPTIONS: Final = ("-I", "-S", "-B")
_HELPER_FILENAME: Final = (
    "_windows_local_store_health_helper.py" if os.name == "nt" else "_local_store_health_helper.py"
)
_HELPER_PATH: Final = Path(os.path.abspath(__file__)).with_name(_HELPER_FILENAME)
_POSIX_LOCALE_VARIABLES: Final = frozenset({"LANG", "LC_ALL", "LC_CTYPE"})
_WINDOWS_RUNTIME_VARIABLES: Final = frozenset({"SYSTEMROOT", "WINDIR"})


@dataclass(frozen=True, slots=True)
class BoundedLocalFilesystemOperations:
    """Run authorized local operations inside one contained helper process."""

    authority: LocalFilesystemAuthority
    executor: ProcessExecutor

    def inspect_directory(
        self,
        candidate: str,
        *,
        deadline: ProcessDeadline,
    ) -> LocalDirectoryInspection:
        """Inspect a directory without exposing its identity in process output."""

        try:
            deadline.check()
            grant = self.authority.select_directory(candidate)
            deadline.check()
            if grant is None:
                return LocalDirectoryInspection.DENIED
            selected_candidate, selected_roots = self.authority._request_for(grant)
            if not selected_roots or len(selected_roots) > MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS:
                return LocalDirectoryInspection.FAILED
            request = _encode_request(selected_candidate, selected_roots)
            if request is None:
                return LocalDirectoryInspection.FAILED
            python_executable = sys.executable
            if (
                not python_executable
                or "\0" in python_executable
                or not os.path.isabs(python_executable)
            ):
                return LocalDirectoryInspection.FAILED
            deadline.check()
            result = self.executor.run(
                [
                    python_executable,
                    *_HELPER_OPTIONS,
                    os.fspath(_HELPER_PATH),
                ],
                deadline=deadline,
                stdout_limit_bytes=0,
                stderr_limit_bytes=0,
                cwd=None,
                env=_helper_environment(request),
            )
            deadline.check()
            return _map_result(result)
        except Exception:
            return LocalDirectoryInspection.FAILED

    def read_regular_file(
        self,
        target: LocalRegularFileTarget,
        *,
        budget: LocalResolutionBudget,
        stdout_consumer: StdoutConsumer,
    ) -> LocalRegularFileReadResult:
        """Stream one retained regular file through a pessimistic reservation."""

        try:
            if type(budget) is not LocalResolutionBudget or not callable(stdout_consumer):
                return _failed_read()
            budget.deadline.check()
            request_target = _admit_read_target(self.authority, target)
            budget.deadline.check()
            if request_target is None:
                return _denied_read()

            with budget.reserve() as reservation:
                request = _encode_read_request(
                    request_target,
                    max_bytes=reservation.allowance_bytes,
                )
                python_executable = sys.executable
                if (
                    request is None
                    or not python_executable
                    or "\0" in python_executable
                    or not os.path.isabs(python_executable)
                ):
                    reservation.consume_terminal()
                    return _failed_read()

                delivered_bytes = 0

                def consume(chunk: bytes) -> None:
                    nonlocal delivered_bytes
                    if type(chunk) is not bytes:
                        raise TypeError("Local regular-file output is invalid.")
                    delivered_bytes += len(chunk)
                    if delivered_bytes > reservation.allowance_bytes + 1:
                        raise ValueError("Local regular-file output is invalid.")
                    stdout_consumer(chunk)

                budget.deadline.check()
                result = self.executor.run(
                    [
                        python_executable,
                        *_HELPER_OPTIONS,
                        os.fspath(_HELPER_PATH),
                    ],
                    deadline=budget.deadline,
                    stdout_limit_bytes=reservation.allowance_bytes + 1,
                    stderr_limit_bytes=0,
                    stdout_consumer=consume,
                    cwd=None,
                    env=_helper_environment(request),
                )
                budget.deadline.check()
                if not _valid_streamed_process_result(
                    result,
                    delivered_bytes=delivered_bytes,
                    output_limit=reservation.allowance_bytes + 1,
                ):
                    reservation.consume_terminal()
                    return _failed_read()

                if result.returncode == _ACCESSIBLE_EXIT:
                    if delivered_bytes > reservation.allowance_bytes:
                        reservation.consume_terminal()
                        return _failed_read()
                    reservation.settle_clean(payload_bytes=delivered_bytes)
                    return LocalRegularFileReadResult(
                        LocalRegularFileReadOutcome.COMPLETE,
                        delivered_bytes,
                    )

                if result.returncode in {_MISSING_EXIT, _DENIED_EXIT}:
                    if delivered_bytes != 0:
                        reservation.consume_terminal()
                        return _failed_read()
                    reservation.release_clean_zero(stdout_bytes=delivered_bytes)
                    return LocalRegularFileReadResult(
                        (
                            LocalRegularFileReadOutcome.MISSING
                            if result.returncode == _MISSING_EXIT
                            else LocalRegularFileReadOutcome.DENIED
                        ),
                        0,
                    )

                reservation.consume_terminal()
                return _failed_read()
        except Exception:
            return _failed_read()


def _encode_request(candidate: str, roots: tuple[str, ...]) -> str | None:
    return _encode_request_payload(
        {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "op": _INSPECT_DIRECTORY_OPERATION,
            "candidate": candidate,
            "roots": list(roots),
        }
    )


def _admit_read_target(
    authority: LocalFilesystemAuthority,
    target: LocalRegularFileTarget,
) -> dict[str, object] | None:
    if type(target) is DirectLocalRegularFileTarget:
        grant = authority.select_directory(target.candidate)
        if grant is None:
            return None
        candidate, roots = authority._request_for(grant)
        if len(roots) != 1:
            return None
        return {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "op": _READ_FILE_OPERATION,
            "candidate": candidate,
            "roots": list(roots),
        }

    if type(target) is RegisteredLocalRegularFileTarget:
        grant = authority.select_directory(target.store_root)
        if grant is None:
            return None
        store_root, roots = authority._request_for(grant)
        if len(roots) != 1:
            return None
        return {
            "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "op": _READ_REGISTERED_FILE_OPERATION,
            "store_root": store_root,
            "locator": list(target.locator),
            "roots": list(roots),
        }

    return None


def _encode_read_request(
    admitted: dict[str, object],
    *,
    max_bytes: int,
) -> str | None:
    payload = dict(admitted)
    payload["max_bytes"] = max_bytes
    return _encode_request_payload(payload)


def _encode_request_payload(payload: dict[str, object]) -> str | None:
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        encoded = rendered.encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        return None
    if len(encoded) > MAX_LOCAL_FILESYSTEM_REQUEST_BYTES:
        return None
    return rendered


def _valid_streamed_process_result(
    result: ProcessResult,
    *,
    delivered_bytes: int,
    output_limit: int,
) -> bool:
    return (
        type(result) is ProcessResult
        and type(result.returncode) is int
        and type(result.stdout) is bytes
        and result.stdout == b""
        and type(result.stdout_bytes) is int
        and 0 <= result.stdout_bytes <= output_limit
        and type(result.stderr_bytes) is int
        and result.stderr_bytes == 0
        and delivered_bytes == result.stdout_bytes
    )


def _failed_read() -> LocalRegularFileReadResult:
    return LocalRegularFileReadResult(LocalRegularFileReadOutcome.FAILED, 0)


def _denied_read() -> LocalRegularFileReadResult:
    return LocalRegularFileReadResult(LocalRegularFileReadOutcome.DENIED, 0)


def _helper_environment(request: str) -> dict[str, str]:
    environment = {LOCAL_FILESYSTEM_REQUEST_ENV: request}
    if os.name == "nt":
        for name, value in os.environ.items():
            if name.upper() in _WINDOWS_RUNTIME_VARIABLES:
                environment[name] = value
        return environment
    if os.name == "posix":
        for name, value in os.environ.items():
            if name in _POSIX_LOCALE_VARIABLES:
                environment[name] = value
    return environment


def _map_result(result: ProcessResult) -> LocalDirectoryInspection:
    if result.stdout != b"" or result.stdout_bytes != 0 or result.stderr_bytes != 0:
        return LocalDirectoryInspection.FAILED
    if result.returncode == _ACCESSIBLE_EXIT:
        return LocalDirectoryInspection.ACCESSIBLE
    if result.returncode == _DENIED_EXIT:
        return LocalDirectoryInspection.DENIED
    if result.returncode == _FAILED_EXIT:
        return LocalDirectoryInspection.FAILED
    return LocalDirectoryInspection.FAILED
