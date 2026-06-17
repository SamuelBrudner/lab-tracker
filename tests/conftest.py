from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from alembic import command
from lab_tracker.app import create_app
from lab_tracker.auth import Role


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session", autouse=True)
def raw_sqlite_test_engines_use_production_pragmas() -> Iterator[None]:
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    event.listen(Engine, "connect", _set_sqlite_pragmas)
    try:
        yield
    finally:
        event.remove(Engine, "connect", _set_sqlite_pragmas)


@pytest.fixture()
def sqlite_database_url(tmp_path) -> str:
    db_path = tmp_path / "integration.db"
    return f"sqlite+pysqlite:///{db_path}"


@pytest.fixture()
def migrated_sqlite_database_url(
    monkeypatch,
    sqlite_database_url: str,
    tmp_path,
) -> str:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", sqlite_database_url)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")

    config = Config(str(_repo_root() / "alembic.ini"))
    command.upgrade(config, "head")
    return sqlite_database_url


@pytest.fixture()
def app(migrated_sqlite_database_url: str):
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def admin_auth_headers(client: TestClient) -> dict[str, str]:
    username = f"admin-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    token = login_response.json()["data"]["access_token"]
    return _auth_headers(token)
