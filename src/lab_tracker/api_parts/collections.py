"""Acquisition collection delegation mixin for :class:`LabTrackerAPI`."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.collection_models import (
    AcquisitionCollectionCaptureResult,
    AcquisitionCollectionManifest,
)
from lab_tracker.models import UsageEventResourceType, UsageEventVerb

UsageResultT = TypeVar("UsageResultT")


class CollectionsApiMixin:
    if TYPE_CHECKING:

        def _with_usage_event(
            self,
            action: Callable[[], UsageResultT],
            *,
            verb: UsageEventVerb,
            resource_type: UsageEventResourceType,
            actor: AuthContext | None = None,
            resource_id: UUID | None = None,
            project_id: UUID | None = None,
            resource_id_attr: str | None = None,
            project_id_attr: str | None = "project_id",
        ) -> UsageResultT: ...

    def capture_collection_snapshot(
        self, *args: Any, **kwargs: Any
    ) -> AcquisitionCollectionCaptureResult:
        return self._with_usage_event(
            lambda: self.acquisition_collections.capture_snapshot(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.ACQUISITION_COLLECTION,
            actor=kwargs.get("actor"),
            resource_id_attr="collection_id",
            project_id_attr=None,
        )

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
