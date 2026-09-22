"""The app refuses to start against an unmigrated or stale database (M17)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from api_helpers import register_test_resources
from fastapi.testclient import TestClient

from lab_tracker.app import create_app
from lab_tracker.app_parts.runtime import DatabaseSchemaError
from lab_tracker.auth import LOCAL_AUTH_USER_ID

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    return Config(str(REPO_ROOT / "alembic.ini"))


@pytest.fixture()
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = f"sqlite+pysqlite:///{tmp_path / 'startup.db'}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", url)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "files"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "notes"))
    yield url


def _create_app(**kwargs: object):
    app = create_app(**kwargs)  # type: ignore[arg-type]
    register_test_resources(app.state.db_engine, None, app.state.cleanup_git_health_workdir)
    return app


def _table_names(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.mark.parametrize("auth_enabled", ["false", "true"])
def test_unmigrated_database_fails_startup_with_actionable_message(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    auth_enabled: str,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", auth_enabled)
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "startup-schema-test-secret")

    with pytest.raises(DatabaseSchemaError) as excinfo:
        create_app()

    message = str(excinfo.value)
    assert "no Alembic revision" in message
    assert "alembic upgrade head" in message
    assert "lab-tracker serve" in message
    assert _table_names(database_url) == set()


def test_database_behind_head_fails_startup(database_url: str) -> None:
    script = ScriptDirectory.from_config(_alembic_config())
    (head,) = script.get_heads()
    previous = script.get_revision(head).down_revision
    assert isinstance(previous, str)
    command.upgrade(_alembic_config(), previous)

    with pytest.raises(DatabaseSchemaError) as excinfo:
        create_app()

    message = str(excinfo.value)
    assert previous in message
    assert head in message
    assert "alembic upgrade head" in message


def test_database_at_head_starts_and_bootstraps_local_user(database_url: str) -> None:
    command.upgrade(_alembic_config(), "head")

    app = _create_app()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

    engine = sa.create_engine(database_url)
    try:
        with engine.connect() as connection:
            user_ids = connection.execute(sa.text("SELECT user_id FROM users")).scalars().all()
    finally:
        engine.dispose()
    assert str(LOCAL_AUTH_USER_ID) in user_ids


def test_database_ahead_of_this_build_starts_with_warning(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Image-only rollback (deployments/dedicated-instance) runs the previous
    # image against a database already migrated by the newer one.
    command.upgrade(_alembic_config(), "head")
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("UPDATE alembic_version SET version_num = 'ffffnewer'"))
    finally:
        engine.dispose()

    warnings: list[str] = []

    class _WarningSpy:
        def warning(self, message: str, *args: object) -> None:
            warnings.append(message % args)

        def info(self, message: str, *args: object) -> None:
            pass

    # configure_logging() replaces the root handlers, so caplog cannot see it.
    monkeypatch.setattr("lab_tracker.app_parts.runtime._logger", _WarningSpy())
    _create_app()

    assert len(warnings) == 1
    assert "ffffnewer" in warnings[0]
    assert "newer than this build" in warnings[0]


def test_unreachable_database_fails_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str
) -> None:
    missing_dir = tmp_path / "missing-dir" / "db.sqlite"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", f"sqlite+pysqlite:///{missing_dir}")

    with pytest.raises(DatabaseSchemaError, match="Could not read the database migration"):
        create_app()


def test_explicit_bypass_builds_the_app_without_touching_the_database(
    database_url: str,
) -> None:
    app = _create_app(verify_schema=False)

    assert "/health" in app.openapi()["paths"]
    assert _table_names(database_url) == set()
