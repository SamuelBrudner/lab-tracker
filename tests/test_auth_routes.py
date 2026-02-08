from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.app import create_app
from lab_tracker.auth import Role
from lab_tracker.db import Base


def _bootstrap_database(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "auth-routes.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(client: TestClient, *, username: str = "root", password: str = "secret") -> None:
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )


def _login(client: TestClient, username: str, password: str) -> str:
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    return login_response.json()["data"]["access_token"]


def test_register_login_and_me_round_trip(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201
        register_payload = register_response.json()["data"]
        assert register_payload["token_type"] == "bearer"
        token = register_payload["access_token"]

        me_response = client.get("/auth/me", headers=_auth_headers(token))
        assert me_response.status_code == 200
        me_payload = me_response.json()["data"]
        assert me_payload["username"] == "sam"
        assert me_payload["role"] == "viewer"

        login_response = client.post(
            "/auth/login",
            json={"username": "sam", "password": "secret"},
        )
        assert login_response.status_code == 200
        login_payload = login_response.json()["data"]
        assert login_payload["user"]["user_id"] == me_payload["user_id"]


def test_login_rejects_invalid_credentials(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201

        login_response = client.post(
            "/auth/login",
            json={"username": "sam", "password": "wrong"},
        )
        assert login_response.status_code == 401
        payload = login_response.json()
        assert payload["error"]["code"] == "auth_error"


def test_protected_routes_require_authorization(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        response = client.post("/projects", json={"name": "No Auth"})
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == "auth_error"


def test_protected_routes_accept_valid_authorization(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        _seed_admin(client, username="sam", password="secret")
        token = _login(client, "sam", "secret")

        create_response = client.post(
            "/projects",
            json={"name": "With Auth"},
            headers=_auth_headers(token),
        )
        assert create_response.status_code == 201
        project_id = create_response.json()["data"]["project_id"]

        list_response = client.get("/projects", headers=_auth_headers(token))
        assert list_response.status_code == 200
        project_ids = [item["project_id"] for item in list_response.json()["data"]]
        assert project_id in project_ids


def test_register_non_viewer_requires_admin_token(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        no_auth_response = client.post(
            "/auth/register",
            json={"username": "editor-1", "password": "secret", "role": "editor"},
        )
        assert no_auth_response.status_code == 401
        assert no_auth_response.json()["error"]["code"] == "auth_error"

        viewer_register_response = client.post(
            "/auth/register",
            json={"username": "viewer-1", "password": "secret"},
        )
        assert viewer_register_response.status_code == 201
        viewer_token = viewer_register_response.json()["data"]["access_token"]

        viewer_auth_response = client.post(
            "/auth/register",
            json={"username": "editor-2", "password": "secret", "role": "editor"},
            headers=_auth_headers(viewer_token),
        )
        assert viewer_auth_response.status_code == 401
        assert viewer_auth_response.json()["error"]["code"] == "auth_error"

        _seed_admin(client)
        admin_token = _login(client, "root", "secret")
        admin_auth_response = client.post(
            "/auth/register",
            json={"username": "editor-3", "password": "secret", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert admin_auth_response.status_code == 201
        assert admin_auth_response.json()["data"]["user"]["role"] == "editor"


def test_bootstrap_admin_allows_first_admin_registration(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN", "bootstrap-secret")
    with TestClient(create_app()) as client:
        bootstrap_response = client.post(
            "/auth/register",
            json={
                "username": "root",
                "password": "secret",
                "role": "admin",
                "bootstrap_token": "bootstrap-secret",
            },
        )
        assert bootstrap_response.status_code == 201
        payload = bootstrap_response.json()["data"]
        assert payload["user"]["role"] == "admin"

        admin_token = payload["access_token"]
        editor_response = client.post(
            "/auth/register",
            json={"username": "editor-1", "password": "secret", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert editor_response.status_code == 201
        assert editor_response.json()["data"]["user"]["role"] == "editor"

        repeat_bootstrap = client.post(
            "/auth/register",
            json={
                "username": "root-2",
                "password": "secret",
                "role": "admin",
                "bootstrap_token": "bootstrap-secret",
            },
        )
        assert repeat_bootstrap.status_code == 401
        assert repeat_bootstrap.json()["error"]["code"] == "auth_error"


def test_refresh_issues_fresh_token_ttl(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)

    import lab_tracker.auth as auth_module

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    current_time = {"value": now}

    def fake_utc_now() -> datetime:
        return current_time["value"]

    monkeypatch.setattr(auth_module, "utc_now", fake_utc_now)

    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201
        token = register_response.json()["data"]["access_token"]
        expires_at = datetime.fromisoformat(register_response.json()["data"]["expires_at"])

        current_time["value"] = now + timedelta(hours=1)
        refresh_response = client.post("/auth/refresh", headers=_auth_headers(token))
        assert refresh_response.status_code == 200
        refresh_payload = refresh_response.json()["data"]
        refreshed_expires_at = datetime.fromisoformat(refresh_payload["expires_at"])

        assert refreshed_expires_at > expires_at


def test_refresh_rejects_expired_token(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES", "1")

    import lab_tracker.auth as auth_module

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    current_time = {"value": now}

    def fake_utc_now() -> datetime:
        return current_time["value"]

    monkeypatch.setattr(auth_module, "utc_now", fake_utc_now)

    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201
        token = register_response.json()["data"]["access_token"]

        current_time["value"] = now + timedelta(minutes=2)
        refresh_response = client.post("/auth/refresh", headers=_auth_headers(token))
        assert refresh_response.status_code == 401
        assert refresh_response.json()["error"]["code"] == "auth_error"
