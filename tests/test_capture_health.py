"""Per-project capture health: which capture paths delivered, which went quiet."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import update

from lab_tracker.db_models import NoteModel


def _project(client: TestClient, headers: dict[str, str], name: str = "Capture health") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    text: str,
    *,
    metadata: dict[str, str] | None = None,
    status: str = "staged",
    created_at: datetime | None = None,
) -> str:
    payload = {"project_id": project_id, "raw_content": text, "status": status}
    if metadata:
        payload["metadata"] = metadata
    response = client.post("/notes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    note_id = response.json()["data"]["note_id"]
    if created_at is not None:
        with client.app.state.db_session_factory() as session:
            session.execute(
                update(NoteModel)
                .where(NoteModel.note_id == UUID(note_id))
                .values(created_at=created_at)
            )
            session.commit()
    return note_id


def test_capture_health_groups_by_adapter_and_host_and_flags_quiet_sources(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    now = datetime.now(timezone.utc)
    days = lambda n: now - timedelta(days=n)  # noqa: E731 - test-local shorthand
    figure = {"evidence_adapter": "lab-tracker-client-figure", "capture_host_label": "rig-2"}
    watch = {"evidence_adapter": "lt-watch-files", "capture_host_label": "rig-2"}
    phone = {"capture_source": "mobile_capture"}

    _note(client, admin_auth_headers, project_id, "fig today", metadata=figure)
    _note(client, admin_auth_headers, project_id, "fig 3d", metadata=figure, created_at=days(3))
    _note(
        client,
        admin_auth_headers,
        project_id,
        "fig 12d committed",
        metadata=figure,
        status="committed",
        created_at=days(12),
    )
    _note(client, admin_auth_headers, project_id, "watch 20d", metadata=watch, created_at=days(20))
    _note(client, admin_auth_headers, project_id, "phone 2d", metadata=phone, created_at=days(2))
    _note(client, admin_auth_headers, project_id, "typed 40d", created_at=days(40))
    _note(client, admin_auth_headers, project_id, "typed 9d", created_at=days(9))

    response = client.get(f"/projects/{project_id}/capture-health", headers=admin_auth_headers)
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["project_id"] == project_id
    assert report["window_days"] == 30
    assert report["recent_days"] == 7
    assert report["captured_window"] == 6  # the 40-day-old note is outside the window
    assert report["staged_unreviewed"] == 5
    assert report["quiet_sources"] == 1

    by_key = {(item["adapter"], item["host_label"]): item for item in report["sources"]}
    assert set(by_key) == {
        ("lab-tracker-client-figure", "rig-2"),
        ("lt-watch-files", "rig-2"),
        ("mobile_capture", ""),
        ("manual", ""),
    }
    figure_row = by_key[("lab-tracker-client-figure", "rig-2")]
    assert figure_row["captured_recent"] == 2
    assert figure_row["captured_window"] == 3
    assert figure_row["staged_unreviewed"] == 2
    assert figure_row["quiet"] is False
    watch_row = by_key[("lt-watch-files", "rig-2")]
    assert watch_row["captured_recent"] == 0
    assert watch_row["quiet"] is True
    # Typed notes never count as a stalled capture path.
    assert by_key[("manual", "")]["quiet"] is False
    # Most recent source first.
    assert report["sources"][0]["adapter"] == "lab-tracker-client-figure"

    narrow = client.get(
        f"/projects/{project_id}/capture-health",
        params={"window_days": 5},
        headers=admin_auth_headers,
    )
    assert narrow.status_code == 200
    assert narrow.json()["data"]["captured_window"] == 3
    assert narrow.json()["data"]["quiet_sources"] == 0


def test_capture_health_is_opaque_for_projects_the_caller_cannot_read(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    visible = client.get(
        f"/projects/{scoped_project_member.visible_project_id}/capture-health",
        headers=scoped_project_member.member_headers,
    )
    assert visible.status_code == 200
    assert visible.json()["data"]["sources"] == []

    hidden = client.get(
        f"/projects/{scoped_project_member.hidden_project_id}/capture-health",
        headers=scoped_project_member.member_headers,
    )
    assert hidden.status_code == 404
