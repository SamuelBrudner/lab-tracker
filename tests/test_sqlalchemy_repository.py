from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from lab_tracker.db import Base
from lab_tracker.models import (
    AcquisitionOutput,
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
    SessionType,
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
