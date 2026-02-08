from fastapi.testclient import TestClient

from lab_tracker.app import create_app


def test_frontend_routes_and_assets_are_served():
    client = TestClient(create_app())

    root_response = client.get("/", follow_redirects=False)
    assert root_response.status_code in (302, 307)
    assert root_response.headers["location"] == "/app"

    app_response = client.get("/app")
    assert app_response.status_code == 200
    assert "text/html" in app_response.headers.get("content-type", "")
    assert "Lab Tracker MVP" in app_response.text
    assert "id=\"app-root\"" in app_response.text

    js_response = client.get("/app/static/app.js")
    assert js_response.status_code == 200
    assert "text/javascript" in js_response.headers.get("content-type", "")
    assert "function App()" in js_response.text
