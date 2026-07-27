from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError, OperationalError

from lab_tracker.db_models import DataStoreModel

_REVISION = "0058_data_store_authority_bindings"
_PREVIOUS_REVISION = "0057_review_email_outbox"
_CHECK_CONSTRAINTS = {
    "ck_data_stores_scope_xor",
    "ck_data_stores_authority_binding_pair",
    "ck_data_stores_authority_grant_id_format",
    "ck_data_stores_authority_fingerprint_format",
}
_FINGERPRINT = f"sag-v1-sha256:{'b' * 64}"


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _set_database_url(monkeypatch, database_url: str) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)


def _seed_project(connection, *, project_id: str) -> None:  # noqa: ANN001
    connection.execute(
        text(
            "INSERT INTO projects "
            "(project_id, name, description, status, created_at, updated_at) "
            "VALUES (:project_id, 'Authority migration', '', 'active', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"project_id": project_id},
    )


def _seed_group(connection, *, group_id: str) -> None:  # noqa: ANN001
    connection.execute(
        text(
            "INSERT INTO project_groups "
            "(group_id, name, description, kind, group_read_all, created_at, "
            "updated_at) VALUES "
            "(:group_id, 'Authority group', '', 'lab', 0, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)"
        ),
        {"group_id": group_id},
    )


def _seed_store(
    connection,  # noqa: ANN001
    *,
    store_id: str,
    project_id: str | None,
    group_id: str | None,
    name: str = "legacy",
    root: str = "https://operator-secret.example/private",
) -> None:
    connection.execute(
        text(
            "INSERT INTO data_stores "
            "(store_id, project_id, group_id, name, kind, capabilities, root, "
            "credential_ref, is_default, created_at, updated_at) VALUES "
            "(:store_id, :project_id, :group_id, :name, 'http', '[]', :root, "
            "'secret-credential-ref', FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "store_id": store_id,
            "project_id": project_id,
            "group_id": group_id,
            "name": name,
            "root": root,
        },
    )


@pytest.mark.skipif(
    not hasattr(sqlite3.Connection, "autocommit"),
    reason="sqlite3 modern transaction control starts in Python 3.12",
)
def test_alembic_sqlite_engine_overrides_modern_transaction_mode(
    monkeypatch,
    tmp_path,
) -> None:
    """Alembic's private engine must install the production SQLite mode pin."""

    database_url = f"sqlite+pysqlite:///{tmp_path / 'modern-alembic.db'}"
    _set_database_url(monkeypatch, database_url)
    original_engine_from_config = sqlalchemy.engine_from_config

    def modern_engine_from_config(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        connect_args = dict(kwargs.get("connect_args", {}))
        connect_args["autocommit"] = False
        kwargs["connect_args"] = connect_args
        return original_engine_from_config(*args, **kwargs)

    monkeypatch.setattr(
        sqlalchemy,
        "engine_from_config",
        modern_engine_from_config,
    )

    command.upgrade(_alembic_config(), _REVISION)

    engine = create_engine(database_url, future=True)
    try:
        assert inspect(engine).has_table("data_stores")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == _REVISION
    finally:
        engine.dispose()


def test_0058_preserves_legacy_rows_without_inferred_binding_across_cycle(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'authority-binding.db'}"
    _set_database_url(monkeypatch, database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    engine = create_engine(database_url, future=True)
    project_id = str(uuid4())
    legacy_store_id = str(uuid4())
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)
            _seed_store(
                connection,
                store_id=legacy_store_id,
                project_id=project_id,
                group_id=None,
            )

        command.upgrade(config, _REVISION)
        inspector = inspect(engine)
        columns = {column["name"]: column for column in inspector.get_columns("data_stores")}
        assert columns["authority_grant_id"]["nullable"] is True
        assert columns["authority_grant_fingerprint"]["nullable"] is True
        assert _CHECK_CONSTRAINTS.issubset(
            {constraint["name"] for constraint in inspector.get_check_constraints("data_stores")}
        )
        with engine.connect() as connection:
            binding = connection.execute(
                text(
                    "SELECT authority_grant_id, authority_grant_fingerprint "
                    "FROM data_stores WHERE store_id = :store_id"
                ),
                {"store_id": legacy_store_id},
            ).one()
        assert tuple(binding) == (None, None)

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO data_stores "
                    "(store_id, project_id, name, kind, capabilities, root, "
                    "authority_grant_id, authority_grant_fingerprint, is_default, "
                    "created_at, updated_at) VALUES "
                    "(:store_id, :project_id, 'bound', 'http', '[]', "
                    "'https://files.example/approved', 'project-http-v1', "
                    ":fingerprint, FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "store_id": str(uuid4()),
                    "project_id": project_id,
                    "fingerprint": _FINGERPRINT,
                },
            )

        command.downgrade(config, _PREVIOUS_REVISION)
        downgraded_columns = {
            column["name"] for column in inspect(engine).get_columns("data_stores")
        }
        assert "authority_grant_id" not in downgraded_columns
        assert "authority_grant_fingerprint" not in downgraded_columns
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM data_stores WHERE store_id = :store_id"),
                    {"store_id": legacy_store_id},
                )
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("grant_id", "fingerprint", "constraint_name"),
    (
        (None, _FINGERPRINT, "ck_data_stores_authority_binding_pair"),
        ("-invalid", _FINGERPRINT, "ck_data_stores_authority_grant_id_format"),
        ("valid\ninvalid", _FINGERPRINT, "ck_data_stores_authority_grant_id_format"),
        ("éinvalid", _FINGERPRINT, "ck_data_stores_authority_grant_id_format"),
        (
            "valid-id",
            f"sag-v1-sha256:{'g' * 64}",
            "ck_data_stores_authority_fingerprint_format",
        ),
    ),
)
def test_0058_sqlite_checks_reject_invalid_bindings(
    monkeypatch,
    tmp_path,
    grant_id: str | None,
    fingerprint: str,
    constraint_name: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / f'{constraint_name}.db'}"
    _set_database_url(monkeypatch, database_url)
    command.upgrade(_alembic_config(), _REVISION)
    engine = create_engine(database_url, future=True)
    project_id = str(uuid4())
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)

        with pytest.raises(IntegrityError) as error, engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO data_stores "
                    "(store_id, project_id, name, kind, capabilities, root, "
                    "authority_grant_id, authority_grant_fingerprint, is_default, "
                    "created_at, updated_at) VALUES "
                    "(:store_id, :project_id, 'invalid', 'http', '[]', "
                    "'https://files.example', :grant_id, :fingerprint, FALSE, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "store_id": str(uuid4()),
                    "project_id": project_id,
                    "grant_id": grant_id,
                    "fingerprint": fingerprint,
                },
            )
        assert constraint_name in str(error.value)
    finally:
        engine.dispose()


