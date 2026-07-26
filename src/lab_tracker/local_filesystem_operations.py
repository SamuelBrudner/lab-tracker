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
)
from lab_tracker.local_filesystem_authority import (
    MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS,
    LocalFilesystemAuthority,
)
from lab_tracker.local_filesystem_ports import LocalDirectoryInspection

LOCAL_FILESYSTEM_REQUEST_ENV: Final = "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
LOCAL_FILESYSTEM_PROTOCOL_VERSION: Final = 1
MAX_LOCAL_FILESYSTEM_REQUEST_BYTES: Final = 24 * 1024
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS: Final = MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS

_INSPECT_DIRECTORY_OPERATION: Final = "inspect-directory"
_ACCESSIBLE_EXIT: Final = 0
_DENIED_EXIT: Final = 2
_FAILED_EXIT: Final = 3
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


def _encode_request(candidate: str, roots: tuple[str, ...]) -> str | None:
    try:
        rendered = json.dumps(
            {
                "v": LOCAL_FILESYSTEM_PROTOCOL_VERSION,
                "op": _INSPECT_DIRECTORY_OPERATION,
                "candidate": candidate,
                "roots": list(roots),
            },
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
