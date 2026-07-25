"""Process-local admission control for expensive artifact resolutions."""

from __future__ import annotations

from threading import Lock
from uuid import UUID

# Artifact resolution runs in Starlette's shared AnyIO worker pool. Keep the
# process-wide cap below its usual 40-token default so other request work can
# still make progress while every admitted resolution is slow.
DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT = 8
DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT = 2
MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT = 32


class ArtifactResolutionLease:
    """One admitted artifact resolution's idempotent capacity release."""

    def __init__(self, admission: ArtifactResolutionAdmission, actor_id: UUID) -> None:
        self._admission = admission
        self._actor_id = actor_id
        self._released = False

    def release(self) -> None:
        """Return this lease's capacity exactly once."""

        with self._admission._lock:
            if self._released:
                return
            self._released = True
            self._admission._total_in_flight -= 1
            actor_in_flight = self._admission._actor_in_flight[self._actor_id] - 1
            if actor_in_flight:
                self._admission._actor_in_flight[self._actor_id] = actor_in_flight
            else:
                del self._admission._actor_in_flight[self._actor_id]


class ArtifactResolutionAdmission:
    """Atomically bound admitted resolutions globally and per authenticated actor."""

    def __init__(
        self,
        *,
        global_in_flight_limit: int = DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
        per_actor_in_flight_limit: int = DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT,
    ) -> None:
        _validate_limits(global_in_flight_limit, per_actor_in_flight_limit)
        self.global_in_flight_limit = global_in_flight_limit
        self.per_actor_in_flight_limit = per_actor_in_flight_limit
        self._lock = Lock()
        self._total_in_flight = 0
        self._actor_in_flight: dict[UUID, int] = {}

    def try_acquire(self, actor_id: UUID) -> ArtifactResolutionLease | None:
        """Return a lease immediately, or ``None`` when either limit is full."""

        with self._lock:
            if self._total_in_flight >= self.global_in_flight_limit:
                return None
            actor_in_flight = self._actor_in_flight.get(actor_id, 0)
            if actor_in_flight >= self.per_actor_in_flight_limit:
                return None
            self._total_in_flight += 1
            self._actor_in_flight[actor_id] = actor_in_flight + 1
            return ArtifactResolutionLease(self, actor_id)


def _validate_limits(global_in_flight_limit: int, per_actor_in_flight_limit: int) -> None:
    if (
        not isinstance(global_in_flight_limit, int)
        or isinstance(global_in_flight_limit, bool)
        or global_in_flight_limit < 1
    ):
        raise ValueError("global_in_flight_limit must be an integer of at least 1.")
    if global_in_flight_limit > MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT:
        raise ValueError(
            "global_in_flight_limit must be no greater than "
            f"{MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT}."
        )
    if (
        not isinstance(per_actor_in_flight_limit, int)
        or isinstance(per_actor_in_flight_limit, bool)
        or per_actor_in_flight_limit < 1
    ):
        raise ValueError("per_actor_in_flight_limit must be an integer of at least 1.")
    if per_actor_in_flight_limit > global_in_flight_limit:
        raise ValueError(
            "per_actor_in_flight_limit must be no greater than global_in_flight_limit."
        )
