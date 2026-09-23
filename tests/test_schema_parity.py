"""The ORM metadata must describe the schema Alembic builds.

Alembic owns the schema of every deployed database, while most unit tests
build theirs with ``Base.metadata.create_all``.  When the two drift, tests run
against constraints production never enforces (or miss indexes production
has), so these tests upgrade a fresh database to head and require Alembic's
autogenerate comparison against ``Base.metadata`` to find nothing.

Scope of the comparison: tables, columns, column types, nullability, indexes,
unique constraints and foreign keys.  Server defaults are deliberately not
compared (``compare_server_default=False``): the ORM declares Python-side
``default=`` values for every write, and migrations add ``server_default`` only
to fill existing rows when a NOT NULL column is added, so a server-default
difference does not change what the application writes or reads.  CHECK
constraints are outside autogenerate's comparison altogether; the migration
tests assert them where they matter.

No representational differences needed ignoring on SQLite or Postgres.  If a
future difference is one a dialect genuinely cannot express, add it to an
explicit, commented allow-list here rather than loosening the comparison.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Index, create_engine

from lab_tracker import db_models  # noqa: F401  (registers every mapped table)
from lab_tracker.db import Base


def _flatten(diffs: Sequence[object]) -> Iterator[object]:
    # compare_metadata groups column modifications into nested lists.
    for diff in diffs:
        if isinstance(diff, list):
            yield from diff
        else:
            yield diff


def _describe(diff: object) -> str:
    if isinstance(diff, tuple) and diff and diff[0] in {"add_index", "remove_index"}:
        index = diff[1]
        assert isinstance(index, Index)
        columns = ", ".join(str(column.name) for column in index.columns)
        return f"{diff[0]} {index.table.name}.{index.name} ({columns})"
    if isinstance(diff, tuple) and diff and str(diff[0]).startswith("modify_"):
        # (op, schema, table, column, info, existing value, metadata value)
        return f"{diff[0]} {diff[2]}.{diff[3]}: database={diff[5]!r} orm={diff[6]!r}"
    return repr(diff)


def _schema_drift(database_url: str) -> list[str]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": False},
            )
            diffs = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    return [_describe(diff) for diff in _flatten(diffs)]


def test_sqlite_head_schema_matches_orm_metadata(migrated_sqlite_database_url: str) -> None:
    assert _schema_drift(migrated_sqlite_database_url) == []


@pytest.mark.postgres
def test_postgres_head_schema_matches_orm_metadata(migrated_postgres_database_url: str) -> None:
    assert _schema_drift(migrated_postgres_database_url) == []
