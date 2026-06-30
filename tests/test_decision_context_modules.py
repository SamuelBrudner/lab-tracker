from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from api_helpers import repository_backed_api
from sqlalchemy import event

from lab_tracker.auth import AuthContext, Role
from lab_tracker.decision_context_builders import (
    build_evidence_map,
    task_guidance,
    truncation,
)
from lab_tracker.decision_context_constants import TASK_KIND_VALUES
from lab_tracker.decision_context_query import RepositoryDecisionContextReader
from lab_tracker.decision_context_selection import merge_entities
from lab_tracker.decision_context_types import JsonObject
from lab_tracker.decision_context_use_case import build_decision_context
from lab_tracker.models import (
    NoteStatus,
    QuestionStatus,
    QuestionType,
)


def _envelope(
    items: list[JsonObject],
    *,
    total: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> JsonObject:
    return {
        "data": items,
        "meta": {"limit": limit, "offset": offset, "total": len(items) if total is None else total},
    }


class FakeDecisionContextReader:
    project = {
        "project_id": "project-1",
        "name": "Decision Context Project",
        "status": "active",
    }
    question = {
        "question_id": "question-1",
        "project_id": "project-1",
        "text": "Which baseline controls matter?",
        "status": "active",
    }
    note = {
        "note_id": "note-1",
        "project_id": "project-1",
        "raw_content": "Baseline control note",
        "status": "committed",
    }
    dataset = {
        "dataset_id": "dataset-1",
        "project_id": "project-1",
        "primary_question_id": "question-1",
        "question_links": [{"question_id": "question-1"}],
        "commit_hash": "dataset-commit",
        "status": "committed",
    }
    analysis = {
        "analysis_id": "analysis-1",
        "project_id": "project-1",
        "dataset_ids": ["dataset-1"],
        "method_hash": "analysis-method",
        "status": "committed",
    }
    claim = {
        "claim_id": "claim-1",
        "project_id": "project-1",
        "statement": "Baseline controls change behavior.",
        "status": "supported",
        "supported_by_dataset_ids": ["dataset-1"],
        "supported_by_analysis_ids": ["analysis-1"],
    }
    visualization = {
        "viz_id": "visualization-1",
        "analysis_id": "analysis-1",
        "caption": "Baseline comparison",
        "related_claim_ids": ["claim-1"],
    }

    def list_projects(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return _envelope([self.project], limit=limit, offset=offset)

    def get_project(self, project_id: str) -> JsonObject | None:
        return self.project if project_id == self.project["project_id"] else None

    def get_question(self, question_id: str) -> JsonObject | None:
        return self.question if question_id == self.question["question_id"] else None

    def get_dataset(self, dataset_id: str) -> JsonObject | None:
        return self.dataset if dataset_id == self.dataset["dataset_id"] else None

    def get_analysis(self, analysis_id: str) -> JsonObject | None:
        return self.analysis if analysis_id == self.analysis["analysis_id"] else None

    def get_claim(self, claim_id: str) -> JsonObject | None:
        return self.claim if claim_id == self.claim["claim_id"] else None

    def get_visualization(self, visualization_id: str) -> JsonObject | None:
        if visualization_id == self.visualization["viz_id"]:
            return self.visualization
        return None

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        include: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> JsonObject:
        if project_id != "project-1":
            return {
                "data": {"questions": [], "notes": []},
                "meta": {"questions_count": 0, "notes_count": 0},
            }
        return {
            "data": {"questions": [self.question], "notes": [self.note]},
            "meta": {"questions_count": 1, "notes_count": 1},
        }

    def project_ids_with_search_matches(self, query: str, *, limit: int = 50) -> set[str]:
        return {"project-1"}

    def list_questions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        question_type: str | None = None,
        search: str | None = None,
        parent_question_id: str | None = None,
        ancestor_question_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        return _envelope([self.question], limit=limit, offset=offset)

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
        return _envelope([self.note], limit=limit, offset=offset)

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
        return _envelope([], limit=limit, offset=offset)

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
        return _envelope([self.dataset], limit=limit, offset=offset)

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
        return _envelope([self.analysis], limit=limit, offset=offset)

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
        return _envelope([self.claim], limit=limit, offset=offset)

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
        return _envelope([self.visualization], limit=limit, offset=offset)


def test_merge_entities_preserves_first_record_and_combines_reasons() -> None:
    merged = merge_entities(
        "entity_id",
        ([{"entity_id": "one", "label": "anchor"}], "anchor"),
        (
            [
                {"entity_id": "one", "label": "search"},
                {"entity_id": "two", "label": "recent"},
            ],
            "search_match",
        ),
    )

    assert merged == [
        {
            "entity_id": "one",
            "label": "anchor",
            "relevance_reasons": ["anchor", "search_match"],
        },
        {
            "entity_id": "two",
            "label": "recent",
            "relevance_reasons": ["search_match"],
        },
    ]


def test_builders_report_evidence_guidance_and_truncation() -> None:
    reader = FakeDecisionContextReader()
    evidence = build_evidence_map(
        [reader.dataset],
        [reader.analysis],
        [reader.claim],
        [reader.visualization],
    )
    guidance = task_guidance(
        "research_writing",
        "baseline controls",
        [reader.question],
        [reader.dataset],
        [reader.analysis],
        [reader.claim],
        [reader.visualization],
    )

    assert [item["reason"] for item in evidence] == [
        "dataset_question_links",
        "analysis_dataset_links",
        "claim_support_links",
        "visualization_links",
    ]
    assert guidance["candidate_outputs"][0]["entity_type"] == "claim"
    assert truncation([("questions", _envelope([reader.question], total=2), None)]) == {
        "was_truncated": True,
        "sections": [{"section": "questions", "returned": 1, "total": 2}],
    }


def test_build_decision_context_orchestrates_reader_selection_and_builders() -> None:
    payload = build_decision_context(
        FakeDecisionContextReader(),
        task_kind="research_writing",
        query="baseline controls",
        project_id="project-1",
        limit=5,
    )

    data = payload["data"]
    assert data["scope"]["project"] == {
        "project_id": "project-1",
        "name": "Decision Context Project",
        "status": "active",
    }
    assert data["context_summary"] == (
        "Found 1 questions, 1 notes, 1 datasets, 1 analyses, "
        "1 claims, and 1 visualizations for research_writing."
    )
    assert data["questions"][0]["relevance_reasons"] == [
        "search_match",
        "recent_activity",
    ]
    assert data["task_guidance"]["candidate_outputs"][0]["entity_type"] == "claim"
    assert data["write_front_door"]["allowed_task_kinds"] == list(TASK_KIND_VALUES)
    assert data["write_front_door"]["resolved_scope"]["project_id"] == "project-1"
    assert data["write_front_door"]["candidate_ids"]["questions"][0] == {
        "entity_type": "question",
        "entity_id": "question-1",
        "label": "Which baseline controls matter?",
    }
    assert data["write_front_door"]["candidate_ids"]["claims"][0]["entity_id"] == (
        "claim-1"
    )
    create_guidance = data["write_front_door"]["create_guidance"]
    assert any("lab_tracker_describe_schema" in item for item in create_guidance)
    # The propose-not-commit gate leads the create guidance (cap apr H4).
    assert any("a person commits" in item for item in create_guidance)
    assert data["evidence_map"][0]["entity"]["entity_type"] == "dataset"
    assert data["truncation"] == {"was_truncated": False, "sections": []}


def test_build_decision_context_uses_one_scoped_lookup_for_auto_resolution() -> None:
    class AmbiguousSearchReader(FakeDecisionContextReader):
        second_project = {
            "project_id": "project-2",
            "name": "Second Project",
            "status": "active",
        }
        project_id_lookup_calls = 0
        search_calls = 0

        def list_projects(
            self,
            *,
            status: str | None = None,
            limit: int = 50,
            offset: int = 0,
        ) -> JsonObject:
            return _envelope([self.project, self.second_project], limit=limit, offset=offset)

        def get_project(self, project_id: str) -> JsonObject | None:
            if project_id == self.project["project_id"]:
                return self.project
            if project_id == self.second_project["project_id"]:
                return self.second_project
            return None

        def search(
            self,
            query: str,
            *,
            project_id: str | None = None,
            include: str | None = None,
            limit: int = 20,
            offset: int = 0,
        ) -> JsonObject:
            self.search_calls += 1
            raise AssertionError("auto-resolution must not search one project at a time")

        def project_ids_with_search_matches(
            self,
            query: str,
            *,
            limit: int = 50,
        ) -> set[str]:
            self.project_id_lookup_calls += 1
            assert query == "baseline"
            assert limit == 500
            return {"project-1", "project-2"}

    reader = AmbiguousSearchReader()
    payload = build_decision_context(
        reader,
        task_kind="summary",
        query="baseline",
        limit=1,
    )

    assert payload["error"]["code"] == "ambiguous_project"
    assert {item["project_id"] for item in payload["error"]["candidate_projects"]} == {
        "project-1",
        "project-2",
    }
    assert reader.project_id_lookup_calls == 1
    assert reader.search_calls == 0


def test_repository_reader_project_match_lookup_finds_scoped_question_and_note_matches() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    question_project = api.create_project("Question match project", actor=actor)
    note_project = api.create_project("Note match project", actor=actor)
    hidden_project = api.create_project("Hidden match project", actor=actor)
    api.create_question(
        project_id=question_project.project_id,
        text="Does the basalt odor protocol need a baseline?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    api.create_note(
        project_id=note_project.project_id,
        raw_content="Basalt odor only appears in the free-text note.",
        status=NoteStatus.COMMITTED,
        actor=actor,
    )
    api.create_question(
        project_id=hidden_project.project_id,
        text="Basalt should not leak from inaccessible projects.",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    reader = RepositoryDecisionContextReader(
        api._repository,  # type: ignore[arg-type,attr-defined]
        accessible_project_ids={question_project.project_id, note_project.project_id},
    )

    select_count = 0

    def before_cursor_execute(
        conn,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    engine = api._test_resources[0]  # type: ignore[attr-defined]
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        matching_project_ids = reader.project_ids_with_search_matches("basalt", limit=500)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert matching_project_ids == {
        str(question_project.project_id),
        str(note_project.project_id),
    }
    assert select_count == 1


def test_repository_reader_project_match_lookup_preserves_ambiguity_after_many_matches() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    crowded_project = api.create_project("Crowded match project", actor=actor)
    sparse_project = api.create_project("Sparse match project", actor=actor)
    hidden_project = api.create_project("Hidden crowded project", actor=actor)
    for index in range(501):
        api.create_question(
            project_id=crowded_project.project_id,
            text=f"Chronicle high-volume match {index}",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            actor=actor,
        )
    api.create_question(
        project_id=sparse_project.project_id,
        text="Chronicle sparse project match",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    api.create_question(
        project_id=hidden_project.project_id,
        text="Chronicle hidden project match",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    reader = RepositoryDecisionContextReader(
        api._repository,  # type: ignore[arg-type,attr-defined]
        accessible_project_ids={crowded_project.project_id, sparse_project.project_id},
    )

    payload = build_decision_context(
        reader,
        task_kind="summary",
        query="chronicle",
        limit=1,
    )

    assert payload["error"]["code"] == "ambiguous_project"
    assert {item["project_id"] for item in payload["error"]["candidate_projects"]} == {
        str(crowded_project.project_id),
        str(sparse_project.project_id),
    }
