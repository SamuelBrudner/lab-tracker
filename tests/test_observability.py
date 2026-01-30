from lab_tracker.app import create_app
from lab_tracker.fastapi_compat import TestClient


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
