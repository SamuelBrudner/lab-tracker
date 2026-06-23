"""Repository-backed readers for assistant decision-context assembly."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from lab_tracker.decision_context_types import JsonObject
from lab_tracker.repository import LabTrackerRepository


class RepositoryDecisionContextReader:
    """Decision-context reader backed by a request-scoped repository."""

    def __init__(
        self,
        repository: LabTrackerRepository,
        *,
        accessible_project_ids: set[UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._accessible_project_ids = accessible_project_ids

    def _project_allowed(self, project_id: str | None) -> bool:
        if self._accessible_project_ids is None:
            return True
        if project_id is None:
            return False
        try:
            return UUID(str(project_id)) in self._accessible_project_ids
        except ValueError:
            return False

    def _project_filter(self, project_id: str | None) -> set[UUID] | None:
        if project_id is not None:
            return None
        return self._accessible_project_ids

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_projects(
            project_ids=self._accessible_project_ids,
            status=status,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def get_project(self, project_id: str) -> JsonObject | None:
        if not self._project_allowed(project_id):
            return None
        project = self._repository.projects.get(UUID(str(project_id)))
        return _entity_to_json(project) if project is not None else None

    def get_question(self, question_id: str) -> JsonObject | None:
        question = self._repository.questions.get(UUID(str(question_id)))
        return self._project_entity_to_json(question)

    def get_dataset(self, dataset_id: str) -> JsonObject | None:
        dataset = self._repository.datasets.get(UUID(str(dataset_id)))
        return self._project_entity_to_json(dataset)

    def get_analysis(self, analysis_id: str) -> JsonObject | None:
        analysis = self._repository.analyses.get(UUID(str(analysis_id)))
        return self._project_entity_to_json(analysis)

    def get_claim(self, claim_id: str) -> JsonObject | None:
        claim = self._repository.claims.get(UUID(str(claim_id)))
        return self._project_entity_to_json(claim)

    def get_visualization(self, visualization_id: str) -> JsonObject | None:
        visualization = self._repository.visualizations.get(UUID(str(visualization_id)))
        if visualization is None:
            return None
        analysis = self._repository.analyses.get(visualization.analysis_id)
        if analysis is None or not self._project_allowed(str(analysis.project_id)):
            return None
        return _entity_to_json(visualization)

    def _project_entity_to_json(self, entity: object | None) -> JsonObject | None:
        if entity is None:
            return None
        project_id = getattr(entity, "project_id", None)
        if not self._project_allowed(str(project_id)):
            return None
        return _entity_to_json(entity)

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        created_by: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_questions(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            status=status,
            question_type=question_type,
            search=search,
            created_by=created_by,
            parent_question_id=_uuid_or_none(parent_question_id),
            ancestor_question_id=_uuid_or_none(ancestor_question_id),
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_notes(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            target_entity_type=target_entity_type,
            target_entity_id=_uuid_or_none(target_entity_id),
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JsonObject:
        include_set = {
            item.strip().casefold()
            for item in (include.split(",") if include else ["questions", "notes"])
            if item.strip()
        }
        if project_id is not None and not self._project_allowed(project_id):
            return {
                "data": {"questions": [], "notes": []},
                "meta": {"questions_count": 0, "notes_count": 0},
            }
        project_ids = (
            {UUID(str(project_id))}
            if project_id is not None
            else self._accessible_project_ids
        )
        if not include_set or "questions" in include_set:
            questions, questions_total = self._repository.query_questions(
                project_id=None,
                project_ids=project_ids,
                search=query,
                limit=limit,
                offset=offset,
                recent_first=True,
            )
        else:
            questions, questions_total = [], 0
        if not include_set or "notes" in include_set:
            notes, notes_total = self._repository.query_notes(
                project_id=None,
                project_ids=project_ids,
                search=query,
                limit=limit,
                offset=offset,
                recent_first=True,
            )
        else:
            notes, notes_total = [], 0
        return {
            "data": {
                "questions": [_entity_to_json(item) for item in questions],
                "notes": [_entity_to_json(item) for item in notes],
            },
            "meta": {"questions_count": questions_total, "notes_count": notes_total},
        }

    def project_ids_with_search_matches(self, query: str, *, limit: int = 50) -> set[str]:
        project_ids = self._repository.project_ids_with_search_matches(
            search=query,
            project_ids=self._accessible_project_ids,
            limit=limit,
        )
        return {str(project_id) for project_id in project_ids}

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_sessions(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            status=status,
            session_type=session_type,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_datasets(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_analyses(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            dataset_id=_uuid_or_none(dataset_id),
            question_id=_uuid_or_none(question_id),
            status=status,
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_claims(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            status=status,
            dataset_id=_uuid_or_none(dataset_id),
            analysis_id=_uuid_or_none(analysis_id),
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        if project_id is not None and not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_visualizations(
            project_id=_uuid_or_none(project_id),
            project_ids=self._project_filter(project_id),
            analysis_id=_uuid_or_none(analysis_id),
            claim_id=_uuid_or_none(claim_id),
            created_by=created_by,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
            recent_first=recent_first,
        )
        return _list_payload(items, total, limit, offset)


def _uuid_or_none(value: str | None) -> UUID | None:
    if value is None:
        return None
    return UUID(str(value))


def _entity_to_json(entity: object) -> JsonObject:
    return entity.model_dump(mode="json")  # type: ignore[attr-defined]


def _list_payload(items: list[object], total: int, limit: int, offset: int) -> JsonObject:
    return {
        "data": [_entity_to_json(item) for item in items],
        "meta": {"limit": limit, "offset": offset, "total": total},
    }
