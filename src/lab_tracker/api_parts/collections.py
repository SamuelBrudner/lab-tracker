"""Acquisition collection delegation mixin for :class:`LabTrackerAPI`."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.collection_models import AcquisitionCollectionManifest


class CollectionsApiMixin:
    def capture_collection_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        return self.acquisition_collections.capture_snapshot(*args, **kwargs)

    def get_acquisition_collection(self, *args: Any, **kwargs: Any) -> Any:
        return self.acquisition_collections.get_collection_for_read(*args, **kwargs)

    def list_acquisition_collections(self, *args: Any, **kwargs: Any) -> Any:
        return self.acquisition_collections.list_collections(*args, **kwargs)

    def get_collection_snapshot(self, *args: Any, **kwargs: Any) -> Any:
        return self.acquisition_collections.get_snapshot_for_read(*args, **kwargs)

    def list_collection_snapshots(self, *args: Any, **kwargs: Any) -> Any:
        return self.acquisition_collections.list_snapshots(*args, **kwargs)

    def get_collection_manifest(
        self,
        snapshot_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> AcquisitionCollectionManifest:
        return self.acquisition_collections.get_manifest(
            snapshot_id,
            actor=actor,
        )

    def list_collection_members(self, *args: Any, **kwargs: Any) -> Any:
        return self.acquisition_collections.list_members(*args, **kwargs)
