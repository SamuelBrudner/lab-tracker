from __future__ import annotations

import json
import logging
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import ReviewEmailDeliveryStatus, utc_now
from lab_tracker.review_links import sign_review_link
from lab_tracker.services.review_email_service import (
    ReviewEmailService,
    normalize_review_email,
)
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


def _enable_review_email(client: TestClient) -> None:
    client.app.state.settings.review_email_enabled = True


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
    assert settings["user_id"] == user_id
    assert settings["notification_email"] == email
    assert settings["notification_email_confirmed_at"] is not None


def test_assigned_ready_batch_enqueues_one_contentless_delivery(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_review_email(client)
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
    _enable_review_email(client)
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
    _enable_review_email(client)
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


def test_globally_disabled_review_email_cannot_opt_in_enqueue_or_claim(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    user_id = _admin_user_id(client, admin_auth_headers)
    project_id, _note_id = _project_and_note(client, admin_auth_headers)

    # Personal Daily Review settings resolve the authenticated user, and the
    # update route rejects an explicit user_id outright, so the capability gate
    # is only reachable when the caller omits it.
    current = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings",
        headers=admin_auth_headers,
    )
    assert current.status_code == 200
    assert current.json()["data"]["review_email_available"] is False

    opt_in = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={
            "email_notifications_enabled": True,
            "notification_email": "reviewer@example.org",
        },
        headers=admin_auth_headers,
    )
    assert opt_in.status_code == 422
    assert "not enabled" in opt_in.json()["error"]["message"].lower()

    test_alert = client.post(
        "/review-email/test",
        json={"destination_email": "reviewer@example.org"},
        headers=admin_auth_headers,
    )
    assert test_alert.status_code == 422

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
        assert api.review_emails.list() == []
        assert api.review_emails.claim_next(lease_seconds=60) is None
        with pytest.raises(ValidationError, match="not enabled"):
            api.review_emails.enqueue_test(
                "reviewer@example.org",
                recipient_user_id=UUID(user_id),
            )

        client.app.state.settings.review_email_enabled = True
        enabled_api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        pending = enabled_api.review_emails.enqueue_test(
            "stale@example.org",
            recipient_user_id=UUID(user_id),
        )
        client.app.state.settings.review_email_enabled = False
        disabled_api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        assert disabled_api.review_emails.claim_next(lease_seconds=60) is None
        assert disabled_api.review_emails.get(pending.delivery_id).status == (
            ReviewEmailDeliveryStatus.PENDING
        )


def test_expired_leases_count_as_attempts_and_dead_letter_at_max_attempts(
    client: TestClient,
) -> None:
    """A worker that dies after every claim must not re-lease a delivery forever."""
    _enable_review_email(client)
    client.app.state.settings.review_email_max_attempts = 2
    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        assert api.review_emails.max_attempts == 2
        delivery = api.review_emails.enqueue_test("poison@example.org")
        start = utc_now()

        first = api.review_emails.claim_next(lease_seconds=60, now=start)
        second = api.review_emails.claim_next(
            lease_seconds=60, now=start + timedelta(seconds=61)
        )
        third = api.review_emails.claim_next(
            lease_seconds=60, now=start + timedelta(seconds=122)
        )

        assert first is not None and first.attempt_count == 1
        assert second is not None and second.attempt_count == 2
        assert third is None
        dead = api.review_emails.get(delivery.delivery_id)
        assert dead.status == ReviewEmailDeliveryStatus.FAILED
        assert dead.attempt_count == 2
        assert dead.claim_token is None
        assert dead.lease_expires_at is None
        assert dead.next_attempt_at is None
        assert "lease expired" in (dead.last_error or "").lower()
        assert (
            api.review_emails.claim_next(lease_seconds=60, now=start + timedelta(hours=1))
            is None
        )


