"""Read-only draft-quality ledger service."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.draft_quality import aggregate_draft_quality
from lab_tracker.errors import ValidationError
from lab_tracker.models import DraftQualityLedger
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.publication_readiness_service import ProjectReadAccess


class DraftQualityService(BaseService):
    """Compute the per-project draft-quality ledger behind the opaque project read."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectReadAccess,
    ) -> None:
        super().__init__(context)
        self.projects = projects

    def ledger(
        self,
        project_id: UUID,
        *,
        since: datetime | None = None,
        actor: AuthContext | None = None,
    ) -> DraftQualityLedger:
        # Authorization first: a missing and an inaccessible project must be
        # indistinguishable, and no repository read may happen before the check.
        self.projects.get_project_for_read(project_id, actor=actor)
        if since is not None and (since.tzinfo is None or since.utcoffset() is None):
            raise ValidationError("since must include a timezone offset.")
        rows = self.repository.query_draft_quality_rows(project_id=project_id, since=since)
        return aggregate_draft_quality(project_id, since, rows)
