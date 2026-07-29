"""Authorized, bounded rclone data-store health checks."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
    ProcessExecutor,
)
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_definition import RegisteredRcloneStoreAddress
from lab_tracker.store_health import (
    RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

RCLONE_HEALTH_OUTPUT_LIMIT_BYTES: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class RcloneStoreHealthProbe:
    """Probe one registered rclone root through an exact remote allowlist."""

    policy: RcloneRemotePolicy
    executor: ProcessExecutor
    binary: str = "rclone"
    deadline_seconds: float = DEFAULT_PROCESS_DEADLINE_SECONDS
    clock: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _validate_binary(self.binary)
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
        address = RegisteredRcloneStoreAddress.parse(
            kind=target.kind,
            name=target.name,
            root=target.root,
            credential_ref=target.credential_ref,
        )
        if address is None:
            return _unreachable()

        approved = self.policy.authorize_name(address.remote)
        if approved is None:
            return _unreachable()

        subprocess_target = address.root.compose_root(approved)
        if subprocess_target is None:  # pragma: no cover - typed composition invariant
            return _unreachable()

        deadline = ProcessDeadline.after(
            self.deadline_seconds,
            clock=self.clock,
        )
        result = self.executor.run(
            [
                self.binary,
                "lsf",
                "--max-depth",
                "1",
                subprocess_target,
            ],
            deadline=deadline,
            stdout_limit_bytes=RCLONE_HEALTH_OUTPUT_LIMIT_BYTES,
            stderr_limit_bytes=DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
        )
        deadline.check()
        if result.returncode == 0:
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return _unreachable()


def _validate_binary(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("binary must be a string.")
    if not value or "\0" in value:
        raise ValueError("binary must be a non-empty string without NUL bytes.")


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
        RCLONE_STORE_HEALTH_FAILURE_DETAIL,
    )
