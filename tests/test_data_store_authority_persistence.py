from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from lab_tracker.db import Base
from lab_tracker.db_models import DataStoreModel
from lab_tracker.models import (
    DataStore,
    Project,
    ProjectGroup,
    StoreCapability,
    StoreKind,
)
from lab_tracker.repository import (
    DataStoreForeignKeyRaceError,
    DataStoreInsertError,
    DataStoreNameRaceError,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.data_stores import (
    DATA_STORE_FOREIGN_KEY_CONSTRAINTS,
    DATA_STORE_GROUP_NAME_CONSTRAINT,
    DATA_STORE_PRIMARY_KEY_CONSTRAINT,
    DATA_STORE_PROJECT_NAME_CONSTRAINT,
    _is_data_store_foreign_key_race,
    _is_data_store_name_race,
    _is_data_store_primary_key_conflict,
)

_FINGERPRINT = f"sag-v1-sha256:{'a' * 64}"


def _timestamp() -> datetime:
    return datetime(2026, 7, 26, 17, 30, tzinfo=timezone.utc)


def _project(project_id: UUID | None = None) -> Project:
    return Project(
        project_id=project_id or uuid4(),
        name="Authority persistence",
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )


def _group(group_id: UUID | None = None) -> ProjectGroup:
    return ProjectGroup(
        group_id=group_id or uuid4(),
        name="Authority persistence group",
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )


def _store(
    *,
    project_id: UUID | None = None,
    group_id: UUID | None = None,
    **overrides: object,
) -> DataStore:
    fields: dict[str, object] = {
        "store_id": uuid4(),
        "project_id": project_id,
        "group_id": group_id,
        "name": "approved-http",
        "kind": StoreKind.HTTP,
        "capabilities": [StoreCapability.BYTES_BY_PATH],
        "root": "https://files.example/approved",
        "authority_grant_id": "project-http-v1",
        "authority_grant_fingerprint": _FINGERPRINT,
        "created_at": _timestamp(),
        "updated_at": _timestamp(),
    }
    fields.update(overrides)
    return DataStore(**fields)


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_data_store_authority_binding_is_nullable_but_strictly_paired() -> None:
    project_id = uuid4()
    legacy = _store(
        project_id=project_id,
        authority_grant_id=None,
        authority_grant_fingerprint=None,
    )
    assert legacy.authority_grant_id is None
    assert legacy.authority_grant_fingerprint is None

    with pytest.raises(PydanticValidationError, match="must both be set"):
        _store(
            project_id=project_id,
            authority_grant_fingerprint=None,
        )
    with pytest.raises(PydanticValidationError, match="must both be set"):
        _store(
            project_id=project_id,
            authority_grant_id=None,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("authority_grant_id", "-leading-punctuation"),
        ("authority_grant_id", "contains space"),
        ("authority_grant_id", "a" * 129),
        ("authority_grant_fingerprint", f"sag-v1-sha256:{'A' * 64}"),
        ("authority_grant_fingerprint", f"sag-v1-sha256:{'g' * 64}"),
        ("authority_grant_fingerprint", f"sha256:{'a' * 64}"),
    ),
)
def test_data_store_authority_binding_rejects_invalid_formats(
    field_name: str,
    value: str,
) -> None:
    with pytest.raises(PydanticValidationError):
        _store(project_id=uuid4(), **{field_name: value})


