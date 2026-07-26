from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import create_engine
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


def _recording_repository() -> tuple[
    SQLAlchemyLabTrackerRepository,
    _RecordingSession,
]:
    session = _RecordingSession()
    repository = SQLAlchemyLabTrackerRepository(cast(Session, session))
    return repository, session


def test_dataset_file_lock_keys_are_stable_scoped_and_use_the_entire_uuid() -> None:
    first = UUID("12345678-0000-4000-8000-000000000001")
    second = UUID("12345678-0000-4000-8000-000000000002")

    first_project_key = repository_module._dataset_file_project_lock_key(first)  # noqa: SLF001
    second_project_key = repository_module._dataset_file_project_lock_key(second)  # noqa: SLF001
    first_dataset_key = repository_module._dataset_file_dataset_lock_key(first)  # noqa: SLF001
    second_dataset_key = repository_module._dataset_file_dataset_lock_key(second)  # noqa: SLF001

    assert (first_project_key, second_project_key) == (
        -3441598901155261898,
        -8914766351020436418,
    )
    assert (first_dataset_key, second_dataset_key) == (
        2000910694323949292,
        3995165982202729605,
    )
    assert first_project_key != second_project_key
    assert first_dataset_key != second_dataset_key
    assert first_project_key != first_dataset_key
    assert all(
        -(2**63) <= key < 2**63
        for key in (
            first_project_key,
            second_project_key,
            first_dataset_key,
            second_dataset_key,
        )
    )


def test_dataset_file_mutation_locks_project_then_dataset_in_shared_mode() -> None:
    repository, session = _recording_repository()
    project_id = UUID(int=1)
    dataset_id = UUID(int=2)

    repository.lock_dataset_file_mutation(project_id, dataset_id)

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock_shared(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_project_lock_key(  # noqa: SLF001
                    project_id
                )
            },
        ),
        (
            "SELECT pg_advisory_xact_lock_shared(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_dataset_lock_key(  # noqa: SLF001
                    dataset_id
                )
            },
        ),
    ]
    assert session.expire_count == 1


def test_dataset_deletion_locks_project_shared_then_dataset_exclusive() -> None:
    repository, session = _recording_repository()
    project_id = UUID(int=1)
    dataset_id = UUID(int=2)

    repository.lock_dataset_deletion(project_id, dataset_id)

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock_shared(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_project_lock_key(  # noqa: SLF001
                    project_id
                )
            },
        ),
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_dataset_lock_key(  # noqa: SLF001
                    dataset_id
                )
            },
        ),
    ]
    assert session.expire_count == 1


def test_dataset_updates_lock_project_then_datasets_in_canonical_uuid_order() -> None:
    repository, session = _recording_repository()
    project_id = UUID(int=1)
    first_dataset_id = UUID(int=2)
    second_dataset_id = UUID(int=3)

    repository.lock_dataset_updates(
        project_id,
        [second_dataset_id, first_dataset_id, second_dataset_id],
    )

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock_shared(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_project_lock_key(  # noqa: SLF001
                    project_id
                )
            },
        ),
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_dataset_lock_key(  # noqa: SLF001
                    first_dataset_id
                )
            },
        ),
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_dataset_lock_key(  # noqa: SLF001
                    second_dataset_id
                )
            },
        ),
    ]
    assert session.expire_count == 1


def test_project_deletion_uses_the_exclusive_project_scope() -> None:
    repository, session = _recording_repository()
    project_id = UUID(int=1)

    repository.lock_project_deletion(project_id)

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_project_lock_key(  # noqa: SLF001
                    project_id
                )
            },
        )
    ]
    assert session.expire_count == 1


def test_project_deletion_guard_uses_the_shared_project_scope() -> None:
    repository, session = _recording_repository()
    project_id = UUID(int=1)

    repository.lock_project_deletion_guard(project_id)

    assert session.calls == [
        (
            "SELECT pg_advisory_xact_lock_shared(:lock_key)",
            {
                "lock_key": repository_module._dataset_file_project_lock_key(  # noqa: SLF001
                    project_id
                )
            },
        )
    ]
    assert session.expire_count == 1


def test_sqlite_dataset_file_locks_are_no_ops() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        with Session(engine) as session:
            repository = SQLAlchemyLabTrackerRepository(session)

            repository.lock_dataset_file_mutation(UUID(int=1), UUID(int=2))
            repository.lock_dataset_deletion(UUID(int=1), UUID(int=2))
            repository.lock_dataset_updates(UUID(int=1), [UUID(int=3), UUID(int=2)])
            repository.lock_project_deletion_guard(UUID(int=1))
            repository.lock_project_deletion(UUID(int=1))

            assert not session.in_transaction()
    finally:
        engine.dispose()
