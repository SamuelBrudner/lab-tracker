from __future__ import annotations

import json
from concurrent.futures import (
    ThreadPoolExecutor,
)
from concurrent.futures import (
    TimeoutError as FutureTimeoutError,
)
from datetime import datetime, time, timedelta, timezone
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app import create_app
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import Role
from lab_tracker.db_models import (
    GraphChangeSetModel,
    GraphDraftBatchRunModel,
    GraphDraftBatchSettingsModel,
    NoteModel,
)
from lab_tracker.graph_drafting import (
    READ_ONLY_AGENT_TOOLS,
    AgenticGraphDraftClient,
    GraphDraftingError,
)
from lab_tracker.models import GraphChangeSetStatus
from lab_tracker.services import graph_draft_batch_policy as batch_policy
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
    assert run["review_assignee_user_id"] is not None
    assert run["review_assignee"] == run["review_assignee_user_id"]
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


def test_manual_runs_keep_notes_cursors_and_dedup_scoped_to_each_reviewer(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    first_headers, first_user_id = _registered_user(client, role=Role.EDITOR)
    second_headers, second_user_id = _registered_user(client, role=Role.EDITOR)
    for user_id in (first_user_id, second_user_id):
        added = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": user_id, "role": "contributor"},
            headers=admin_auth_headers,
        )
        assert added.status_code == 201

    first_note = _note(client, first_headers, project_id, "First user's earlier note.")
    second_note = _note(client, second_headers, project_id, "Second user's earlier note.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client

    first_response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=first_headers,
    )
    second_response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=second_headers,
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    first_run = first_response.json()["data"]
    second_run = second_response.json()["data"]
    assert first_run["source_note_ids"] == [first_note]
    assert second_run["source_note_ids"] == [second_note]
    assert first_run["review_assignee_user_id"] == first_user_id
    assert second_run["review_assignee_user_id"] == second_user_id
    assert first_run["review_assignee"] == first_user_id
    assert second_run["review_assignee"] == second_user_id
    first_draft = client.get(
        f"/batches/{first_run['change_set_id']}",
        headers=first_headers,
    )
    second_draft = client.get(
        f"/batches/{second_run['change_set_id']}",
        headers=second_headers,
    )
    assert first_draft.status_code == 200
    assert second_draft.status_code == 200
    assert first_draft.json()["data"]["review_assignee_user_id"] == first_user_id
    assert second_draft.json()["data"]["review_assignee_user_id"] == second_user_id

    # Interleave another pair after both cursors have advanced. Running the
    # second reviewer first must not consume or hide the first reviewer's note.
    later_first_note = _note(client, first_headers, project_id, "First user's later note.")
    later_second_note = _note(client, second_headers, project_id, "Second user's later note.")
    later_second = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=second_headers,
    )
    later_first = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=first_headers,
    )
    assert later_second.status_code == 201
    assert later_first.status_code == 201
    assert later_second.json()["data"]["source_note_ids"] == [later_second_note]
    assert later_first.json()["data"]["source_note_ids"] == [later_first_note]

    # A query with no reviewer is the legacy unassigned bucket, not a wildcard
    # over both reviewers' successful runs or source-note dedup records.
    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        assert repository.latest_successful_graph_draft_batch_run(UUID(project_id)) is None
        assert (
            repository.successful_graph_draft_batch_source_note_ids_at_window_end(
                UUID(project_id),
                _api_datetime(first_run["window_end"]),
            )
            == set()
        )
        assert (
            repository.latest_successful_graph_draft_batch_run(
                UUID(project_id),
                review_assignee_user_id=UUID(first_user_id),
            )
            is not None
        )
        assert (
            repository.latest_successful_graph_draft_batch_run(
                UUID(project_id),
                review_assignee_user_id=UUID(second_user_id),
            )
            is not None
        )


