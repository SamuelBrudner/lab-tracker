"""Typed, authorization-aware catalog read handlers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    Dataset,
    ExplorationNode,
    Note,
    Project,
    ProvenanceLink,
    Question,
    Session,
    Visualization,
)
from lab_tracker.repository import LabTrackerRepository

from .types import Page


@dataclass(frozen=True)
class CatalogQueries:
    """Optimized list/read-model queries behind the application boundary."""

    api: LabTrackerAPI
    repository: LabTrackerRepository

    def _project_scope(
        self,
        *,
        project_id: UUID | None,
        actor: AuthContext,
    ) -> set[UUID] | None:
        if project_id is not None:
            self.api.require_project_read(project_id, actor=actor)
            return None
        # Deliberately preserve an empty set. ``None`` means global access while
        # ``set()`` means the principal can see no projects.
        return self.api.accessible_project_ids(actor)

    def list_projects(
        self,
        *,
        actor: AuthContext,
        status: str | None,
        limit: int,
        offset: int,
    ) -> Page[Project]:
        items, total = self.repository.query_projects(
            project_ids=self.api.accessible_project_ids(actor),
            status=status,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_questions(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        status: str | None,
        question_type: str | None,
        search: str | None,
        created_by: str | None,
        parent_question_id: UUID | None,
        ancestor_question_id: UUID | None,
        limit: int,
        offset: int,
    ) -> Page[Question]:
        items, total = self.repository.query_questions(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            status=status,
            question_type=question_type,
            search=search,
            created_by=created_by,
            parent_question_id=parent_question_id,
            ancestor_question_id=ancestor_question_id,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_notes(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        status: str | None,
        created_by: str | None,
        since: datetime | None,
        until: datetime | None,
        target_entity_type: str | None,
        target_entity_id: UUID | None,
        limit: int,
        offset: int,
    ) -> Page[Note]:
        items, total = self.repository.query_notes(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_datasets(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        status: str | None,
        created_by: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        offset: int,
    ) -> Page[Dataset]:
        items, total = self.repository.query_datasets(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_sessions(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        status: str | None,
        session_type: str | None,
        limit: int,
        offset: int,
    ) -> Page[Session]:
        items, total = self.repository.query_sessions(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_acquisition_outputs(
        self,
        *,
        actor: AuthContext,
        session_id: UUID,
        limit: int,
        offset: int,
    ) -> Page[AcquisitionOutput]:
        session = self.api.get_session(session_id)
        self.api.require_project_read(session.project_id, actor=actor)
        items, total = self.repository.query_acquisition_outputs(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_analyses(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        dataset_id: UUID | None,
        question_id: UUID | None,
        status: str | None,
        created_by: str | None,
        since: datetime | None,
        until: datetime | None,
        recent_first: bool,
        limit: int,
        offset: int,
    ) -> Page[Analysis]:
        items, total = self.repository.query_analyses(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            dataset_id=dataset_id,
            question_id=question_id,
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return Page(items=items, total=total)

    def list_claims(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        status: str | None,
        dataset_id: UUID | None,
        analysis_id: UUID | None,
        created_by: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        offset: int,
    ) -> Page[Claim]:
        items, total = self.repository.query_claims(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            status=status,
            dataset_id=dataset_id,
            analysis_id=analysis_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_exploration_nodes(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        node_type: str | None,
        status: str | None,
        target_entity_type: str | None,
        target_entity_id: UUID | None,
        created_by: str | None,
        limit: int,
        offset: int,
    ) -> Page[ExplorationNode]:
        items, total = self.repository.query_exploration_nodes(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            node_type=node_type,
            status=status,
            target_entity_type=target_entity_type,
            target_entity_id=target_entity_id,
            created_by=created_by,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_provenance_links(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> Page[ProvenanceLink]:
        items, total = self.repository.query_provenance_links(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            status=status,
            limit=limit,
            offset=offset,
        )
        return Page(items=items, total=total)

    def list_visualizations(
        self,
        *,
        actor: AuthContext,
        project_id: UUID | None,
        analysis_id: UUID | None,
        claim_id: UUID | None,
        created_by: str | None,
        since: datetime | None,
        until: datetime | None,
        recent_first: bool,
        limit: int,
        offset: int,
    ) -> Page[Visualization]:
        items, total = self.repository.query_visualizations(
            project_id=project_id,
            project_ids=self._project_scope(project_id=project_id, actor=actor),
            analysis_id=analysis_id,
            claim_id=claim_id,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return Page(items=items, total=total)
