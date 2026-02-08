from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.app import create_app
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


def test_register_login_and_me_round_trip(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret", "role": "admin"},
        )
        assert register_response.status_code == 201
        register_payload = register_response.json()["data"]
        assert register_payload["token_type"] == "bearer"
        token = register_payload["access_token"]

        me_response = client.get("/auth/me", headers=_auth_headers(token))
        assert me_response.status_code == 200
        me_payload = me_response.json()["data"]
        assert me_payload["username"] == "sam"
        assert me_payload["role"] == "admin"

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
            json={"username": "sam", "password": "secret", "role": "admin"},
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
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret", "role": "admin"},
        )
        assert register_response.status_code == 201
        token = register_response.json()["data"]["access_token"]

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
