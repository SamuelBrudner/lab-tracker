"""Tests for the daily-review email-cue delivery surface.

Covers the ``GET /batches/editions-ready`` contentless read model and the public
``GET /r/{token}`` deep-link landing route.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from lab_tracker.auth import Role
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    utc_now,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _project(client: TestClient, headers: dict[str, str], name: str = "Delivery Project") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _register(client: TestClient, *, role: Role = Role.EDITOR) -> UUID:
    username = f"reviewer-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    return user.user_id


def _login_headers(client: TestClient, *, role: Role = Role.VIEWER) -> dict[str, str]:
    username = f"user-{uuid4().hex[:8]}"
    client.app.state.auth_service.register_user(username=username, password="secret", role=role)
    response = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert response.status_code == 200
    return _auth_headers(response.json()["data"]["access_token"])


def _scheduler_token_headers(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> dict[str, str]:
    expires_at = (utc_now() + timedelta(days=1)).isoformat()
    issued = client.post(
        "/auth/tokens",
        json={"label": "scheduler", "role": "admin", "read_only": True, "expires_at": expires_at},
        headers=admin_auth_headers,
    )
    assert issued.status_code == 201, issued.text
    return _auth_headers(issued.json()["data"]["secret"])


def _seed_ready_edition(
    client: TestClient,
    project_id: str,
    *,
    review_user_id: UUID,
    sensitive: bool = False,
    proposed_ops: int = 1,
    accepted_ops: int = 0,
    status: GraphChangeSetStatus = GraphChangeSetStatus.READY,
    use_legacy_single_note: bool = False,
    created_at=None,
) -> UUID:
    change_set_id = uuid4()
    note_id = uuid4()
    note = Note(
        note_id=note_id,
        project_id=UUID(project_id),
        raw_content="captured observation",
        metadata={"sensitivity": "sensitive"} if sensitive else {},
        created_by_user_id=review_user_id,
    )
    operations = [
        GraphChangeOperation(
            operation_id=uuid4(),
            change_set_id=change_set_id,
            sequence=index,
            op=GraphChangeOp.CREATE,
            entity_type=EntityType.QUESTION,
            status=(
                GraphChangeOperationStatus.PROPOSED
                if index < proposed_ops
                else GraphChangeOperationStatus.ACCEPTED
            ),
        )
        for index in range(proposed_ops + accepted_ops)
    ]
    change_set = GraphChangeSet(
        change_set_id=change_set_id,
        project_id=UUID(project_id),
        source_note_id=note_id,
        # Exercise the legacy single-note shape (source_note_ids empty, the
        # source_note_id fallback) when asked.
        source_note_ids=[] if use_legacy_single_note else [note_id],
        model="fake-model",
        prompt_version="v-test",
        draft_mode=GraphDraftMode.GRAPH_BATCH,
        status=status,
        summary="This proposes a merged question (must NOT leak to the cue).",
        operations=operations,
        operation_count=len(operations),
        review_assignee=str(review_user_id),
        review_assignee_user_id=review_user_id,
        batch_key=f"batch:{change_set_id.hex[:32]}",
        created_at=created_at or utc_now(),
    )
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        repository.notes.save(note)
        repository.graph_change_sets.save(change_set)
        session.commit()
    return change_set_id


def test_editions_ready_returns_contentless_cue_with_signed_link(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    change_set_id = _seed_ready_edition(client, project_id, review_user_id=reviewer)

    response = client.get("/batches/editions-ready", headers=admin_auth_headers)
    assert response.status_code == 200, response.text
    items = response.json()["data"]
    assert len(items) == 1
    item = items[0]
    assert item["change_set_id"] == str(change_set_id)
    assert item["decidable_count"] == 1
    assert item["sensitivity_suppressed"] is False
    assert item["review_assignee_user_id"] == str(reviewer)
    assert "/r/" in item["deep_link"]
    # Contentless: no science rides along, project name suppressed by default.
    assert "summary" not in item
    assert "operations" not in item
    assert "context_packet" not in item
    assert item["project_name"] is None


def test_editions_ready_can_include_project_name_on_request(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers, name="Odor Gradient")
    reviewer = _register(client)
    _seed_ready_edition(client, project_id, review_user_id=reviewer)

    response = client.get(
        "/batches/editions-ready?include_project_name=true",
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["project_name"] == "Odor Gradient"


def test_editions_ready_suppresses_count_for_sensitive_edition(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    _seed_ready_edition(client, project_id, review_user_id=reviewer, sensitive=True)

    response = client.get("/batches/editions-ready", headers=admin_auth_headers)
    assert response.status_code == 200
    item = response.json()["data"][0]
    assert item["sensitivity_suppressed"] is True
    assert item["decidable_count"] is None
    # A signed link is still offered; only the volume is hidden.
    assert "/r/" in item["deep_link"]


def test_editions_ready_filters_by_reviewer_and_since(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer_a = _register(client)
    reviewer_b = _register(client)
    old = utc_now() - timedelta(hours=6)
    _seed_ready_edition(client, project_id, review_user_id=reviewer_a, created_at=old)
    new_id = _seed_ready_edition(client, project_id, review_user_id=reviewer_a)
    _seed_ready_edition(client, project_id, review_user_id=reviewer_b)

    by_reviewer = client.get(
        f"/batches/editions-ready?user_id={reviewer_a}", headers=admin_auth_headers
    )
    assert by_reviewer.status_code == 200
    rows = by_reviewer.json()["data"]
    assert len(rows) == 2  # both of reviewer_a's editions, none of reviewer_b's
    assert all(row["review_assignee_user_id"] == str(reviewer_a) for row in rows)
    assert str(new_id) in {row["change_set_id"] for row in rows}

    watermark = (utc_now() - timedelta(hours=1)).isoformat()
    since = client.get(
        "/batches/editions-ready",
        params={"user_id": str(reviewer_a), "since": watermark},
        headers=admin_auth_headers,
    )
    assert since.status_code == 200
    since_ids = {row["change_set_id"] for row in since.json()["data"]}
    assert since_ids == {str(new_id)}


def test_editions_ready_skips_non_ready_and_fully_decided(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    # A submitted edition (skipped by status), an empty-ops edition, and a
    # NON-empty edition whose ops are all ACCEPTED (skipped by the count, not the
    # emptiness) must all be excluded.
    _seed_ready_edition(
        client, project_id, review_user_id=reviewer, status=GraphChangeSetStatus.SUBMITTED
    )
    _seed_ready_edition(client, project_id, review_user_id=reviewer, proposed_ops=0)
    _seed_ready_edition(
        client, project_id, review_user_id=reviewer, proposed_ops=0, accepted_ops=2
    )

    response = client.get("/batches/editions-ready", headers=admin_auth_headers)
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_editions_ready_counts_only_undecided_operations(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    # 1 still-proposed + 2 already-accepted -> the cue counts only the 1 that
    # still needs a decision, so a decided edition is never re-cued in full.
    _seed_ready_edition(
        client, project_id, review_user_id=reviewer, proposed_ops=1, accepted_ops=2
    )

    response = client.get("/batches/editions-ready", headers=admin_auth_headers)
    assert response.status_code == 200
    item = response.json()["data"][0]
    assert item["decidable_count"] == 1


def test_editions_ready_orders_oldest_first(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    older = _seed_ready_edition(
        client, project_id, review_user_id=reviewer, created_at=utc_now() - timedelta(hours=3)
    )
    newer = _seed_ready_edition(client, project_id, review_user_id=reviewer)

    response = client.get("/batches/editions-ready", headers=admin_auth_headers)
    ids = [row["change_set_id"] for row in response.json()["data"]]
    assert ids == [str(older), str(newer)]


def test_editions_ready_suppresses_count_for_legacy_single_note(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    # The source_note_id fallback (source_note_ids empty) must still detect a
    # sensitive note.
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    _seed_ready_edition(
        client,
        project_id,
        review_user_id=reviewer,
        sensitive=True,
        use_legacy_single_note=True,
    )

    response = client.get("/batches/editions-ready", headers=admin_auth_headers)
    item = response.json()["data"][0]
    assert item["sensitivity_suppressed"] is True
    assert item["decidable_count"] is None


def test_review_link_bounces_on_non_ascii_token(client: TestClient) -> None:
    # Regression: a crafted non-ASCII token must 302-bounce, not 500.
    redirect = client.get("/r/%C3%A9.YWJj", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/app/"


def test_editions_ready_requires_admin(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    _seed_ready_edition(client, project_id, review_user_id=reviewer)

    viewer_headers = _login_headers(client, role=Role.VIEWER)
    denied = client.get("/batches/editions-ready", headers=viewer_headers)
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "auth_error"


def test_editions_ready_allows_scheduler_service_token(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    _seed_ready_edition(client, project_id, review_user_id=reviewer)

    scheduler_headers = _scheduler_token_headers(client, admin_auth_headers)
    response = client.get("/batches/editions-ready", headers=scheduler_headers)
    assert response.status_code == 200
    assert len(response.json()["data"]) == 1


def test_review_link_redirects_into_app_queue(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer = _register(client)
    change_set_id = _seed_ready_edition(client, project_id, review_user_id=reviewer)

    listing = client.get("/batches/editions-ready", headers=admin_auth_headers)
    deep_link = listing.json()["data"][0]["deep_link"]
    link_path = "/r/" + deep_link.split("/r/", 1)[1]

    # Public route: no auth header needed, and it must not follow through.
    redirect = client.get(link_path, follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == f"/app/batches/{change_set_id}"


def test_review_link_bounces_on_bad_token(client: TestClient) -> None:
    redirect = client.get("/r/not-a-valid-token", follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/app/"
