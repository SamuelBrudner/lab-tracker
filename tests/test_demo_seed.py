from __future__ import annotations

from api_helpers import repository_backed_api

from lab_tracker.demo_seed import DEMO_PROJECT_NAME, seed_demo_data
from lab_tracker.golden_day import GOLDEN_DAY_CLARIFICATION, GOLDEN_DAY_PROVIDER
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    GraphChangeSetStatus,
    GraphDraftMode,
    SessionType,
)


def test_seed_demo_data_creates_representative_project_graph() -> None:
    api = repository_backed_api()

    result = seed_demo_data(api)

    assert result.created is True
    assert result.project_name == DEMO_PROJECT_NAME
    assert result.question_count == 1
    assert result.dataset_count == 1
    assert result.note_count == 1
    assert result.analysis_count == 1
    assert result.claim_count == 1
    assert result.visualization_count == 1

    sessions = api.list_sessions(project_id=result.project_id)
    notes = api.list_notes(project_id=result.project_id)
    datasets = api.list_datasets(project_id=result.project_id)
    analyses = api.list_analyses(project_id=result.project_id)
    claims = api.list_claims(project_id=result.project_id)
    visualizations = api.list_visualizations(project_id=result.project_id)

    assert sessions[0].session_type == SessionType.OPERATIONAL
    assert notes[0].targets[0].entity_type == EntityType.QUESTION
    assert datasets[0].status == DatasetStatus.COMMITTED
    assert datasets[0].commit_manifest.note_ids == [notes[0].note_id]
    assert datasets[0].commit_manifest.source_session_id == sessions[0].session_id
    assert analyses[0].status == AnalysisStatus.COMMITTED
    assert claims[0].status == ClaimStatus.SUPPORTED
    assert claims[0].supported_by_dataset_ids == [datasets[0].dataset_id]
    assert claims[0].supported_by_analysis_ids == [analyses[0].analysis_id]
    assert visualizations[0].related_claim_ids == [claims[0].claim_id]


def test_seed_demo_data_is_idempotent_by_default() -> None:
    api = repository_backed_api()
    first = seed_demo_data(api)

    second = seed_demo_data(api)

    assert second.created is False
    assert second.project_id == first.project_id
    assert len(api.list_projects()) == 1


def test_seed_demo_data_without_review_reports_no_batch() -> None:
    api = repository_backed_api()

    result = seed_demo_data(api)

    assert result.staged_note_count == 0
    assert result.review_change_set_id is None
    assert result.as_dict()["review_change_set_id"] is None


def test_seed_demo_with_review_stages_captures_and_a_ready_batch() -> None:
    api = repository_backed_api()

    result = seed_demo_data(api, with_review=True)

    assert result.created is True
    assert result.review_change_set_id is not None
    assert result.as_dict()["review_change_set_id"] == str(result.review_change_set_id)
    assert result.staged_note_count == 14
    change_set = api.get_graph_change_set(result.review_change_set_id)
    assert change_set.project_id == result.project_id
    assert change_set.status == GraphChangeSetStatus.READY
    assert change_set.draft_mode == GraphDraftMode.GRAPH_BATCH
    assert change_set.provider == GOLDEN_DAY_PROVIDER
    assert change_set.operations
    assert change_set.clarification_requests == [GOLDEN_DAY_CLARIFICATION]
    assert change_set.review_assignee is not None


def test_seed_demo_with_review_is_idempotent() -> None:
    api = repository_backed_api()
    first = seed_demo_data(api, with_review=True)

    second = seed_demo_data(api, with_review=True)

    assert second.created is False
    assert second.project_id == first.project_id
    assert second.review_change_set_id == first.review_change_set_id
    assert second.staged_note_count == first.staged_note_count == 14
    batches, total = api.query_graph_change_sets(
        project_id=first.project_id,
        draft_mode=GraphDraftMode.GRAPH_BATCH,
        include_operations=False,
    )
    assert total == len(batches) == 1


def test_seed_demo_with_review_adds_review_to_an_existing_demo_project() -> None:
    api = repository_backed_api()
    plain = seed_demo_data(api)
    assert plain.review_change_set_id is None

    reviewed = seed_demo_data(api, with_review=True)

    assert reviewed.created is False
    assert reviewed.project_id == plain.project_id
    assert reviewed.review_change_set_id is not None
    assert reviewed.staged_note_count == 14
    assert len(api.list_projects()) == 1
