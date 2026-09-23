from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import engine_from_config, event, pool, text
from sqlalchemy.engine import Connection, Engine

from lab_tracker import db_models  # noqa: F401
from lab_tracker.config import get_settings
from lab_tracker.db import Base, configure_sqlite_engine

config = context.config

if config.config_file_name is not None:
    # The default disable_existing_loggers=True would silence every logger that
    # already exists, including this module's own logger on a second in-process
    # run.
    fileConfig(config.config_file_name, disable_existing_loggers=False)


target_metadata = Base.metadata

# Not an "alembic.*" logger: Alembic attaches a NullHandler to "alembic", so
# without alembic.ini (``lab-tracker serve``) its records never reach stderr.
# This one propagates to the root logger, whose last-resort handler prints
# WARNING records when nothing else is configured.
logger = logging.getLogger("lab_tracker.alembic.env")

_ADVISORY = "docs/advisories/2026-09-sqlite-migration-cascade.md"

# Transaction-scoped Postgres advisory lock that serializes concurrent
# ``alembic upgrade head`` runs (every container's entrypoint runs one) against
# the same database. The value is arbitrary but fixed ("LTMG").
_POSTGRES_MIGRATION_LOCK_KEY = 1280593223

# (child table, parent table, referencing columns, referencing values); see
# _foreign_key_violations for why rowid and fkid are not part of the identity.
_ForeignKeyViolation = tuple[str, str, tuple[str, ...], tuple[object, ...]]


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
            if connection.dialect.name == "postgresql":
                # Taken before Alembic reads alembic_version, so a waiting run
                # sees the winner's committed head; released at commit/rollback.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": _POSTGRES_MIGRATION_LOCK_KEY},
                )
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
    verified with ``PRAGMA foreign_key_check`` before the run commits: only
    violations the run introduced block it, while violations that were
    already in the database are logged as a warning.

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
            step = kwargs["step"]
            # A stamp only rewrites alembic_version; no revision code ran.
            if not step.is_stamp:
                applied_steps.append(str(step))

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

        preexisting: Counter[_ForeignKeyViolation] = Counter()
        with context.begin_transaction():
            _refuse_stale_batch_tables(connection)
            # The baseline is read inside the BEGIN IMMEDIATE transaction, so
            # no other writer can add or repair rows between it and the run.
            # Should the prediction ever miss a step, the empty baseline fails
            # closed: every violation then counts as introduced by this run.
            baseline: Counter[_ForeignKeyViolation] = Counter()
            if _revision_steps_pending(context.get_context()):
                baseline = _foreign_key_violations(connection)
            context.run_migrations()
            if applied_steps:
                preexisting = _refuse_new_foreign_key_violations(connection, baseline)

        if preexisting:
            _warn_preexisting_foreign_key_violations(preexisting)
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
        "stop Lab Tracker, restore the snapshot taken before that upgrade with "
        "`lab-tracker restore <backup> --force`, then rerun the upgrade. "
        "Otherwise, after confirming each original table (the name without "
        "the _alembic_tmp_ prefix) still exists with all its rows, DROP the "
        "leftover table(s) and rerun `alembic upgrade head`. No changes were "
        f"made by this run. See {_ADVISORY}."
    )


def _revision_steps_pending(migration_context: MigrationContext) -> bool:
    """Whether this run's work function may yield revision steps.

    Compares the requested destination with the database's current heads:
    Alembic has nothing to do when they are the same revisions, in either
    direction.  Only exact targets (``head``, ``heads``, ``base`` or a full
    revision id) can be proven to be no-ops.  Relative targets (``-1``,
    ``+2``, ``head-1``, ``branch@-1``), partial ids and anything that fails to
    resolve count as pending, which costs one extra baseline scan and nothing
    else.
    """

    try:
        destination = context.get_revision_argument()
    except KeyError:
        # current, check and autogenerate pass no destination; their work
        # functions yield no steps.
        return False
    except CommandError:
        return True
    if destination is None:
        requested: tuple[str, ...] = ()
    elif isinstance(destination, str):
        requested = (destination,)
    else:
        requested = tuple(destination)
    for revision in requested:
        try:
            script = context.script.get_revision(revision)
        except CommandError:
            return True
        if script is None or script.revision != revision:
            return True
    return frozenset(requested) != frozenset(migration_context.get_current_heads())


