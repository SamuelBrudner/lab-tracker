"""Experiment delegation mixin for :class:`LabTrackerAPI`."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.models import Experiment, UsageEventResourceType, UsageEventVerb


class ExperimentsApiMixin:
    def create_experiment(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.experiments.create_experiment(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.EXPERIMENT,
            actor=kwargs.get("actor"),
            resource_id_attr="experiment_id",
        )

    def get_experiment(self, experiment_id: UUID) -> Experiment:
        return self.experiments.get_experiment(experiment_id)

    def query_experiments(self, *args: Any, **kwargs: Any) -> Any:
        return self.experiments.query_experiments(*args, **kwargs)

    def update_experiment(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.experiments.update_experiment(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPERIMENT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="experiment_id",
        )

    def add_experiment_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.experiments.add_session(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPERIMENT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="experiment_id",
        )

    def remove_experiment_session(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.experiments.remove_session(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPERIMENT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="experiment_id",
        )

    def add_experiment_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.experiments.add_dataset(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPERIMENT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="experiment_id",
        )

    def remove_experiment_dataset(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.experiments.remove_dataset(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.EXPERIMENT,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="experiment_id",
        )

    def query_experiment_sessions(self, *args: Any, **kwargs: Any) -> Any:
        return self.experiments.query_sessions(*args, **kwargs)

    def query_experiment_datasets(self, *args: Any, **kwargs: Any) -> Any:
        return self.experiments.query_datasets(*args, **kwargs)
