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
    MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES,
    MAX_LOCAL_FILESYSTEM_PATH_BYTES,
    MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS,
    LocalFilesystemAuthority,
)
from lab_tracker.local_filesystem_ports import (
    DirectLocalRecoveryScope,
    DirectLocalRegularFileTarget,
    EnumeratedLocalRegularFileTarget,
    LocalDirectoryInspection,
    LocalRecoveryCandidate,
    LocalRecoveryEnumerationOutcome,
    LocalRecoveryEnumerationResult,
    LocalRecoveryScope,
    LocalRegularFileReadOutcome,
    LocalRegularFileReadResult,
    LocalRegularFileTarget,
    RegisteredLocalRecoveryScope,
    RegisteredLocalRegularFileTarget,
)
from lab_tracker.local_resolution_budget import (
    MAX_LOCAL_RECOVERY_MAX_DIRECTORIES,
    MAX_LOCAL_RECOVERY_MAX_FILES,
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
    LocalResolutionBudget,
)

LOCAL_FILESYSTEM_REQUEST_ENV: Final = "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
LOCAL_FILESYSTEM_PROTOCOL_VERSION: Final = 1
MAX_LOCAL_FILESYSTEM_REQUEST_BYTES: Final = 24 * 1024
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS: Final = MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS
MAX_LOCAL_RECOVERY_RESPONSE_BYTES: Final = 8 * 1024 * 1024
_MAX_LOCAL_RECOVERY_NAME_JSON_BYTES: Final = (
    6 * MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES + 2
)
_JSON_NULL_BYTES: Final = 4

_INSPECT_DIRECTORY_OPERATION: Final = "inspect-directory"
_READ_FILE_OPERATION: Final = "read-file"
_READ_REGISTERED_FILE_OPERATION: Final = "read-registered-file"
_ENUMERATE_FILES_OPERATION: Final = "enumerate-files"
_ENUMERATE_REGISTERED_FILES_OPERATION: Final = "enumerate-registered-files"
_ENUMERATION_COMPLETE: Final = "complete"
_ENUMERATION_LIMIT: Final = "limit"
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
_WINDOWS_RESERVED_CHARACTERS: Final = frozenset('"*:<>?|')
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)


@dataclass(frozen=True, slots=True)
class BoundedLocalFilesystemOperations:
    """Run authorized local operations inside one contained helper process."""

    authority: LocalFilesystemAuthority
    executor: ProcessExecutor

    def validate_direct_recovery_configuration(
        self,
        *,
        max_files: int,
        max_directories: int,
    ) -> None:
        """Reject enabled recovery that cannot fit one bounded helper request."""

        if (
            type(max_files) is not int
            or not 0 <= max_files <= MAX_LOCAL_RECOVERY_MAX_FILES
            or type(max_directories) is not int
            or not 0 <= max_directories <= MAX_LOCAL_RECOVERY_MAX_DIRECTORIES
        ):
            raise ValueError("Local recovery configuration is invalid.")
        admitted = _admit_recovery_scope(self.authority, DirectLocalRecoveryScope())
        if admitted is None:
            raise ValueError("Local recovery configuration is invalid.")
        request_payload, selected_roots, _registered_store_root = admitted
        if not selected_roots or max_files == 0 or max_directories == 0:
            return

        request_payload["max_directories"] = max_directories
        request_payload["max_files"] = max_files
        request_payload["target_name"] = None
        rendered = _render_request_payload(request_payload)
        if (
            rendered is None
            or len(rendered.encode("utf-8"))
            - _JSON_NULL_BYTES
            + _MAX_LOCAL_RECOVERY_NAME_JSON_BYTES
            > MAX_LOCAL_FILESYSTEM_REQUEST_BYTES
        ):
            raise ValueError(
                "Local recovery roots exceed the bounded helper request limit."
            )
        for root_index in range(len(selected_roots)):
            target = EnumeratedLocalRegularFileTarget(root_index, ("x",))
            admitted_read = _admit_read_target(self.authority, target)
            if admitted_read is None or _encode_read_request(
                admitted_read,
                max_bytes=MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
            ) is None:
                raise ValueError(
                    "Local recovery roots exceed the bounded helper request limit."
                )

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

    def enumerate_recovery_candidates(
        self,
        scope: LocalRecoveryScope,
        *,
        target_name: str | None,
        max_files: int,
        max_directories: int,
        budget: LocalResolutionBudget,
    ) -> LocalRecoveryEnumerationResult:
        """Enumerate one bounded candidate set without exposing host paths."""

        try:
            if type(budget) is not LocalResolutionBudget:
                return _failed_enumeration()
            if (
                type(scope) not in (DirectLocalRecoveryScope, RegisteredLocalRecoveryScope)
                or not _valid_recovery_name(target_name)
                or type(max_files) is not int
                or not 0 <= max_files <= MAX_LOCAL_RECOVERY_MAX_FILES
                or type(max_directories) is not int
                or not 0 <= max_directories <= MAX_LOCAL_RECOVERY_MAX_DIRECTORIES
            ):
                budget.abort_terminal()
                return _failed_enumeration()

            budget.deadline.check()
            admitted = _admit_recovery_scope(self.authority, scope)
            budget.deadline.check()
            if admitted is None:
                budget.abort_terminal()
                return _failed_enumeration()
            request_payload, selected_roots, registered_store_root = admitted
            if not selected_roots:
                return _complete_enumeration()
            if max_files == 0:
                return _complete_enumeration()
            if max_directories == 0:
                return LocalRecoveryEnumerationResult(
                    LocalRecoveryEnumerationOutcome.LIMIT_REACHED,
                    (),
                    0,
                )

            request_payload["max_directories"] = max_directories
            request_payload["max_files"] = max_files
            request_payload["target_name"] = target_name
            request = _encode_request_payload(request_payload)
            python_executable = sys.executable
            if (
                request is None
                or not python_executable
                or "\0" in python_executable
                or not os.path.isabs(python_executable)
            ):
                budget.abort_terminal()
                return _failed_enumeration()

            budget.deadline.check()
            result = self.executor.run(
                [
                    python_executable,
                    *_HELPER_OPTIONS,
                    os.fspath(_HELPER_PATH),
                ],
                deadline=budget.deadline,
                stdout_limit_bytes=MAX_LOCAL_RECOVERY_RESPONSE_BYTES,
                stderr_limit_bytes=0,
                cwd=None,
                env=_helper_environment(request),
            )
            budget.deadline.check()
            parsed = _parse_enumeration_result(
                result,
                authority=self.authority,
                scope=scope,
                roots=selected_roots,
                registered_store_root=registered_store_root,
                target_name=target_name,
                max_files=max_files,
                max_directories=max_directories,
            )
            budget.deadline.check()
            if parsed.outcome is LocalRecoveryEnumerationOutcome.FAILED:
                budget.abort_terminal()
            return parsed
        except Exception:
            if type(budget) is LocalResolutionBudget:
                budget.abort_terminal()
            return _failed_enumeration()
        except BaseException:
            if type(budget) is LocalResolutionBudget:
                budget.abort_terminal()
            raise


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

    if type(target) is EnumeratedLocalRegularFileTarget:
        try:
            store_root, roots = authority._request_for_root_index(target.root_index)
        except ValueError:
            return None
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


