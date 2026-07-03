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


class GraphDraftsApiMixin:
    def create_graph_draft_from_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_graph_draft_from_note(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id_attr="change_set_id",
        )

    def create_analysis_graph_draft_from_note(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_analysis_graph_draft_from_note(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id_attr="change_set_id",
        )

    def create_batch_graph_draft(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.create_batch_graph_draft(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id_attr="change_set_id",
        )

    def get_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_change_set(*args, **kwargs)

    def list_graph_change_sets(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_graph_change_sets(*args, **kwargs)

    def query_graph_change_sets(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.query_graph_change_sets(*args, **kwargs)

    def list_batch_graph_drafts(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_batch_graph_drafts(*args, **kwargs)

    def update_graph_change_operation(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.update_graph_change_operation(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            project_id_attr=None,
        )

    def bulk_accept_graph_change_operations(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.bulk_accept_graph_change_operations(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def submit_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.submit_graph_change_set(*args, **kwargs),
            verb=UsageEventVerb.SUBMIT,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def review_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.review_graph_change_set(*args, **kwargs),
            verb=UsageEventVerb.REVIEW,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def revise_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.revise_graph_change_set(*args, **kwargs)

    def commit_graph_change_set(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.commit_graph_change_set(*args, **kwargs),
            verb=UsageEventVerb.COMMIT,
            resource_type=UsageEventResourceType.GRAPH_CHANGE_SET,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="change_set_id",
        )

    def build_graph_context_for_note(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.build_graph_context_for_note(*args, **kwargs)

    def build_batch_graph_context(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.build_batch_graph_context(*args, **kwargs)

    def get_graph_draft_batch_settings(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_draft_batch_settings(*args, **kwargs)

    def update_graph_draft_batch_settings(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.update_graph_draft_batch_settings(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_SETTINGS,
            actor=kwargs.get("actor"),
            resource_id_attr="settings_id",
        )

    def run_graph_draft_batch_for_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.run_graph_draft_batch_for_project(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_RUN,
            actor=kwargs.get("actor"),
            resource_id_attr="run_id",
        )

    def enqueue_graph_draft_batch_for_project(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.graph_drafts.enqueue_graph_draft_batch_for_project(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.GRAPH_DRAFT_BATCH_RUN,
            actor=kwargs.get("actor"),
            resource_id_attr="run_id",
        )

    def process_next_graph_draft_batch_run(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.process_next_graph_draft_batch_run(*args, **kwargs)

    def execute_graph_draft_batch_run(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.execute_graph_draft_batch_run(*args, **kwargs)

    def get_graph_draft_batch_run(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.get_graph_draft_batch_run(*args, **kwargs)

    def run_due_graph_draft_batches(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.run_due_graph_draft_batches(*args, **kwargs)

    def enqueue_due_graph_draft_batches(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.enqueue_due_graph_draft_batches(*args, **kwargs)

    def list_graph_draft_batch_runs(self, *args: Any, **kwargs: Any) -> Any:
        return self.graph_drafts.list_graph_draft_batch_runs(*args, **kwargs)
