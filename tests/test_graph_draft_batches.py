from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import Role
from lab_tracker.db_models import GraphDraftBatchSettingsModel, NoteModel
from lab_tracker.graph_drafting import (
    READ_ONLY_AGENT_TOOLS,
    AgenticGraphDraftClient,
    GraphDraftingError,
)
from lab_tracker.sqlalchemy_repository_parts.repository import SQLAlchemyLabTrackerRepository


class FakeBatchDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def __init__(
        self,
        patch: dict[str, Any] | None = None,
        *,
        fail_attempts: int = 0,
        error: str = "temporary model outage",
    ) -> None:
        self.patch = patch or {
            "summary": "empty",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
        }
        self.fail_attempts = fail_attempts
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
        if len(self.calls) <= self.fail_attempts:
            raise GraphDraftingError(self.error)
        return self.patch

    def close(self) -> None:
        self.closed = True


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_auth_headers(client: TestClient, *, role: Role = Role.VIEWER) -> dict[str, str]:
    username = f"batch-{role.value}-{uuid4().hex[:8]}"
    password = "secret"
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    return _auth_headers(login_response.json()["data"]["access_token"])


def _registered_user(
    client: TestClient,
    *,
    role: Role = Role.VIEWER,
) -> tuple[dict[str, str], str]:
    username = f"batch-{role.value}-{uuid4().hex[:8]}"
    password = "secret"
    user = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    return _auth_headers(login_response.json()["data"]["access_token"]), str(user.user_id)


def _api_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Batch Project"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note(client: TestClient, headers: dict[str, str], project_id: str, text: str) -> str:
    response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": text, "status": "staged"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def _batch_patch(project_id: str) -> dict[str, Any]:
    return {
        "summary": "Batch drafted one question",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "batch_question",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Do pooled notes support a merged observation?",
                        "question_type": "descriptive",
                        "status": "staged",
                    }
                ),
                "rationale": "Multiple staged notes point at the same follow-up.",
                "confidence": 0.82,
                "source_refs": [],
            }
        ],
    }


def _process_next_background_run(client: TestClient):
    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=client.app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
            surface="background",
        )
        return api.process_next_graph_draft_batch_run(
            draft_client_factory=client.app.state.graph_draft_client_factory,
            app_settings=client.app.state.settings,
            actor=system_auth_context(),
        )