def test_bound_and_legacy_stores_round_trip_through_repository(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()
    bound = _store(project_id=project.project_id)
    legacy = _store(
        project_id=project.project_id,
        store_id=uuid4(),
        name="legacy-http",
        authority_grant_id=None,
        authority_grant_fingerprint=None,
    )

    repository.data_stores.insert(bound)
    repository.data_stores.insert(legacy)

    assert repository.data_stores.get(bound.store_id) == bound
    assert repository.data_stores.get(legacy.store_id) == legacy


def test_data_store_save_allows_noop_but_rejects_authority_mutation(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()
    store = _store(project_id=project.project_id)
    repository.data_stores.insert(store)
    db_session.commit()

    repository.data_stores.save(store.model_copy(deep=True))

    with pytest.raises(ValueError, match="registrations are immutable"):
        repository.data_stores.save(
            store.model_copy(update={"root": "https://files.example/expanded"})
        )
    assert repository.data_stores.get(store.store_id) == store


def test_data_store_save_rejects_an_absent_registration(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()

    with pytest.raises(ValueError, match="must already exist"):
        repository.data_stores.save(_store(project_id=project.project_id))


def test_sqlite_registration_fence_uses_begin_immediate(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    statements: list[str] = []
    engine = db_session.get_bind()

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        repository.data_stores.reserve_registration_write()
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert statements == ["BEGIN IMMEDIATE"]
    driver_connection = db_session.connection().connection.driver_connection
    assert driver_connection.in_transaction is True
    db_session.rollback()


def test_direct_repository_insert_reserves_sqlite_writer_before_savepoint(
    db_session,
) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()
    statements: list[str] = []
    engine = db_session.get_bind()

    def record_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        repository.data_stores.insert(_store(project_id=project.project_id))
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert statements[0] == "BEGIN IMMEDIATE"
    assert any(statement.startswith("SAVEPOINT ") for statement in statements)
    assert any(statement.startswith("INSERT INTO data_stores") for statement in statements)
    db_session.rollback()


@pytest.mark.parametrize("scope_kind", ("project", "group"))
def test_insert_translates_only_exact_scope_name_races(
    db_session,
    scope_kind: str,
) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    if scope_kind == "project":
        scope = _project()
        repository.projects.save(scope)
        scope_args = {"project_id": scope.project_id}
    else:
        scope = _group()
        repository.project_groups.save(scope)
        scope_args = {"group_id": scope.group_id}
    db_session.commit()
    repository.data_stores.insert(_store(**scope_args))
    db_session.commit()

    with pytest.raises(DataStoreNameRaceError) as error:
        repository.data_stores.insert(_store(**scope_args, store_id=uuid4()))
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_insert_rejects_same_id_as_append_only(db_session) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()
    store = _store(project_id=project.project_id)
    repository.data_stores.insert(store)

    with pytest.raises(ValueError, match="append-only") as error:
        repository.data_stores.insert(store)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_insert_translates_sqlite_foreign_key_race_without_raw_chain(
    db_session,
) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    secret = "missing-scope-secret-must-not-survive"
    store = _store(
        project_id=uuid4(),
        root=f"https://files.example.test/{secret}",
        authority_grant_id=secret,
    )

    with pytest.raises(DataStoreForeignKeyRaceError) as error:
        repository.data_stores.insert(store)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret not in str(error.value)


def test_insert_translates_unknown_sqlalchemy_error_without_raw_chain(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()
    secret = "unknown-insert-secret-must-not-survive"

    def fail_mapping(_entity: DataStore) -> DataStoreModel:
        raise OperationalError(
            "INSERT INTO data_stores (...) VALUES (...)",
            {"root": secret, "authority_grant_id": secret},
            RuntimeError(secret),
        )

    monkeypatch.setattr(
        "lab_tracker.sqlalchemy_repository_parts.data_stores.data_store_to_model",
        fail_mapping,
    )

    with pytest.raises(DataStoreInsertError) as error:
        repository.data_stores.insert(
            _store(
                project_id=project.project_id,
                root=f"https://files.example.test/{secret}",
                authority_grant_id=secret,
            )
        )

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret not in str(error.value)


def test_clear_default_translates_sqlalchemy_error_without_raw_chain(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SQLAlchemyLabTrackerRepository(db_session)
    project = _project()
    repository.projects.save(project)
    db_session.commit()
    secret = "default-update-secret-must-not-survive"

    def fail_default_query(*_args: object, **_kwargs: object) -> None:
        raise OperationalError(
            "SELECT * FROM data_stores WHERE root = :root",
            {"root": secret, "authority_grant_id": secret},
            RuntimeError(secret),
        )

    monkeypatch.setattr(db_session, "scalars", fail_default_query)

    with pytest.raises(DataStoreInsertError) as error:
        repository.data_stores.clear_default(project.project_id)

    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert secret not in str(error.value)


class _Diagnostic:
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name


class _PsycopgLikeError(Exception):
    def __init__(self, constraint_name: str) -> None:
        super().__init__("duplicate key value violates unique constraint")
        self.diag = _Diagnostic(constraint_name)


@pytest.mark.parametrize(
    "constraint_name",
    (DATA_STORE_PROJECT_NAME_CONSTRAINT, DATA_STORE_GROUP_NAME_CONSTRAINT),
)
def test_name_race_detection_uses_exact_postgres_constraint(
    constraint_name: str,
) -> None:
    expected = IntegrityError(None, None, _PsycopgLikeError(constraint_name))
    assert _is_data_store_name_race(expected) is True

    unrelated = IntegrityError(None, None, _PsycopgLikeError("uq_other_constraint"))
    assert _is_data_store_name_race(unrelated) is False


def test_primary_key_detection_uses_exact_postgres_constraint() -> None:
    expected = IntegrityError(
        None,
        None,
        _PsycopgLikeError(DATA_STORE_PRIMARY_KEY_CONSTRAINT),
    )
    assert _is_data_store_primary_key_conflict(expected) is True

    unrelated = IntegrityError(None, None, _PsycopgLikeError("other_table_pkey"))
    assert _is_data_store_primary_key_conflict(unrelated) is False


@pytest.mark.parametrize(
    "constraint_name",
    sorted(DATA_STORE_FOREIGN_KEY_CONSTRAINTS),
)
def test_foreign_key_detection_uses_exact_postgres_constraint(
    constraint_name: str,
) -> None:
    expected = IntegrityError(None, None, _PsycopgLikeError(constraint_name))
    assert _is_data_store_foreign_key_race(expected) is True

    unrelated = IntegrityError(None, None, _PsycopgLikeError("other_table_fkey"))
    assert _is_data_store_foreign_key_race(unrelated) is False


@pytest.mark.parametrize(
    ("grant_id", "fingerprint", "constraint_name"),
    (
        (None, _FINGERPRINT, "ck_data_stores_authority_binding_pair"),
        (
            "-invalid",
            _FINGERPRINT,
            "ck_data_stores_authority_grant_id_format",
        ),
        (
            "valid\ninvalid",
            _FINGERPRINT,
            "ck_data_stores_authority_grant_id_format",
        ),
        (
            "éinvalid",
            _FINGERPRINT,
            "ck_data_stores_authority_grant_id_format",
        ),
        (
            "valid-id",
            f"sag-v1-sha256:{'g' * 64}",
            "ck_data_stores_authority_fingerprint_format",
        ),
    ),
)
def test_sqlite_model_constraints_reject_invalid_authority_bindings(
    db_session,
    grant_id: str | None,
    fingerprint: str,
    constraint_name: str,
) -> None:
    project = _project()
    repository = SQLAlchemyLabTrackerRepository(db_session)
    repository.projects.save(project)
    with pytest.raises(IntegrityError) as error:
        db_session.execute(
            text(
                "INSERT INTO data_stores "
                "(store_id, project_id, name, kind, capabilities, root, "
                "authority_grant_id, authority_grant_fingerprint, is_default, "
                "created_at, updated_at) VALUES "
                "(:store_id, :project_id, 'invalid', 'http', '[]', "
                "'https://files.example', :grant_id, :fingerprint, 0, "
                ":created_at, :updated_at)"
            ),
            {
                "store_id": str(uuid4()),
                "project_id": str(project.project_id),
                "grant_id": grant_id,
                "fingerprint": fingerprint,
                "created_at": _timestamp(),
                "updated_at": _timestamp(),
            },
        )

    assert constraint_name in str(error.value)
