from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from lab_tracker.db import Base
from lab_tracker.db_models import DatasetFileModel
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetStatus,
    EntityRef,
    EntityType,
    Note,
    NoteStatus,
    OutcomeStatus,
    Project,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    Session,
    SessionStatus,
    SessionType,
    Visualization,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _ts(offset_seconds: int = 0) -> datetime:
    return datetime(2026, 2, 7, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_project_repository_crud(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Persistence",
        description="Scaffold SQL repository",
        status=ProjectStatus.ACTIVE,
        created_by="operator-1",
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)
    repo.commit()
    assert repo.projects.get(project.project_id) == project
    assert repo.projects.list() == [project]
    deleted = repo.projects.delete(project.project_id)
    repo.commit()
    assert deleted == project
    assert repo.projects.get(project.project_id) is None


def test_question_repository_persists_parent_links(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Question Graph",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)
    parent_question = Question(
        question_id=uuid4(),
        project_id=project.project_id,
        text="Primary hypothesis?",
        question_type=QuestionType.HYPOTHESIS_DRIVEN,
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[],
        created_at=_ts(1),
        updated_at=_ts(1),
    )
    child_question = Question(
        question_id=uuid4(),
        project_id=project.project_id,
        text="Follow-up measure?",
        question_type=QuestionType.METHOD_DEV,
        status=QuestionStatus.STAGED,
        parent_question_ids=[parent_question.question_id],
        created_at=_ts(2),
        updated_at=_ts(2),
    )
    repo.questions.save(parent_question)
    repo.questions.save(child_question)
    repo.commit()
    loaded_child = repo.questions.get(child_question.question_id)
    assert loaded_child is not None
    assert loaded_child.parent_question_ids == [parent_question.question_id]


def test_note_repository_persists_supported_children(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Notes",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)
    note = Note(
        note_id=uuid4(),
        project_id=project.project_id,
        raw_content="capture.md",
        transcribed_text="signal stable",
        targets=[EntityRef(entity_type=EntityType.PROJECT, entity_id=project.project_id)],
        metadata={"ignored": "schema-gap"},
        status=NoteStatus.COMMITTED,
        created_at=_ts(3),
        updated_at=_ts(3),
    )
    repo.notes.save(note)
    repo.commit()
    loaded_note = repo.notes.get(note.note_id)
    assert loaded_note is not None
    assert loaded_note.targets == note.targets
    assert loaded_note.metadata == {"ignored": "schema-gap"}
    assert loaded_note.raw_asset is None


def test_dataset_repository_preserves_commit_manifest(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Datasets",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    question = Question(
        question_id=uuid4(),
        project_id=project.project_id,
        text="Does the manifest survive persistence?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[],
        created_at=_ts(1),
        updated_at=_ts(1),
    )
    dataset = Dataset(
        dataset_id=uuid4(),
        project_id=project.project_id,
        commit_hash="commit-1",
        primary_question_id=question.question_id,
        question_links=[
            QuestionLink(
                question_id=question.question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
            files=[],
            metadata={"run": "7"},
            nwb_metadata={"Session Description": "baseline"},
            bids_metadata={"Name": "Example"},
            note_ids=[uuid4()],
            question_links=[
                QuestionLink(
                    question_id=question.question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
            source_session_id=uuid4(),
        ),
        status=DatasetStatus.COMMITTED,
        created_at=_ts(2),
        updated_at=_ts(2),
    )

    repo.projects.save(project)
    repo.questions.save(question)
    repo.datasets.save(dataset)
    repo.commit()

    loaded_dataset = repo.datasets.get(dataset.dataset_id)
    assert loaded_dataset is not None
    assert loaded_dataset.commit_manifest.metadata == {"run": "7"}
    assert loaded_dataset.commit_manifest.nwb_metadata == {"Session Description": "baseline"}
    assert loaded_dataset.commit_manifest.bids_metadata == {"Name": "Example"}
    assert loaded_dataset.commit_manifest.note_ids == dataset.commit_manifest.note_ids
    assert (
        loaded_dataset.commit_manifest.source_session_id
        == dataset.commit_manifest.source_session_id
    )


def test_acquisition_output_repository_crud(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Sessions",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    session = Session(
        session_id=uuid4(),
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        created_at=_ts(1),
        updated_at=_ts(1),
    )
    output = AcquisitionOutput(
        output_id=uuid4(),
        session_id=session.session_id,
        file_path="rig/output.bin",
        checksum="abc123",
        size_bytes=42,
        created_at=_ts(2),
        updated_at=_ts(2),
    )

    repo.projects.save(project)
    repo.sessions.save(session)
    repo.acquisition_outputs.save(output)
    repo.commit()

    assert repo.acquisition_outputs.get(output.output_id) == output
    assert repo.acquisition_outputs.list() == [output]

    deleted = repo.acquisition_outputs.delete(output.output_id)
    repo.commit()
    assert deleted == output
    assert repo.acquisition_outputs.get(output.output_id) is None


def test_query_questions_applies_filters_and_pagination(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Filtered questions",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)

    active_ids = []
    for index in range(3):
        question = Question(
            question_id=uuid4(),
            project_id=project.project_id,
            text=f"Question {index}",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE if index < 2 else QuestionStatus.STAGED,
            parent_question_ids=[],
            created_at=_ts(index + 1),
            updated_at=_ts(index + 1),
        )
        repo.questions.save(question)
        if question.status == QuestionStatus.ACTIVE:
            active_ids.append(question.question_id)
    repo.commit()

    page, total = repo.query_questions(
        project_id=project.project_id,
        status=QuestionStatus.ACTIVE.value,
        limit=1,
        offset=1,
    )

    assert total == 2
    assert len(page) == 1
    assert page[0].question_id == active_ids[1]


def test_query_questions_applies_substring_search_in_database(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Question search",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)
    repo.questions.save(
        Question(
            question_id=uuid4(),
            project_id=project.project_id,
            text="What is the baseline distribution?",
            hypothesis="Baseline differs by condition",
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            parent_question_ids=[],
            created_at=_ts(1),
            updated_at=_ts(1),
        )
    )
    repo.questions.save(
        Question(
            question_id=uuid4(),
            project_id=project.project_id,
            text="Signal drift check",
            hypothesis=None,
            question_type=QuestionType.DESCRIPTIVE,
            status=QuestionStatus.ACTIVE,
            parent_question_ids=[],
            created_at=_ts(2),
            updated_at=_ts(2),
        )
    )
    repo.commit()

    page, total = repo.query_questions(project_id=project.project_id, search="baseline")

    assert total == 1
    assert [question.text for question in page] == ["What is the baseline distribution?"]


def test_note_repository_list_batches_child_queries(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Batch notes",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)
    for index in range(2):
        repo.notes.save(
            Note(
                note_id=uuid4(),
                project_id=project.project_id,
                raw_content=f"note {index}",
                targets=[
                    EntityRef(
                        entity_type=EntityType.PROJECT,
                        entity_id=project.project_id,
                    )
                ],
                status=NoteStatus.STAGED,
                created_at=_ts(index + 1),
                updated_at=_ts(index + 1),
            )
        )
    repo.commit()

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

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        notes = repo.notes.list()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert len(notes) == 2
    assert select_count == 2


def test_query_notes_filters_by_target(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Note targets",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    dataset_target = uuid4()
    other_target = uuid4()
    repo.projects.save(project)
    repo.notes.save(
        Note(
            note_id=uuid4(),
            project_id=project.project_id,
            raw_content="dataset note",
            targets=[EntityRef(entity_type=EntityType.DATASET, entity_id=dataset_target)],
            status=NoteStatus.STAGED,
            created_at=_ts(1),
            updated_at=_ts(1),
        )
    )
    repo.notes.save(
        Note(
            note_id=uuid4(),
            project_id=project.project_id,
            raw_content="other note",
            targets=[EntityRef(entity_type=EntityType.DATASET, entity_id=other_target)],
            status=NoteStatus.STAGED,
            created_at=_ts(2),
            updated_at=_ts(2),
        )
    )
    repo.commit()

    notes, total = repo.query_notes(
        project_id=project.project_id,
        target_entity_type=EntityType.DATASET.value,
        target_entity_id=dataset_target,
        limit=None,
        offset=0,
    )

    assert total == 1
    assert [note.raw_content for note in notes] == ["dataset note"]


def test_query_notes_applies_substring_search_in_database(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Note search",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    repo.projects.save(project)
    repo.notes.save(
        Note(
            note_id=uuid4(),
            project_id=project.project_id,
            raw_content="raw baseline capture",
            transcribed_text=None,
            status=NoteStatus.STAGED,
            created_at=_ts(1),
            updated_at=_ts(1),
        )
    )
    repo.notes.save(
        Note(
            note_id=uuid4(),
            project_id=project.project_id,
            raw_content="raw capture",
            transcribed_text="Follow-up baseline summary",
            status=NoteStatus.STAGED,
            created_at=_ts(2),
            updated_at=_ts(2),
        )
    )
    repo.commit()

    notes, total = repo.query_notes(project_id=project.project_id, search="baseline")

    assert total == 2
    assert [note.raw_content for note in notes] == ["raw baseline capture", "raw capture"]


def test_query_sessions_and_acquisition_outputs_apply_filters_and_pagination(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Session filters",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    question = Question(
        question_id=uuid4(),
        project_id=project.project_id,
        text="Session anchor",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[],
        created_at=_ts(1),
        updated_at=_ts(1),
    )
    active_session = Session(
        session_id=uuid4(),
        project_id=project.project_id,
        session_type=SessionType.SCIENTIFIC,
        status=SessionStatus.ACTIVE,
        primary_question_id=question.question_id,
        started_at=_ts(2),
        created_at=_ts(2),
        updated_at=_ts(2),
    )
    closed_session = Session(
        session_id=uuid4(),
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        status=SessionStatus.CLOSED,
        started_at=_ts(3),
        created_at=_ts(3),
        updated_at=_ts(3),
    )

    repo.projects.save(project)
    repo.questions.save(question)
    repo.sessions.save(active_session)
    repo.sessions.save(closed_session)
    repo.acquisition_outputs.save(
        AcquisitionOutput(
            output_id=uuid4(),
            session_id=active_session.session_id,
            file_path="capture/alpha.bin",
            checksum="checksum-1",
            size_bytes=10,
            created_at=_ts(4),
            updated_at=_ts(4),
        )
    )
    repo.acquisition_outputs.save(
        AcquisitionOutput(
            output_id=uuid4(),
            session_id=active_session.session_id,
            file_path="capture/beta.bin",
            checksum="checksum-2",
            size_bytes=12,
            created_at=_ts(5),
            updated_at=_ts(5),
        )
    )
    repo.acquisition_outputs.save(
        AcquisitionOutput(
            output_id=uuid4(),
            session_id=closed_session.session_id,
            file_path="capture/other.bin",
            checksum="checksum-3",
            size_bytes=14,
            created_at=_ts(6),
            updated_at=_ts(6),
        )
    )
    repo.commit()

    sessions, total_sessions = repo.query_sessions(
        project_id=project.project_id,
        status=SessionStatus.ACTIVE.value,
        limit=1,
        offset=0,
    )
    outputs, total_outputs = repo.query_acquisition_outputs(
        session_id=active_session.session_id,
        limit=1,
        offset=1,
    )

    assert total_sessions == 1
    assert [item.session_id for item in sessions] == [active_session.session_id]
    assert total_outputs == 2
    assert [item.file_path for item in outputs] == ["capture/beta.bin"]


def test_query_datasets_dataset_files_and_note_targets_use_focused_repository_paths(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Dataset focus",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    question = Question(
        question_id=uuid4(),
        project_id=project.project_id,
        text="Dataset anchor",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[],
        created_at=_ts(1),
        updated_at=_ts(1),
    )
    staged_dataset = Dataset(
        dataset_id=uuid4(),
        project_id=project.project_id,
        commit_hash="dataset-commit-1",
        primary_question_id=question.question_id,
        question_links=[
            QuestionLink(
                question_id=question.question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
            files=[],
            question_links=[
                QuestionLink(
                    question_id=question.question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
        ),
        status=DatasetStatus.STAGED,
        created_at=_ts(2),
        updated_at=_ts(2),
    )
    committed_dataset = Dataset(
        dataset_id=uuid4(),
        project_id=project.project_id,
        commit_hash="dataset-commit-2",
        primary_question_id=question.question_id,
        question_links=[
            QuestionLink(
                question_id=question.question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
            files=[],
            question_links=[
                QuestionLink(
                    question_id=question.question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
        ),
        status=DatasetStatus.COMMITTED,
        created_at=_ts(3),
        updated_at=_ts(3),
    )

    repo.projects.save(project)
    repo.questions.save(question)
    repo.datasets.save(staged_dataset)
    repo.datasets.save(committed_dataset)
    repo.notes.save(
        Note(
            note_id=uuid4(),
            project_id=project.project_id,
            raw_content="dataset note",
            targets=[
                EntityRef(
                    entity_type=EntityType.DATASET,
                    entity_id=staged_dataset.dataset_id,
                )
            ],
            status=NoteStatus.STAGED,
            created_at=_ts(4),
            updated_at=_ts(4),
        )
    )
    db_session.add(
        DatasetFileModel(
            file_id=str(uuid4()),
            dataset_id=str(staged_dataset.dataset_id),
            storage_id="storage-1",
            path="capture/alpha.bin",
            filename="alpha.bin",
            content_type="application/octet-stream",
            size_bytes=10,
            checksum="checksum-1",
            created_at=_ts(5),
        )
    )
    db_session.add(
        DatasetFileModel(
            file_id=str(uuid4()),
            dataset_id=str(staged_dataset.dataset_id),
            storage_id="storage-2",
            path="capture/beta.bin",
            filename="beta.bin",
            content_type="application/octet-stream",
            size_bytes=12,
            checksum="checksum-2",
            created_at=_ts(6),
        )
    )
    db_session.add(
        DatasetFileModel(
            file_id=str(uuid4()),
            dataset_id=str(committed_dataset.dataset_id),
            storage_id="storage-3",
            path="capture/other.bin",
            filename="other.bin",
            content_type="application/octet-stream",
            size_bytes=14,
            checksum="checksum-3",
            created_at=_ts(7),
        )
    )
    repo.commit()

    datasets, total_datasets = repo.query_datasets(
        project_id=project.project_id,
        status=DatasetStatus.STAGED.value,
        limit=1,
        offset=0,
    )
    files, total_files = repo.query_dataset_files(
        dataset_id=staged_dataset.dataset_id,
        limit=1,
        offset=1,
    )
    note_target_ids = repo.list_dataset_note_target_ids(staged_dataset.dataset_id)

    assert total_datasets == 1
    assert [item.dataset_id for item in datasets] == [staged_dataset.dataset_id]
    assert total_files == 2
    assert [item.path for item in files] == ["capture/beta.bin"]
    assert [item.path for item in repo.list_dataset_files(staged_dataset.dataset_id)] == [
        "capture/alpha.bin",
        "capture/beta.bin",
    ]
    assert note_target_ids == [repo.notes.list()[0].note_id]


def test_query_analyses_claims_and_visualizations_apply_focused_filters(db_session):
    repo = SQLAlchemyLabTrackerRepository(db_session)
    project = Project(
        project_id=uuid4(),
        name="Analysis focus",
        status=ProjectStatus.ACTIVE,
        created_at=_ts(),
        updated_at=_ts(),
    )
    question = Question(
        question_id=uuid4(),
        project_id=project.project_id,
        text="Analysis anchor",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        parent_question_ids=[],
        created_at=_ts(1),
        updated_at=_ts(1),
    )
    dataset_one = Dataset(
        dataset_id=uuid4(),
        project_id=project.project_id,
        commit_hash="dataset-commit-1",
        primary_question_id=question.question_id,
        question_links=[
            QuestionLink(
                question_id=question.question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
            files=[],
            question_links=[
                QuestionLink(
                    question_id=question.question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
        ),
        status=DatasetStatus.COMMITTED,
        created_at=_ts(2),
        updated_at=_ts(2),
    )
    dataset_two = Dataset(
        dataset_id=uuid4(),
        project_id=project.project_id,
        commit_hash="dataset-commit-2",
        primary_question_id=question.question_id,
        question_links=[
            QuestionLink(
                question_id=question.question_id,
                role=QuestionLinkRole.PRIMARY,
                outcome_status=OutcomeStatus.SUPPORTS,
            )
        ],
        commit_manifest=DatasetCommitManifest(
            files=[],
            question_links=[
                QuestionLink(
                    question_id=question.question_id,
                    role=QuestionLinkRole.PRIMARY,
                    outcome_status=OutcomeStatus.SUPPORTS,
                )
            ],
        ),
        status=DatasetStatus.COMMITTED,
        created_at=_ts(3),
        updated_at=_ts(3),
    )
    joined_analysis = Analysis(
        analysis_id=uuid4(),
        project_id=project.project_id,
        dataset_ids=[dataset_one.dataset_id, dataset_two.dataset_id],
        method_hash="method-1",
        code_version="code-1",
        status=AnalysisStatus.COMMITTED,
        executed_at=_ts(4),
        created_at=_ts(4),
        updated_at=_ts(4),
    )
    other_analysis = Analysis(
        analysis_id=uuid4(),
        project_id=project.project_id,
        dataset_ids=[dataset_two.dataset_id],
        method_hash="method-2",
        code_version="code-2",
        status=AnalysisStatus.STAGED,
        executed_at=_ts(5),
        created_at=_ts(5),
        updated_at=_ts(5),
    )
    supported_claim = Claim(
        claim_id=uuid4(),
        project_id=project.project_id,
        statement="Supported claim",
        confidence=0.9,
        status=ClaimStatus.PROPOSED,
        supported_by_dataset_ids=[dataset_one.dataset_id],
        supported_by_analysis_ids=[joined_analysis.analysis_id],
        created_at=_ts(6),
        updated_at=_ts(6),
    )
    other_claim = Claim(
        claim_id=uuid4(),
        project_id=project.project_id,
        statement="Other claim",
        confidence=0.5,
        status=ClaimStatus.PROPOSED,
        supported_by_dataset_ids=[dataset_two.dataset_id],
        supported_by_analysis_ids=[other_analysis.analysis_id],
        created_at=_ts(7),
        updated_at=_ts(7),
    )
    linked_visualization = Visualization(
        viz_id=uuid4(),
        analysis_id=joined_analysis.analysis_id,
        viz_type="heatmap",
        file_path="viz/heatmap.png",
        related_claim_ids=[supported_claim.claim_id],
        created_at=_ts(8),
        updated_at=_ts(8),
    )
    other_visualization = Visualization(
        viz_id=uuid4(),
        analysis_id=other_analysis.analysis_id,
        viz_type="scatter",
        file_path="viz/scatter.png",
        related_claim_ids=[other_claim.claim_id],
        created_at=_ts(9),
        updated_at=_ts(9),
    )

    repo.projects.save(project)
    repo.questions.save(question)
    repo.datasets.save(dataset_one)
    repo.datasets.save(dataset_two)
    repo.analyses.save(joined_analysis)
    repo.analyses.save(other_analysis)
    repo.claims.save(supported_claim)
    repo.claims.save(other_claim)
    repo.visualizations.save(linked_visualization)
    repo.visualizations.save(other_visualization)
    repo.commit()

    analyses, total_analyses = repo.query_analyses(
        question_id=question.question_id,
        status=AnalysisStatus.COMMITTED.value,
        limit=10,
        offset=0,
    )
    claims, total_claims = repo.query_claims(
        project_id=project.project_id,
        dataset_id=dataset_one.dataset_id,
        analysis_id=joined_analysis.analysis_id,
        limit=10,
        offset=0,
    )
    visualizations, total_visualizations = repo.query_visualizations(
        project_id=project.project_id,
        analysis_id=joined_analysis.analysis_id,
        claim_id=supported_claim.claim_id,
        limit=10,
        offset=0,
    )

    assert total_analyses == 1
    assert [item.analysis_id for item in analyses] == [joined_analysis.analysis_id]
    assert total_claims == 1
    assert [item.claim_id for item in claims] == [supported_claim.claim_id]
    assert total_visualizations == 1
    assert [item.viz_id for item in visualizations] == [linked_visualization.viz_id]