def test_run_now_persists_pending_batch_with_source_traceability(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_a = _note(client, admin_auth_headers, project_id, "Gel photo A looked clean.")
    note_b = _note(client, admin_auth_headers, project_id, "Voice memo: same gel, lane 2.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "merge gel notes"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "ready"
    assert run["note_count"] == 2
    assert run["change_set_id"]
    assert fake_client.closed is True
    assert fake_client.calls[0]["user_hint"] == "merge gel notes"

    draft = client.get(f"/batches/{run['change_set_id']}", headers=admin_auth_headers)
    assert draft.status_code == 200
    payload = draft.json()["data"]
    assert payload["draft_mode"] == "graph_batch"
    assert payload["status"] == "ready"
    assert set(payload["source_note_ids"]) == {note_a, note_b}
    assert payload["source_note_count"] == 2
    assert payload["operations"][0]["source_refs"][0]["source_note_ids"] == [note_a, note_b]
    assert (
        payload["operations"][0]["source_refs"][0]["source_note_ids_resolution"]
        == "ambiguous_bundle"
    )

    operation = payload["operations"][0]
    rejected = client.patch(
        f"/graph-drafts/{run['change_set_id']}/operations/{operation['operation_id']}",
        json={
            "payload": operation["payload"],
            "review_note": "Already covered by the existing question queue.",
            "status": "rejected",
        },
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200
    rejected_operation = rejected.json()["data"]["operations"][0]
    assert rejected_operation["status"] == "rejected"
    assert rejected_operation["review_note"] == "Already covered by the existing question queue."
    assert (
        rejected_operation["error_metadata"]["review_note"]
        == "Already covered by the existing question queue."
    )

    listed = client.get(f"/batches?project_id={project_id}", headers=admin_auth_headers)
    assert listed.status_code == 200
    assert listed.json()["data"][0]["change_set_id"] == run["change_set_id"]


def _register_project_member(
    client: TestClient,
    *,
    owner_headers: dict[str, str],
    project_id: str,
    role: str,
    global_role: Role,
) -> dict[str, str]:
    username = f"member-{role}-{uuid4().hex[:8]}"
    client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=global_role,
    )
    login = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert login.status_code == 200
    member_headers = _auth_headers(login.json()["data"]["access_token"])
    added = client.post(
        f"/projects/{project_id}/members",
        json={"username": username, "role": role},
        headers=owner_headers,
    )
    assert added.status_code == 201
    return member_headers


def test_contributor_can_schedule_and_run_project_batch(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    contributor_headers = _register_project_member(
        client,
        owner_headers=admin_auth_headers,
        project_id=project_id,
        role="contributor",
        global_role=Role.EDITOR,
    )

    # A contributor may configure their own project's batch cadence (schedule).
    settings = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True, "run_at_local_time": "06:00"},
        headers=contributor_headers,
    )
    assert settings.status_code == 200
    assert settings.json()["data"]["enabled"] is True
    assert settings.json()["data"]["run_at_local_time"] == "06:00"

    # A contributor may run their own project's batch now.
    _note(client, contributor_headers, project_id, "Contributor captured a staged note.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client
    run = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=contributor_headers,
    )
    assert run.status_code == 201
    assert run.json()["data"]["status"] == "ready"


def test_viewer_cannot_schedule_or_run_project_batch(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    viewer_headers = _register_project_member(
        client,
        owner_headers=admin_auth_headers,
        project_id=project_id,
        role="viewer",
        global_role=Role.VIEWER,
    )

    settings = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=viewer_headers,
    )
    assert settings.status_code == 401

    run = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=viewer_headers,
    )
    assert run.status_code == 401


def test_background_run_now_enqueues_and_worker_processes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Queued background note.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    client.app.state.settings.graph_draft_background_enabled = True

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "queue it"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    queued = response.json()["data"]
    assert queued["status"] == "pending"
    assert queued["change_set_id"] is None
    assert queued["source_note_ids"]
    assert fake_client.calls == []

    processed = _process_next_background_run(client)

    assert processed.status.value == "ready"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["user_hint"] == "queue it"
    draft = client.get(f"/batches/{processed.change_set_id}", headers=admin_auth_headers)
    assert draft.status_code == 200
    assert draft.json()["data"]["status"] == "ready"


