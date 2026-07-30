"""Authorized, bounded health checks for host-local data stores."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
)
from lab_tracker.local_filesystem_ports import (
    LocalDirectoryInspection,
    LocalDirectoryInspector,
)
from lab_tracker.models import StoreKind
from lab_tracker.store_health import (
    LOCAL_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)


@dataclass(frozen=True, slots=True)
class LocalStoreHealthProbe:
    """Map one bounded directory inspection to the redacted health contract."""

    inspector: LocalDirectoryInspector
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

        deadline = ProcessDeadline.after(
            self.deadline_seconds,
            clock=self.clock,
        )
        result = self.inspector.inspect_directory(
            target.root,
            deadline=deadline,
        )
        deadline.check()
        if result is LocalDirectoryInspection.ACCESSIBLE:
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return _unreachable()


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
