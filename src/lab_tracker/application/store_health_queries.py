"""Authorized, database-free data-store health queries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.models import DataStore, StoreKind
from lab_tracker.store_health import (
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    StoreHealth,
    StoreHealthStatus,
    StoreProbe,
)

_STORE_HEALTH_UNAVAILABLE = StoreHealth(
    StoreHealthStatus.UNSUPPORTED,
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
)


class StoreHealthAccess(Protocol):
    """The single authorized read needed to prepare a store-health probe."""

    def get_data_store_for_read(
        self,
        store_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> DataStore: ...


@dataclass(frozen=True, slots=True)
class DataStoreHealthResult:
    """Transport-neutral result for one authorized data-store health query."""

    store_id: UUID
    kind: StoreKind
    health: StoreHealth


@dataclass(frozen=True, slots=True)
class StoreHealthQueries:
    """Authorize a store while registered-store probes remain fail-closed."""

    api: StoreHealthAccess
    checker: StoreProbe
    release_read_scope: Callable[[], None]

    def check(
        self,
        store_id: UUID,
        *,
        actor: AuthContext,
    ) -> DataStoreHealthResult:
        store = self.api.get_data_store_for_read(store_id, actor=actor)
        result_store_id = store.store_id
        result_kind = store.kind
        self.release_read_scope()
        return DataStoreHealthResult(
            store_id=result_store_id,
            kind=result_kind,
            health=_STORE_HEALTH_UNAVAILABLE,
        )