def test_personal_views_honor_reassignment_before_creator_fallback(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    creator_headers, creator_user_id = _registered_user(client, role=Role.EDITOR)
    assignee_headers, assignee_user_id = _registered_user(client, role=Role.EDITOR)
    for user_id in (creator_user_id, assignee_user_id):
        added = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": user_id, "role": "contributor"},
            headers=admin_auth_headers,
        )
        assert added.status_code == 201
    _note(client, creator_headers, project_id, "A review that will be reassigned.")
    client.app.state.graph_draft_client_factory = lambda _settings: FakeBatchDraftClient(
        _batch_patch(project_id)
    )
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=creator_headers,
    )
    assert response.status_code == 201
    run = response.json()["data"]

    with client.app.state.db_session_factory() as session:
        run_row = session.get(GraphDraftBatchRunModel, run["run_id"])
        change_set_row = session.get(GraphChangeSetModel, run["change_set_id"])
        assert run_row is not None
        assert change_set_row is not None
        assert str(run_row.created_by_user_id) == creator_user_id
        assert str(change_set_row.created_by_user_id) == creator_user_id
        run_row.review_assignee = assignee_user_id
        run_row.review_assignee_user_id = assignee_user_id
        change_set_row.review_assignee = assignee_user_id
        change_set_row.review_assignee_user_id = assignee_user_id
        session.commit()

    creator_queue = client.get("/batches?mine=true", headers=creator_headers)
    assignee_queue = client.get("/batches?mine=true", headers=assignee_headers)
    creator_runs = client.get("/batches/runs?mine=true", headers=creator_headers)
    assignee_runs = client.get("/batches/runs?mine=true", headers=assignee_headers)
    assert creator_queue.json()["data"] == []
    assert creator_runs.json()["data"] == []
    assert [item["change_set_id"] for item in assignee_queue.json()["data"]] == [
        run["change_set_id"]
    ]
    assert [item["run_id"] for item in assignee_runs.json()["data"]] == [run["run_id"]]