def test_dead_lettering_is_logged_and_idle_polls_do_not_write(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An idle claim is a read; a dead-lettered delivery is named in a warning."""
    _enable_review_email(client)
    client.app.state.settings.review_email_max_attempts = 1
    engine = client.app.state.db_engine
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert api.review_emails.claim_next(lease_seconds=60, now=utc_now()) is None
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert "UPDATE" not in statements

        delivery = api.review_emails.enqueue_test("poison@example.org")
        start = utc_now()
        assert api.review_emails.claim_next(lease_seconds=60, now=start) is not None
        with caplog.at_level(logging.WARNING, logger="lab_tracker.services.review_email_service"):
            assert (
                api.review_emails.claim_next(lease_seconds=60, now=start + timedelta(seconds=61))
                is None
            )

    assert api.review_emails.get(delivery.delivery_id).status == ReviewEmailDeliveryStatus.FAILED
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(delivery.delivery_id) in r.getMessage() for r in warnings)


def _unbound_review_link(client: TestClient) -> str:
    return sign_review_link(
        client.app.state.settings.auth_secret_key,
        uuid4(),
        recipient_user_id=uuid4(),
        delivery_id=uuid4(),
    )


def test_review_link_redirects_invalid_or_unknown_links_to_app_root(
    client: TestClient,
) -> None:
    tampered = client.get("/r/not-a-valid-token", follow_redirects=False)
    unknown_delivery = client.get(
        f"/r/{_unbound_review_link(client)}",
        follow_redirects=False,
    )

    for response in (tampered, unknown_delivery):
        assert response.status_code == 302
        assert response.headers["location"] == "/app/"


def test_review_link_surfaces_backend_failures_instead_of_redirecting(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anti-enumeration only needs a uniform answer for bad tokens and missing
    # deliveries; a database outage must fail loudly, not look like a bad link.
    def _database_down(_self, _delivery_id):
        raise OperationalError("SELECT 1", {}, Exception("database is down"))

    monkeypatch.setattr(ReviewEmailService, "get", _database_down)
    failing_client = TestClient(client.app, raise_server_exceptions=False)

    response = failing_client.get(
        f"/r/{_unbound_review_link(client)}",
        follow_redirects=False,
    )

    assert response.status_code == 500
    assert "location" not in response.headers


def test_admin_delivery_responses_do_not_expose_the_claim_token(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_review_email(client)
    response = client.post(
        "/review-email/test",
        json={"destination_email": "lease@example.org"},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    assert "claim_token" not in response.json()["data"]

    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
        )
        claimed = api.review_emails.claim_next(lease_seconds=60)
        assert claimed is not None
        assert claimed.claim_token is not None
        live_token = str(claimed.claim_token)

    listing = client.get("/review-email/deliveries", headers=admin_auth_headers)
    assert listing.status_code == 200
    items = listing.json()["data"]
    assert [item["status"] for item in items] == ["sending"]
    assert "claim_token" not in items[0]
    assert live_token not in listing.text


def test_test_email_with_unknown_recipient_user_is_rejected(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_review_email(client)
    response = client.post(
        "/review-email/test",
        json={
            "destination_email": "nobody@example.org",
            "recipient_user_id": "00000000-0000-4000-8000-000000000001",
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 422
    assert "recipient_user_id" in response.text

    listing = client.get("/review-email/deliveries", headers=admin_auth_headers)
    assert listing.status_code == 200
    assert listing.json()["data"] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Test.User@Example.ORG", "Test.User@example.org"),
        ('"ab"@Example.org', "ab@example.org"),
        ('"a b"@Example.org', '"a b"@example.org'),
        ('"a\\"b"@example.org', '"a\\"b"@example.org'),
        ('".ab"@example.org', '".ab"@example.org'),
        ('"a."@example.org', '"a."@example.org'),
        ('"a..b"@example.org', '"a..b"@example.org'),
    ],
)
def test_normalize_review_email_is_idempotent(raw: str, expected: str) -> None:
    normalized = normalize_review_email(raw)

    assert normalized == expected
    assert normalize_review_email(normalized) == normalized
