"""Immutable store-health values and bounded probe coordination.

The store registry's mutable persistence/domain model must not escape the
authorization scope that loaded it.  :class:`StoreProbeTarget` is the small,
detached snapshot handed to a probe after authorization and scope cleanup.

Store-health results are safe to coalesce by that complete snapshot.  The
coordinator below combines a bounded TTL/LRU cache with exact-key
single-flight: one thread probes while followers wait for that same result.
Probe failures are never cached or exposed to followers.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Final, Protocol
from uuid import UUID

from lab_tracker.models import StoreKind
from lab_tracker.store_authority_use import (
    StoreAuthorityBindingIdentity,
    StoreAuthorityUseProof,
)

DEFAULT_STORE_HEALTH_CACHE_MAX_ENTRIES: Final = 256
MAX_STORE_HEALTH_CACHE_MAX_ENTRIES: Final = 4096
DEFAULT_STORE_HEALTH_CACHE_TTL_SECONDS: Final = 10.0
MAX_STORE_HEALTH_CACHE_TTL_SECONDS: Final = 300.0
DEFAULT_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS: Final = 10.0
MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS: Final = 60.0

STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE: Final = (
    "Store health probe is temporarily unavailable."
)
HTTP_STORE_HEALTH_FAILURE_DETAIL: Final = "HTTP store health check failed."
LOCAL_STORE_HEALTH_FAILURE_DETAIL: Final = "Local store health check failed."
RCLONE_STORE_HEALTH_FAILURE_DETAIL: Final = "Rclone store health check failed."
GIT_STORE_HEALTH_FAILURE_DETAIL: Final = "Git store health check failed."


_STORE_PROBE_TARGET_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class StoreProbeTarget:
    """Complete immutable identity and adapter input for one health probe."""

    store_id: UUID
    name: str
    kind: StoreKind
    root: str
    endpoint: str | None
    credential_ref: str | None
    authority_binding_identity: StoreAuthorityBindingIdentity

    def __init__(
        self,
        proof: StoreAuthorityUseProof,
        *,
        _factory_token: object,
    ) -> None:
        """Bind every probe field to one sealed use-time authority proof.

        The constructor is intentionally incompatible with the dataclass field
        signature.  Normal direct construction and ``dataclasses.replace`` therefore
        cannot pair a valid authority identity with unrelated adapter inputs.
        """

        if (
            _factory_token is not _STORE_PROBE_TARGET_FACTORY_TOKEN
            or type(proof) is not StoreAuthorityUseProof
        ):
            raise TypeError("Store probe target requires an authority use proof.")
        definition = proof.definition
        object.__setattr__(self, "store_id", proof.store_id)
        object.__setattr__(self, "name", definition.name)
        object.__setattr__(self, "kind", definition.kind)
        object.__setattr__(self, "root", definition.root)
        object.__setattr__(self, "endpoint", definition.endpoint)
        object.__setattr__(self, "credential_ref", definition.credential_ref)
        object.__setattr__(
            self,
            "authority_binding_identity",
            proof.binding_identity,
        )

    @classmethod
    def from_authority_proof(
        cls,
        proof: StoreAuthorityUseProof,
    ) -> StoreProbeTarget:
        """Build an exact cache/probe target from a use-time authority proof."""

        return cls(
            proof,
            _factory_token=_STORE_PROBE_TARGET_FACTORY_TOKEN,
        )

    def __repr__(self) -> str:
        """Return only non-secret probe classification, never target material."""

        return f"StoreProbeTarget(store_id={self.store_id!r}, kind={self.kind!r})"


class StoreHealthStatus(str, Enum):
    """Outcome of probing a registered store's reachability."""

    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class StoreHealth:
    """Immutable public result of a store-health probe."""

    status: StoreHealthStatus
    detail: str | None = None

    @property
    def is_healthy(self) -> bool:
        return self.status is StoreHealthStatus.HEALTHY

    def to_json_dict(self) -> dict[str, object]:
        return {"status": self.status.value, "detail": self.detail}


class StoreProbe(Protocol):
    """Port implemented by a concrete, read-only store-health adapter."""

    def __call__(self, target: StoreProbeTarget) -> StoreHealth: ...


class StoreHealthProbeUnavailable(RuntimeError):
    """A sanitized single-flight timeout or leader-failure notification."""

    def __init__(self) -> None:
        super().__init__(STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE)


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    result: StoreHealth
    expires_at: float