def _admit_recovery_scope(
    authority: LocalFilesystemAuthority,
    scope: LocalRecoveryScope,
) -> tuple[dict[str, object], tuple[str, ...], str | None] | None:
    if type(scope) is DirectLocalRecoveryScope:
        roots = authority._recovery_roots()
        if len(roots) > MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS:
            return None
        return (
            {
                "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
                "op": _ENUMERATE_FILES_OPERATION,
                "roots": list(roots),
            },
            roots,
            None,
        )

    if type(scope) is RegisteredLocalRecoveryScope:
        grant = authority.select_directory(scope.store_root)
        if grant is None:
            return None
        normalized_store_root, roots = authority._request_for(grant)
        if len(roots) != 1:
            return None
        return (
            {
                "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
                "op": _ENUMERATE_REGISTERED_FILES_OPERATION,
                "roots": list(roots),
                "store_root": normalized_store_root,
            },
            roots,
            scope.store_root,
        )

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
    rendered = _render_request_payload(payload)
    if rendered is None or len(rendered.encode("utf-8")) > MAX_LOCAL_FILESYSTEM_REQUEST_BYTES:
        return None
    return rendered


def _render_request_payload(payload: dict[str, object]) -> str | None:
    try:
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, UnicodeError, ValueError):
        return None


