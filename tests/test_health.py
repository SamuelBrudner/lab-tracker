from api_helpers import app_test_client


def test_health_endpoint():
    client = app_test_client(verify_schema=False)
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "timestamp" in payload
