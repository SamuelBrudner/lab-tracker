"""Migration 0063 adds users.session_epoch reversibly, keeping users and their dependents."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

_PREVIOUS_REVISION = "0062_member_onboarding_purpose"
_REVISION = "0063_user_session_epoch"
_USER_ID = "00000000-0000-4000-8000-0000000000aa"


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
    heads = script.get_heads()
    assert len(heads) == 1
    assert _REVISION in {
        revision.revision for revision in script.iterate_revisions(heads[0], "base")
    }


def test_session_epoch_migration_round_trips_users_and_dependents(monkeypatch, tmp_path) -> None:
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
                {"user_id": _USER_ID},
            )
            connection.execute(
                text(
                    "INSERT INTO personal_access_tokens (token_id, user_id, label, token_hash, "
                    "role, read_only, expires_at, created_at, scope) VALUES (:token_id, "
                    ":user_id, 'laptop', :token_hash, 'admin', 1, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP, 'all')"
                ),
                {
                    "token_id": "00000000-0000-4000-8000-0000000000bb",
                    "user_id": _USER_ID,
                    "token_hash": "a" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO projects (project_id, name, status, created_at, updated_at, "
                    "created_by, created_by_user_id) VALUES (:project_id, 'Existing', "
                    "'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'existing', :user_id)"
                ),
                {"project_id": "00000000-0000-4000-8000-0000000000cc", "user_id": _USER_ID},
            )
    finally:
        engine.dispose()

    command.upgrade(config, _REVISION)

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT session_epoch FROM users WHERE username = 'existing'")
                )
                == 0
            )
    finally:
        engine.dispose()

    command.downgrade(config, _PREVIOUS_REVISION)

    assert "session_epoch" not in _user_columns(database_url)
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM users")) == 1
            # The SQLite downgrade rebuilds users; with foreign keys enforced the
            # DROP TABLE would cascade into dependents and null their creators.
            assert connection.scalar(text("SELECT count(*) FROM personal_access_tokens")) == 1
            assert connection.scalar(text("SELECT created_by_user_id FROM projects")) == _USER_ID
    finally:
        engine.dispose()