def test_legacy_unassigned_reviews_are_owner_oversight_not_personal_work(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    contributor_headers, contributor_user_id = _registered_user(
        client,
        role=Role.EDITOR,
    )
    added = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": contributor_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 201
    _note(client, admin_auth_headers, project_id, "Legacy scheduled review content.")
    client.app.state.graph_draft_client_factory = lambda _settings: FakeBatchDraftClient(
        _batch_patch(project_id)
    )
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    run = response.json()["data"]

    with client.app.state.db_session_factory() as session:
        run_row = session.get(GraphDraftBatchRunModel, run["run_id"])
        change_set_row = session.get(GraphChangeSetModel, run["change_set_id"])
        assert run_row is not None
        assert change_set_row is not None
        run_row.review_assignee = None
        run_row.review_assignee_user_id = None
        change_set_row.review_assignee = None
        change_set_row.review_assignee_user_id = None
        session.commit()

    owner_personal = client.get("/batches?mine=true", headers=admin_auth_headers)
    owner_history = client.get("/batches/runs?mine=true", headers=admin_auth_headers)
    contributor_personal = client.get(
        "/batches?mine=true",
        headers=contributor_headers,
    )
    owner_oversight = client.get(
        f"/batches?unassigned_oversight=true&project_id={project_id}",
        headers=admin_auth_headers,
    )
    contributor_project_oversight = client.get(
        f"/batches?unassigned_oversight=true&project_id={project_id}",
        headers=contributor_headers,
    )
    contributor_all_oversight = client.get(
        "/batches?unassigned_oversight=true",
        headers=contributor_headers,
    )

    assert owner_personal.json()["data"] == []
    assert owner_history.json()["data"] == []
    assert contributor_personal.json()["data"] == []
    assert [item["change_set_id"] for item in owner_oversight.json()["data"]] == [
        run["change_set_id"]
    ]
    assert contributor_project_oversight.status_code == 401
    assert contributor_all_oversight.status_code == 200
    assert contributor_all_oversight.json()["data"] == []

    with client.app.state.db_session_factory() as session:
        change_set_row = session.get(GraphChangeSetModel, run["change_set_id"])
        assert change_set_row is not None
        change_set_row.status = GraphChangeSetStatus.CHANGES_REQUESTED.value
        session.commit()
    changes_requested = client.get(
        "/batches?unassigned_oversight=true&status=changes_requested",
        headers=admin_auth_headers,
    )
    assert [item["change_set_id"] for item in changes_requested.json()["data"]] == [
        run["change_set_id"]
    ]
    assert (
        client.get(
            "/batches?mine=true&unassigned_oversight=true",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/batches?needs_commit=true&unassigned_oversight=true",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/batches?unassigned_oversight=true&status=submitted",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )


def test_project_owner_can_recover_but_not_hijack_legacy_batch_review(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    project_owner_headers = _register_project_member(
        client,
        owner_headers=admin_auth_headers,
        project_id=project_id,
        role="owner",
        global_role=Role.VIEWER,
    )
    _note(client, admin_auth_headers, project_id, "Legacy owner recovery content.")
    client.app.state.graph_draft_client_factory = lambda _settings: FakeBatchDraftClient(
        _batch_patch(project_id)
    )
    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    run = response.json()["data"]
    draft = client.get(
        f"/batches/{run['change_set_id']}",
        headers=project_owner_headers,
    ).json()["data"]
    operation = draft["operations"][0]
    revised_payload = {
        **operation["payload"],
        "text": "Does the recovered legacy batch support this revised question?",
    }

    assigned_edit = client.patch(
        (f"/graph-drafts/{run['change_set_id']}/operations/{operation['operation_id']}"),
        json={"payload": revised_payload},
        headers=project_owner_headers,
    )
    assert assigned_edit.status_code == 422

    with client.app.state.db_session_factory() as session:
        run_row = session.get(GraphDraftBatchRunModel, run["run_id"])
        change_set_row = session.get(GraphChangeSetModel, run["change_set_id"])
        assert run_row is not None
        assert change_set_row is not None
        run_row.review_assignee = None
        run_row.review_assignee_user_id = None
        change_set_row.review_assignee = None
        change_set_row.review_assignee_user_id = None
        session.commit()

    revised = client.patch(
        (f"/graph-drafts/{run['change_set_id']}/operations/{operation['operation_id']}"),
        json={"payload": revised_payload},
        headers=project_owner_headers,
    )
    accepted = client.post(
        f"/graph-drafts/{run['change_set_id']}/accept-all",
        headers=project_owner_headers,
    )
    submitted = client.post(
        f"/graph-drafts/{run['change_set_id']}/submit",
        headers=project_owner_headers,
    )

    assert revised.status_code == 200
    assert revised.json()["data"]["operations"][0]["payload"] == revised_payload
    assert accepted.status_code == 200
    assert accepted.json()["data"]["operations"][0]["status"] == "accepted"
    assert submitted.status_code == 200
    assert submitted.json()["data"]["status"] == "submitted"


def test_personal_daily_review_views_are_separate_from_project_oversight(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    first_project_id = _project(client, admin_auth_headers)
    second_project_id = _project(client, admin_auth_headers)
    first_headers, first_user_id = _registered_user(client, role=Role.EDITOR)
    second_headers, second_user_id = _registered_user(client, role=Role.EDITOR)
    for project_id, user_id in (
        (first_project_id, first_user_id),
        (first_project_id, second_user_id),
        (second_project_id, first_user_id),
    ):
        added = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": user_id, "role": "contributor"},
            headers=admin_auth_headers,
        )
        assert added.status_code == 201

    _note(client, first_headers, first_project_id, "First reviewer's project-one note.")
    _note(client, second_headers, first_project_id, "Second reviewer's project-one note.")
    _note(client, first_headers, second_project_id, "First reviewer's project-two note.")
    client.app.state.graph_draft_client_factory = lambda _settings: FakeBatchDraftClient(
        _batch_patch(first_project_id)
    )
    first_project_response = client.post(
        "/batches/run-now",
        json={"project_id": first_project_id},
        headers=first_headers,
    )
    second_user_response = client.post(
        "/batches/run-now",
        json={"project_id": first_project_id},
        headers=second_headers,
    )
    client.app.state.graph_draft_client_factory = lambda _settings: FakeBatchDraftClient(
        _batch_patch(second_project_id)
    )
    second_project_response = client.post(
        "/batches/run-now",
        json={"project_id": second_project_id},
        headers=first_headers,
    )
    assert first_project_response.status_code == 201
    assert second_user_response.status_code == 201
    assert second_project_response.status_code == 201
    first_project_run = first_project_response.json()["data"]
    second_user_run = second_user_response.json()["data"]
    second_project_run = second_project_response.json()["data"]

    first_queue = client.get("/batches?mine=true", headers=first_headers)
    filtered_queue = client.get(
        f"/batches?mine=true&project_id={first_project_id}",
        headers=first_headers,
    )
    second_queue = client.get("/batches?mine=true", headers=second_headers)
    first_history = client.get("/batches/runs?mine=true", headers=first_headers)
    assert first_queue.status_code == 200
    assert filtered_queue.status_code == 200
    assert second_queue.status_code == 200
    assert first_history.status_code == 200
    assert {item["change_set_id"] for item in first_queue.json()["data"]} == {
        first_project_run["change_set_id"],
        second_project_run["change_set_id"],
    }
    assert [item["change_set_id"] for item in filtered_queue.json()["data"]] == [
        first_project_run["change_set_id"]
    ]
    assert [item["change_set_id"] for item in second_queue.json()["data"]] == [
        second_user_run["change_set_id"]
    ]
    assert {item["run_id"] for item in first_history.json()["data"]} == {
        first_project_run["run_id"],
        second_project_run["run_id"],
    }

    first_draft = client.get(
        f"/batches/{first_project_run['change_set_id']}",
        headers=first_headers,
    ).json()["data"]
    operation = first_draft["operations"][0]
    accepted = client.patch(
        "/graph-drafts/"
        f"{first_project_run['change_set_id']}/operations/{operation['operation_id']}",
        json={"payload": operation["payload"], "status": "accepted"},
        headers=first_headers,
    )
    assert accepted.status_code == 200
    submitted = client.post(
        f"/graph-drafts/{first_project_run['change_set_id']}/submit",
        headers=first_headers,
    )
    assert submitted.status_code == 200

    actionable = client.get(
        f"/batches?mine=true&project_id={first_project_id}",
        headers=first_headers,
    )
    waiting = client.get(
        f"/batches?mine=true&status=submitted&project_id={first_project_id}",
        headers=first_headers,
    )
    contributor_oversight = client.get(
        f"/batches?needs_commit=true&project_id={first_project_id}",
        headers=first_headers,
    )
    owner_oversight = client.get(
        f"/batches?needs_commit=true&project_id={first_project_id}",
        headers=admin_auth_headers,
    )
    complete_project_queue = client.get(
        f"/batches?project_id={first_project_id}",
        headers=admin_auth_headers,
    )
    assert actionable.json()["data"] == []
    assert [item["change_set_id"] for item in waiting.json()["data"]] == [
        first_project_run["change_set_id"]
    ]
    assert contributor_oversight.json()["data"] == []
    assert [item["change_set_id"] for item in owner_oversight.json()["data"]] == [
        first_project_run["change_set_id"]
    ]
    assert {item["change_set_id"] for item in complete_project_queue.json()["data"]} == {
        first_project_run["change_set_id"],
        second_user_run["change_set_id"],
    }
    assert (
        client.get(
            "/batches?mine=true&needs_commit=true",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/batches?needs_commit=true&status=ready",
            headers=admin_auth_headers,
        ).status_code
        == 422
    )


def test_personal_cadence_and_owner_project_template_are_independent(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    first_headers, first_user_id = _registered_user(client, role=Role.EDITOR)
    second_headers, second_user_id = _registered_user(client, role=Role.EDITOR)
    for user_id in (first_user_id, second_user_id):
        added = client.post(
            f"/projects/{project_id}/members",
            json={"user_id": user_id, "role": "contributor"},
            headers=admin_auth_headers,
        )
        assert added.status_code == 201

    default_path = f"/projects/{project_id}/graph-draft-batch-settings/project-default"
    personal_path = f"/projects/{project_id}/graph-draft-batch-settings"
    project_default = client.patch(
        default_path,
        json={"enabled": True, "cadence_minutes": 720},
        headers=admin_auth_headers,
    )
    assert project_default.status_code == 200
    assert project_default.json()["data"]["user_id"] is None

    inherited_first = client.get(personal_path, headers=first_headers)
    inherited_second = client.get(personal_path, headers=second_headers)
    assert inherited_first.json()["data"]["user_id"] == first_user_id
    assert inherited_second.json()["data"]["user_id"] == second_user_id
    assert inherited_first.json()["data"]["cadence_minutes"] == 720
    assert inherited_second.json()["data"]["cadence_minutes"] == 720

    updated_first = client.patch(
        personal_path,
        json={"enabled": True, "cadence_minutes": 1440},
        headers=first_headers,
    )
    assert updated_first.status_code == 200
    assert updated_first.json()["data"]["user_id"] == first_user_id
    assert updated_first.json()["data"]["cadence_minutes"] == 1440
    assert (
        client.get(personal_path, headers=second_headers).json()["data"]["cadence_minutes"] == 720
    )
    assert (
        client.get(default_path, headers=admin_auth_headers).json()["data"]["cadence_minutes"]
        == 720
    )

    forbidden_default = client.patch(
        default_path,
        json={"cadence_minutes": 10080},
        headers=first_headers,
    )
    spoofed_beneficiary = client.patch(
        personal_path,
        json={"enabled": True, "user_id": second_user_id},
        headers=first_headers,
    )
    spoofed_read = client.get(
        personal_path,
        params={"user_id": second_user_id},
        headers=first_headers,
    )
    null_beneficiary = client.patch(
        personal_path,
        json={"enabled": True, "user_id": None},
        headers=first_headers,
    )
    default_with_user = client.patch(
        default_path,
        json={"enabled": True, "user_id": first_user_id},
        headers=admin_auth_headers,
    )
    assert forbidden_default.status_code == 401
    assert spoofed_beneficiary.status_code == 422
    assert spoofed_read.status_code == 401
    assert null_beneficiary.status_code == 422
    assert default_with_user.status_code == 422


def test_auth_disabled_cadence_keeps_the_single_legacy_reviewer_bucket(
    monkeypatch,
    migrated_sqlite_database_url: str,
) -> None:
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    with TestClient(create_app()) as local_client:
        project = local_client.post("/projects", json={"name": "Local Daily Review"})
        project_id = project.json()["data"]["project_id"]
        settings_path = f"/projects/{project_id}/graph-draft-batch-settings"
        settings = local_client.patch(settings_path, json={"enabled": True})
        assert settings.status_code == 200
        assert settings.json()["data"]["user_id"] is None
        synthetic_user_id = str(uuid4())
        assert (
            local_client.get(settings_path, params={"user_id": synthetic_user_id}).status_code
            == 422
        )
        assert (
            local_client.patch(
                settings_path,
                json={"enabled": True, "user_id": synthetic_user_id},
            ).status_code
            == 422
        )
        _note(local_client, {}, project_id, "Local staged note.")
        with local_client.app.state.db_session_factory() as session:
            row = session.scalar(
                select(GraphDraftBatchSettingsModel).where(
                    GraphDraftBatchSettingsModel.project_id == project_id
                )
            )
            assert row.user_id is None
            row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            session.commit()
        fake_client = FakeBatchDraftClient(_batch_patch(project_id))
        local_client.app.state.graph_draft_client_factory = lambda _settings: fake_client
        due = local_client.post("/batches/run-due")
        assert due.status_code == 200
        assert len(due.json()["data"]) == 1
        assert due.json()["data"][0]["review_assignee_user_id"] is None
        assert due.json()["data"][0]["review_assignee"] is not None


def test_batch_settings_for_missing_project_return_not_found(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    missing_project_id = uuid4()

    personal = client.get(
        f"/projects/{missing_project_id}/graph-draft-batch-settings",
        headers=admin_auth_headers,
    )
    project_default = client.get(
        f"/projects/{missing_project_id}/graph-draft-batch-settings/project-default",
        headers=admin_auth_headers,
    )
    update = client.patch(
        f"/projects/{missing_project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )

    for response in (personal, project_default, update):
        assert response.status_code == 404
        assert response.json()["error"] == {
            "code": "not_found",
            "message": "Project does not exist.",
            "issues": None,
        }


def test_run_now_for_missing_project_returns_not_found_before_settings_creation(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    missing_project_id = uuid4()
    client.app.state.settings.graph_draft_background_enabled = True

    response = client.post(
        "/batches/run-now",
        json={"project_id": str(missing_project_id)},
        headers=admin_auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "Project does not exist.",
        "issues": None,
    }
    with client.app.state.db_session_factory() as session:
        settings_row = session.scalar(
            select(GraphDraftBatchSettingsModel).where(
                GraphDraftBatchSettingsModel.project_id == str(missing_project_id)
            )
        )
        assert settings_row is None


def test_per_user_batch_notification_address_is_private_to_user_and_owner(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    first_headers = _register_project_member(
        client,
        owner_headers=admin_auth_headers,
        project_id=project_id,
        role="contributor",
        global_role=Role.EDITOR,
    )
    second_headers = _register_project_member(
        client,
        owner_headers=admin_auth_headers,
        project_id=project_id,
        role="contributor",
        global_role=Role.EDITOR,
    )
    first_user_id = client.get("/auth/me", headers=first_headers).json()["data"]["user_id"]

    configured = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={
            "email_notifications_enabled": True,
            "notification_email": "first.reviewer@example.org",
        },
        headers=first_headers,
    )
    assert configured.status_code == 200

    own = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings",
        headers=first_headers,
    )
    assert own.status_code == 200
    assert own.json()["data"]["notification_email"] == "first.reviewer@example.org"

    other = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings",
        params={"user_id": first_user_id},
        headers=second_headers,
    )
    assert other.status_code == 401

    owner = client.get(
        f"/projects/{project_id}/graph-draft-batch-settings",
        params={"user_id": first_user_id},
        headers=admin_auth_headers,
    )
    assert owner.status_code == 200
    assert owner.json()["data"]["notification_email"] == "first.reviewer@example.org"


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


def test_background_run_now_rejoins_reserved_notes_and_only_queues_new_arrivals(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    first_note_id = _note(
        client,
        admin_auth_headers,
        project_id,
        "First note reserved by the pending run.",
    )
    client.app.state.settings.graph_draft_background_enabled = True

    first = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    duplicate = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    second_note_id = _note(
        client,
        admin_auth_headers,
        project_id,
        "A note that arrived after the first reservation.",
    )
    new_arrival = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 201
    assert new_arrival.status_code == 201
    first_run = first.json()["data"]
    assert duplicate.json()["data"]["run_id"] == first_run["run_id"]
    assert first_run["source_note_ids"] == [first_note_id]
    assert new_arrival.json()["data"]["run_id"] != first_run["run_id"]
    assert new_arrival.json()["data"]["source_note_ids"] == [second_note_id]
    assert set(first_run["source_note_ids"]).isdisjoint(
        new_arrival.json()["data"]["source_note_ids"]
    )


def test_explicit_replay_rejoins_the_active_run_that_owns_its_notes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    first_note_id = _note(client, admin_auth_headers, project_id, "Pending note A.")
    second_note_id = _note(client, admin_auth_headers, project_id, "Pending note B.")
    with client.app.state.db_session_factory() as session:
        first_row = session.get(NoteModel, first_note_id)
        second_row = session.get(NoteModel, second_note_id)
        assert first_row is not None
        assert second_row is not None
        first_row.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        first_row.updated_at = first_row.created_at
        second_row.created_at = datetime(2026, 1, 4, tzinfo=timezone.utc)
        second_row.updated_at = second_row.created_at
        session.commit()
    client.app.state.settings.graph_draft_background_enabled = True
    first_window = {
        "project_id": project_id,
        "since": "2026-01-01T00:00:00Z",
        "until": "2026-01-03T00:00:00Z",
    }
    second_window = {
        "project_id": project_id,
        "since": "2026-01-03T00:00:00Z",
        "until": "2026-01-05T00:00:00Z",
    }

    pending_a = client.post(
        "/batches/run-now",
        json=first_window,
        headers=admin_auth_headers,
    )
    pending_b = client.post(
        "/batches/run-now",
        json=second_window,
        headers=admin_auth_headers,
    )
    replay_a = client.post(
        "/batches/run-now",
        json=first_window,
        headers=admin_auth_headers,
    )

    assert pending_a.status_code == 201
    assert pending_b.status_code == 201
    assert replay_a.status_code == 201
    assert pending_a.json()["data"]["source_note_ids"] == [first_note_id]
    assert pending_b.json()["data"]["source_note_ids"] == [second_note_id]
    assert pending_b.json()["data"]["run_id"] != pending_a.json()["data"]["run_id"]
    assert replay_a.json()["data"]["run_id"] == pending_a.json()["data"]["run_id"]


def test_concurrent_inline_run_now_calls_share_one_provider_run(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "One concurrently requested note.")
    entered_provider = Event()
    release_provider = Event()

    class BlockingBatchDraftClient(FakeBatchDraftClient):
        def draft_from_batch(
            self,
            *,
            batch_context: dict[str, Any],
            user_hint: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
            entered_provider.set()
            if not release_provider.wait(timeout=5):
                raise AssertionError("Timed out waiting to release the provider call.")
            return self.patch

    fake_client = BlockingBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client

    def run_now():
        return client.post(
            "/batches/run-now",
            json={"project_id": project_id},
            headers=admin_auth_headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(run_now)
        assert entered_provider.wait(timeout=5)
        second_future = executor.submit(run_now)
        try:
            with pytest.raises(FutureTimeoutError):
                second_future.result(timeout=0.2)
        finally:
            release_provider.set()
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["data"]["run_id"] == first.json()["data"]["run_id"]
    assert second.json()["data"]["batch_key"] == first.json()["data"]["batch_key"]
    assert len(fake_client.calls) == 1


def test_background_worker_redacts_configured_provider_secret_from_failed_run(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    api_key = "nonstandard queued/google secret"
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Queued background failure.")
    client.app.state.settings.google_api_key = api_key
    client.app.state.settings.graph_draft_background_enabled = True

    def failing_factory(settings):  # noqa: ANN001
        raise RuntimeError(
            f"provider factory unavailable for {api_key} at https://provider.test/v1?key={api_key}"
        )

    client.app.state.graph_draft_client_factory = failing_factory
    queued_response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )
    assert queued_response.status_code == 201
    queued = queued_response.json()["data"]
    assert queued["status"] == "pending"

    processed = _process_next_background_run(client)

    assert processed.status.value == "failed"
    assert processed.error_metadata["category"] == "worker_error"
    message = str(processed.error_metadata["message"])
    assert "provider factory unavailable" in message
    assert api_key not in message
    assert "?key=" not in message
    with client.app.state.db_session_factory() as session:
        persisted = session.get(GraphDraftBatchRunModel, queued["run_id"])
        assert persisted is not None
        assert persisted.error_metadata == processed.error_metadata


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


def test_explicit_window_replay_honors_a_pre_stable_key_success(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _note(client, admin_auth_headers, project_id, "Pre-upgrade batch note.")
    with client.app.state.db_session_factory() as session:
        row = session.get(NoteModel, note_id)
        assert row is not None
        row.created_at = datetime(2026, 2, 2, tzinfo=timezone.utc)
        row.updated_at = row.created_at
        session.commit()
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client
    window = {
        "project_id": project_id,
        "since": "2026-02-01T00:00:00Z",
        "until": "2026-02-03T00:00:00Z",
    }

    first = client.post("/batches/run-now", json=window, headers=admin_auth_headers)
    assert first.status_code == 201
    first_run = first.json()["data"]
    legacy_key = batch_policy.make_batch_key(
        project_id=UUID(project_id),
        since=_api_datetime(first_run["window_start"]),
        until=_api_datetime(first_run["window_end"]),
        note_ids=[UUID(value) for value in first_run["source_note_ids"]],
        review_assignee=first_run["review_assignee"],
        review_assignee_user_id=UUID(first_run["review_assignee_user_id"]),
    )
    assert legacy_key != first_run["batch_key"]
    with client.app.state.db_session_factory() as session:
        run_row = session.get(GraphDraftBatchRunModel, first_run["run_id"])
        change_set_row = session.get(
            GraphChangeSetModel,
            first_run["change_set_id"],
        )
        assert run_row is not None
        assert change_set_row is not None
        run_row.batch_key = legacy_key
        change_set_row.batch_key = legacy_key
        session.commit()

    replay = client.post("/batches/run-now", json=window, headers=admin_auth_headers)

    assert replay.status_code == 201
    assert replay.json()["data"]["run_id"] == first_run["run_id"]
    assert replay.json()["data"]["batch_key"] == legacy_key
    assert len(fake_client.calls) == 1


def test_failed_batch_advances_the_stable_key_generation_for_an_explicit_retry(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _note(client, admin_auth_headers, project_id, "Retry this failed batch.")
    created_clients: list[FakeBatchDraftClient] = []

    def failing_factory(_settings):
        draft_client = FakeBatchDraftClient(
            _batch_patch(project_id),
            fail_attempts=10,
        )
        created_clients.append(draft_client)
        return draft_client

    client.app.state.graph_draft_client_factory = failing_factory
    window = {
        "project_id": project_id,
        "since": "1970-01-01T00:00:00Z",
        "until": "2099-01-01T00:00:00Z",
    }

    first = client.post("/batches/run-now", json=window, headers=admin_auth_headers)
    retry = client.post("/batches/run-now", json=window, headers=admin_auth_headers)

    assert first.status_code == 201
    assert retry.status_code == 201
    first_run = first.json()["data"]
    retry_run = retry.json()["data"]
    assert first_run["status"] == "failed"
    assert retry_run["status"] == "failed"
    assert first_run["source_note_ids"] == [note_id]
    assert retry_run["source_note_ids"] == [note_id]
    assert retry_run["run_id"] != first_run["run_id"]
    assert retry_run["batch_key"] != first_run["batch_key"]
    assert [len(draft_client.calls) for draft_client in created_clients] == [3, 3]


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


def test_failed_batch_redacts_provider_secret_from_persisted_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    api_key = "AIza" + ("0" * 35)
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Draft this staged note.")
    failing_client = FakeBatchDraftClient(
        fail_attempts=10,
        error=(
            f"quota exceeded for credential {api_key} at "
            f"https://provider.test/v1?key={api_key}&retry=true"
        ),
    )
    client.app.state.graph_draft_client_factory = lambda settings: failing_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "failed"
    assert run["error_metadata"]["category"] == "model_error"
    assert "quota exceeded" in run["error_metadata"]["message"]
    with client.app.state.db_session_factory() as session:
        persisted_run = session.get(GraphDraftBatchRunModel, run["run_id"])
        persisted_change_set = session.get(
            GraphChangeSetModel,
            run["change_set_id"],
        )
        assert persisted_run is not None
        assert persisted_change_set is not None
        persisted_messages = (
            str(persisted_run.error_metadata["message"]),
            str(persisted_change_set.error_metadata["message"]),
        )
    for message in persisted_messages:
        assert "quota exceeded" in message
        assert api_key not in message
        assert "?key=" not in message


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
    api_key = "nonstandard scheduled/google secret"
    client.app.state.settings.google_api_key = api_key
    healthy_client = FakeBatchDraftClient(_batch_patch(second_project_id))

    def factory(settings):  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(
                f"factory down for {api_key} at https://provider.test/v1?key={api_key}&retry=true"
            )
        return healthy_client

    client.app.state.graph_draft_client_factory = factory

    response = client.post("/batches/run-due", headers=admin_auth_headers)

    assert response.status_code == 200
    runs = response.json()["data"]
    assert [run["status"] for run in runs] == ["failed", "ready"]
    assert runs[0]["error_metadata"]["category"] == "scheduler_error"
    assert "factory down" in runs[0]["error_metadata"]["message"]
    assert api_key not in runs[0]["error_metadata"]["message"]
    assert "?key=" not in runs[0]["error_metadata"]["message"]
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
        f"/projects/{project_id}/graph-draft-batch-settings/project-default",
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