def test_batch_run_is_idempotent_for_same_window(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _note(client, admin_auth_headers, project_id, "One staged note.")
    with client.app.state.db_session_factory() as session:
        row = session.get(NoteModel, note_id)
        row.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        row.updated_at = row.created_at
        session.commit()
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    window = {
        "project_id": project_id,
        "since": "2026-01-01T00:00:00Z",
        "until": "2026-01-03T00:00:00Z",
    }

    first = client.post("/batches/run-now", json=window, headers=admin_auth_headers)
    second = client.post("/batches/run-now", json=window, headers=admin_auth_headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["data"]["run_id"] == first.json()["data"]["run_id"]
    assert second.json()["data"]["change_set_id"] == first.json()["data"]["change_set_id"]
    assert len(fake_client.calls) == 1


def test_batch_retry_and_dead_letter_paths_are_persisted(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Retry this staged note.")
    retry_client = FakeBatchDraftClient(_batch_patch(project_id), fail_attempts=2)
    client.app.state.graph_draft_client_factory = lambda settings: retry_client

    retry_response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert retry_response.status_code == 201
    assert retry_response.json()["data"]["status"] == "ready"
    assert len(retry_client.calls) == 3

    bad_patch = {
        "summary": "bad",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": None,
                "op": "delete",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": "{}",
                "rationale": "Unsupported op.",
                "confidence": 0.2,
                "source_refs": [],
            }
        ],
    }
    _note(client, admin_auth_headers, project_id, "This note triggers invalid JSON shape.")
    dead_letter_client = FakeBatchDraftClient(bad_patch)
    client.app.state.graph_draft_client_factory = lambda settings: dead_letter_client

    failed_response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert failed_response.status_code == 201
    failed_run = failed_response.json()["data"]
    assert failed_run["status"] == "failed"
    assert failed_run["error_metadata"]["category"] == "validation_error"
    draft = client.get(f"/batches/{failed_run['change_set_id']}", headers=admin_auth_headers)
    assert draft.json()["data"]["status"] == "failed"
    assert draft.json()["data"]["error_metadata"]["input_snapshot"]["source_note_ids"]


def test_empty_batch_window_skips_without_creating_change_set(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "skipped"
    assert run["change_set_id"] is None
    assert run["note_count"] == 0
    assert fake_client.calls == []
    with client.app.state.db_session_factory() as session:
        settings = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
    assert settings is not None
    assert settings.enabled is False


def test_batch_cadence_settings_are_user_visible_and_rescheduled(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    defaults = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings",
        headers=admin_auth_headers,
    )
    assert defaults.status_code == 200
    assert defaults.json()["data"]["enabled"] is False
    assert defaults.json()["data"]["cadence_minutes"] == 1440
    assert defaults.json()["data"]["next_run_at"] is None
    with client.app.state.db_session_factory() as session:
        persisted_default = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
    assert persisted_default is None

    updated = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={
            "enabled": True,
            "cadence_minutes": 720,
            "run_at_local_time": "07:30",
            "timezone_name": "America/New_York",
        },
        headers=admin_auth_headers,
    )
    assert updated.status_code == 200
    payload = updated.json()["data"]
    assert payload["cadence_minutes"] == 720
    assert payload["run_at_local_time"] == "07:30"
    assert payload["next_run_at"]


def test_empty_batch_settings_patch_does_not_materialize_default_row(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    response = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={},
        headers=admin_auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["enabled"] is False
    with client.app.state.db_session_factory() as session:
        persisted = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
    assert persisted is None


def test_batch_run_validates_and_clamps_manual_windows(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Manual window note.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    invalid = client.post(
        "/batches/run-now",
        json={
            "project_id": project_id,
            "since": "2026-01-03T00:00:00Z",
            "until": "2026-01-02T00:00:00Z",
        },
        headers=admin_auth_headers,
    )

    assert invalid.status_code == 422
    assert "since must be before until" in invalid.json()["error"]["message"]

    before = datetime.now(timezone.utc)
    future = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "until": "2099-01-01T00:00:00Z"},
        headers=admin_auth_headers,
    )
    after = datetime.now(timezone.utc)

    assert future.status_code == 201
    run = future.json()["data"]
    window_end = datetime.fromisoformat(run["window_end"].replace("Z", "+00:00"))
    assert before <= window_end <= after


def test_batch_run_limits_window_to_notes_sent_to_model(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_ids = [
        _note(client, admin_auth_headers, project_id, f"Staged note {index}")
        for index in range(101)
    ]
    base_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    with client.app.state.db_session_factory() as session:
        for index, note_id in enumerate(note_ids):
            row = session.get(NoteModel, note_id)
            row.created_at = base_time + timedelta(seconds=index)
            row.updated_at = row.created_at
        session.commit()
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    first = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "until": "2099-01-01T00:00:00Z"},
        headers=admin_auth_headers,
    )

    assert first.status_code == 201
    first_run = first.json()["data"]
    assert first_run["note_count"] == 100
    first_context_notes = fake_client.calls[0]["batch_context"]["batch_notes"]
    assert len(first_context_notes) == 100
    assert first_context_notes[-1]["id"] == note_ids[99]

    draft = client.get(f"/batches/{first_run['change_set_id']}", headers=admin_auth_headers)
    assert draft.status_code == 200
    assert draft.json()["data"]["source_note_ids"][-1] == note_ids[99]
    assert note_ids[100] not in draft.json()["data"]["source_note_ids"]

    second = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "until": "2099-01-01T00:00:00Z"},
        headers=admin_auth_headers,
    )

    assert second.status_code == 201
    assert second.json()["data"]["note_count"] == 1
    assert len(fake_client.calls) == 2
    assert fake_client.calls[1]["batch_context"]["batch_notes"][0]["id"] == note_ids[100]


