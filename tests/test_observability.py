from api_helpers import app_test_client
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.app import create_app
from lab_tracker.auth import Role
from lab_tracker.db import Base
from lab_tracker.db_models import ProjectModel


def _bootstrap_database(monkeypatch, tmp_path, name: str) -> str:
    db_path = tmp_path / name
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    return database_url


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_readiness_endpoint(monkeypatch, tmp_path):
    db_path = tmp_path / "readiness.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path))

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    client = app_test_client()
    response = client.get("/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["auth"] == {"enabled": False}
    assert "timestamp" in payload
    checks = payload["checks"]
    assert any(check["name"] == "database" and check["status"] == "ok" for check in checks)
    assert any(check["name"] == "note_storage" for check in checks)
    assert any(check["name"] == "file_storage" for check in checks)


def test_metrics_endpoint(monkeypatch, tmp_path):
    db_path = tmp_path / "metrics.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path))

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    client = app_test_client()
    response = client.get("/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["store"]["projects"] == 0
    assert payload["store"]["questions"] == 0
    assert payload["store"]["datasets"] == 0


def test_observability_reports_database_failures(monkeypatch, tmp_path):
    db_path = tmp_path / "broken.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path))

    client = app_test_client()

    readiness = client.get("/readiness")
    assert readiness.status_code == 503
    readiness_payload = readiness.json()
    assert readiness_payload["status"] == "fail"
    assert any(
        check["name"] == "database" and check["status"] == "fail"
        for check in readiness_payload["checks"]
    )

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    metrics_payload = metrics.json()
    assert metrics_payload["status"] == "fail"
    assert metrics_payload["errors"][0]["name"] == "database"


def test_metrics_endpoint_reads_database_counts(monkeypatch, tmp_path):
    db_path = tmp_path / "observability.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()

    app = create_app()
    with app.state.db_session_factory() as session:
        session.add(
            ProjectModel(
                name="Inserted directly in DB",
                description="metrics should query DB",
            )
        )
        session.commit()

    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["store"]["projects"] == 1


def test_readiness_and_metrics_require_auth_when_auth_enabled(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path, "auth-observability.db")
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    app = create_app()
    app.state.auth_service.register_user(
        username="admin",
        password="secret",
        role=Role.ADMIN,
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/readiness").status_code == 401
        assert client.get("/metrics").status_code == 401

        login = client.post(
            "/auth/login",
            json={"username": "admin", "password": "secret"},
        )
        assert login.status_code == 200
        headers = _auth_headers(login.json()["data"]["access_token"])

        readiness = client.get("/readiness", headers=headers)
        metrics = client.get("/metrics", headers=headers)

    assert readiness.status_code == 200
    assert readiness.json()["checks"][0]["name"] == "database"
    assert readiness.json()["auth"] == {"enabled": True}
    assert metrics.status_code == 200
    assert metrics.json()["store"]["projects"] == 0


def test_test_prefix_is_not_a_public_auth_bypass(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path, "auth-test-prefix.db")
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    app = create_app()
    app.state.auth_service.register_user(
        username="admin",
        password="secret",
        role=Role.ADMIN,
    )

    @app.get("/_test/probe")
    def test_probe():
        return {"status": "ok"}

    with TestClient(app) as client:
        unauthenticated = client.get("/_test/probe")
        login = client.post(
            "/auth/login",
            json={"username": "admin", "password": "secret"},
        )
        authenticated = client.get(
            "/_test/probe",
            headers=_auth_headers(login.json()["data"]["access_token"]),
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {"status": "ok"}
