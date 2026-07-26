"""Authorized, bounded health checks for host-local data stores."""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
    ProcessExecutor,
)
from lab_tracker.local_path_policy import LocalPathPolicy
from lab_tracker.models import StoreKind
from lab_tracker.store_health import (
    LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

LOCAL_STORE_HEALTH_ROOT_ENV: Final = "LAB_TRACKER_INTERNAL_LOCAL_STORE_HEALTH_ROOT"
_HELPER_FILENAME: Final = (
    "_windows_local_store_health_helper.py"
    if os.name == "nt"
    else "_local_store_health_helper.py"
)
_HELPER_PATH: Final = Path(os.path.abspath(__file__)).with_name(_HELPER_FILENAME)
_HELPER_OPTIONS: Final = ("-I", "-S", "-B")
_POSIX_LOCALE_VARIABLES: Final = frozenset({"LANG", "LC_ALL", "LC_CTYPE"})
_WINDOWS_RUNTIME_VARIABLES: Final = frozenset({"SYSTEMROOT", "WINDIR"})


@dataclass(frozen=True, slots=True)
class LocalStoreHealthProbe:
    """Probe one statically authorized local root in a contained helper."""

    policy: LocalPathPolicy
    executor: ProcessExecutor
    deadline_seconds: float = DEFAULT_PROCESS_DEADLINE_SECONDS
    clock: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        normalized_deadline = _validate_deadline_seconds(self.deadline_seconds)
        if not callable(self.clock):
            raise TypeError("clock must be callable.")
        object.__setattr__(self, "deadline_seconds", normalized_deadline)

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        """Return only a static, redacted failure when probing cannot succeed."""

        try:
            return self._probe(target)
        except Exception:
            return _unreachable()

    def _probe(self, target: StoreProbeTarget) -> StoreHealth:
        if target.kind is not StoreKind.LOCAL_FS:
            return _unreachable()

        restricted = self.policy.restricted_to_absolute_root(target.root)
        if restricted is None:
            return _unreachable()
        roots = restricted.canonical_roots
        if roots is None or len(roots) != 1:
            return _unreachable()
        canonical_root = roots[0]

        python_executable = sys.executable
        if (
            not python_executable
            or "\0" in python_executable
            or not os.path.isabs(python_executable)
        ):
            return _unreachable()

        deadline = ProcessDeadline.after(
            self.deadline_seconds,
            clock=self.clock,
        )
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
            env=_helper_environment(canonical_root),
        )
        deadline.check()
        if (
            result.returncode == 0
            and result.stdout == b""
            and result.stdout_bytes == 0
            and result.stderr_bytes == 0
        ):
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return _unreachable()


def _helper_environment(canonical_root: str) -> dict[str, str]:
    environment = {LOCAL_STORE_HEALTH_ROOT_ENV: canonical_root}
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


def _validate_deadline_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadline_seconds must be a number.")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0.0
        or normalized > MAX_PROCESS_DEADLINE_SECONDS
    ):
        raise ValueError(
            "deadline_seconds must be finite, positive, and no greater than "
            f"{MAX_PROCESS_DEADLINE_SECONDS:g}."
        )
    return normalized


def _unreachable() -> StoreHealth:
    return StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    )
