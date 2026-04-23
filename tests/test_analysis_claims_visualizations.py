from uuid import uuid4

import pytest

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AnalysisStatus,
    ClaimInput,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    QuestionStatus,
    QuestionType,
    VisualizationInput,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _setup_project_with_question(api: LabTrackerAPI, actor: AuthContext):
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the signal stable?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    return project, question


def test_analysis_commit_requires_committed_datasets():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project, question = _setup_project_with_question(api, actor)
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-1",
        code_version="v1",
        actor=actor,
    )
    with pytest.raises(ValidationError):
        api.commit_analysis(analysis.analysis_id, actor=actor)
    api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    analysis_result, claims, visualizations = api.commit_analysis(
        analysis.analysis_id,
        environment_hash="env-1",
        claims=[
            ClaimInput(
                statement="Signal is stable",
                confidence=0.8,
                status=ClaimStatus.SUPPORTED,
            )
        ],
        visualizations=[
            VisualizationInput(
                viz_type="line",
                file_path="figs/signal.png",
            )
        ],
        actor=actor,
    )
    assert analysis_result.status == AnalysisStatus.COMMITTED
    assert analysis_result.environment_hash == "env-1"
    assert claims and analysis.analysis_id in claims[0].supported_by_analysis_ids
    assert visualizations


def test_analysis_cannot_be_created_as_committed_with_staged_datasets():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project, question = _setup_project_with_question(api, actor)
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )

    with pytest.raises(ValidationError):
        api.create_analysis(
            project_id=project.project_id,
            dataset_ids=[dataset.dataset_id],
            method_hash="method-1",
            code_version="v1",
            status=AnalysisStatus.COMMITTED,
            actor=actor,
        )

    api.update_dataset(
        dataset.dataset_id,
        commit_manifest=DatasetCommitManifestInput(
            files=[DatasetFile(path="data.csv", checksum="abc123")]
        ),
        actor=actor,
    )
    api.update_dataset(dataset.dataset_id, status=DatasetStatus.COMMITTED, actor=actor)
    committed = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-2",
        code_version="v2",
        status=AnalysisStatus.COMMITTED,
        actor=actor,
    )

    assert committed.status == AnalysisStatus.COMMITTED


def test_claim_status_transitions_and_edits():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project, question = _setup_project_with_question(api, actor)
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    claim = api.create_claim(
        project_id=project.project_id,
        statement="Baseline distribution is stable",
        confidence=45.0,
        actor=actor,
    )
    with pytest.raises(ValidationError):
        api.update_claim(claim.claim_id, status=ClaimStatus.SUPPORTED, actor=actor)
    supported = api.update_claim(
        claim.claim_id,
        status=ClaimStatus.SUPPORTED,
        supported_by_dataset_ids=[dataset.dataset_id],
        actor=actor,
    )
    assert supported.status == ClaimStatus.SUPPORTED
    with pytest.raises(ValidationError):
        api.update_claim(claim.claim_id, statement="Updated statement", actor=actor)


def test_visualization_filters_and_analysis_question_links():
    api = LabTrackerAPI.in_memory()
    actor = _actor()
    project, question = _setup_project_with_question(api, actor)
    dataset = api.create_dataset(
        project_id=project.project_id,
        primary_question_id=question.question_id,
        actor=actor,
    )
    analysis = api.create_analysis(
        project_id=project.project_id,
        dataset_ids=[dataset.dataset_id],
        method_hash="method-2",
        code_version="v2",
        actor=actor,
    )
    claim = api.create_claim(
        project_id=project.project_id,
        statement="Event rate increases",
        confidence=0.6,
        supported_by_analysis_ids=[analysis.analysis_id],
        actor=actor,
    )
    visualization = api.create_visualization(
        analysis_id=analysis.analysis_id,
        viz_type="heatmap",
        file_path="figs/heatmap.png",
        related_claim_ids=[claim.claim_id],
        actor=actor,
    )
    by_claim = api.list_visualizations(claim_id=claim.claim_id)
    assert any(viz.viz_id == visualization.viz_id for viz in by_claim)
    by_analysis = api.list_visualizations(analysis_id=analysis.analysis_id)
    assert any(viz.viz_id == visualization.viz_id for viz in by_analysis)
    by_question = api.list_analyses(question_id=question.question_id)
    assert any(item.analysis_id == analysis.analysis_id for item in by_question)
