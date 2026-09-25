from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from api_helpers import repository_backed_api
from sqlalchemy import event

from lab_tracker.auth import AuthContext, Role
from lab_tracker.decision_context_builders import (
    build_evidence_map,
    task_guidance,
    truncation,
    write_front_door,
)
from lab_tracker.decision_context_constants import (
    CONTEXT_LOOKUP_LIMIT,
    EXPLORATION_NODE_TYPE_ORDER,
    TASK_KIND_VALUES,
)
from lab_tracker.decision_context_query import RepositoryDecisionContextReader
from lab_tracker.decision_context_selection import merge_entities
from lab_tracker.decision_context_types import JsonObject
from lab_tracker.decision_context_use_case import (
    AMBIGUOUS_PROJECT_CANDIDATE_LIMIT,
    build_decision_context,
)
from lab_tracker.models import (
    EntityRef,
    EntityType,
    ExplorationNodeType,
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
        # The repository reader adds the read-time interpretation to every claim.
        "effective_status": "supported",
        "superseded_by_claim_id": None,
        "contested_by_claim_ids": [],
        "invalidated_by_node_id": None,
        "pre_registered": False,
        "supported_by_dataset_ids": ["dataset-1"],
        "supported_by_analysis_ids": ["analysis-1"],
    }
    visualization = {
        "viz_id": "visualization-1",
        "analysis_id": "analysis-1",
        "caption": "Baseline comparison",
        "related_claim_ids": ["claim-1"],
    }
    # Stored in the order a naive single read would return them, so the
    # dead-end-first ordering below is the orchestrator's doing.
    exploration_nodes = [
        {
            "node_id": "decision-1",
            "project_id": "project-1",
            "node_type": "decision",
            "title": "Use the committed analysis",
            "status": "committed",
            "target": {"entity_type": "claim", "entity_id": "claim-1"},
        },
        {
            "node_id": "dead-end-1",
            "project_id": "project-1",
            "node_type": "dead_end",
            "title": "Side analysis went nowhere",
            "status": "committed",
            "target": {"entity_type": "dataset", "entity_id": "dataset-1"},
        },
        {
            "node_id": "pivot-1",
            "project_id": "project-1",
            "node_type": "pivot",
            "title": "Pivot back to the linked analysis",
            "status": "committed",
            "target": {"entity_type": "claim", "entity_id": "claim-1"},
        },
    ]
    coverage = {
        "project_id": "project-1",
        "unreviewed_count": 3,
        "oldest_unreviewed_at": "2026-06-01T00:00:00Z",
        "unplaced_count": 1,
        "archived_unreviewed_count": 0,
        "pending_change_sets": 1,
        "open_clarification_requests": 2,
        "last_capture_at": "2026-06-02T00:00:00Z",
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
        updated_first: bool = False,
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

    def list_exploration_nodes(
        self,
        *,
        project_id: str | None = None,
        node_type: str | None = None,
        status: str | None = None,
        created_by: str | None = None,
        limit: int = 50,
        offset: int = 0,
        recent_first: bool = False,
    ) -> JsonObject:
        nodes = [
            node
            for node in self.exploration_nodes
            if node_type is None or node["node_type"] == node_type
        ]
        return _envelope(nodes, limit=limit, offset=offset)

    def project_coverage(self, project_id: str) -> JsonObject | None:
        if project_id != self.project["project_id"]:
            return None
        return dict(self.coverage)


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


def test_build_decision_context_lists_questions_by_last_update() -> None:
    class OrderingSpyReader(FakeDecisionContextReader):
        def __init__(self) -> None:
            self.question_calls: list[dict[str, object]] = []
            self.note_calls: list[dict[str, object]] = []

        def list_questions(self, **kwargs: object) -> JsonObject:
            self.question_calls.append(dict(kwargs))
            return super().list_questions(**kwargs)  # type: ignore[arg-type]

        def list_notes(self, **kwargs: object) -> JsonObject:
            self.note_calls.append(dict(kwargs))
            return super().list_notes(**kwargs)  # type: ignore[arg-type]

    reader = OrderingSpyReader()
    build_decision_context(
        reader,
        task_kind="summary",
        query="baseline controls",
        project_id="project-1",
        limit=5,
    )

    assert len(reader.question_calls) == 1
    assert reader.question_calls[0].get("updated_first") is True
    assert reader.question_calls[0].get("recent_first", False) is False
    assert len(reader.note_calls) == 1
    assert reader.note_calls[0].get("recent_first") is True
    assert "updated_first" not in reader.note_calls[0]


def test_repository_reader_passes_updated_first_to_query_questions() -> None:
    class SpyRepository:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def query_questions(self, **kwargs: object) -> tuple[list[object], int]:
            self.calls.append(dict(kwargs))
            return [], 0

    repository = SpyRepository()
    reader = RepositoryDecisionContextReader(repository)  # type: ignore[arg-type]

    payload = reader.list_questions(project_id=None, limit=5, updated_first=True)

    assert payload["meta"]["total"] == 0
    assert len(repository.calls) == 1
    assert repository.calls[0]["updated_first"] is True
    assert repository.calls[0]["recent_first"] is False
    assert repository.calls[0]["limit"] == 5


def test_build_decision_context_reads_an_explicit_project_once() -> None:
    class CountingReader(FakeDecisionContextReader):
        def __init__(self) -> None:
            self.project_reads: list[str] = []

        def get_project(self, project_id: str) -> JsonObject | None:
            self.project_reads.append(project_id)
            return super().get_project(project_id)

    reader = CountingReader()
    payload = build_decision_context(
        reader,
        task_kind="summary",
        query="baseline controls",
        project_id="project-1",
        question_id="question-1",
        limit=5,
    )

    assert payload["data"]["scope"]["project"]["project_id"] == "project-1"
    assert reader.project_reads == ["project-1"]


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
    # Both matches count; the caller's limit of 1 caps the listed candidates.
    assert payload["error"]["candidate_projects_total"] == 2
    assert [item["project_id"] for item in payload["error"]["candidate_projects"]] == [
        "project-1"
    ]
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

    error = payload["error"]
    assert error["code"] == "ambiguous_project"
    # Both accessible matches count; the caller's limit of 1 caps the list.
    accessible_ids = sorted((str(crowded_project.project_id), str(sparse_project.project_id)))
    assert error["candidate_projects_total"] == 2
    assert [item["project_id"] for item in error["candidate_projects"]] == accessible_ids[:1]


def test_ambiguous_project_error_reads_a_bounded_number_of_candidates() -> None:
    matched_ids = {f"project-{index:03d}" for index in range(CONTEXT_LOOKUP_LIMIT)}

    class ManyMatchesReader(FakeDecisionContextReader):
        get_project_calls = 0

        def get_project(self, project_id: str) -> JsonObject | None:
            self.get_project_calls += 1
            return {"project_id": project_id, "name": project_id, "status": "active"}

        def project_ids_with_search_matches(
            self,
            query: str,
            *,
            limit: int = 50,
        ) -> set[str]:
            return set(sorted(matched_ids)[:limit])

    reader = ManyMatchesReader()
    payload = build_decision_context(reader, task_kind="summary", query="common")

    error = payload["error"]
    assert error["code"] == "ambiguous_project"
    assert reader.get_project_calls == AMBIGUOUS_PROJECT_CANDIDATE_LIMIT
    assert [item["project_id"] for item in error["candidate_projects"]] == sorted(matched_ids)[
        :AMBIGUOUS_PROJECT_CANDIDATE_LIMIT
    ]
    # The lookup stopped at its limit, so the total is a lower bound.
    assert error["candidate_projects_total"] == CONTEXT_LOOKUP_LIMIT
    assert error["candidate_projects_truncated"] is True
    assert error["candidate_projects_omitted"] == (
        CONTEXT_LOOKUP_LIMIT - AMBIGUOUS_PROJECT_CANDIDATE_LIMIT
    )
    assert f"{CONTEXT_LOOKUP_LIMIT} or more projects match" in error["message"]
    assert "project_id" in error["message"]


def test_ambiguous_project_error_reports_exact_count_of_omitted_matches() -> None:
    matched_ids = {f"project-{index:03d}" for index in range(AMBIGUOUS_PROJECT_CANDIDATE_LIMIT + 3)}

    class SomeMatchesReader(FakeDecisionContextReader):
        def get_project(self, project_id: str) -> JsonObject | None:
            return {"project_id": project_id, "name": project_id, "status": "active"}

        def project_ids_with_search_matches(
            self,
            query: str,
            *,
            limit: int = 50,
        ) -> set[str]:
            return set(matched_ids)

    payload = build_decision_context(SomeMatchesReader(), task_kind="summary", query="common")

    error = payload["error"]
    assert len(error["candidate_projects"]) == AMBIGUOUS_PROJECT_CANDIDATE_LIMIT
    assert error["candidate_projects_total"] == AMBIGUOUS_PROJECT_CANDIDATE_LIMIT + 3
    assert error["candidate_projects_truncated"] is True
    assert error["candidate_projects_omitted"] == 3
    assert f"{AMBIGUOUS_PROJECT_CANDIDATE_LIMIT + 3} projects match" in error["message"]


class _ThirteenMatchesReader(FakeDecisionContextReader):
    matched_ids = {f"project-{index:03d}" for index in range(AMBIGUOUS_PROJECT_CANDIDATE_LIMIT + 3)}

    def __init__(self) -> None:
        super().__init__()
        self.get_project_calls = 0

    def get_project(self, project_id: str) -> JsonObject | None:
        self.get_project_calls += 1
        return {"project_id": project_id, "name": project_id, "status": "active"}

    def project_ids_with_search_matches(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> set[str]:
        return set(self.matched_ids)


@pytest.mark.parametrize(
    ("limit", "listed_text"),
    [(3, "3 are listed"), (1, "1 is listed")],
)
def test_ambiguous_project_error_lists_no_more_search_matches_than_the_limit(
    limit: int,
    listed_text: str,
) -> None:
    """Search candidates honour the caller's limit, as the active fallback does."""

    reader = _ThirteenMatchesReader()
    payload = build_decision_context(reader, task_kind="summary", query="common", limit=limit)

    error = payload["error"]
    assert error["code"] == "ambiguous_project"
    assert reader.get_project_calls == limit
    assert [item["project_id"] for item in error["candidate_projects"]] == sorted(
        reader.matched_ids
    )[:limit]
    assert error["candidate_projects_total"] == len(reader.matched_ids)
    assert error["candidate_projects_truncated"] is True
    assert error["candidate_projects_omitted"] == len(reader.matched_ids) - limit
    assert f"{len(reader.matched_ids)} projects match the query; {listed_text}." in (
        error["message"]
    )


def test_repository_reader_claims_carry_effective_status() -> None:
    from lab_tracker.models import ClaimRelation

    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Effective status project", actor=actor)
    hidden = api.create_project("Hidden project", actor=actor)
    old = api.create_claim(project.project_id, "Old claim", 50.0, actor=actor)
    new = api.create_claim(project.project_id, "Newer claim", 50.0, actor=actor)
    api.create_claim_edge(
        new.claim_id,
        target_claim_id=old.claim_id,
        relation=ClaimRelation.SUPERSEDES,
        actor=actor,
    )
    hidden_claim = api.create_claim(hidden.project_id, "Hidden claim", 50.0, actor=actor)
    reader = RepositoryDecisionContextReader(
        api._repository,  # type: ignore[arg-type,attr-defined]
        accessible_project_ids={project.project_id},
    )

    detail = reader.get_claim(str(old.claim_id))
    assert detail is not None
    assert detail["status"] == "proposed"
    assert detail["effective_status"] == "superseded"
    assert detail["superseded_by_claim_id"] == str(new.claim_id)
    assert detail["contested_by_claim_ids"] == []
    assert detail["invalidated_by_node_id"] is None
    assert detail["pre_registered"] is False
    assert reader.get_claim(str(hidden_claim.claim_id)) is None

    listing = reader.list_claims(project_id=str(project.project_id))
    assert listing["meta"]["total"] == 2
    rows = {row["claim_id"]: row for row in listing["data"]}
    keys = {
        "effective_status",
        "superseded_by_claim_id",
        "contested_by_claim_ids",
        "invalidated_by_node_id",
        "pre_registered",
    }
    assert all(keys <= set(row) for row in rows.values())
    assert rows[str(old.claim_id)]["effective_status"] == "superseded"
    assert rows[str(new.claim_id)]["effective_status"] == "proposed"
    assert reader.list_claims(project_id=str(hidden.project_id))["data"] == []


def test_exploration_node_type_order_puts_dead_ends_first() -> None:
    assert EXPLORATION_NODE_TYPE_ORDER == ("dead_end", "pivot", "decision")
    assert sorted(EXPLORATION_NODE_TYPE_ORDER) == sorted(item.value for item in ExplorationNodeType)


def test_write_front_door_lists_exploration_node_candidate_ids() -> None:
    nodes = [
        {"node_id": f"dead-end-{index}", "node_type": "dead_end", "title": f"Dead end {index}"}
        for index in range(1, 13)
    ]
    nodes[0]["title"] = "Side analysis went nowhere"

    payload = write_front_door(
        task_kind="summary",
        project=FakeDecisionContextReader.project,
        anchors=[],
        questions=[],
        sessions=[],
        datasets=[],
        analyses=[],
        claims=[],
        visualizations=[],
        exploration_nodes=nodes,
    )

    candidates = payload["candidate_ids"]["exploration_nodes"]
    assert candidates[0] == {
        "entity_type": "exploration_node",
        "entity_id": "dead-end-1",
        "label": "Side analysis went nowhere",
    }
    assert len(candidates) == 10


def test_build_decision_context_orders_exploration_nodes_dead_end_first() -> None:
    payload = build_decision_context(
        FakeDecisionContextReader(),
        task_kind="summary",
        query="baseline controls",
        project_id="project-1",
        limit=5,
    )

    data = payload["data"]
    assert [node["node_type"] for node in data["exploration_nodes"]] == [
        "dead_end",
        "pivot",
        "decision",
    ]
    assert all(
        node["relevance_reasons"] == ["recent_activity"] for node in data["exploration_nodes"]
    )
    assert [
        ref["entity_id"] for ref in data["write_front_door"]["candidate_ids"]["exploration_nodes"]
    ] == ["dead-end-1", "pivot-1", "decision-1"]


def test_build_decision_context_reports_exploration_truncation_per_type() -> None:
    limit = 5

    class TruncatedDeadEndsReader(FakeDecisionContextReader):
        def list_exploration_nodes(self, **kwargs: object) -> JsonObject:
            payload = super().list_exploration_nodes(**kwargs)  # type: ignore[arg-type]
            if kwargs.get("node_type") == "dead_end":
                payload["meta"]["total"] = limit + 1
            return payload

    payload = build_decision_context(
        TruncatedDeadEndsReader(),
        task_kind="summary",
        query="baseline controls",
        project_id="project-1",
        limit=limit,
    )

    truncated = payload["data"]["truncation"]
    assert truncated["was_truncated"] is True
    assert {
        "section": "exploration_nodes.dead_end",
        "returned": 1,
        "total": limit + 1,
    } in truncated["sections"]
    assert not any(
        section["section"] in {"exploration_nodes.pivot", "exploration_nodes.decision"}
        for section in truncated["sections"]
    )


def test_build_decision_context_includes_coverage_block() -> None:
    class CoverageSpyReader(FakeDecisionContextReader):
        def __init__(self) -> None:
            self.coverage_reads: list[str] = []

        def project_coverage(self, project_id: str) -> JsonObject | None:
            self.coverage_reads.append(project_id)
            return super().project_coverage(project_id)

    reader = CoverageSpyReader()
    payload = build_decision_context(
        reader,
        task_kind="summary",
        query="baseline controls",
        project_id="project-1",
        limit=5,
    )

    data = payload["data"]
    assert data["coverage"] == FakeDecisionContextReader.coverage
    assert reader.coverage_reads == ["project-1"]
    # Coverage is a separate key; the pinned summary sentence does not change.
    assert data["context_summary"] == (
        "Found 1 questions, 1 notes, 1 datasets, 1 analyses, "
        "1 claims, and 1 visualizations for summary."
    )


def test_build_decision_context_passes_created_by_to_exploration_reads() -> None:
    class ExplorationSpyReader(FakeDecisionContextReader):
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def list_exploration_nodes(self, **kwargs: object) -> JsonObject:
            self.calls.append(dict(kwargs))
            return super().list_exploration_nodes(**kwargs)  # type: ignore[arg-type]

    reader = ExplorationSpyReader()
    build_decision_context(
        reader,
        task_kind="progress_review",
        query="baseline controls",
        project_id="project-1",
        created_by="user-1",
        limit=5,
    )

    assert [call["node_type"] for call in reader.calls] == list(EXPLORATION_NODE_TYPE_ORDER)
    assert all(call["created_by"] == "user-1" for call in reader.calls)
    assert all(call["recent_first"] is True and call["limit"] == 5 for call in reader.calls)
    assert all(call["project_id"] == "project-1" for call in reader.calls)


def test_repository_reader_returns_project_coverage_summary() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Coverage project", actor=actor)
    hidden = api.create_project("Hidden coverage project", actor=actor)
    api.create_note(project.project_id, "First capture", actor=actor)
    api.create_note(project.project_id, "Second capture", actor=actor)
    api.create_note(hidden.project_id, "Hidden capture", actor=actor)
    reader = RepositoryDecisionContextReader(
        api._repository,  # type: ignore[arg-type,attr-defined]
        accessible_project_ids={project.project_id},
    )

    coverage = reader.project_coverage(str(project.project_id))

    assert coverage is not None
    assert coverage["project_id"] == str(project.project_id)
    assert coverage["unreviewed_count"] == 2
    assert coverage["unplaced_count"] == 0
    assert coverage["archived_unreviewed_count"] == 0
    assert coverage["pending_change_sets"] == 0
    assert coverage["open_clarification_requests"] == 0
    assert coverage["oldest_unreviewed_at"] is not None
    assert coverage["last_capture_at"] is not None
    assert reader.project_coverage(str(hidden.project_id)) is None


def test_repository_reader_lists_exploration_nodes_scoped_to_accessible_projects() -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)
    project = api.create_project("Exploration project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Which baseline controls matter?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    target = EntityRef(entity_type=EntityType.QUESTION, entity_id=question.question_id)
    dead_end = api.create_exploration_node(
        project.project_id,
        node_type=ExplorationNodeType.DEAD_END,
        title="Side analysis went nowhere",
        target=target,
        hypothesis="The side analysis would settle the question.",
        failure_mode="It never reused the committed dataset.",
        lesson="Keep the spine intact first.",
        actor=actor,
    )
    pivot = api.create_exploration_node(
        project.project_id,
        node_type=ExplorationNodeType.PIVOT,
        title="Pivot back to the linked analysis",
        target=target,
        trigger="Dead-end side analysis",
        rationale="The retained graph already has a support path.",
        invalidates_node_id=dead_end.node_id,
        actor=actor,
    )
    decision = api.create_exploration_node(
        project.project_id,
        node_type=ExplorationNodeType.DECISION,
        title="Use the committed analysis",
        target=target,
        choice="Reuse it",
        alternatives_considered=["Wait for more data"],
        rationale="It is already linked.",
        actor=actor,
    )
    reader = RepositoryDecisionContextReader(
        api._repository,  # type: ignore[arg-type,attr-defined]
        accessible_project_ids={project.project_id},
    )

    listing = reader.list_exploration_nodes(project_id=str(project.project_id), recent_first=True)
    assert listing["meta"]["total"] == 3
    assert [item["node_id"] for item in listing["data"]] == [
        str(decision.node_id),
        str(pivot.node_id),
        str(dead_end.node_id),
    ]
    assert listing["data"][0]["node_type"] == "decision"

    dead_ends = reader.list_exploration_nodes(
        project_id=str(project.project_id),
        node_type="dead_end",
    )
    assert dead_ends["meta"]["total"] == 1
    assert [item["node_id"] for item in dead_ends["data"]] == [str(dead_end.node_id)]

    outsider = RepositoryDecisionContextReader(
        api._repository,  # type: ignore[arg-type,attr-defined]
        accessible_project_ids=set(),
    )
    assert outsider.list_exploration_nodes(project_id=str(project.project_id)) == {
        "data": [],
        "meta": {"limit": 50, "offset": 0, "total": 0},
    }
