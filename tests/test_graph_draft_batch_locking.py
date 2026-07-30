from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts import repository as repository_module


class _PostgresDialect:
    name = "postgresql"


class _PostgresBind:
    dialect = _PostgresDialect()


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, int]]] = []
        self.expire_count = 0

    def get_bind(self) -> _PostgresBind:
        return _PostgresBind()

    def execute(
        self,
        statement: TextClause,
        parameters: dict[str, int],
    ) -> None:
        self.calls.append((str(statement), parameters))

    def expire_all(self) -> None:
        self.expire_count += 1


def test_postgres_batch_settings_lock_is_shared_across_reviewer_scopes() -> None:
    session = _RecordingSession()
    repository = SQLAlchemyLabTrackerRepository(cast(Session, session))
    project_id = UUID("12345678-0000-4000-8000-000000000001")
    first_reviewer_id = UUID("12345678-0000-4000-8000-000000000002")
    second_reviewer_id = UUID("12345678-0000-4000-8000-000000000003")

    repository.lock_graph_draft_batch_settings(project_id)
    repository.lock_graph_draft_batch_settings(project_id)
    repository.lock_graph_draft_batch_reviewer(
        project_id,
        review_assignee_user_id=first_reviewer_id,
    )
    repository.lock_graph_draft_batch_reviewer(
        project_id,
        review_assignee_user_id=second_reviewer_id,
    )

    assert [statement for statement, _parameters in session.calls] == [
        "SELECT pg_advisory_xact_lock(:lock_key)",
    ] * 4
    settings_keys = [
        session.calls[0][1]["lock_key"],
        session.calls[1][1]["lock_key"],
    ]
    reviewer_keys = [
        session.calls[2][1]["lock_key"],
        session.calls[3][1]["lock_key"],
    ]
    assert (
        settings_keys
        == [
            repository_module._graph_draft_settings_lock_key(project_id),  # noqa: SLF001
        ]
        * 2
    )
    assert reviewer_keys == [
        repository_module._graph_draft_reviewer_lock_key(  # noqa: SLF001
            project_id,
            review_assignee_user_id=first_reviewer_id,
            review_assignee=None,
        ),
        repository_module._graph_draft_reviewer_lock_key(  # noqa: SLF001
            project_id,
            review_assignee_user_id=second_reviewer_id,
            review_assignee=None,
        ),
    ]
    assert reviewer_keys[0] != reviewer_keys[1]
    assert settings_keys[0] not in reviewer_keys
    assert session.expire_count == 4
