from __future__ import annotations

import sqlite3
from collections import Counter
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, event, pool
from sqlalchemy.engine import Connection, Engine

from lab_tracker import db_models  # noqa: F401
from lab_tracker.config import get_settings
from lab_tracker.db import Base, configure_sqlite_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


target_metadata = Base.metadata


def _get_url() -> str:
    settings = get_settings()
    return settings.database_url


def run_migrations_offline() -> None:
    url = _get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section)
    if configuration is None:
        raise RuntimeError("Missing Alembic configuration section.")
    configuration["sqlalchemy.url"] = _get_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        _run_sqlite_migrations(connectable)
        return

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


def _run_sqlite_migrations(engine: Engine) -> None:
    """Run SQLite migrations atomically with foreign-key actions suspended.

    SQLite's ``batch_alter_table`` rebuilds a table as ``CREATE _alembic_tmp_x
    -> INSERT ... SELECT -> DROP TABLE x -> RENAME``.  With foreign keys
    enforced, ``DROP TABLE x`` performs an implicit ``DELETE FROM x`` that
    fires ``ON DELETE CASCADE`` / ``SET NULL`` on every child table and
    silently destroys data.  ``PRAGMA foreign_keys`` cannot change inside a
    transaction, so it is switched off on the raw connection before the
    migration transaction begins, and referential integrity is instead
    verified with ``PRAGMA foreign_key_check`` before the run commits.

    The whole run executes inside one ``BEGIN IMMEDIATE`` transaction: SQLite
    DDL is transactional, so a failing revision rolls back its DDL, any
    ``_alembic_tmp_*`` table and the version stamp together.  Runtime
    connections from ``lab_tracker.db.get_engine`` keep enforcement on; only
    this private, ``NullPool`` migration engine is affected.
    """

    # Alembic constructs its own engine rather than using get_engine().
    # Install the production mode pin (legacy transaction control, WAL,
    # busy_timeout) first, then the migration-only overrides below.
    configure_sqlite_engine(engine)
    event.listen(engine, "connect", _configure_sqlite_migration_connection)
    event.listen(engine, "begin", _begin_sqlite_migration_transaction)

    with engine.connect() as connection:
        driver_connection = _sqlite_driver_connection(connection)
        _require_sqlite_foreign_keys(driver_connection, enabled=False)
        applied_steps: list[str] = []

        def _record_step(*_args: Any, **kwargs: Any) -> None:
            applied_steps.append(str(kwargs["step"]))

        if connection.in_transaction() or driver_connection.in_transaction:
            # Alembic would treat an already-open transaction as external and
            # never commit or roll back the run itself.
            raise RuntimeError(
                "Alembic's SQLite connection opened a transaction before the "
                "migration transaction; refusing to run migrations outside "
                "an atomic BEGIN IMMEDIATE transaction."
            )
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transactional_ddl=True,
            on_version_apply=_record_step,
        )

        with context.begin_transaction():
            _refuse_stale_batch_tables(connection)
            context.run_migrations()
            if applied_steps:
                _refuse_foreign_key_violations(connection)

        _set_sqlite_foreign_keys(driver_connection, enabled=True)


def _configure_sqlite_migration_connection(
    dbapi_connection: sqlite3.Connection,
    _connection_record: object,
) -> None:
    """Hand transaction control to SQLAlchemy and suspend FK actions.

    ``isolation_level = None`` stops pysqlite from issuing its own implicit
    ``BEGIN`` only before DML, which previously let each revision's leading DDL
    autocommit.  ``_begin_sqlite_migration_transaction`` issues the ``BEGIN``
    instead, so DDL, data backfills and the version stamp share one physical
    transaction.
    """

    if dbapi_connection.in_transaction:
        raise RuntimeError(
            "SQLite migration connection unexpectedly opened a transaction "
            "during connect; PRAGMA foreign_keys could not be changed."
        )
    dbapi_connection.isolation_level = None
    _set_sqlite_foreign_keys(dbapi_connection, enabled=False)


def _begin_sqlite_migration_transaction(connection: Connection) -> None:
    # Reserve the writer slot up front so revision preflight reads and the
    # schema changes they guard observe the same database state.
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _sqlite_driver_connection(connection: Connection) -> sqlite3.Connection:
    driver_connection = connection.connection.driver_connection
    if not isinstance(driver_connection, sqlite3.Connection):
        raise RuntimeError(
            "Expected a sqlite3 driver connection for SQLite migrations, got "
            f"{type(driver_connection).__name__}."
        )
    return driver_connection


def _set_sqlite_foreign_keys(driver_connection: sqlite3.Connection, *, enabled: bool) -> None:
    value = "ON" if enabled else "OFF"
    cursor = driver_connection.cursor()
    try:
        cursor.execute(f"PRAGMA foreign_keys={value}")
    finally:
        cursor.close()
    _require_sqlite_foreign_keys(driver_connection, enabled=enabled)


def _require_sqlite_foreign_keys(
    driver_connection: sqlite3.Connection,
    *,
    enabled: bool,
) -> None:
    """Fail loudly when ``PRAGMA foreign_keys`` did not take effect.

    SQLite silently ignores the pragma inside an open transaction.
    """

    cursor = driver_connection.cursor()
    try:
        row = cursor.execute("PRAGMA foreign_keys").fetchone()
    finally:
        cursor.close()
    actual = bool(row[0]) if row is not None else None
    if actual is not enabled:
        raise RuntimeError(
            "SQLite PRAGMA foreign_keys is "
            f"{'unknown' if actual is None else ('ON' if actual else 'OFF')}; "
            f"migrations require it {'ON' if enabled else 'OFF'} at this point "
            f"(in_transaction={driver_connection.in_transaction}; SQLite "
            "ignores this pragma inside an open transaction). Refusing to "
            "continue: with foreign keys enforced, batch table rebuilds cascade "
            "deletes into child tables."
        )


def _refuse_stale_batch_tables(connection: Connection) -> None:
    stale_tables = [
        str(row[0])
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE '\\_alembic\\_tmp\\_%' ESCAPE '\\' ORDER BY name"
        )
    ]
    if not stale_tables:
        return
    raise RuntimeError(
        "Refusing to run SQLite migrations: found leftover Alembic batch "
        f"table(s) {', '.join(stale_tables)} from an earlier migration that "
        "failed part-way. That run may also have left other schema changes "
        "behind, so the database may match neither revision. Safest recovery: "
        "restore the backup taken before that upgrade (lab-tracker backup "
        "restore), then rerun the upgrade. Otherwise, after confirming each "
        "original table (the name without the _alembic_tmp_ prefix) still "
        "exists with all its rows, DROP the leftover table(s) and rerun "
        "`alembic upgrade head`. No changes were made by this run."
    )


def _refuse_foreign_key_violations(connection: Connection) -> None:
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if not violations:
        return
    by_reference = Counter((str(row[0]), str(row[2])) for row in violations)
    details = "; ".join(
        f"{table} -> {parent}: {count} row(s)"
        for (table, parent), count in sorted(by_reference.items())
    )
    raise RuntimeError(
        "Refusing to commit SQLite migrations: PRAGMA foreign_key_check "
        f"reported {len(violations)} foreign-key violation(s) ({details}). "
        "The migration run was rolled back and alembic_version is unchanged. "
        "Run `PRAGMA foreign_key_check;` against the database to list the "
        "offending rowids, repair or remove those rows (restoring from a "
        "backup if they were lost), then rerun `alembic upgrade head`."
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