def test_batch_run_continues_notes_sharing_cutoff_timestamp(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_ids = [
        _note(client, admin_auth_headers, project_id, f"Same timestamp note {index}")
        for index in range(101)
    ]
    shared_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    with client.app.state.db_session_factory() as session:
        for note_id in note_ids:
            row = session.get(NoteModel, note_id)
            row.created_at = shared_time
            row.updated_at = shared_time
        session.commit()
    sorted_note_ids = sorted(note_ids)
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    first = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "until": "2099-01-01T00:00:00Z"},
        headers=admin_auth_headers,
    )
    second = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "until": "2099-01-01T00:00:00Z"},
        headers=admin_auth_headers,
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["note_count"] == 100
    assert second.json()["data"]["note_count"] == 1
    first_context_ids = [
        note["id"] for note in fake_client.calls[0]["batch_context"]["batch_notes"]
    ]
    second_context_ids = [
        note["id"] for note in fake_client.calls[1]["batch_context"]["batch_notes"]
    ]
    assert first_context_ids == sorted_note_ids[:100]
    assert second_context_ids == sorted_note_ids[100:]


def test_run_due_isolates_project_errors_and_continues(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    first_project_id = _project(client, admin_auth_headers)
    second_project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, first_project_id, "First due project.")
    _note(client, admin_auth_headers, second_project_id, "Second due project.")
    for project_id in (first_project_id, second_project_id):
        payload: dict[str, Any] = {"enabled": True}
        if project_id == second_project_id:
            payload |= {
                "cadence_minutes": 1440,
                "run_at_local_time": "00:00",
                "timezone_name": "UTC",
            }
        response = client.patch(
            f"/projects/{project_id}/graph-draft-batch-settings",
            json=payload,
            headers=admin_auth_headers,
        )
        assert response.status_code == 200

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    with client.app.state.db_session_factory() as session:
        first = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == first_project_id
            )
        )
        second = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == second_project_id
            )
        )
        first.next_run_at = past
        second.next_run_at = past + timedelta(seconds=1)
        session.commit()

    calls = 0
    healthy_client = FakeBatchDraftClient(_batch_patch(second_project_id))

    def factory(settings):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("factory down")
        return healthy_client

    client.app.state.graph_draft_client_factory = factory

    response = client.post("/batches/run-due", headers=admin_auth_headers)

    assert response.status_code == 200
    runs = response.json()["data"]
    assert [run["status"] for run in runs] == ["failed", "ready"]
    assert runs[0]["error_metadata"]["category"] == "scheduler_error"
    assert runs[1]["project_id"] == second_project_id
    assert healthy_client.closed is True

    settings = client.get(
        f"/projects/{second_project_id}/graph-draft-batch-settings",
        headers=admin_auth_headers,
    )
    assert settings.status_code == 200
    window_end = _api_datetime(runs[1]["window_end"])
    expected_next_run_at = datetime.combine(
        (window_end + timedelta(days=1)).date(),
        time.min,
        tzinfo=timezone.utc,
    )
    assert _api_datetime(settings.json()["data"]["next_run_at"]) == expected_next_run_at


