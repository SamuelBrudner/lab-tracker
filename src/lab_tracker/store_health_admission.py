"""Process-local admission control for data-store health checks."""

from __future__ import annotations

from uuid import UUID

from lab_tracker.actor_admission import ActorAdmission

DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT = 4
DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT = 1
MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT = 16


class StoreHealthAdmission(ActorAdmission[UUID]):
    """Atomically bound admitted store-health checks globally and per actor."""

    def __init__(
        self,
        *,
        global_in_flight_limit: int = DEFAULT_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
        per_actor_in_flight_limit: int = DEFAULT_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT,
    ) -> None:
        super().__init__(
            global_in_flight_limit=global_in_flight_limit,
            per_actor_in_flight_limit=per_actor_in_flight_limit,
            max_global_in_flight_limit=MAX_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT,
        )
