from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    QuestionStatus,
    QuestionType,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts import repository as repository_module


def test_question_dag_lock_key_is_stable_signed_and_uses_the_entire_uuid() -> None:
    first = UUID("12345678-0000-4000-8000-000000000001")
    second = UUID("12345678-0000-4000-8000-000000000002")

    first_key = repository_module._project_question_dag_lock_key(first)  # noqa: SLF001
    second_key = repository_module._project_question_dag_lock_key(second)  # noqa: SLF001

    assert first_key == 7429867810538065048
    assert second_key == -1559687969942006345
    assert first_key != second_key
    assert -(2**63) <= first_key < 2**63
    assert -(2**63) <= second_key < 2**63


def test_sqlite_question_dag_lock_is_a_no_op() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with Session(engine) as session:
            repository = SQLAlchemyLabTrackerRepository(session)

            repository.lock_project_question_dag(UUID(int=1))

            assert not session.in_transaction()
    finally:
        engine.dispose()


def test_all_existing_question_updates_share_the_project_dag_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=1), role=Role.ADMIN)
    project = api.create_project("Selective DAG locking", actor=actor)
    question = api.create_question(
        project.project_id,
        "Question",
        QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    parent = api.create_question(
        project.project_id,
        "Parent",
        QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    locked_projects: list[UUID] = []
    monkeypatch.setattr(
        api.questions.repository,
        "lock_project_question_dag",
        locked_projects.append,
    )

    api.update_question(question.question_id, text="Renamed question", actor=actor)
    assert locked_projects == [project.project_id]

    api.update_question(
        question.question_id,
        parent_question_ids=[parent.question_id],
        actor=actor,
    )
    assert locked_projects == [project.project_id, project.project_id]

    api.refactor_question(
        question.question_id,
        replacement_text="Replacement",
        replacement_question_type=QuestionType.DESCRIPTIVE,
        replacement_status=QuestionStatus.STAGED,
        reason="Refactor",
        actor=actor,
    )
    assert locked_projects == [project.project_id, project.project_id, project.project_id]


def test_graph_draft_prelocks_question_projects_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = repository_backed_api()
    actor = AuthContext(user_id=UUID(int=2), role=Role.ADMIN)
    first_project = api.create_project("First graph-lock project", actor=actor)
    second_project = api.create_project("Second graph-lock project", actor=actor)
    first_question = api.create_question(
        first_project.project_id,
        "First project question",
        QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    second_question = api.create_question(
        second_project.project_id,
        "Second project question",
        QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    change_set_id = uuid4()
    operations = [
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=1,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.QUESTION,
            target_entity_id=second_question.question_id,
            payload={"text": "Second update"},
        ),
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=2,
            op=GraphChangeOp.UPDATE,
            entity_type=EntityType.QUESTION,
            target_entity_id=first_question.question_id,
            payload={"text": "First update"},
        ),
    ]
    locked_projects: list[UUID] = []
    monkeypatch.setattr(
        api.graph_drafts.commit.repository,
        "lock_project_question_dag",
        locked_projects.append,
    )

    api.graph_drafts.commit._lock_question_update_projects(operations)  # noqa: SLF001

    assert locked_projects == sorted(
        [first_project.project_id, second_project.project_id],
        key=str,
    )
