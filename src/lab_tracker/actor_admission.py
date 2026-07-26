"""Reusable process-local admission control keyed by authenticated actor."""

from __future__ import annotations

from collections.abc import Hashable
from threading import Lock
from typing import Generic, TypeVar

ActorKey = TypeVar("ActorKey", bound=Hashable)


class ActorAdmissionLease(Generic[ActorKey]):
    """One admission's idempotent capacity release."""

    def __init__(
        self,
        admission: ActorAdmission[ActorKey],
        actor_id: ActorKey,
    ) -> None:
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


class ActorAdmission(Generic[ActorKey]):
    """Atomically bound no-wait work globally and per authenticated actor."""

    def __init__(
        self,
        *,
        global_in_flight_limit: int,
        per_actor_in_flight_limit: int,
        max_global_in_flight_limit: int,
    ) -> None:
        _validate_limits(
            global_in_flight_limit,
            per_actor_in_flight_limit,
            max_global_in_flight_limit=max_global_in_flight_limit,
        )
        self.global_in_flight_limit = global_in_flight_limit
        self.per_actor_in_flight_limit = per_actor_in_flight_limit
        self._lock = Lock()
        self._total_in_flight = 0
        self._actor_in_flight: dict[ActorKey, int] = {}

    def try_acquire(self, actor_id: ActorKey) -> ActorAdmissionLease[ActorKey] | None:
        """Return a lease immediately, or ``None`` when either limit is full."""

        if not self._try_reserve(actor_id):
            return None
        return ActorAdmissionLease(self, actor_id)

    def _try_reserve(self, actor_id: ActorKey) -> bool:
        with self._lock:
            if self._total_in_flight >= self.global_in_flight_limit:
                return False
            actor_in_flight = self._actor_in_flight.get(actor_id, 0)
            if actor_in_flight >= self.per_actor_in_flight_limit:
                return False
            self._total_in_flight += 1
            self._actor_in_flight[actor_id] = actor_in_flight + 1
            return True


def _validate_limits(
    global_in_flight_limit: int,
    per_actor_in_flight_limit: int,
    *,
    max_global_in_flight_limit: int,
) -> None:
    if (
        not isinstance(max_global_in_flight_limit, int)
        or isinstance(max_global_in_flight_limit, bool)
        or max_global_in_flight_limit < 1
    ):
        raise ValueError("max_global_in_flight_limit must be an integer of at least 1.")
    if (
        not isinstance(global_in_flight_limit, int)
        or isinstance(global_in_flight_limit, bool)
        or global_in_flight_limit < 1
    ):
        raise ValueError("global_in_flight_limit must be an integer of at least 1.")
    if global_in_flight_limit > max_global_in_flight_limit:
        raise ValueError(
            "global_in_flight_limit must be no greater than "
            f"{max_global_in_flight_limit}."
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
