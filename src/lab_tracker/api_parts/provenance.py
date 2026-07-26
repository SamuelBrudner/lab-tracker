"""Per-domain delegation mixins for LabTrackerAPI.

Split out of api.py to keep the facade file cohesive and give each domain
its own edit locality. These are mixins: LabTrackerAPI inherits them, so
``self`` exposes the composed services and the usage-telemetry helpers
(``_with_usage_event``, ``record_usage_event``) defined on the facade.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from lab_tracker.api_parts._base import _first_uuid
from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    ProvenanceLink,
    UsageEventResourceType,
    UsageEventVerb,
)


class ProvenanceApiMixin:
    def create_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.supervision.create_supervision_edge(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.SUPERVISION_EDGE,
            actor=kwargs.get("actor"),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def get_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.get_supervision_edge(*args, **kwargs)

    def list_supervision_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self.supervision.list_supervision_edges(*args, **kwargs)

    def update_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.supervision.update_supervision_edge(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.SUPERVISION_EDGE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def delete_supervision_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.supervision.delete_supervision_edge(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.SUPERVISION_EDGE,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def reassign_ownership(self, *args: Any, **kwargs: Any) -> Any:
        return self.ownership_reassignments.reassign_ownership(*args, **kwargs)

    def get_ownership_reassignment(self, *args: Any, **kwargs: Any) -> Any:
        return self.ownership_reassignments.get_ownership_reassignment(*args, **kwargs)

    def list_ownership_reassignments(self, *args: Any, **kwargs: Any) -> Any:
        return self.ownership_reassignments.list_ownership_reassignments(
            *args,
            **kwargs,
        )

    def export_user_records(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.record_exports.export_user_records(*args, **kwargs),
            verb=UsageEventVerb.EXPORT,
            resource_type=UsageEventResourceType.RECORD_EXPORT,
            actor=kwargs.get("actor"),
            project_id=None,
            project_id_attr=None,
        )

    def export_goal_artifact(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.record_exports.export_goal_artifact(*args, **kwargs),
            verb=UsageEventVerb.EXPORT,
            resource_type=UsageEventResourceType.GOAL,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args) or kwargs.get("goal_id"),
            project_id_attr="project_id",
            suppress_opaque_target_not_found=True,
        )

    def export_question_subtree(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.record_exports.export_question_subtree(*args, **kwargs),
            verb=UsageEventVerb.EXPORT,
            resource_type=UsageEventResourceType.QUESTION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args) or kwargs.get("root_id"),
            project_id_attr="project_id",
            suppress_opaque_target_not_found=True,
        )

    def check_publication_readiness(self, *args: Any, **kwargs: Any) -> Any:
        return self.publication_readiness.check(*args, **kwargs)

    def list_entity_versions(self, *args: Any, **kwargs: Any) -> Any:
        return self.entity_versions.list_entity_versions(*args, **kwargs)

    def diff_entity_versions(self, *args: Any, **kwargs: Any) -> Any:
        return self.entity_versions.diff_entity_versions(*args, **kwargs)

    def get_provenance_link(self, *args: Any, **kwargs: Any) -> Any:
        return self.provenance_links.get_provenance_link(*args, **kwargs)

    def get_provenance_link_for_read(
        self,
        link_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ProvenanceLink:
        return self.provenance_links.get_provenance_link_for_read(
            link_id,
            actor=actor,
        )

    def list_provenance_links(self, *args: Any, **kwargs: Any) -> Any:
        return self.provenance_links.list_provenance_links(*args, **kwargs)

    def update_provenance_link_status(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.provenance_links.update_status(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.PROVENANCE_LINK,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="link_id",
        )