def _foreign_key_violations(connection: Connection) -> Counter[_ForeignKeyViolation]:
    """Count ``PRAGMA foreign_key_check`` rows by an identity rebuilds keep.

    The check reports ``(table, rowid, parent, fkid)``, but a batch rebuild
    preserves neither number.  ``INSERT INTO _alembic_tmp_x (columns) SELECT
    columns FROM x`` does not copy the implicit rowid of a table without an
    INTEGER PRIMARY KEY (every UUID-keyed table here), so rows are renumbered
    past any gaps.  ``fkid`` indexes ``PRAGMA foreign_key_list``, which is
    renumbered when a rebuild adds, drops or reorders a constraint.  A
    violation is identified instead by what a rebuild copies verbatim: child
    table, parent table, referencing column names and the values in them.
    Counting, rather than collecting a set, keeps orphans that share one
    dangling value distinct, so a run that adds another such orphan is still
    caught.  WITHOUT ROWID tables report no rowid and are compared by count.
    """

    violations: Counter[_ForeignKeyViolation] = Counter()
    columns_by_constraint: dict[tuple[str, int], tuple[str, ...]] = {}
    for table, rowid, parent, fkid in connection.exec_driver_sql("PRAGMA foreign_key_check"):
        constraint = (str(table), int(fkid))
        if constraint not in columns_by_constraint:
            columns_by_constraint[constraint] = _foreign_key_columns(connection, *constraint)
        columns = columns_by_constraint[constraint]
        values = _referencing_values(connection, str(table), rowid, columns)
        violations[(str(table), str(parent), columns, values)] += 1
    return violations


def _foreign_key_columns(connection: Connection, table: str, fkid: int) -> tuple[str, ...]:
    quoted_table = connection.dialect.identifier_preparer.quote_identifier(table)
    rows = connection.exec_driver_sql(f"PRAGMA foreign_key_list({quoted_table})").all()
    # Columns: id, seq, table, from, to, on_update, on_delete, match.
    return tuple(str(row[3]) for row in sorted(rows, key=lambda row: row[1]) if row[0] == fkid)


def _referencing_values(
    connection: Connection,
    table: str,
    rowid: int | None,
    columns: tuple[str, ...],
) -> tuple[object, ...]:
    if rowid is None or not columns:
        return ()
    quote = connection.dialect.identifier_preparer.quote_identifier
    selected = ", ".join(quote(column) for column in columns)
    row = connection.exec_driver_sql(
        f"SELECT {selected} FROM {quote(table)} WHERE rowid = ?",  # noqa: S608
        (rowid,),
    ).one()
    return tuple(row)


def _describe_foreign_key_violations(violations: Counter[_ForeignKeyViolation]) -> str:
    by_reference: Counter[tuple[str, str]] = Counter()
    for (table, parent, _columns, _values), count in violations.items():
        by_reference[(table, parent)] += count
    return "; ".join(
        f"{table} -> {parent}: {count} row(s)"
        for (table, parent), count in sorted(by_reference.items())
    )


def _refuse_new_foreign_key_violations(
    connection: Connection,
    baseline: Counter[_ForeignKeyViolation],
) -> Counter[_ForeignKeyViolation]:
    """Raise, rolling back the run, if it left violations not in ``baseline``.

    Returns the violations still present, all of which predate the run.
    """

    violations = _foreign_key_violations(connection)
    introduced = violations - baseline
    if not introduced:
        return violations
    preexisting = violations - introduced
    not_counted = (
        f" {preexisting.total()} violation(s) that predate this run were not counted."
        if preexisting
        else ""
    )
    raise RuntimeError(
        "Refusing to commit SQLite migrations: PRAGMA foreign_key_check "
        f"reported {introduced.total()} foreign-key violation(s) introduced by "
        f"this migration run ({_describe_foreign_key_violations(introduced)})."
        f"{not_counted} The run was rolled back and alembic_version is "
        "unchanged, so the database is still usable at its current revision. "
        "A migration lost parent rows, wrote references to rows that do not "
        "exist, or added a constraint that existing rows do not satisfy. Fix "
        "that migration, or the rows a new constraint rejects, before "
        f"rerunning the upgrade. See {_ADVISORY}."
    )


def _warn_preexisting_foreign_key_violations(
    violations: Counter[_ForeignKeyViolation],
) -> None:
    logger.warning(
        "SQLite database has %d foreign-key violation(s) that predate this "
        "migration run (%s). The run did not introduce them, so it was "
        "committed and they were left in place; writes that touch these rows "
        "can fail with 'FOREIGN KEY constraint failed'. Such rows date from "
        "before Lab Tracker enforced SQLite foreign keys, or from a damaging "
        "upgrade. List them with `PRAGMA foreign_key_check;` and see %s to "
        "recover or remove them.",
        violations.total(),
        _describe_foreign_key_violations(violations),
        _ADVISORY,
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
