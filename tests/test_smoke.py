from fastapi.testclient import TestClient

from lab_tracker.app import create_app


def test_app_boots_with_core_routes_and_state():
    app = create_app()

    route_paths = set(app.openapi()["paths"])
    assert {
        "/health",
        "/readiness",
        "/metrics",
        "/projects",
        "/auth/login",
        "/portfolio/summary",
    }.issubset(route_paths)
    assert app.state.lab_tracker_api is not None
    assert app.state.auth_service is not None

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
