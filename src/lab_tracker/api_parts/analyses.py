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
    Analysis,
    Claim,
    UsageEventResourceType,
    UsageEventVerb,
    Visualization,
)

if TYPE_CHECKING:
    from lab_tracker.services import AnalysisService, ClaimService, VisualizationService

UsageResultT = TypeVar("UsageResultT")


class AnalysesApiMixin:
    if TYPE_CHECKING:
        analyses: AnalysisService
        claims: ClaimService
        visualizations: VisualizationService

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

    def create_analysis(self, *args: Any, **kwargs: Any) -> Any:
        return self._with_usage_event(
            lambda: self.analyses.create_analysis(*args, **kwargs),
            verb=UsageEventVerb.CREATE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=kwargs.get("actor"),
            resource_id_attr="analysis_id",
        )

    def get_analysis(self, analysis_id: UUID) -> Analysis:
        return self.analyses.get_analysis(analysis_id)

    def get_analysis_for_read(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Analysis:
        return self.analyses.get_analysis_for_read(analysis_id, actor=actor)

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

    def delete_analysis(
        self,
        analysis_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Analysis:
        return self._with_usage_event(
            lambda: self.analyses.delete_analysis(analysis_id, actor=actor),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.ANALYSIS,
            actor=actor,
            resource_id=analysis_id,
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

    def get_claim(self, claim_id: UUID) -> Claim:
        return self.claims.get_claim(claim_id)

    def get_claim_for_read(
        self,
        claim_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Claim:
        return self.claims.get_claim_for_read(claim_id, actor=actor)

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

    def get_visualization(self, viz_id: UUID) -> Visualization:
        return self.visualizations.get_visualization(viz_id)

    def get_visualization_for_read(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> tuple[Visualization, UUID]:
        return self.visualizations.get_visualization_for_read(
            viz_id,
            actor=actor,
        )

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

    def delete_visualization(
        self,
        viz_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Visualization:
        return self._with_usage_event(
            lambda: self.visualizations.delete_visualization(viz_id, actor=actor),
            verb=UsageEventVerb.DELETE,
            resource_type=UsageEventResourceType.VISUALIZATION,
            actor=actor,
            resource_id=viz_id,
            resource_id_attr="viz_id",
        )
