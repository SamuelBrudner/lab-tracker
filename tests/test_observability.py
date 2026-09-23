import logging
from collections.abc import Iterator
from contextlib import contextmanager

from api_helpers import app_test_client, stamp_schema_at_head
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

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
    stamp_schema_at_head(engine)
    engine.dispose()
    return database_url


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@contextmanager
def _observability_log() -> Iterator[list[str]]:
    # App startup replaces the root handlers, so pytest's caplog handler never
    # sees these records; attach directly to the module logger instead.
    handler = _RecordingHandler()
    target = logging.getLogger("lab_tracker.app_parts.observability")
    original_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield handler.messages
    finally:
        target.removeHandler(handler)
        target.setLevel(original_level)


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
    stamp_schema_at_head(engine)
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


def test_health_identifies_the_exact_deployment_without_secrets(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path, "health-identity.db")
    monkeypatch.setenv("LAB_TRACKER_APP_NAME", "deployment-test-lab-tracker")
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv(
        "LAB_TRACKER_SOURCE_REVISION",
        "0123456789abcdef0123456789abcdef01234567",
    )

    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["app"] == {
        "name": "deployment-test-lab-tracker",
        "environment": "local",
        "source_revision": "0123456789abcdef0123456789abcdef01234567",
    }
    assert "database_url" not in response.text
    assert "api_key" not in response.text


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
    stamp_schema_at_head(engine)
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

    # Startup refuses an unmigrated database; skip that check to simulate a
    # database that fails after the app has started.
    client = app_test_client(verify_schema=False)

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
    stamp_schema_at_head(engine)
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


def test_metrics_is_admin_only_while_readiness_serves_any_principal(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path, "viewer-observability.db")
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    app = create_app()
    app.state.auth_service.register_user(
        username="admin",
        password="secret",
        role=Role.ADMIN,
    )
    app.state.auth_service.register_user(
        username="viewer",
        password="secret",
        role=Role.VIEWER,
    )

    with TestClient(app) as client:
        tokens = {
            username: client.post(
                "/auth/login",
                json={"username": username, "password": "secret"},
            ).json()["data"]["access_token"]
            for username in ("admin", "viewer")
        }
        viewer_readiness = client.get("/readiness", headers=_auth_headers(tokens["viewer"]))
        viewer_metrics = client.get("/metrics", headers=_auth_headers(tokens["viewer"]))
        admin_metrics = client.get("/metrics", headers=_auth_headers(tokens["admin"]))

    assert viewer_readiness.status_code == 200
    assert viewer_metrics.status_code == 403
    assert viewer_metrics.json()["error"]["code"] == "forbidden"
    assert "store" not in viewer_metrics.text
    assert admin_metrics.status_code == 200
    assert admin_metrics.json()["store"]["projects"] == 0


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


def test_database_failures_do_not_leak_raw_driver_errors(monkeypatch, tmp_path):
    db_path = tmp_path / "broken-detail.db"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("LAB_TRACKER_FILE_STORAGE_PATH", str(tmp_path / "file-storage"))
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path))
    client = app_test_client(verify_schema=False)

    with _observability_log() as messages:
        readiness = client.get("/readiness")
        metrics = client.get("/metrics")

    database_check = next(
        check for check in readiness.json()["checks"] if check["name"] == "database"
    )
    assert database_check["detail"] == "database unavailable (OperationalError)"
    assert metrics.json()["errors"] == [
        {"name": "database", "detail": "database unavailable (OperationalError)"}
    ]
    for body in (readiness.text, metrics.text):
        assert "no such table" not in body
        assert "SELECT" not in body
    # The raw driver error stays available to operators in the server log.
    assert any("no such table" in message for message in messages)


def test_readiness_checks_connectivity_without_counting_rows(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path, "readiness-cheap.db")
    client = app_test_client()
    statements: list[str] = []

    def _record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(client.app.state.db_engine, "before_cursor_execute", _record)
    try:
        response = client.get("/readiness")
    finally:
        event.remove(client.app.state.db_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert statements, "readiness must still touch the database"
    assert not [statement for statement in statements if "count(" in statement.lower()]


def test_readiness_does_not_expose_server_storage_paths(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path, "readiness-paths.db")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file, not a directory")
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(blocked))
    client = app_test_client()

    with _observability_log() as messages:
        response = client.get("/readiness")

    assert response.status_code == 503
    assert str(tmp_path) not in response.text
    note_check = next(
        check for check in response.json()["checks"] if check["name"] == "note_storage"
    )
    assert note_check == {
        "name": "note_storage",
        "status": "fail",
        "detail": "path exists but is not a directory",
    }
    assert any(str(blocked) in message for message in messages)
