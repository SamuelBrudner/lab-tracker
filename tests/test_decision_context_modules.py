from __future__ import annotations

from lab_tracker.decision_context_builders import (
    build_evidence_map,
    task_guidance,
    truncation,
)
from lab_tracker.decision_context_selection import merge_entities, project_ids_from_search
from lab_tracker.decision_context_types import JsonObject
from lab_tracker.decision_context_use_case import build_decision_context


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
    ) -> JsonObject:
        return _envelope([self.question], limit=limit, offset=offset)

    def list_notes(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return _envelope([self.note], limit=limit, offset=offset)

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        session_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return _envelope([], limit=limit, offset=offset)

    def list_datasets(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return _envelope([self.dataset], limit=limit, offset=offset)

    def list_analyses(
        self,
        *,
        project_id: str | None = None,
        dataset_id: str | None = None,
        question_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return _envelope([self.analysis], limit=limit, offset=offset)

    def list_claims(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
        dataset_id: str | None = None,
        analysis_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JsonObject:
        return _envelope([self.claim], limit=limit, offset=offset)

    def list_visualizations(
        self,
        *,
        project_id: str | None = None,
        analysis_id: str | None = None,
        claim_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
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


def test_project_ids_from_search_uses_question_and_note_payloads() -> None:
    payload = {
        "data": {
            "questions": [{"project_id": "project-1"}],
            "notes": [{"project_id": "project-2"}],
        },
        "meta": {"questions_count": 1, "notes_count": 1},
    }

    assert project_ids_from_search(payload) == {"project-1", "project-2"}


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
    assert data["evidence_map"][0]["entity"]["entity_type"] == "dataset"
    assert data["truncation"] == {"was_truncated": False, "sections": []}
