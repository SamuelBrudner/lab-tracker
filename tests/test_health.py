import pytest
from api_helpers import app_test_client, isolate_default_database_url


@pytest.fixture(autouse=True)
def _database_outside_the_working_directory(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    isolate_default_database_url(monkeypatch, tmp_path)


def test_health_endpoint():
    client = app_test_client(verify_schema=False)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "timestamp" in payload
