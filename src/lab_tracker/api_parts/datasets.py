"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    Dataset,
    DataStore,
    UsageEventResourceType,
    UsageEventVerb,
)

if TYPE_CHECKING:
    from lab_tracker.services import (
        DatasetService,
        DataStoreService,
    )

UsageResultT = TypeVar("UsageResultT")


class DatasetsApiMixin:
    if TYPE_CHECKING:
        datasets: DatasetService
        data_stores: DataStoreService

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

    def create_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.create_dataset(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id_attr="dataset_id",
        )

    def get_dataset(self, dataset_id: UUID) -> Dataset:
        return self.datasets.get_dataset(dataset_id)

    def get_dataset_for_read(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Dataset:
        return self.datasets.get_dataset_for_read(dataset_id, actor=actor)

    def list_datasets(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.list_datasets(*args, **kwargs)

    def update_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.update_dataset(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="dataset_id",
        )

    def delete_dataset(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Dataset:
        return self._with_usage_event(
            lambda: self.datasets.delete_dataset(dataset_id, actor=actor),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.DATASET,
            actor=actor,
            resource_id=dataset_id,
            resource_id_attr="dataset_id",
        )

    def create_data_store(self, *args: Any, **kwargs: Any) -> Any:
        return self.data_stores.create_data_store(*args, **kwargs)

    def get_data_store(self, store_id: UUID) -> DataStore:
        return self.data_stores.get_data_store(store_id)

    def get_data_store_for_read(
        self,
        store_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> DataStore:
        return self.data_stores.get_data_store_for_read(store_id, actor=actor)

    def list_data_stores(self, *args: Any, **kwargs: Any) -> Any:
        return self.data_stores.list_data_stores(*args, **kwargs)
