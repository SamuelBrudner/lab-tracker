"""Migration 0063 adds users.session_epoch reversibly without touching users."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

_PREVIOUS_REVISION = "0062_member_onboarding_purpose"
_REVISION = "0063_user_session_epoch"


def _alembic_config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


def _user_columns(database_url: str) -> set[str]:
    engine = create_engine(database_url, future=True)
    try:
        return {column["name"] for column in inspect(engine).get_columns("users")}
    finally:
        engine.dispose()


def test_session_epoch_revision_extends_the_single_chain() -> None:
    script = ScriptDirectory.from_config(_alembic_config())

    assert script.get_revision(_REVISION).down_revision == _PREVIOUS_REVISION
    assert script.get_heads() == [_REVISION]


def test_session_epoch_migration_round_trips_existing_users(monkeypatch, tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'session-epoch.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    config = _alembic_config()
    command.upgrade(config, _PREVIOUS_REVISION)
    assert "session_epoch" not in _user_columns(database_url)
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (user_id, username, password_hash, role, created_at) "
                    "VALUES (:user_id, 'existing', 'hash', 'admin', CURRENT_TIMESTAMP)"
                ),
                {"user_id": "00000000-0000-4000-8000-0000000000aa"},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION)

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT session_epoch FROM users WHERE username = 'existing'")
            ) == 0
    finally:
        engine.dispose()

    command.downgrade(config, _PREVIOUS_REVISION)

    assert "session_epoch" not in _user_columns(database_url)
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM users")) == 1
    finally:
        engine.dispose()
