"""Repository-backed readers for assistant decision-context assembly."""

from __future__ import annotations

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
        return UUID(str(project_id)) in self._accessible_project_ids

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        items, total = self._repository.query_projects(
            status=status,
            limit=None if self._accessible_project_ids is not None else limit,
            offset=0 if self._accessible_project_ids is not None else offset,
        )
        if self._accessible_project_ids is not None:
            items = [
                item for item in items if item.project_id in self._accessible_project_ids
            ]
            total = len(items)
            items = items[offset : offset + limit] if limit is not None else items[offset:]
        return _list_payload(items, total, limit, offset)

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
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_questions(
            project_id=_uuid_or_none(project_id),
            status=status,
            question_type=question_type,
            search=search,
            created_by=created_by,
            parent_question_id=_uuid_or_none(parent_question_id),
            ancestor_question_id=_uuid_or_none(ancestor_question_id),
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_notes(
            project_id=_uuid_or_none(project_id),
            status=status,
            created_by=created_by,
            target_entity_type=target_entity_type,
            target_entity_id=_uuid_or_none(target_entity_id),
            limit=limit,
            offset=offset,
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
        resolved_project_id = _uuid_or_none(project_id)
        if project_id is not None and not self._project_allowed(project_id):
            return {
                "data": {"questions": [], "notes": []},
                "meta": {"questions_count": 0, "notes_count": 0},
            }
        if project_id is None and self._accessible_project_ids is not None:
            questions: list[object] = []
            notes: list[object] = []
            for scoped_project_id in sorted(self._accessible_project_ids):
                if not include_set or "questions" in include_set:
                    questions.extend(
                        self._repository.query_questions(
                            project_id=scoped_project_id,
                            search=query,
                            limit=limit,
                            offset=0,
                        )[0]
                    )
                if not include_set or "notes" in include_set:
                    notes.extend(
                        self._repository.query_notes(
                            project_id=scoped_project_id,
                            search=query,
                            limit=limit,
                            offset=0,
                        )[0]
                    )
            questions = questions[offset : offset + limit]
            notes = notes[offset : offset + limit]
            return {
                "data": {
                    "questions": [_entity_to_json(item) for item in questions],
                    "notes": [_entity_to_json(item) for item in notes],
                },
                "meta": {"questions_count": len(questions), "notes_count": len(notes)},
            }
        questions = (
            self._repository.query_questions(
                project_id=resolved_project_id,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "questions" in include_set
            else []
        )
        notes = (
            self._repository.query_notes(
                project_id=resolved_project_id,
                search=query,
                limit=limit,
                offset=offset,
            )[0]
            if not include_set or "notes" in include_set
            else []
        )
        return {
            "data": {
                "questions": [_entity_to_json(item) for item in questions],
                "notes": [_entity_to_json(item) for item in notes],
            },
            "meta": {"questions_count": len(questions), "notes_count": len(notes)},
        }

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_sessions(
            project_id=_uuid_or_none(project_id),
            status=status,
            session_type=session_type,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_datasets(
            project_id=_uuid_or_none(project_id),
            status=status,
            created_by=created_by,
            limit=limit,
            offset=offset,
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
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_analyses(
            project_id=_uuid_or_none(project_id),
            dataset_id=_uuid_or_none(dataset_id),
            question_id=_uuid_or_none(question_id),
            status=status,
            created_by=created_by,
            limit=limit,
            offset=offset,
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
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_claims(
            project_id=_uuid_or_none(project_id),
            status=status,
            dataset_id=_uuid_or_none(dataset_id),
            analysis_id=_uuid_or_none(analysis_id),
            created_by=created_by,
            limit=limit,
            offset=offset,
        )
        return _list_payload(items, total, limit, offset)

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        if not self._project_allowed(project_id):
            return _list_payload([], 0, limit, offset)
        items, total = self._repository.query_visualizations(
            project_id=_uuid_or_none(project_id),
            analysis_id=_uuid_or_none(analysis_id),
            claim_id=_uuid_or_none(claim_id),
            limit=limit,
            offset=offset,
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