def _parse_enumeration_result(
    result: ProcessResult,
    *,
    authority: LocalFilesystemAuthority,
    scope: LocalRecoveryScope,
    roots: tuple[str, ...],
    registered_store_root: str | None,
    target_name: str | None,
    max_files: int,
    max_directories: int,
) -> LocalRecoveryEnumerationResult:
    if (
        type(result) is not ProcessResult
        or type(result.returncode) is not int
        or type(result.stdout) is not bytes
        or type(result.stdout_bytes) is not int
        or result.stdout_bytes != len(result.stdout)
        or not 0 <= result.stdout_bytes <= MAX_LOCAL_RECOVERY_RESPONSE_BYTES
        or type(result.stderr_bytes) is not int
        or result.stderr_bytes != 0
        or result.returncode != _ACCESSIBLE_EXIT
        or not result.stdout
    ):
        return _failed_enumeration()

    try:
        rendered = result.stdout.decode("ascii")
        payload = json.loads(rendered)
        if (
            type(payload) is not dict
            or json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            != rendered
            or set(payload) != {"candidates", "directories", "status", "v"}
            or type(payload["v"]) is not int
            or payload["v"] != LOCAL_FILESYSTEM_PROTOCOL_VERSION
            or type(payload["status"]) is not str
            or payload["status"] not in {_ENUMERATION_COMPLETE, _ENUMERATION_LIMIT}
            or type(payload["directories"]) is not int
            or not 1 <= payload["directories"] <= max_directories
            or type(payload["candidates"]) is not list
            or len(payload["candidates"]) > max_files
        ):
            return _failed_enumeration()

        candidates: list[LocalRecoveryCandidate] = []
        seen: set[tuple[int, tuple[str, ...]]] = set()
        fallback_seen = False
        read_request_limited = False
        for raw_candidate in payload["candidates"]:
            if (
                type(raw_candidate) is not dict
                or set(raw_candidate) != {"locator", "root_index"}
                or type(raw_candidate["root_index"]) is not int
                or type(raw_candidate["locator"]) is not list
            ):
                return _failed_enumeration()
            root_index = raw_candidate["root_index"]
            locator = tuple(raw_candidate["locator"])
            if (
                root_index < 0
                or root_index >= len(roots)
                or not _valid_recovery_locator(locator)
                or len(locator) > payload["directories"]
                or (type(scope) is RegisteredLocalRecoveryScope and root_index != 0)
            ):
                return _failed_enumeration()
            key = (root_index, locator)
            if key in seen:
                return _failed_enumeration()
            seen.add(key)

            name = locator[-1]
            preferred = target_name is not None and name == target_name
            if fallback_seen and preferred:
                return _failed_enumeration()
            if not preferred:
                fallback_seen = True

            if type(scope) is DirectLocalRecoveryScope:
                target: LocalRegularFileTarget = EnumeratedLocalRegularFileTarget(
                    root_index,
                    locator,
                )
            else:
                if registered_store_root is None:
                    return _failed_enumeration()
                target = RegisteredLocalRegularFileTarget(
                    registered_store_root,
                    locator,
                )
            admitted_read = _admit_read_target(authority, target)
            if admitted_read is None:
                return _failed_enumeration()
            if _encode_read_request(
                admitted_read,
                max_bytes=MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
            ) is None:
                read_request_limited = True
                continue
            candidates.append(LocalRecoveryCandidate(target, name))
    except (MemoryError, RecursionError, TypeError, UnicodeError, ValueError):
        return _failed_enumeration()

    return LocalRecoveryEnumerationResult(
        (
            LocalRecoveryEnumerationOutcome.COMPLETE
            if (
                payload["status"] == _ENUMERATION_COMPLETE
                and not read_request_limited
            )
            else LocalRecoveryEnumerationOutcome.LIMIT_REACHED
        ),
        tuple(candidates),
        payload["directories"],
    )


def _valid_recovery_name(value: str | None) -> bool:
    return value is None or _valid_recovery_component(value)


def _valid_recovery_locator(locator: tuple[object, ...]) -> bool:
    if not locator or len(locator) > MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS:
        return False
    total_units = len(locator) - 1
    for component in locator:
        if type(component) is not str or not _valid_recovery_component(component):
            return False
        try:
            component_units = (
                len(component.encode("utf-16-le", errors="strict")) // 2
                if os.name == "nt"
                else len(os.fsencode(component))
            )
        except (UnicodeError, ValueError):
            return False
        total_units += component_units
        if total_units > MAX_LOCAL_FILESYSTEM_PATH_BYTES:
            return False
    return True


def _valid_recovery_component(component: str) -> bool:
    if (
        type(component) is not str
        or component in ("", ".", "..")
        or "\0" in component
        or "/" in component
        or (os.name == "nt" and "\\" in component)
        or (os.name == "nt" and not _valid_windows_recovery_component(component))
    ):
        return False
    if os.name == "nt":
        return True
    try:
        return len(os.fsencode(component)) <= MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES
    except (UnicodeError, ValueError):
        return False


def _valid_windows_recovery_component(component: str) -> bool:
    stem = component.partition(".")[0].rstrip(" ").upper()
    if (
        component[-1:] in (".", " ")
        or any(character in _WINDOWS_RESERVED_CHARACTERS for character in component)
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
        or stem in _WINDOWS_RESERVED_NAMES
    ):
        return False
    try:
        encoded = component.encode("utf-16-le", errors="strict")
    except UnicodeError:
        return False
    return len(encoded) // 2 <= MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES


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


def _failed_enumeration() -> LocalRecoveryEnumerationResult:
    return LocalRecoveryEnumerationResult(
        LocalRecoveryEnumerationOutcome.FAILED,
        (),
        0,
    )


def _complete_enumeration() -> LocalRecoveryEnumerationResult:
    return LocalRecoveryEnumerationResult(
        LocalRecoveryEnumerationOutcome.COMPLETE,
        (),
        0,
    )


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
