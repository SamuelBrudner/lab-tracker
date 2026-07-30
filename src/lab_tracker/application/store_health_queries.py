"""Authorized, database-free data-store health queries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.models import DataStore, StoreKind
from lab_tracker.rclone_store_definition import RCLONE_BACKED_STORE_KINDS
from lab_tracker.store_authority_use import (
    StoreAuthoritySnapshotProvider,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)
from lab_tracker.store_health import (
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
    StoreHealth,
    StoreHealthStatus,
    StoreProbe,
    StoreProbeTarget,
)

_STORE_HEALTH_UNAVAILABLE = StoreHealth(
    StoreHealthStatus.UNSUPPORTED,
    STORE_HEALTH_PROBE_UNAVAILABLE_MESSAGE,
)
_REMOTE_HEALTH_STORE_KINDS = frozenset(
    {StoreKind.HTTP, StoreKind.GIT, *RCLONE_BACKED_STORE_KINDS}
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
    """Authorize, detach, and revalidate a store before remote probe work."""

    api: StoreHealthAccess
    checker: StoreProbe
    release_read_scope: Callable[[], None]
    store_authority_snapshot_provider: StoreAuthoritySnapshotProvider

    def check(
        self,
        store_id: UUID,
        *,
        actor: AuthContext,
    ) -> DataStoreHealthResult:
        store = self.api.get_data_store_for_read(store_id, actor=actor)
        result_store_id = store.store_id
        result_kind = store.kind
        binding = (
            detach_store_authority_binding(store)
            if result_kind in _REMOTE_HEALTH_STORE_KINDS
            else None
        )
        self.release_read_scope()

        if binding is None:
            return DataStoreHealthResult(
                store_id=result_store_id,
                kind=result_kind,
                health=_STORE_HEALTH_UNAVAILABLE,
            )

        registry = self.store_authority_snapshot_provider()
        proof = revalidate_store_authority_binding(registry, binding)
        if proof is None:
            return DataStoreHealthResult(
                store_id=result_store_id,
                kind=result_kind,
                health=_STORE_HEALTH_UNAVAILABLE,
            )

        # Past this point the revalidated proof, not the released ORM row, is
        # the authority for every reported field.
        target = StoreProbeTarget.from_authority_proof(proof)
        return DataStoreHealthResult(
            store_id=proof.store_id,
            kind=proof.definition.kind,
            health=self.checker(target),
        )
