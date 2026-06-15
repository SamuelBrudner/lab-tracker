"""Per-person and group-scoped record export service."""

from __future__ import annotations

from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, Role, require_role
from lab_tracker.errors import NotFoundError
from lab_tracker.models import (
    Analysis,
    Claim,
    Dataset,
    Note,
    Question,
    RecordExport,
    RecordExportEvent,
    RecordExportRecords,
    utc_now,
)
from lab_tracker.provenance import build_record_export_provenance_document
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import actor_user_fk, actor_user_id

ADMIN_ROLES = {Role.ADMIN}


class RecordExportService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def export_user_records(
        self,
        *,
        user_id: UUID,
        base_url: str,
        group_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> RecordExport:
        project_ids = self._authorized_project_scope(group_id=group_id, actor=actor)
        repository = self.repository
        if not repository.user_exists(user_id):
            raise NotFoundError("User does not exist.")
        records = self._collect_records(user_id=user_id, project_ids=project_ids)
        exported_project_ids = self._project_ids_for_records(records, fallback=project_ids)
        supervision_edges, _ = repository.query_supervision_edges(limit=None, offset=0)
        provenance = build_record_export_provenance_document(
            base_url,
            records,
            supervision_edges=supervision_edges,
        )
        generated_at = utc_now()
        event = RecordExportEvent(
            export_id=uuid4(),
            user_id=user_id,
            group_id=group_id,
            project_ids=exported_project_ids,
            record_counts=self._record_counts(records),
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, repository),
            created_at=generated_at,
        )
        with self.unit_of_work() as unit_repository:
            unit_repository.record_export_events.save(event)
        return RecordExport(
            export_event_id=event.export_id,
            user_id=user_id,
            group_id=group_id,
            project_ids=exported_project_ids,
            generated_at=generated_at,
            records=records,
            provenance=provenance,
        )

    def _authorized_project_scope(
        self,
        *,
        group_id: UUID | None,
        actor: AuthContext | None,
    ) -> set[UUID] | None:
        if group_id is None:
            require_role(actor, ADMIN_ROLES)
            return None
        self.authorization.require_group_owner(group_id, actor=actor)
        projects, _ = self.repository.query_projects(
            group_id=group_id,
            limit=None,
            offset=0,
        )
        return {project.project_id for project in projects}

    def _collect_records(
        self,
        *,
        user_id: UUID,
        project_ids: set[UUID] | None,
    ) -> RecordExportRecords:
        user_id_filter = str(user_id)
        questions, _ = self.repository.query_questions(
            created_by=user_id_filter,
            limit=None,
            offset=0,
        )
        datasets, _ = self.repository.query_datasets(
            created_by=user_id_filter,
            limit=None,
            offset=0,
        )
        notes, _ = self.repository.query_notes(
            created_by=user_id_filter,
            limit=None,
            offset=0,
        )
        analyses, _ = self.repository.query_analyses(
            created_by=user_id_filter,
            limit=None,
            offset=0,
        )
        claims, _ = self.repository.query_claims(
            created_by=user_id_filter,
            limit=None,
            offset=0,
        )
        return RecordExportRecords(
            questions=self._filter_projects(questions, project_ids),
            datasets=self._filter_projects(datasets, project_ids),
            analyses=self._filter_projects(analyses, project_ids),
            claims=self._filter_projects(claims, project_ids),
            notes=self._filter_projects(notes, project_ids),
        )

    def _filter_projects(
        self,
        items: list[Question] | list[Dataset] | list[Analysis] | list[Claim] | list[Note],
        project_ids: set[UUID] | None,
    ):
        if project_ids is None:
            return items
        return [item for item in items if item.project_id in project_ids]

    def _project_ids_for_records(
        self,
        records: RecordExportRecords,
        *,
        fallback: set[UUID] | None,
    ) -> list[UUID]:
        if fallback is not None:
            return sorted(fallback, key=str)
        project_ids = {
            *[item.project_id for item in records.questions],
            *[item.project_id for item in records.datasets],
            *[item.project_id for item in records.analyses],
            *[item.project_id for item in records.claims],
            *[item.project_id for item in records.notes],
        }
        return sorted(project_ids, key=str)

    def _record_counts(self, records: RecordExportRecords) -> dict[str, int]:
        return {
            "questions": len(records.questions),
            "datasets": len(records.datasets),
            "analyses": len(records.analyses),
            "claims": len(records.claims),
            "notes": len(records.notes),
        }
