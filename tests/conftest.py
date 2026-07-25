from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url

from lab_tracker.app import create_app
from lab_tracker.auth import Role


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _drain_test_db_resources() -> Iterator[None]:
    """Close and dispose every test-owned engine/session after each test.

    repository_backed_api (and the two hand-rolled builders) enroll their
    in-memory SQLite engine/session in a registry; draining here on both success
    and failure keeps connections from leaking as GC-time ResourceWarnings.
    """

    from api_helpers import drain_test_resources

    try:
        yield
    finally:
        drain_test_resources()


@pytest.fixture(autouse=True)
def _isolate_lab_tracker_home(tmp_path_factory, monkeypatch):
    """Keep every test away from the developer's real machine-level state.

    init/update/hooks record into ~/.lab-tracker/applied-repos.json,
    from_env reads ~/.lab-tracker/config.json, and --install-skills writes
    into agent skill homes — none of which a test run may touch or observe.
    Tests that care about these paths override the env vars themselves.
    """

    home = tmp_path_factory.mktemp("lt-isolated-home")
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(home / "lab-tracker"))
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(home / "skills"))


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@dataclass(frozen=True)
class TestUser:
    headers: dict[str, str]
    user_id: str
    username: str


@dataclass(frozen=True)
class ScopedProjectMember:
    member_headers: dict[str, str]
    member_user_id: str
    member_username: str
    visible_project_id: str
    hidden_project_id: str


def _register_test_user(
    client: TestClient,
    *,
    role: Role = Role.VIEWER,
    username_prefix: str = "user",
) -> TestUser:
    username = f"{username_prefix}-{uuid4().hex[:8]}"
    password = "secret"
    user = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    token = login_response.json()["data"]["access_token"]
    return TestUser(
        headers=_auth_headers(token),
        user_id=str(user.user_id),
        username=username,
    )


def _create_test_project(
    client: TestClient,
    headers: dict[str, str],
    name: str,
) -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


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
def migrated_postgres_database_url(monkeypatch, tmp_path) -> Iterator[str]:
    base_database_url = os.getenv("LAB_TRACKER_POSTGRES_TEST_DATABASE_URL")
    if not base_database_url:
        pytest.skip("LAB_TRACKER_POSTGRES_TEST_DATABASE_URL is not set")

    normalized_base_url = _normalize_postgres_url(base_database_url)
    base_url = make_url(normalized_base_url)
    database_name = f"lab_tracker_test_{uuid4().hex}"
    admin_url = base_url.set(database=base_url.database or "postgres")
    target_url = base_url.set(database=database_name)
    admin_engine = create_engine(
        admin_url.render_as_string(hide_password=False),
        future=True,
        isolation_level="AUTOCOMMIT",
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    admin_engine.dispose()

    target_database_url = target_url.render_as_string(hide_password=False)
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", target_database_url)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")

    config = Config(str(_repo_root() / "alembic.ini"))
    command.upgrade(config, "head")
    try:
        yield target_database_url
    finally:
        cleanup_engine = create_engine(
            admin_url.render_as_string(hide_password=False),
            future=True,
            isolation_level="AUTOCOMMIT",
        )
        with cleanup_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'))
        cleanup_engine.dispose()


def _normalize_postgres_url(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("postgres://"):
        return f"postgresql+psycopg://{cleaned.removeprefix('postgres://')}"
    if cleaned.startswith("postgresql://"):
        return f"postgresql+psycopg://{cleaned.removeprefix('postgresql://')}"
    return cleaned


def _app_with_disposed_engine(application):
    """Yield a created app and dispose its DB engine on teardown.

    create_app builds an engine that is only disposed by the app's lifespan
    shutdown. Tests that use the `app` fixture directly (their own bare
    TestClient, or app.state access) never run that shutdown, so dispose the
    engine here to keep in-memory SQLite connections from leaking. Idempotent
    with the lifespan's own dispose when a TestClient context is used.
    """

    try:
        yield application
    finally:
        engine = getattr(application.state, "db_engine", None)
        if engine is not None:
            engine.dispose()


@pytest.fixture()
def app(migrated_sqlite_database_url: str):
    yield from _app_with_disposed_engine(create_app())


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def postgres_app(migrated_postgres_database_url: str):
    yield from _app_with_disposed_engine(create_app())


@pytest.fixture()
def postgres_client(postgres_app):
    with TestClient(postgres_app) as test_client:
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


@pytest.fixture()
def postgres_admin_auth_headers(postgres_client: TestClient) -> dict[str, str]:
    username = f"pg-admin-{uuid4().hex[:8]}"
    password = "secret"
    postgres_client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )
    login_response = postgres_client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    token = login_response.json()["data"]["access_token"]
    return _auth_headers(token)


@pytest.fixture()
def viewer_user(client: TestClient) -> TestUser:
    return _register_test_user(client, username_prefix="viewer")


@pytest.fixture()
def scoped_project_member(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> ScopedProjectMember:
    member = _register_test_user(client, username_prefix="scoped-member")
    visible_project_id = _create_test_project(
        client,
        admin_auth_headers,
        "Scoped visible project",
    )
    hidden_project_id = _create_test_project(
        client,
        admin_auth_headers,
        "Scoped hidden project",
    )
    membership_response = client.post(
        f"/projects/{visible_project_id}/members",
        json={"user_id": member.user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert membership_response.status_code == 201
    return ScopedProjectMember(
        member_headers=member.headers,
        member_user_id=member.user_id,
        member_username=member.username,
        visible_project_id=visible_project_id,
        hidden_project_id=hidden_project_id,
    )
