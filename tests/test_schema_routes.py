from __future__ import annotations

from fastapi.testclient import TestClient

from lab_tracker.models import QuestionType


def test_schema_describe_route_returns_filtered_entity_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    response = client.get(
        "/schema/describe?entity_type=question",
        headers=admin_auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert list(payload["entities"]) == ["question"]
    assert payload["entities"]["question"]["create"]["fields"]["question_type"][
        "controlled_values"
    ]["allowed_values"] == [question_type.value for question_type in QuestionType]


def test_schema_describe_route_rejects_unknown_entity_type(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    response = client.get(
        "/schema/describe?entity_type=figure",
        headers=admin_auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
