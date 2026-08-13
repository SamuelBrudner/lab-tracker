from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

_PREVIOUS_REVISION = "0058_data_store_authority_bindings"
_EXPERIMENT_REVISION = "0059_experiments"
_COLLECTION_REVISION = "0060_acquisition_collections"
_CURRENT_HEAD_REVISION = "0061_semantic_index"
_ACQUISITION_TABLES = {
    "experiments",
    "experiment_sessions",
    "experiment_datasets",
    "acquisition_collections",
    "acquisition_collection_snapshots",
    "acquisition_collection_manifests",
    "acquisition_collection_captures",
}


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _set_database_url(monkeypatch, database_url: str) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)


def _assert_acquisition_schema(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert _ACQUISITION_TABLES.issubset(table_names)

        experiment_columns = {column["name"] for column in inspector.get_columns("experiments")}
        assert {
            "experiment_id",
            "project_id",
            "name",
            "description",
            "primary_question_id",
            "status",
            "closed_at",
            "archived_at",
            "created_by_user_id",
            "origin",
            "change_set_id",
            "created_at",
            "updated_at",
        }.issubset(experiment_columns)

        collection_columns = {
            column["name"] for column in inspector.get_columns("acquisition_collections")
        }
        assert collection_columns == {
            "collection_id",
            "session_id",
            "collection_key",
            "current_snapshot_id",
            "current_capture_id",
            "current_observed_at",
            "created_at",
            "updated_at",
        }

        snapshot_columns = {
            column["name"] for column in inspector.get_columns("acquisition_collection_snapshots")
        }
        capture_columns = {
            column["name"] for column in inspector.get_columns("acquisition_collection_captures")
        }
        observation_columns = {
            "client_capture_id",
            "capture_actor_user_id",
            "capture_principal_type",
            "capture_principal_instance_id",
            "capture_principal_label",
        }
        assert observation_columns.issubset(snapshot_columns)
        assert observation_columns.issubset(capture_columns)
        assert {"request_hash", "snapshot_id", "observed_at"}.issubset(capture_columns)

        collection_uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("acquisition_collections")
        }
        capture_uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("acquisition_collection_captures")
        }
        assert "uq_acquisition_collections_session_key" in collection_uniques
        assert "uq_collection_captures_collection_client_id" in capture_uniques
        collection_foreign_keys = {
            constraint["name"]: (
                constraint["constrained_columns"],
                constraint["referred_table"],
                constraint["referred_columns"],
            )
            for constraint in inspector.get_foreign_keys("acquisition_collections")
        }
        capture_foreign_keys = {
            constraint["name"]: (
                constraint["constrained_columns"],
                constraint["referred_table"],
                constraint["referred_columns"],
            )
            for constraint in inspector.get_foreign_keys("acquisition_collection_captures")
        }
        assert collection_foreign_keys["fk_acquisition_collections_current_capture"] == (
            [
                "collection_id",
                "current_snapshot_id",
                "current_capture_id",
                "current_observed_at",
            ],
            "acquisition_collection_captures",
            ["collection_id", "snapshot_id", "capture_id", "observed_at"],
        )
        assert capture_foreign_keys["fk_collection_captures_snapshot_owner"] == (
            ["collection_id", "snapshot_id"],
            "acquisition_collection_snapshots",
            ["collection_id", "snapshot_id"],
        )

        dataset_columns = {column["name"] for column in inspector.get_columns("datasets")}
        assert "manifest_collection_snapshots" not in dataset_columns
        assert "dataset_collection_snapshot_links" not in table_names
    finally:
        engine.dispose()


def test_acquisition_revisions_extend_current_main_in_one_chain() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_heads() == [_CURRENT_HEAD_REVISION]
    assert script.get_revision(_EXPERIMENT_REVISION).down_revision == _PREVIOUS_REVISION
    assert script.get_revision(_COLLECTION_REVISION).down_revision == _EXPERIMENT_REVISION
    assert script.get_revision(_CURRENT_HEAD_REVISION).down_revision == _COLLECTION_REVISION


def test_sqlite_acquisition_migrations_round_trip(
    monkeypatch,
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'acquisition-migrations.db'}"
    config = _alembic_config()
    _set_database_url(monkeypatch, database_url)

    command.upgrade(config, _PREVIOUS_REVISION)
    engine = create_engine(database_url, future=True)
    try:
        assert _ACQUISITION_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, _COLLECTION_REVISION)
    _assert_acquisition_schema(database_url)

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == _COLLECTION_REVISION
            )
    finally:
        engine.dispose()

    command.downgrade(config, _PREVIOUS_REVISION)
    engine = create_engine(database_url, future=True)
    try:
        assert _ACQUISITION_TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    _assert_acquisition_schema(database_url)


@pytest.mark.postgres
def test_postgres_acquisition_schema(
    migrated_postgres_database_url: str,
) -> None:
    _assert_acquisition_schema(migrated_postgres_database_url)
