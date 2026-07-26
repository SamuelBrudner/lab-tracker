"""Authorized, database-free data-store health queries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.models import DataStore, StoreKind
from lab_tracker.store_health import StoreHealth, StoreProbe, StoreProbeTarget


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
    """Authorize and detach a store before invoking host-side probe work."""

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
        target = StoreProbeTarget.from_store(store)
        self.release_read_scope()
        health = self.checker(target)
        return DataStoreHealthResult(
            store_id=target.store_id,
            kind=target.kind,
            health=health,
        )
