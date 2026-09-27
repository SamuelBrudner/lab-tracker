"""Migration 0065 indexes notes.evidence_content_hash and adds the settings policy columns.

The hash column is backfilled from the metadata JSON (never modified), over-long
values stop the upgrade before anything changes, and graph_draft_batch_settings
rows gain ``external_context_policy`` through the server default.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

_PREVIOUS_REVISION = "0064_orm_schema_parity"
_REVISION = "0065_note_hash_and_external_context_policy"
_HASH_COLUMN = "evidence_content_hash"
_HASH_INDEX = "ix_notes_project_evidence_content_hash"
_HASH_MAX_LENGTH = 255
_SETTINGS_COLUMNS = {
    "external_context_policy",
    "external_provider_acknowledged_at",
    "external_provider_acknowledged_by",
}
_PROJECT_ID = "00000000-0000-4000-8000-0000000000cc"
_SETTINGS_ID = "00000000-0000-4000-8000-0000000000dd"
# Ascending so ``ORDER BY note_id`` returns them in insertion order.
_NOTE_IDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
)
_SEEDED_METADATA = (
    '{"evidence_content_hash": "h1"}',
    '{"other": "x"}',
    '{"evidence_content_hash": ""}',
)
_EXPECTED_BACKFILL = ["h1", None, None]


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _columns(database_url: str, table_name: str) -> set[str]:
    engine = create_engine(database_url, future=True)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table_name)}
    finally:
        engine.dispose()


def _index_names(database_url: str, table_name: str) -> set[str]:
    engine = create_engine(database_url, future=True)
    try:
        return {index["name"] for index in inspect(engine).get_indexes(table_name)}
    finally:
        engine.dispose()


def _metadata_parameter(database_url: str) -> str:
    # notes.metadata is a json column on PostgreSQL; make the cast explicit
    # rather than depend on the driver's parameter typing.
    if database_url.startswith("postgresql"):
        return "CAST(:metadata AS json)"
    return ":metadata"


def _seed_at_previous_revision(database_url: str, metadata_values: tuple[str, ...]) -> None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO projects (project_id, name, status, created_at, updated_at, "
                    "created_by) VALUES (:project_id, 'Existing', 'active', CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP, 'existing')"
                ),
                {"project_id": _PROJECT_ID},
            )
            connection.execute(
                text(
                    "INSERT INTO graph_draft_batch_settings (settings_id, project_id, enabled, "
                    "cadence_minutes, run_at_local_time, timezone_name, "
                    "email_notifications_enabled, created_at, updated_at) VALUES "
                    "(:settings_id, :project_id, TRUE, 1440, '06:00', 'UTC', FALSE, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"settings_id": _SETTINGS_ID, "project_id": _PROJECT_ID},
            )
            for note_id, metadata in zip(_NOTE_IDS, metadata_values, strict=True):
                connection.execute(
                    text(
                        "INSERT INTO notes (note_id, project_id, raw_content, metadata, status, "
                        "origin, created_at, updated_at) VALUES (:note_id, :project_id, 'raw', "
                        f"{_metadata_parameter(database_url)}, 'staged', 'user', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"note_id": note_id, "project_id": _PROJECT_ID, "metadata": metadata},
                )
    finally:
        engine.dispose()


def _hash_values_in_note_order(database_url: str) -> list[str | None]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            return list(
                connection.scalars(
                    text(f"SELECT {_HASH_COLUMN} FROM notes ORDER BY note_id")
                ).all()
            )
    finally:
        engine.dispose()


def _metadata_in_note_order(database_url: str) -> list[str]:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            return [
                str(value)
                for value in connection.scalars(
                    text("SELECT metadata FROM notes ORDER BY note_id")
                ).all()
            ]
    finally:
        engine.dispose()


def _current_revision(database_url: str) -> str | None:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            return connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()


def _assert_upgraded_schema_and_backfill(database_url: str) -> None:
    assert _hash_values_in_note_order(database_url) == _EXPECTED_BACKFILL
    assert _HASH_INDEX in _index_names(database_url, "notes")
    assert _columns(database_url, "graph_draft_batch_settings") >= _SETTINGS_COLUMNS
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            settings_row = connection.execute(
                text(
                    "SELECT external_context_policy, external_provider_acknowledged_at, "
                    "external_provider_acknowledged_by FROM graph_draft_batch_settings "
                    "WHERE settings_id = :settings_id"
                ),
                {"settings_id": _SETTINGS_ID},
            ).one()
    finally:
        engine.dispose()
    assert tuple(settings_row) == ("own_notes_only", None, None)


def test_note_hash_revision_extends_the_single_chain() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_revision(_REVISION).down_revision == _PREVIOUS_REVISION
    heads = script.get_heads()
    assert heads == [_REVISION]


def test_note_hash_migration_backfills_from_metadata_and_indexes(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'note-hash.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    assert _HASH_COLUMN not in _columns(database_url, "notes")
    assert not _SETTINGS_COLUMNS & _columns(database_url, "graph_draft_batch_settings")
    _seed_at_previous_revision(database_url, _SEEDED_METADATA)
    metadata_before = _metadata_in_note_order(database_url)

    command.upgrade(config, _REVISION)

    _assert_upgraded_schema_and_backfill(database_url)
    # The JSON is the source of truth and is never rewritten by the backfill.
    assert _metadata_in_note_order(database_url) == metadata_before

    command.downgrade(config, _PREVIOUS_REVISION)

    assert _HASH_COLUMN not in _columns(database_url, "notes")
    assert _HASH_INDEX not in _index_names(database_url, "notes")
    assert not _SETTINGS_COLUMNS & _columns(database_url, "graph_draft_batch_settings")
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            # The SQLite downgrade rebuilds both tables; their rows must survive.
            assert connection.scalar(text("SELECT count(*) FROM notes")) == len(_NOTE_IDS)
            assert (
                connection.scalar(text("SELECT count(*) FROM graph_draft_batch_settings")) == 1
            )
    finally:
        engine.dispose()


def test_note_hash_migration_refuses_over_long_values(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'note-hash-too-long.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    over_long = "a" * (_HASH_MAX_LENGTH + 45)
    _seed_at_previous_revision(
        database_url,
        (
            '{"evidence_content_hash": "fits"}',
            f'{{"evidence_content_hash": "{over_long}"}}',
            '{"other": "x"}',
        ),
    )

    with pytest.raises(RuntimeError, match=f"note_id={_NOTE_IDS[1]}") as failure:
        command.upgrade(config, _REVISION)

    assert "1 note(s)" in str(failure.value)
    assert "No rows were changed" in str(failure.value)
    assert _current_revision(database_url) == _PREVIOUS_REVISION
    assert _HASH_COLUMN not in _columns(database_url, "notes")
    assert _HASH_INDEX not in _index_names(database_url, "notes")
    assert not _SETTINGS_COLUMNS & _columns(database_url, "graph_draft_batch_settings")


@pytest.mark.postgres
def test_note_hash_migration_backfills_from_metadata_on_postgres(
    migrated_postgres_database_url: str,
) -> None:
    config = _alembic_config()
    command.downgrade(config, _PREVIOUS_REVISION)
    assert _HASH_COLUMN not in _columns(migrated_postgres_database_url, "notes")
    _seed_at_previous_revision(migrated_postgres_database_url, _SEEDED_METADATA)

    command.upgrade(config, _REVISION)

    _assert_upgraded_schema_and_backfill(migrated_postgres_database_url)
