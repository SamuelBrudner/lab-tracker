"""Process-local admission control for expensive artifact resolutions."""

from __future__ import annotations

from uuid import UUID

from lab_tracker.actor_admission import ActorAdmission, ActorAdmissionLease

# Artifact resolution runs in Starlette's shared AnyIO worker pool. Keep the
# process-wide cap below its usual 40-token default so other request work can
# still make progress while every admitted resolution is slow.
DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT = 8
DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT = 2
MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT = 32


class ArtifactResolutionLease(ActorAdmissionLease[UUID]):
    """One admitted artifact resolution's idempotent capacity release."""


class ArtifactResolutionAdmission(ActorAdmission[UUID]):
    """Atomically bound admitted resolutions globally and per authenticated actor."""

    def __init__(
        self,
        *,
        global_in_flight_limit: int = DEFAULT_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
        per_actor_in_flight_limit: int = DEFAULT_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT,
    ) -> None:
        super().__init__(
            global_in_flight_limit=global_in_flight_limit,
            per_actor_in_flight_limit=per_actor_in_flight_limit,
            max_global_in_flight_limit=MAX_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT,
        )

    def try_acquire(self, actor_id: UUID) -> ArtifactResolutionLease | None:
        """Return a lease immediately, or ``None`` when either limit is full."""

        if not self._try_reserve(actor_id):
            return None
        return ArtifactResolutionLease(self, actor_id)
