from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lab_tracker.db import Base
from lab_tracker.models import (
    AcquisitionOutput,
    EntityRef,
    EntityTagSuggestion,
    EntityType,
    ExtractedEntity,
    Note,
    NoteStatus,
    Project,
    ProjectStatus,
    Question,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    Session,
    SessionType,
    TagSuggestionStatus,
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
        created_from=QuestionSource.MANUAL,
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
        created_from=QuestionSource.API,
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
        extracted_entities=[
            ExtractedEntity(label="hippocampus", confidence=0.91, provenance="ocr")
        ],
        tag_suggestions=[
            EntityTagSuggestion(
                suggestion_id=uuid4(),
                entity_label="hippocampus",
                vocabulary="UBERON",
                term_id="0002421",
                term_label="Hippocampus",
                confidence=0.92,
                provenance="nlp-v1",
                status=TagSuggestionStatus.STAGED,
            )
        ],
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
    assert loaded_note.extracted_entities == note.extracted_entities
    assert loaded_note.tag_suggestions == note.tag_suggestions
    assert loaded_note.targets == note.targets
    assert loaded_note.metadata == {}
    assert loaded_note.raw_asset is None


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
