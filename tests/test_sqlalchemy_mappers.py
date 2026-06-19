from datetime import datetime, timezone
from uuid import UUID, uuid4

from lab_tracker import sqlalchemy_mappers
from lab_tracker.db_models import (
    GraphDraftBatchRunModel,
    GraphDraftBatchSettingsModel,
    ProjectModel,
)
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Dataset,
    DatasetCommitManifest,
    EntityRef,
    EntityType,
    ExternalArtifactReference,
    Note,
    NoteStatus,
    OutcomeStatus,
    Project,
    ProjectGroup,
    ProjectGroupKind,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
)
from lab_tracker.provenance_ingestion import (
    EXTERNAL_ARTIFACTS_METADATA_KEY,
    encode_external_artifacts,
)
from lab_tracker.schemas import Envelope, ProjectCreate
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    apply_project_group_to_model,
    apply_project_to_model,
    project_from_model,
    project_group_from_model,
    project_group_to_model,
    project_to_model,
)
from lab_tracker.sqlalchemy_mappers import (
    analysis_from_model,
    analysis_to_model,
    dataset_from_model,
    dataset_question_link_from_model,
    dataset_question_link_models,
    dataset_to_model,
    entity_ref_from_model,
    note_from_model,
    note_target_models,
    note_to_model,
    question_from_model,
    question_parent_models,
    question_to_model,
)
from lab_tracker.sqlalchemy_repository_parts.graph_batches import (
    run_from_model,
    settings_from_model,
)


def _ts() -> datetime:
    return datetime(2026, 2, 7, tzinfo=timezone.utc)


def _naive_ts() -> datetime:
    return datetime(2026, 2, 7, 9, 30, 15)


def _assert_utc(value: datetime) -> None:
    assert value.tzinfo == timezone.utc


