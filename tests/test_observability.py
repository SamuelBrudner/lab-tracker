from lab_tracker.app import create_app
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from lab_tracker.db import Base
from lab_tracker.db_models import ProjectModel


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