def test_0058_sqlite_schema_uses_shallow_dialect_compiled_format_checks(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'shallow-format-checks.db'}"
    _set_database_url(monkeypatch, database_url)

    command.upgrade(_alembic_config(), _REVISION)

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            schema = connection.scalar(
                text("SELECT sql FROM sqlite_master WHERE name = 'data_stores'")
            )
        assert isinstance(schema, str)
        assert " REGEXP " in schema
        assert "REPLACE(" not in schema
    finally:
        engine.dispose()


def test_0058_format_checks_match_model_and_compile_without_ranges() -> None:
    migration = importlib.import_module(
        "lab_tracker.alembic.versions.0058_data_store_authority_bindings"
    )
    model_checks = {
        constraint.name: constraint.sqltext
        for constraint in DataStoreModel.__table__.constraints
        if isinstance(constraint, sqlalchemy.CheckConstraint)
    }
    expressions = (
        (
            "ck_data_stores_authority_grant_id_format",
            migration._GRANT_ID_FORMAT_EXPRESSION,
        ),
        (
            "ck_data_stores_authority_fingerprint_format",
            migration._FINGERPRINT_FORMAT_EXPRESSION,
        ),
    )

    for dialect in (sqlite.dialect(), postgresql.dialect()):
        for constraint_name, migration_expression in expressions:
            compile_options = {"literal_binds": True}
            migration_sql = str(
                migration_expression.compile(
                    dialect=dialect,
                    compile_kwargs=compile_options,
                )
            )
            model_sql = str(
                model_checks[constraint_name].compile(
                    dialect=dialect,
                    compile_kwargs=compile_options,
                )
            )
            assert migration_sql == model_sql
            assert "REPLACE(" not in migration_sql
            assert all(
                character_range not in migration_sql
                for character_range in ("A-Z", "a-z", "0-9")
            )


@pytest.mark.parametrize("invalid_scope", ("missing", "multiple"))
def test_0058_preflight_reports_only_safe_scope_diagnostics(
    monkeypatch,
    tmp_path,
    invalid_scope: str,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / f'invalid-{invalid_scope}.db'}"
    _set_database_url(monkeypatch, database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    engine = create_engine(database_url, future=True)
    project_id = str(uuid4())
    group_id = str(uuid4())
    store_id = str(uuid4())
    secret_root = "https://operator-secret.example/private"
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)
            _seed_group(connection, group_id=group_id)
            _seed_store(
                connection,
                store_id=store_id,
                project_id=project_id if invalid_scope == "multiple" else None,
                group_id=group_id if invalid_scope == "multiple" else None,
                root=secret_root,
            )

        with pytest.raises(RuntimeError) as error:
            command.upgrade(config, _REVISION)

        message = str(error.value)
        expected_violation = "multiple_scopes" if invalid_scope == "multiple" else "missing_scope"
        assert store_id in message
        assert expected_violation in message
        assert secret_root not in message
        assert "secret-credential-ref" not in message
        assert _REVISION in message
        assert {column["name"] for column in inspect(engine).get_columns("data_stores")}.isdisjoint(
            {"authority_grant_id", "authority_grant_fingerprint"}
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == _PREVIOUS_REVISION
            )
    finally:
        engine.dispose()


