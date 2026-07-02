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


class AnalysesApiMixin:
    def create_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.create_analysis(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id_attr="analysis_id",
        )

    def get_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.get_analysis(*args, **kwargs)

    def list_analyses(self, *args: Any, **kwargs: Any) -> Any:
        return self.analyses.list_analyses(*args, **kwargs)

    def update_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.update_analysis(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="analysis_id",
        )

    def delete_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.delete_analysis(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="analysis_id",
        )

    def commit_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.commit_analysis(*args, **kwargs),
            verb=UsageEventVerb.COMMIT,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="analysis_id",
        )

    def create_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.create_claim(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.CLAIM,
            actor=kwargs.get("actor"),
            resource_id_attr="claim_id",
        )

    def get_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.get_claim(*args, **kwargs)

    def list_claims(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.list_claims(*args, **kwargs)

    def update_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.update_claim(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.CLAIM,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="claim_id",
        )

    def delete_claim(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.delete_claim(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.CLAIM,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="claim_id",
        )

    def create_claim_edge(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.claims.create_claim_edge(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.CLAIM_EDGE,
            actor=kwargs.get("actor"),
            resource_id_attr="edge_id",
            project_id_attr=None,
        )

    def list_claim_edges(self, *args: Any, **kwargs: Any) -> Any:
        return self.claims.list_claim_edges(*args, **kwargs)

    def create_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.visualizations.create_visualization(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=kwargs.get("actor"),
            resource_id_attr="viz_id",
        )

    def get_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.get_visualization(*args, **kwargs)

    def list_visualizations(self, *args: Any, **kwargs: Any) -> Any:
        return self.visualizations.list_visualizations(*args, **kwargs)

    def update_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.visualizations.update_visualization(*args, **kwargs),
            verb=UsageEventVerb.UPDATE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="viz_id",
        )

    def delete_visualization(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.visualizations.delete_visualization(*args, **kwargs),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=kwargs.get("actor"),
            resource_id=_first_uuid(args),
            resource_id_attr="viz_id",
        )
