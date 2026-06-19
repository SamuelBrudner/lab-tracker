from __future__ import annotations

from api_helpers import repository_backed_api

from lab_tracker.demo_seed import DEMO_PROJECT_NAME, seed_demo_data
from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
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
