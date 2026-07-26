from __future__ import annotations

import json
from uuid import UUID

from fastapi.testclient import TestClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import ReviewEmailDeliveryStatus
from lab_tracker.review_links import sign_review_link
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


class _BatchDraftClient:
    provider = "fake"
    model = "fake-review-email"

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id

    def draft_from_batch(self, **_kwargs):
        return {
            "summary": "Private summary that must not enter email.",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                {
                    "client_ref": "email-test-question",
                    "op": "create",
                    "entity_type": "question",
                    "semantic_type": "suggest_new_question",
                    "target_entity_id": None,
                    "payload_json": json.dumps(
                        {
                            "project_id": self.project_id,
                            "text": "Private proposed question",
                            "question_type": "descriptive",
                            "status": "staged",
                        }
                    ),
                    "rationale": "Private rationale",
                    "confidence": 0.8,
                    "source_refs": [],
                }
            ],
        }


def _admin_user_id(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]["user_id"]


def _project_and_note(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    project_response = client.post(
        "/projects",
        json={"name": "Review email project"},
        headers=headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]
    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Private staged observation",
            "status": "staged",
        },
        headers=headers,
    )
    assert note_response.status_code == 201
    return project_id, note_response.json()["data"]["note_id"]


def _configure_email(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: str,
    user_id: str,
    email: str = "reviewer@example.org",
) -> None:
    response = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={
            "user_id": user_id,
            "enabled": True,
            "cadence_minutes": 1440,
            "run_at_local_time": "17:00",
            "timezone_name": "America/New_York",
            "email_notifications_enabled": True,
            "notification_email": email,
        },
        headers=headers,
    )
    assert response.status_code == 200
    settings = response.json()["data"]
    assert settings["notification_email"] == email
    assert settings["notification_email_confirmed_at"] is not None


def test_assigned_ready_batch_enqueues_one_contentless_delivery(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    user_id = _admin_user_id(client, admin_auth_headers)
    project_id, _note_id = _project_and_note(client, admin_auth_headers)
    _configure_email(
        client,
        admin_auth_headers,
        project_id=project_id,
        user_id=user_id,
    )

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=_BatchDraftClient(project_id),
            actor=AuthContext(user_id=UUID(user_id), role=Role.ADMIN),
            review_assignee=user_id,
            review_assignee_user_id=UUID(user_id),
        )
        assert run.change_set_id is not None
        deliveries = api.review_emails.list()
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery.change_set_id == run.change_set_id
        assert delivery.destination_email == "reviewer@example.org"
        assert delivery.status == ReviewEmailDeliveryStatus.PENDING

        change_set = api.get_graph_change_set(run.change_set_id)
        api.review_emails.enqueue_ready_review(change_set)
        assert len(api.review_emails.list()) == 1

        serialized = delivery.model_dump_json()
        assert "Private" not in serialized
        assert project_id not in serialized


def test_disabled_preference_does_not_enqueue(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    user_id = _admin_user_id(client, admin_auth_headers)
    project_id, _note_id = _project_and_note(client, admin_auth_headers)

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=_BatchDraftClient(project_id),
            actor=AuthContext(user_id=UUID(user_id), role=Role.ADMIN),
            review_assignee=user_id,
            review_assignee_user_id=UUID(user_id),
        )
        assert api.review_emails.list() == []


def test_non_graph_test_alert_can_be_claimed_and_accepted(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    response = client.post(
        "/review-email/test",
        json={"destination_email": "Test.User@Example.ORG"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["change_set_id"] is None
    assert payload["event_type"] == "test"
    assert payload["destination_email"] == "Test.User@example.org"

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        claimed = api.review_emails.claim_next(lease_seconds=60)
        assert claimed is not None
        assert claimed.attempt_count == 1
        assert claimed.claim_token is not None
        accepted = api.review_emails.mark_accepted(
            claimed.delivery_id,
            claim_token=claimed.claim_token,
            provider_message_id="<provider-message-id>",
        )
        assert accepted.status == ReviewEmailDeliveryStatus.ACCEPTED
        assert accepted.accepted_at is not None


def test_signed_link_redirects_without_bypassing_app_auth(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    user_id = _admin_user_id(client, admin_auth_headers)
    project_id, _note_id = _project_and_note(client, admin_auth_headers)
    _configure_email(
        client,
        admin_auth_headers,
        project_id=project_id,
        user_id=user_id,
    )
    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=_BatchDraftClient(project_id),
            actor=system_auth_context(),
            review_assignee=user_id,
            review_assignee_user_id=UUID(user_id),
        )
        delivery = api.review_emails.list()[0]
        token = sign_review_link(
            client.app.state.settings.auth_secret_key,
            run.change_set_id,
            recipient_user_id=UUID(user_id),
            delivery_id=delivery.delivery_id,
        )

    response = client.get(f"/r/{token}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/app/batches/{run.change_set_id}"
    protected = client.get(f"/batches/{run.change_set_id}")
    assert protected.status_code == 401