def _validate_max_entries(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_entries must be an integer.")
    if value < 1 or value > MAX_STORE_HEALTH_CACHE_MAX_ENTRIES:
        raise ValueError(
            "max_entries must be between 1 and "
            f"{MAX_STORE_HEALTH_CACHE_MAX_ENTRIES}."
        )
    return value


def _validate_finite_seconds(
    value: float,
    *,
    name: str,
    hard_max: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number.")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite.")
    if normalized <= 0.0 or normalized > hard_max:
        raise ValueError(f"{name} must be greater than 0 and at most {hard_max}.")
    return normalized


class CachedStoreHealthProbe:
    """Bounded TTL/LRU cache with exact-target single-flight coordination.

    The internal lock protects only the small cache/in-flight maps.  It is never
    held while the injected probe runs or while a follower waits.  A completed
    result's TTL starts only after the probe returns.
    """

    def __init__(
        self,
        probe: StoreProbe,
        *,
        max_entries: int = DEFAULT_STORE_HEALTH_CACHE_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_STORE_HEALTH_CACHE_TTL_SECONDS,
        singleflight_wait_seconds: float = (
            DEFAULT_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS
        ),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._max_entries = _validate_max_entries(max_entries)
        self._ttl_seconds = _validate_finite_seconds(
            ttl_seconds,
            name="ttl_seconds",
            hard_max=MAX_STORE_HEALTH_CACHE_TTL_SECONDS,
        )
        self._singleflight_wait_seconds = _validate_finite_seconds(
            singleflight_wait_seconds,
            name="singleflight_wait_seconds",
            hard_max=MAX_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS,
        )
        self._clock = clock
        self._lock = Lock()
        self._entries: OrderedDict[StoreProbeTarget, _CacheEntry] = OrderedDict()
        self._in_flight: dict[StoreProbeTarget, Future[StoreHealth]] = {}

    @property
    def entry_count(self) -> int:
        """Number of cache slots currently occupied (never above the bound)."""

        with self._lock:
            return len(self._entries)

    @property
    def max_entries(self) -> int:
        """Configured hard cache-entry bound."""

        return self._max_entries

    @property
    def ttl_seconds(self) -> float:
        """Configured result lifetime, measured from probe completion."""

        return self._ttl_seconds

    @property
    def waiter_timeout_seconds(self) -> float:
        """Configured maximum time a same-key follower waits for its leader."""

        return self._singleflight_wait_seconds

    @property
    def in_flight_count(self) -> int:
        """Number of exact targets that currently have a leader probe."""

        with self._lock:
            return len(self._in_flight)

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        with self._lock:
            now = self._clock()
            self._discard_expired_locked(now)
            entry = self._entries.get(target)
            if entry is not None:
                self._entries.move_to_end(target)
                return entry.result

            future = self._in_flight.get(target)
            if future is None:
                future = Future()
                self._in_flight[target] = future
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            return self._await_follower(future)
        return self._run_leader(target, future)

    def _await_follower(self, future: Future[StoreHealth]) -> StoreHealth:
        try:
            return future.result(timeout=self._singleflight_wait_seconds)
        except (FutureTimeoutError, StoreHealthProbeUnavailable):
            # Every follower receives a fresh exception with the single public
            # message and no causal chain or leader traceback.
            pass
        raise StoreHealthProbeUnavailable() from None

    def _run_leader(
        self,
        target: StoreProbeTarget,
        future: Future[StoreHealth],
    ) -> StoreHealth:
        try:
            result = self._probe(target)
        except BaseException:
            self._remove_in_flight(target, future)
            future.set_exception(StoreHealthProbeUnavailable())
            # Preserve the leader's exact original exception, including
            # non-Exception cancellation/termination subclasses.
            raise

        try:
            completed_at = self._clock()
        except BaseException:
            self._remove_in_flight(target, future)
            future.set_exception(StoreHealthProbeUnavailable())
            # A completion-clock failure is infrastructure failure, not a
            # cacheable probe result. Preserve it for the leader while waking
            # every follower with the sanitized public exception.
            raise

        with self._lock:
            if self._in_flight.get(target) is future:
                del self._in_flight[target]
            self._entries[target] = _CacheEntry(
                result=result,
                expires_at=completed_at + self._ttl_seconds,
            )
            self._entries.move_to_end(target)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

        # Future callbacks and awakened followers run outside our lock.
        future.set_result(result)
        return result

    def _remove_in_flight(
        self,
        target: StoreProbeTarget,
        future: Future[StoreHealth],
    ) -> None:
        with self._lock:
            if self._in_flight.get(target) is future:
                del self._in_flight[target]

    def _discard_expired_locked(self, now: float) -> None:
        expired = [
            target
            for target, entry in self._entries.items()
            if now >= entry.expires_at
        ]
        for target in expired:
            del self._entries[target]