def test_background_run_due_partitions_by_note_author_and_assigns_reviewers(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    first_headers, first_user_id = _registered_user(client, role=Role.VIEWER)
    second_headers, second_user_id = _registered_user(client, role=Role.VIEWER)
    for user_id in (first_user_id, second_user_id):
        response = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": user_id, "role": "contributor"},
            headers=admin_auth_headers,
        )
        assert response.status_code == 201
    first_note = _note(client, first_headers, project_id, "First user's staged note.")
    second_note = _note(client, second_headers, project_id, "Second user's staged note.")
    settings = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )
    assert settings.status_code == 200
    with client.app.state.db_session_factory() as session:
        row = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
        row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client
    client.app.state.settings.graph_draft_background_enabled = True

    response = client.post("/batches/run-due", headers=admin_auth_headers)

    assert response.status_code == 200
    queued_runs = response.json()["data"]
    assert len(queued_runs) == 2
    assert {run["status"] for run in queued_runs} == {"pending"}
    assert {run["review_assignee_user_id"] for run in queued_runs} == {
        first_user_id,
        second_user_id,
    }
    assert {tuple(run["source_note_ids"]) for run in queued_runs} == {
        (first_note,),
        (second_note,),
    }
    assert fake_client.calls == []

    processed = []
    for _ in queued_runs:
        run = _process_next_background_run(client)
        assert run is not None
        processed.append(run)

    assert {run.review_assignee_user_id for run in processed} == {
        UUID(first_user_id),
        UUID(second_user_id),
    }
    first_run = next(run for run in processed if str(run.review_assignee_user_id) == first_user_id)
    first_draft = client.get(
        f"/batches/{first_run.change_set_id}",
        headers=admin_auth_headers,
    )
    assert first_draft.status_code == 200
    first_payload = first_draft.json()["data"]
    assert first_payload["review_assignee_user_id"] == first_user_id
    operation = first_payload["operations"][0]

    wrong_user = client.patch(
        f"/graph-drafts/{first_run.change_set_id}/operations/{operation['operation_id']}",
        json={"payload": operation["payload"], "status": "accepted"},
        headers=second_headers,
    )
    assert wrong_user.status_code == 422
    assert "assigned reviewer" in wrong_user.json()["error"]["message"]

    accepted = client.patch(
        f"/graph-drafts/{first_run.change_set_id}/operations/{operation['operation_id']}",
        json={"payload": operation["payload"], "status": "accepted"},
        headers=first_headers,
    )
    assert accepted.status_code == 200
    submitted = client.post(
        f"/graph-drafts/{first_run.change_set_id}/submit",
        headers=first_headers,
    )
    assert submitted.status_code == 200


def test_agentic_graph_draft_client_uses_read_only_context_trace() -> None:
    class CapturingBaseClient:
        provider = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.batch_context: dict[str, Any] | None = None
            self.user_hint: str | None = None
            self.closed = False

        def draft_from_batch(
            self,
            *,
            batch_context: dict[str, Any],
            user_hint: str | None = None,
        ) -> dict[str, Any]:
            self.batch_context = batch_context
            self.user_hint = user_hint
            return {
                "summary": "agentic",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [],
            }

        def close(self) -> None:
            self.closed = True

    base = CapturingBaseClient()
    client = AgenticGraphDraftClient(base_client=base)

    result = client.draft_from_batch(
        batch_context={
            "batch_notes": [
                {
                    "id": "note-1",
                    "raw_content_preview": "PV inhibition broadened odor tuning.",
                }
            ],
            "projects": [
                {
                    "id": "project-1",
                    "label": "Olfaction",
                    "active_or_staged_questions": [
                        {
                            "id": "question-1",
                            "text": "Does PV inhibition broaden odor tuning?",
                        }
                    ],
                    "recent_claims": [],
                    "recent_analyses": [],
                    "known_aliases": [],
                }
            ],
            "context_summary": {"counts": {"batch_notes": 1}},
        },
        user_hint="prefer existing questions",
    )

    assert result["summary"] == "agentic"
    assert base.batch_context is not None
    trace = base.batch_context["agentic_tool_trace"]
    assert trace["tool_policy"] == {
        "allowed_tools": list(READ_ONLY_AGENT_TOOLS),
        "write_tools_available": False,
    }
    assert trace["matched_existing_nodes"][0]["id"] == "question-1"
    assert "prefer existing questions" in (base.user_hint or "")
    with pytest.raises(GraphDraftingError, match="background batch drafts"):
        client.draft_from_note()