def test_0058_sqlite_writer_fence_reserves_database_before_preflight(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'writer-fence.db'}"
    _set_database_url(monkeypatch, database_url)
    command.upgrade(_alembic_config(), _PREVIOUS_REVISION)
    engine = create_engine(database_url, future=True)
    project_id = str(uuid4())
    migration = importlib.import_module(
        "lab_tracker.alembic.versions.0058_data_store_authority_bindings"
    )
    migration_connection = engine.connect()
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)
        monkeypatch.setattr(migration.op, "get_bind", lambda: migration_connection)
        migration._acquire_writer_fence()

        with engine.connect() as writer:
            writer.exec_driver_sql("PRAGMA busy_timeout=50")
            with pytest.raises(OperationalError, match="database is locked"):
                _seed_store(
                    writer,
                    store_id=str(uuid4()),
                    project_id=project_id,
                    group_id=None,
                )
    finally:
        migration_connection.rollback()
        migration_connection.close()
        engine.dispose()


@pytest.mark.postgres
def test_0058_postgres_preserves_legacy_rows_across_real_upgrade_downgrade(
    migrated_postgres_database_url: str,
) -> None:
    config = _alembic_config()
    command.downgrade(config, _PREVIOUS_REVISION)
    engine = create_engine(migrated_postgres_database_url, future=True)
    project_id = str(uuid4())
    legacy_store_id = str(uuid4())
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)
            _seed_store(
                connection,
                store_id=legacy_store_id,
                project_id=project_id,
                group_id=None,
            )

        command.upgrade(config, _REVISION)
        columns = {column["name"] for column in inspect(engine).get_columns("data_stores")}
        assert {
            "authority_grant_id",
            "authority_grant_fingerprint",
        } <= columns
        with engine.connect() as connection:
            binding = connection.execute(
                text(
                    "SELECT authority_grant_id, authority_grant_fingerprint "
                    "FROM data_stores WHERE store_id = :store_id"
                ),
                {"store_id": legacy_store_id},
            ).one()
        assert tuple(binding) == (None, None)

        command.downgrade(config, _PREVIOUS_REVISION)
        downgraded_columns = {
            column["name"] for column in inspect(engine).get_columns("data_stores")
        }
        assert "authority_grant_id" not in downgraded_columns
        assert "authority_grant_fingerprint" not in downgraded_columns
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_0058_postgres_checks_reject_non_ascii_and_newline_bindings(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_engine(migrated_postgres_database_url, future=True)
    project_id = str(uuid4())
    invalid_bindings = (
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
            f"sag-v1-sha256:{'é' * 64}",
            "ck_data_stores_authority_fingerprint_format",
        ),
    )
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)

        for grant_id, fingerprint, constraint_name in invalid_bindings:
            with pytest.raises(IntegrityError) as error, engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO data_stores "
                        "(store_id, project_id, name, kind, capabilities, root, "
                        "authority_grant_id, authority_grant_fingerprint, "
                        "is_default, created_at, updated_at) VALUES "
                        "(:store_id, :project_id, :name, 'http', '[]', "
                        "'https://files.example', :grant_id, :fingerprint, "
                        "FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "store_id": str(uuid4()),
                        "project_id": project_id,
                        "name": f"invalid-{uuid4()}",
                        "grant_id": grant_id,
                        "fingerprint": fingerprint,
                    },
                )
            assert constraint_name in str(error.value)
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_0058_postgres_writer_fence_blocks_concurrent_registration(
    migrated_postgres_database_url: str,
    monkeypatch,
) -> None:
    engine = create_engine(migrated_postgres_database_url, future=True)
    project_id = str(uuid4())
    store_id = str(uuid4())
    migration = importlib.import_module(
        "lab_tracker.alembic.versions.0058_data_store_authority_bindings"
    )
    migration_connection = engine.connect()
    transaction = migration_connection.begin()
    try:
        with engine.begin() as connection:
            _seed_project(connection, project_id=project_id)
        monkeypatch.setattr(migration.op, "get_bind", lambda: migration_connection)
        migration._acquire_writer_fence()

        with pytest.raises(OperationalError), engine.begin() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '200ms'"))
            _seed_store(
                writer,
                store_id=store_id,
                project_id=project_id,
                group_id=None,
            )
    finally:
        transaction.rollback()
        migration_connection.close()

    try:
        with engine.begin() as writer:
            _seed_store(
                writer,
                store_id=store_id,
                project_id=project_id,
                group_id=None,
            )
    finally:
        engine.dispose()
