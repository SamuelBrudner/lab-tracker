"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from typing import Any

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.models import (
    UsageEventResourceType,
    UsageEventVerb,
)


class DatasetsApiMixin:
    def create_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.create_dataset(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id_attr="dataset_id",
        )

    def get_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self.datasets.get_dataset(*args, **kwargs)

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

    def delete_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.datasets.delete_dataset(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.DATASET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="dataset_id",
        )

    def create_data_store(self, *args: Any, **kwargs: Any) -> Any:
        return self.data_stores.create_data_store(*args, **kwargs)

    def get_data_store(self, *args: Any, **kwargs: Any) -> Any:
        return self.data_stores.get_data_store(*args, **kwargs)

    def list_data_stores(self, *args: Any, **kwargs: Any) -> Any:
        return self.data_stores.list_data_stores(*args, **kwargs)