def test_batch_settings_claim_requires_observed_next_run_at(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    response = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    past = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first_next_run_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    second_next_run_at = datetime(2026, 1, 3, tzinfo=timezone.utc)

    with client.app.state.db_session_factory() as session:
        settings = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
        settings.next_run_at = past
        session.commit()

    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        due_settings = repository.list_due_graph_draft_batch_settings(
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        )
        observed_next_run_at = due_settings[0].next_run_at

        claimed = repository.claim_due_graph_draft_batch_settings(
            due_settings[0].settings_id,
            observed_next_run_at=observed_next_run_at,
            next_run_at=first_next_run_at,
            updated_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            updated_by="scheduler",
        )
        stale = repository.claim_due_graph_draft_batch_settings(
            due_settings[0].settings_id,
            observed_next_run_at=observed_next_run_at,
            next_run_at=second_next_run_at,
            updated_at=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            updated_by="scheduler",
        )
        session.commit()

    assert claimed is not None
    assert claimed.next_run_at == first_next_run_at
    assert stale is None
    with client.app.state.db_session_factory() as session:
        persisted = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
        persisted_next_run_at = persisted.next_run_at
        if persisted_next_run_at.tzinfo is None:
            persisted_next_run_at = persisted_next_run_at.replace(tzinfo=timezone.utc)
        else:
            persisted_next_run_at = persisted_next_run_at.astimezone(timezone.utc)
        assert persisted_next_run_at == first_next_run_at


def test_run_due_skips_settings_lost_to_concurrent_claim(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "This due note must not draft.")
    response = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    with client.app.state.db_session_factory() as session:
        settings = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
        settings.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()

    def lost_claim(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return None

    def factory(settings):  # noqa: ANN001
        raise AssertionError("lost scheduled claims must not start a draft client")

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "claim_due_graph_draft_batch_settings",
        lost_claim,
    )
    client.app.state.graph_draft_client_factory = factory

    response = client.post("/batches/run-due", headers=admin_auth_headers)

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_read_only_admin_service_token_can_run_due_batches(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Scheduled service token note.")
    response = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    with client.app.state.db_session_factory() as session:
        settings = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
        settings.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()
    issued = client.post(
        "/auth/tokens",
        json={
            "label": "Daily review automation",
            "role": "admin",
            "read_only": True,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert issued.status_code == 201, issued.text
    service_headers = _auth_headers(issued.json()["data"]["secret"])
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post("/batches/run-due", headers=service_headers)

    assert response.status_code == 200, response.text
    runs = response.json()["data"]
    assert len(runs) == 1
    assert runs[0]["status"] == "ready"
    assert runs[0]["project_id"] == project_id


def test_run_due_rejects_non_admin_user(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    viewer_headers = _user_auth_headers(client)
    project_id = _project(client, admin_auth_headers)
    response = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    with client.app.state.db_session_factory() as session:
        settings = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == project_id
            )
        )
        settings.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.commit()

    def factory(settings):  # noqa: ANN001
        raise AssertionError("non-admin scheduled runs must not start a draft client")

    client.app.state.graph_draft_client_factory = factory

    response = client.post("/batches/run-due", headers=viewer_headers)

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "auth_error",
        "message": "Only admins can run scheduled batch drafts.",
        "issues": None,
    }
