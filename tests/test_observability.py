from lab_tracker.app import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.auth import Role
from lab_tracker.db import Base
from lab_tracker.db_models import ProjectModel
from lab_tracker.services.search_backends import SearchBackend


class _FlakySearchBackend(SearchBackend):
    backend_name = "flaky"

    def __init__(self) -> None:
        self._fail_next_upsert = True

    def upsert_questions(self, questions) -> None:  # noqa: ANN001
        if self._fail_next_upsert and list(questions):
            self._fail_next_upsert = False
            raise RuntimeError("temporary index outage")

    def upsert_notes(self, notes) -> None:  # noqa: ANN001
        return

    def delete_questions(self, question_ids) -> None:  # noqa: ANN001
        return

    def delete_notes(self, note_ids) -> None:  # noqa: ANN001
        return

    def search_question_ids(self, query, *, question_ids=None):  # noqa: ANN001
        return []

    def search_note_ids(self, query, *, note_ids=None):  # noqa: ANN001
        return []


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

    client = TestClient(create_app())
    response = client.get("/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
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

    client = TestClient(create_app())
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

    client = TestClient(create_app())

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


def test_observability_reports_search_degradation(monkeypatch, tmp_path):
    db_path = tmp_path / "search-health.db"
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
    app.state.lab_tracker_api._record_search_failure("upsert_questions", RuntimeError("index down"))

    with TestClient(app) as client:
        readiness = client.get("/readiness")
        assert readiness.status_code == 503
        readiness_payload = readiness.json()
        search_check = next(
            check for check in readiness_payload["checks"] if check["name"] == "search"
        )
        assert search_check["status"] == "fail"
        assert search_check["operation"] == "upsert_questions"

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        metrics_payload = metrics.json()
        assert metrics_payload["status"] == "fail"
        assert metrics_payload["search"]["degraded"] is True
        assert metrics_payload["search"]["failure_count"] == 1
        assert metrics_payload["errors"][0]["name"] == "search"


def test_readiness_recovers_after_transient_search_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "search-recovery.db"
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
    backend = _FlakySearchBackend()
    app.state.search_backend = backend
    app.state.lab_tracker_api._search_backend = backend
    app.state.auth_service.register_user(
        username="search-recovery-admin",
        password="secret",
        role=Role.ADMIN,
    )

    with TestClient(app) as client:
        login_response = client.post(
            "/auth/login",
            json={"username": "search-recovery-admin", "password": "secret"},
        )
        assert login_response.status_code == 200
        token = login_response.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        project_id = client.post(
            "/projects",
            json={"name": "Search recovery"},
            headers=headers,
        ).json()["data"]["project_id"]

        first_question = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Does the first sync fail?",
                "question_type": "descriptive",
            },
            headers=headers,
        )
        assert first_question.status_code == 201

        first_readiness = client.get("/readiness")
        assert first_readiness.status_code == 503

        second_question = client.post(
            "/questions",
            json={
                "project_id": project_id,
                "text": "Does the second sync recover?",
                "question_type": "descriptive",
            },
            headers=headers,
        )
        assert second_question.status_code == 201

        recovered_readiness = client.get("/readiness")
        assert recovered_readiness.status_code == 200
        assert recovered_readiness.json()["status"] == "ok"
