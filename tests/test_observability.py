from lab_tracker.app import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.db import Base
from lab_tracker.db_models import ProjectModel


def test_readiness_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path))
    client = TestClient(create_app())
    response = client.get("/readiness")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "timestamp" in payload
    checks = payload["checks"]
    assert any(check["name"] == "note_storage" for check in checks)


def test_metrics_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path))
    client = TestClient(create_app())
    response = client.get("/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["store"]["projects"] == 0
    assert payload["store"]["questions"] == 0
    assert payload["store"]["datasets"] == 0


def test_metrics_endpoint_reads_database_counts(monkeypatch, tmp_path):
    db_path = tmp_path / "observability.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
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