def test_project_mapper_round_trip():
    group_id = uuid4()
    project = Project(
        project_id=uuid4(),
        group_id=group_id,
        name="Neural Mapping",
        description="Parent persistence milestone",
        status=ProjectStatus.ARCHIVED,
        created_by="scientist@example.com",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = project_to_model(project)
    mapped = project_from_model(row)
    assert mapped == project


def test_project_group_mapper_round_trip():
    group = ProjectGroup(
        group_id=uuid4(),
        name="Fly navigation lab",
        description="Shared PI oversight container",
        kind=ProjectGroupKind.LAB,
        group_read_all=True,
        created_by="scientist@example.com",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = project_group_to_model(group)
    mapped = project_group_from_model(row)

    assert mapped == group

    updated = group.model_copy(update={"name": "Updated lab", "group_read_all": False})
    apply_project_group_to_model(row, updated)
    assert project_group_from_model(row) == updated


def test_graph_batch_mappers_normalize_sqlite_naive_datetimes():
    timestamp = _naive_ts()
    expected = timestamp.replace(tzinfo=timezone.utc)
    project_id = uuid4()
    user_id = uuid4()

    settings = settings_from_model(
        GraphDraftBatchSettingsModel(
            settings_id=str(uuid4()),
            project_id=str(project_id),
            enabled=True,
            cadence_minutes=60,
            run_at_local_time="06:00",
            timezone_name="UTC",
            next_run_at=timestamp,
            created_at=timestamp,
            updated_at=timestamp,
            updated_by=str(user_id),
        )
    )
    assert settings.next_run_at == expected
    assert settings.created_at == expected
    assert settings.updated_at == expected
    _assert_utc(settings.next_run_at)
    _assert_utc(settings.created_at)
    _assert_utc(settings.updated_at)

    run = run_from_model(
        GraphDraftBatchRunModel(
            run_id=str(uuid4()),
            project_id=str(project_id),
            trigger="manual",
            status="skipped",
            window_start=timestamp,
            window_end=timestamp,
            note_count=0,
            batch_key="manual:test",
            change_set_id=None,
            summary="No staged notes.",
            error_metadata={},
            created_by=str(user_id),
            created_by_user_id=str(user_id),
            created_at=timestamp,
            updated_at=timestamp,
            started_at=timestamp,
            finished_at=timestamp,
        )
    )
    for value in (
        run.window_start,
        run.window_end,
        run.created_at,
        run.updated_at,
        run.started_at,
        run.finished_at,
    ):
        assert value == expected
        _assert_utc(value)


def test_project_mapper_pilot_preserves_compatibility_barrel():
    assert sqlalchemy_mappers.project_to_model is project_to_model
    assert sqlalchemy_mappers.project_from_model is project_from_model
    assert sqlalchemy_mappers.apply_project_to_model is apply_project_to_model
    assert sqlalchemy_mappers.project_group_to_model is project_group_to_model
    assert sqlalchemy_mappers.project_group_from_model is project_group_from_model
    assert sqlalchemy_mappers.apply_project_group_to_model is apply_project_group_to_model


def test_project_mapper_pilot_keeps_schema_domain_and_orm_boundaries():
    payload = ProjectCreate(
        name="Neural Mapping",
        description="wire input",
        status=ProjectStatus.ACTIVE,
    )
    project = Project(
        project_id=uuid4(),
        name=payload.name,
        description=payload.description or "",
        status=payload.status or ProjectStatus.ACTIVE,
        created_by="scientist@example.com",
        created_at=_ts(),
        updated_at=_ts(),
    )

    row = project_to_model(project)
    response_payload = Envelope[Project](data=project).model_dump(mode="json")

    assert isinstance(row, ProjectModel)
    assert row.project_id == str(project.project_id)
    assert project_from_model(row) == project
    assert response_payload["data"]["project_id"] == str(project.project_id)
    assert response_payload["data"]["status"] == "active"

    updated = project.model_copy(update={"name": "Neural Mapping Updated"})
    apply_project_to_model(row, updated)
    assert project_from_model(row) == updated


def test_question_mapper_round_trip_with_parent_links():
    parent_id = uuid4()
    question = Question(
        question_id=uuid4(),
        project_id=uuid4(),
        text="How stable is the baseline?",
        question_type=QuestionType.DESCRIPTIVE,
        hypothesis="Baseline should stay within 2 SD.",
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[parent_id],
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = question_to_model(question)
    parent_rows = question_parent_models(question)
    parent_ids = [UUID(parent_row.parent_question_id) for parent_row in parent_rows]
    mapped = question_from_model(row, parent_question_ids=parent_ids)
    assert parent_ids == [parent_id]
    assert mapped == question


def test_dataset_mapper_round_trip_preserves_manifest_fields():
    primary_question_id = uuid4()
    secondary_question_id = uuid4()
    links = [
        QuestionLink(
            question_id=primary_question_id,
            role=QuestionLinkRole.PRIMARY,
            outcome_status=OutcomeStatus.SUPPORTS,
        ),
        QuestionLink(
            question_id=secondary_question_id,
            role=QuestionLinkRole.SECONDARY,
            outcome_status=OutcomeStatus.INCONCLUSIVE,
        ),
    ]
    artifact = ExternalArtifactReference(
        source_system="s3",
        uri="s3://lab-bucket/acquisitions/run-001/manifest.json",
        content_hash="sha256:manifest001",
        metadata={"file_count": 12},
    )
    dataset = Dataset(
        dataset_id=uuid4(),
        project_id=uuid4(),
        commit_hash="abc123",
        primary_question_id=primary_question_id,
        question_links=links,
        commit_manifest=DatasetCommitManifest(
            question_links=links,
            external_artifacts=[artifact],
            metadata={"run": "7"},
            bids_metadata={"Name": "Example"},
            nwb_metadata={"Session Description": "baseline"},
            note_ids=[uuid4()],
            source_session_id=uuid4(),
        ),
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = dataset_to_model(dataset)
    link_rows = dataset_question_link_models(dataset)
    mapped_links = [dataset_question_link_from_model(item) for item in link_rows]
    mapped = dataset_from_model(row, question_links=mapped_links)
    assert mapped.question_links == links
    assert mapped.commit_manifest.question_links == links
    assert mapped.commit_manifest.files == []
    assert mapped.commit_manifest.external_artifacts == [artifact]
    assert mapped.commit_manifest.metadata == {"run": "7"}
    assert mapped.commit_manifest.bids_metadata == {"Name": "Example"}
    assert mapped.commit_manifest.nwb_metadata == {"Session Description": "baseline"}
    assert mapped.commit_manifest.note_ids == dataset.commit_manifest.note_ids
    assert mapped.commit_manifest.source_session_id == dataset.commit_manifest.source_session_id


def test_dataset_mapper_reads_legacy_external_artifact_metadata():
    artifact = ExternalArtifactReference(
        source_system="datalad",
        uri="datalad://lab/plume-navigation?commit=abc123",
        content_hash="sha256:manifest123",
    )
    dataset = Dataset(
        dataset_id=uuid4(),
        project_id=uuid4(),
        commit_hash="legacy-hash",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(
            metadata={
                EXTERNAL_ARTIFACTS_METADATA_KEY: encode_external_artifacts([artifact, artifact])
            },
        ),
    )
    row = dataset_to_model(dataset)
    row.manifest_external_artifacts = []

    mapped = dataset_from_model(row)

    assert mapped.commit_manifest.external_artifacts == [artifact]
    assert mapped.commit_manifest.metadata == dataset.commit_manifest.metadata


def test_dataset_mapper_ignores_malformed_legacy_external_artifact_metadata():
    dataset = Dataset(
        dataset_id=uuid4(),
        project_id=uuid4(),
        commit_hash="legacy-hash",
        primary_question_id=uuid4(),
        question_links=[],
        commit_manifest=DatasetCommitManifest(
            metadata={EXTERNAL_ARTIFACTS_METADATA_KEY: "not-json"},
        ),
    )
    row = dataset_to_model(dataset)
    row.manifest_external_artifacts = []

    mapped = dataset_from_model(row)

    assert mapped.commit_manifest.external_artifacts == []
    assert mapped.commit_manifest.metadata == dataset.commit_manifest.metadata


def test_analysis_mapper_round_trip_preserves_external_artifacts():
    artifact = ExternalArtifactReference(
        source_system="wandb",
        uri="wandb://entity/project/runs/run-001",
        content_hash="sha256:wandb-run-001",
        metadata={"sweep": "gain-fit"},
    )
    dataset_id = uuid4()
    analysis = Analysis(
        analysis_id=uuid4(),
        project_id=uuid4(),
        dataset_ids=[dataset_id],
        method_hash="method-1",
        code_version="git:abc123",
        environment_hash="conda:lock",
        external_artifacts=[artifact],
        status=AnalysisStatus.COMMITTED,
        executed_at=_ts(),
        created_at=_ts(),
        updated_at=_ts(),
    )

    row = analysis_to_model(analysis)
    mapped = analysis_from_model(row, dataset_ids=[dataset_id])

    assert mapped == analysis


def test_note_mapper_round_trip_for_supported_fields():
    targets = [EntityRef(entity_type=EntityType.QUESTION, entity_id=uuid4())]
    note = Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="notes.md",
        transcribed_text="signal is stable",
        targets=targets,
        metadata={"device": "np2"},
        client_capture_id="capture-round-trip",
        status=NoteStatus.COMMITTED,
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    row = note_to_model(note)
    mapped = note_from_model(
        row,
        targets=[entity_ref_from_model(item) for item in note_target_models(note)],
    )
    assert mapped.raw_asset is None
    assert mapped.metadata == {"device": "np2"}
    assert mapped.client_capture_id == "capture-round-trip"
    assert mapped.targets == targets
