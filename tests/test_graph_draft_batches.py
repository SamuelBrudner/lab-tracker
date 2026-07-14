from __future__ import annotations

import asyncio
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
from lab_tracker.db_models import (
    GraphDraftBatchSettingsModel,
    NoteModel,
    ProjectMembershipModel,
)
from lab_tracker.graph_drafting import (
    EXTERNAL_HARNESS_LAUNCH_TABLE,
    READ_ONLY_AGENT_TOOLS,
    AgenticGraphDraftClient,
    GraphDraftBatchResult,
    GraphDraftingError,
    HarnessDraftRunResult,
    HarnessGraphDraftClient,
)
from lab_tracker.services.graph_draft_context import SENSITIVE_NOTE_REDACTION
from lab_tracker.services.graph_draft_harness_mcp import (
    SUBMIT_GRAPH_PATCH_TOOL,
    HarnessGraphDraftMCPServer,
)
from lab_tracker.services.graph_draft_read_tools import (
    AGENTIC_READ_TOOL_ALLOWLIST,
    ScopedGraphDraftReadToolExecutor,
)
from lab_tracker.services.shared import SENSITIVE_NOTE_VALUE, SENSITIVITY_METADATA_KEY
from lab_tracker.sqlalchemy_repository_parts.repository import SQLAlchemyLabTrackerRepository


class FakeBatchDraftClient:
    provider = "fake"
    model = "fake-batch-model"

    def __init__(
        self,
        patch: dict[str, Any] | GraphDraftBatchResult | None = None,
        *,
        fail_attempts: int = 0,
        error: str = "temporary model outage",
        augment_dispositions: bool = True,
    ) -> None:
        # Like a well-behaved drafter, the fake fills the per-note disposition
        # checklist for any patch that lacks one. Coverage-gate tests pass
        # augment_dispositions=False to submit contract-violating patches.
        self._augment_dispositions = augment_dispositions
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
    ) -> dict[str, Any] | GraphDraftBatchResult:
        self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
        if len(self.calls) <= self.fail_attempts:
            raise GraphDraftingError(self.error)
        if not self._augment_dispositions:
            return self.patch
        if isinstance(self.patch, GraphDraftBatchResult):
            graph_patch = self.patch.graph_patch
            if isinstance(graph_patch, dict) and "note_dispositions" not in graph_patch:
                return GraphDraftBatchResult(
                    graph_patch=_with_fake_dispositions(graph_patch, batch_context),
                    tool_trace=self.patch.tool_trace,
                )
            return self.patch
        if isinstance(self.patch, dict) and "note_dispositions" not in self.patch:
            return _with_fake_dispositions(self.patch, batch_context)
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


def _checklist_note_ids(batch_context: dict[str, Any]) -> list[str]:
    checklist = batch_context.get("note_ids_requiring_disposition")
    if checklist is None:
        checklist = [
            note.get("id")
            for note in batch_context.get("batch_notes") or []
            if isinstance(note, dict) and note.get("id")
        ]
    return [str(note_id) for note_id in checklist]


def _with_fake_dispositions(
    graph_patch: dict[str, Any],
    batch_context: dict[str, Any],
) -> dict[str, Any]:
    # Content-omitted/redacted notes must attest insufficient_info (the
    # coverage gate's honeypot rule); everything else gets no_change.
    unavailable = {
        str(note.get("id"))
        for note in batch_context.get("batch_notes") or []
        if isinstance(note, dict)
        and (note.get("sensitive_content_omitted") or note.get("sensitive_content_redacted"))
    }
    return {
        **graph_patch,
        "note_dispositions": [
            {
                "note_id": note_id,
                "disposition": (
                    "insufficient_info" if note_id in unavailable else "no_change"
                ),
                "reason": "considered by fake drafter",
                "evidence_quote": "",
                "client_refs": [],
            }
            for note_id in _checklist_note_ids(batch_context)
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


def test_run_now_persists_agentic_tool_trace(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _note(client, admin_auth_headers, project_id, "Odor tracking looked stable.")
    trace = {
        "provider": "anthropic",
        "tool_call_count": 1,
        "max_tool_calls": 4,
        "tool_calls": [
            {
                "tool": "search",
                "arguments": {"query": "odor"},
                "result_ids": [{"field": "note_id", "value": note_id}],
            }
        ],
    }
    fake_client = FakeBatchDraftClient(
        GraphDraftBatchResult(graph_patch=_batch_patch(project_id), tool_trace=trace)
    )
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    run = response.json()["data"]
    draft = client.get(f"/batches/{run['change_set_id']}", headers=admin_auth_headers)
    assert draft.status_code == 200, draft.text
    assert draft.json()["data"]["context_packet"]["agentic_tool_trace"] == trace


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


def test_external_harness_background_run_now_stamps_actor_as_reviewer(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Queued harness note.")
    client.app.state.settings.graph_draft_provider = "external_harness"
    client.app.state.settings.graph_draft_background_enabled = True

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201, response.text
    queued = response.json()["data"]
    assert queued["status"] == "pending"
    assert queued["review_assignee_user_id"]
    assert queued["review_assignee"] == queued["review_assignee_user_id"]


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


def test_per_user_batch_settings_validate_reviewer_project_access(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _outsider_headers, outsider_id = _registered_user(client, role=Role.VIEWER)

    updated = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True, "user_id": outsider_id},
        headers=admin_auth_headers,
    )

    assert updated.status_code == 422
    assert "cannot read the batch project" in updated.json()["error"]["message"]


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


def test_run_due_skips_reviewer_who_lost_project_access(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer_headers, reviewer_user_id = _registered_user(client, role=Role.VIEWER)
    added = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": reviewer_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 201, added.text
    note_id = _note(client, reviewer_headers, project_id, "Departing reviewer note.")
    settings = client.patch(
        f"/projects/{project_id}/graph-draft-batch-settings",
        json={"enabled": True},
        headers=admin_auth_headers,
    )
    assert settings.status_code == 200
    with client.app.state.db_session_factory() as session:
        membership = session.scalar(
            select(ProjectMembershipModel).where(
                ProjectMembershipModel.project_id == str(project_id),
                ProjectMembershipModel.user_id == reviewer_user_id,
            )
        )
        assert membership is not None
        session.delete(membership)
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

    assert response.status_code == 200, response.text
    runs = response.json()["data"]
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "skipped"
    assert run["change_set_id"] is None
    assert run["source_note_ids"] == []
    assert run["review_assignee_user_id"] == reviewer_user_id
    assert run["error_metadata"]["category"] == "reviewer_unavailable"
    assert "cannot read" in run["error_metadata"]["message"]
    assert fake_client.calls == []

    with client.app.state.db_session_factory() as session:
        row = session.get(NoteModel, note_id)
        assert row is not None


def test_scoped_graph_draft_read_tools_enforce_scope_sensitivity_and_allowlist(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_a = _project(client, admin_auth_headers)
    project_b = _project(client, admin_auth_headers)
    _viewer_headers, viewer_user_id = _registered_user(client, role=Role.VIEWER)
    added = client.post(
        f"/projects/{project_a}/members",
        json={"user_id": viewer_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 201, added.text
    note = client.post(
        "/notes",
        json={
            "project_id": project_a,
            "raw_content": "Embargoed odor observation",
            "transcribed_text": "Sensitive transcript",
            "metadata": {SENSITIVITY_METADATA_KEY: SENSITIVE_NOTE_VALUE},
            "status": "staged",
        },
        headers=admin_auth_headers,
    )
    assert note.status_code == 201, note.text

    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        executor = ScopedGraphDraftReadToolExecutor(
            repository=repository,
            authorization=api.project_authorization,
            project_id=UUID(project_a),
            target_user_id=UUID(viewer_user_id),
            sensitivity_policy="redact",
            goals=api.goals,
            publication_readiness=api.publication_readiness,
        )

        result = executor.execute("list_notes", {"project_id": project_a}).payload
        redacted_note = result["data"][0]
        assert redacted_note["raw_content"] == SENSITIVE_NOTE_REDACTION
        assert redacted_note["transcribed_text"] == SENSITIVE_NOTE_REDACTION
        assert redacted_note["sensitive_content_redacted"] is True

        with pytest.raises(GraphDraftingError, match="outside the graph draft read scope"):
            executor.execute("list_notes", {"project_id": project_b})
        with pytest.raises(GraphDraftingError, match="not allowlisted"):
            executor.execute("resolve_artifact", {})


def test_external_harness_mcp_surface_is_scoped_omit_only_and_propose_only(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_a = _project(client, admin_auth_headers)
    project_b = _project(client, admin_auth_headers)
    _viewer_headers, viewer_user_id = _registered_user(client, role=Role.VIEWER)
    added = client.post(
        f"/projects/{project_a}/members",
        json={"user_id": viewer_user_id, "role": "viewer"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 201, added.text
    note = client.post(
        "/notes",
        json={
            "project_id": project_a,
            "raw_content": "Embargoed odor observation",
            "transcribed_text": "Sensitive transcript",
            "metadata": {SENSITIVITY_METADATA_KEY: SENSITIVE_NOTE_VALUE},
            "status": "staged",
        },
        headers=admin_auth_headers,
    )
    assert note.status_code == 201, note.text

    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        executor = ScopedGraphDraftReadToolExecutor(
            repository=repository,
            authorization=api.project_authorization,
            project_id=UUID(project_a),
            target_user_id=UUID(viewer_user_id),
            sensitivity_policy="redact",
            goals=api.goals,
            publication_readiness=api.publication_readiness,
        )
        harness_mcp = HarnessGraphDraftMCPServer(executor=executor, max_tool_calls=4)

        tool_names = {item["name"] for item in harness_mcp.tool_specs()}
        assert tool_names == set(AGENTIC_READ_TOOL_ALLOWLIST) | {SUBMIT_GRAPH_PATCH_TOOL}
        assert "resolve_artifact" not in tool_names
        assert not any(name.startswith("create_") for name in tool_names)

        fastmcp_names = {
            tool.name
            for tool in asyncio.run(harness_mcp.build_fastmcp_server().list_tools())
        }
        assert fastmcp_names == tool_names

        result = harness_mcp.execute_tool("list_notes", {"project_id": project_a})
        omitted_note = result["data"][0]
        assert omitted_note["sensitive_content_omitted"] is True
        assert "raw_content" not in omitted_note
        assert executor.sensitivity_policy == "omit"

        with pytest.raises(GraphDraftingError, match="outside the graph draft read scope"):
            harness_mcp.execute_tool("list_notes", {"project_id": project_b})
        with pytest.raises(GraphDraftingError, match="not registered"):
            harness_mcp.execute_tool("resolve_artifact", {})

        submitted = harness_mcp.execute_tool(
            SUBMIT_GRAPH_PATCH_TOOL,
            {
                "graph_patch": {
                    "summary": "no-op",
                    "uncertain_fields": [],
                    "clarification_requests": [],
                    "operations": [],
                }
            },
        )
        assert submitted == {"accepted": True, "propose_only": True, "operation_count": 0}


def test_agentic_live_read_tools_fail_closed_without_review_assignee(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Scheduled note without reviewer.")

    class LiveToolClient(FakeBatchDraftClient):
        provider = "agentic"
        requires_background_worker = True
        _tool_loop_enabled = True

        def configure_live_read_tools(self, executor) -> None:  # noqa: ANN001
            raise AssertionError("unassigned live runs must fail before executor injection")

    fake_client = LiveToolClient(_batch_patch(project_id))
    with client.app.state.db_session_factory() as session:
        api = client.app.state.lab_tracker_api.for_request(
            SQLAlchemyLabTrackerRepository(session)
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=fake_client,
            actor=system_auth_context(),
        )

    assert run.status.value == "failed"
    assert run.change_set_id is None
    assert fake_client.calls == []
    assert "review_assignee_user_id" in run.error_metadata["message"]


def test_external_harness_background_run_lands_proposals_through_existing_pipeline(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer_headers, reviewer_user_id = _registered_user(client, role=Role.VIEWER)
    added = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": reviewer_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 201, added.text
    _note(client, reviewer_headers, project_id, "Daily note about odor panel.")
    patch = _batch_patch(project_id)

    class FakeHarnessRunner:
        def run(self, *, request, mcp_server):  # noqa: ANN001
            assert request.launch.selector == "codex"
            assert request.sandbox_profile == "operator_managed"
            assert request.egress_profile == "vendor_api_only"
            read_result = mcp_server.execute_tool(
                "list_notes",
                {"project_id": project_id, "limit": 1},
            )
            assert read_result["data"]
            mcp_server.execute_tool(
                SUBMIT_GRAPH_PATCH_TOOL,
                {"graph_patch": _with_fake_dispositions(patch, request.batch_context)},
            )
            return HarnessDraftRunResult(tool_trace=mcp_server.tool_trace)

    draft_client = HarnessGraphDraftClient(
        launch=EXTERNAL_HARNESS_LAUNCH_TABLE["codex"],
        enabled=True,
        sandbox_profile="operator_managed",
        egress_profile="vendor_api_only",
        timeout_seconds=30,
        max_tool_calls=4,
        max_stdout_bytes=4096,
        runner=FakeHarnessRunner(),
    )

    with client.app.state.db_session_factory() as session:
        api = client.app.state.lab_tracker_api.for_request(
            SQLAlchemyLabTrackerRepository(session)
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=draft_client,
            actor=system_auth_context(),
            review_assignee=reviewer_user_id,
            review_assignee_user_id=UUID(reviewer_user_id),
        )
        change_set = api.get_graph_change_set(run.change_set_id)

    assert run.status.value == "ready"
    assert run.change_set_id is not None
    assert change_set.provider == "external_harness"
    assert change_set.status.value == "ready"
    assert change_set.context_packet["agentic_tool_trace"]["provider"] == "external_harness"
    assert change_set.context_packet["agentic_tool_trace"]["tool_call_count"] == 1
    assert change_set.operations[0].status.value == "proposed"
    assert change_set.operations[0].acceptance_mode is None


def test_external_harness_failure_does_not_retry_full_subprocess_spawn(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    reviewer_headers, reviewer_user_id = _registered_user(client, role=Role.VIEWER)
    added = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": reviewer_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert added.status_code == 201, added.text
    _note(client, reviewer_headers, project_id, "Daily note for failing harness.")

    class FailingHarnessRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, *, request, mcp_server):  # noqa: ANN001
            self.calls += 1
            raise GraphDraftingError("harness failed once")

    runner = FailingHarnessRunner()
    draft_client = HarnessGraphDraftClient(
        launch=EXTERNAL_HARNESS_LAUNCH_TABLE["codex"],
        enabled=True,
        sandbox_profile="operator_managed",
        egress_profile="vendor_api_only",
        timeout_seconds=30,
        max_tool_calls=4,
        max_stdout_bytes=4096,
        runner=runner,
    )

    with client.app.state.db_session_factory() as session:
        api = client.app.state.lab_tracker_api.for_request(
            SQLAlchemyLabTrackerRepository(session)
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id),
            draft_client=draft_client,
            actor=system_auth_context(),
            review_assignee=reviewer_user_id,
            review_assignee_user_id=UUID(reviewer_user_id),
        )

    assert run.status.value == "failed"
    assert runner.calls == 1
    assert run.error_metadata["attempts"] == 1
    assert run.error_metadata["message"] == "harness failed once"


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
                "note_dispositions": [
                    {
                        "note_id": str(note_id),
                        "disposition": "no_change",
                        "reason": "considered by fake drafter",
                        "evidence_quote": "",
                        "client_refs": [],
                    }
                    for note_id in batch_context.get("note_ids_requiring_disposition") or []
                ],
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


def test_batch_coverage_violation_gets_targeted_repair_hint(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    """Gate 2: a coverage violation is retried with a hint naming the gap."""
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Coverage repair note.")

    class RepairingClient:
        provider = "fake"
        model = "fake-batch-model"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def draft_from_batch(
            self,
            *,
            batch_context: dict[str, Any],
            user_hint: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
            base = {
                "summary": "empty",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [],
            }
            if len(self.calls) == 1:
                return base  # violates coverage: no note_dispositions
            return _with_fake_dispositions(base, batch_context)

        def close(self) -> None:
            return None

    repairing = RepairingClient()
    client.app.state.graph_draft_client_factory = lambda settings: repairing

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "merge my notes"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["data"]["status"] == "ready"
    assert len(repairing.calls) == 2
    assert repairing.calls[0]["user_hint"] == "merge my notes"
    repair_hint = repairing.calls[1]["user_hint"]
    assert "merge my notes" in repair_hint
    assert "note_dispositions coverage contract" in repair_hint
    checklist = repairing.calls[0]["batch_context"]["note_ids_requiring_disposition"]
    assert checklist and checklist[0] in repair_hint


def test_batch_coverage_violation_exhausts_retries_with_coverage_error(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    """Gate 2: persistent violations fail the run with machine-readable gaps."""
    project_id = _project(client, admin_auth_headers)
    note_id = _note(client, admin_auth_headers, project_id, "Silently skipped note.")
    silent_client = FakeBatchDraftClient(augment_dispositions=False)
    client.app.state.graph_draft_client_factory = lambda settings: silent_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    failed_run = response.json()["data"]
    assert failed_run["status"] == "failed"
    assert failed_run["error_metadata"]["category"] == "coverage_error"
    assert failed_run["error_metadata"]["missing_note_ids"] == [note_id]
    # Every retry attempt saw the same violation before the run failed.
    assert len(silent_client.calls) > 1


def test_batch_coverage_exhaustion_preserves_agentic_tool_trace(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    """A coverage_error failure keeps the failing attempt's read audit trail."""
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Uncovered note with a tool trace.")
    trace = {"provider": "agentic", "tool_call_count": 2, "tool_calls": []}
    silent_client = FakeBatchDraftClient(
        GraphDraftBatchResult(
            graph_patch={
                "summary": "empty",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [],
            },
            tool_trace=trace,
        ),
        augment_dispositions=False,
    )
    client.app.state.graph_draft_client_factory = lambda settings: silent_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    failed_run = response.json()["data"]
    assert failed_run["status"] == "failed"
    assert failed_run["error_metadata"]["category"] == "coverage_error"
    assert failed_run["error_metadata"]["agentic_tool_trace"] == trace


def test_batch_repair_hint_resets_after_non_coverage_error(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    """A stale coverage repair hint must not survive an unrelated model error."""
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Hint hygiene note.")

    class FlakyClient:
        provider = "fake"
        model = "fake-batch-model"

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def draft_from_batch(
            self,
            *,
            batch_context: dict[str, Any],
            user_hint: str | None = None,
        ) -> dict[str, Any]:
            self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
            base = {
                "summary": "empty",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": [],
            }
            if len(self.calls) == 1:
                return base  # coverage violation
            if len(self.calls) == 2:
                raise GraphDraftingError("temporary model outage")
            return _with_fake_dispositions(base, batch_context)

        def close(self) -> None:
            return None

    flaky = FlakyClient()
    client.app.state.graph_draft_client_factory = lambda settings: flaky

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id, "user_hint": "base hint"},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    assert response.json()["data"]["status"] == "ready"
    assert len(flaky.calls) == 3
    assert flaky.calls[0]["user_hint"] == "base hint"
    assert "coverage contract" in flaky.calls[1]["user_hint"]
    # The model error on attempt 2 invalidates the repair claim for attempt 3.
    assert flaky.calls[2]["user_hint"] == "base hint"


def test_batch_ledger_persists_dispositions_and_coverage_summary(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_a = _note(client, admin_auth_headers, project_id, "Ledger note A.")
    note_b = _note(client, admin_auth_headers, project_id, "Ledger note B.")
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda settings: fake_client

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "ready"
    draft = client.get(f"/batches/{run['change_set_id']}", headers=admin_auth_headers)
    data = draft.json()["data"]
    ledger = data["note_dispositions"]
    assert {entry["note_id"] for entry in ledger} == {note_a, note_b}
    assert all(entry["disposition"] == "no_change" for entry in ledger)
    # The list surface carries the aggregated coverage counts for its chip.
    listing = client.get(
        "/graph-drafts",
        params={"project_id": project_id},
        headers=admin_auth_headers,
    )
    summaries = [
        item
        for item in listing.json()["data"]
        if item["change_set_id"] == run["change_set_id"]
    ]
    assert summaries, listing.json()
    assert summaries[0]["coverage_summary"] == {"no_change": 2, "total": 2}
    # The lean list surface carries only the aggregate; the ledger itself
    # rides the detail endpoints.
    assert "note_dispositions" not in summaries[0]


def test_batch_ledger_marks_unpresented_notes_and_scopes_provenance(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    """Notes dropped before drafting get server-written not_presented rows and
    are never stamped into operation provenance."""
    from lab_tracker.services import graph_draft_service

    monkeypatch.setattr(graph_draft_service, "_BATCH_NOTE_LIMIT", 2)
    project_id = _project(client, admin_auth_headers)
    note_ids = [
        _note(client, admin_auth_headers, project_id, f"Overflow note {index}.")
        for index in range(4)
    ]
    fake_client = FakeBatchDraftClient(_batch_patch(project_id))

    with client.app.state.db_session_factory() as session:
        api = client.app.state.lab_tracker_api.for_request(
            SQLAlchemyLabTrackerRepository(session)
        )
        notes = [api.get_note(UUID(note_id)) for note_id in note_ids]
        change_set = api.create_batch_graph_draft(
            notes,
            draft_client=fake_client,
            actor=system_auth_context(),
        )
        session.commit()

    assert change_set.status.value == "ready"
    presented = fake_client.calls[0]["batch_context"]["note_ids_requiring_disposition"]
    assert len(presented) == 2
    ledger = change_set.note_dispositions
    not_presented = [e for e in ledger if e["disposition"] == "not_presented"]
    assert {e["note_id"] for e in not_presented} == set(note_ids) - set(presented)
    assert all(
        e["reason"] == "truncated from context packet before drafting" for e in not_presented
    )
    agent_entries = [e for e in ledger if e["disposition"] != "not_presented"]
    assert {e["note_id"] for e in agent_entries} == set(presented)
    # Operation provenance falls back to the PRESENTED notes only — unseen
    # overflow notes are never stamped as sources.
    assert change_set.operations[0].source_refs[0]["source_note_ids"] == presented


def test_batch_ledger_links_ops_to_citing_notes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    """proposed_change client_refs give per-op source attribution."""
    project_id = _project(client, admin_auth_headers)
    note_a = _note(client, admin_auth_headers, project_id, "Note that produced the op.")
    note_b = _note(client, admin_auth_headers, project_id, "Unrelated note.")
    patch = _batch_patch(project_id)

    class CitingClient:
        provider = "fake"
        model = "fake-batch-model"

        def draft_from_batch(
            self,
            *,
            batch_context: dict[str, Any],
            user_hint: str | None = None,
        ) -> dict[str, Any]:
            entries = []
            for note_id in _checklist_note_ids(batch_context):
                if note_id == note_a:
                    entries.append(
                        {
                            "note_id": note_id,
                            "disposition": "proposed_change",
                            "reason": "this capture states the follow-up question",
                            "evidence_quote": "",
                            "client_refs": ["batch_question"],
                        }
                    )
                else:
                    entries.append(
                        {
                            "note_id": note_id,
                            "disposition": "no_change",
                            "reason": "considered; nothing to add",
                            "evidence_quote": "",
                            "client_refs": [],
                        }
                    )
            return {**patch, "note_dispositions": entries}

        def close(self) -> None:
            return None

    client.app.state.graph_draft_client_factory = lambda settings: CitingClient()

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "ready"
    with client.app.state.db_session_factory() as session:
        api = client.app.state.lab_tracker_api.for_request(
            SQLAlchemyLabTrackerRepository(session)
        )
        change_set = api.get_graph_change_set(UUID(run["change_set_id"]))
    operation = change_set.operations[0]
    assert operation.client_ref == "batch_question"
    assert operation.source_refs[0]["source_note_ids"] == [note_a]
    assert note_b not in operation.source_refs[0]["source_note_ids"]


class _QuotingClient:
    """Emits one grounded and one fabricated evidence quote per batch."""

    provider = "fake"
    model = "fake-batch-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def draft_from_batch(
        self,
        *,
        batch_context: dict[str, Any],
        user_hint: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"batch_context": batch_context, "user_hint": user_hint})
        previews = {
            str(note.get("id")): str(note.get("preview") or "")
            for note in batch_context.get("batch_notes") or []
            if isinstance(note, dict)
        }
        entries = []
        for index, note_id in enumerate(_checklist_note_ids(batch_context)):
            grounded = index == 0
            preview = previews.get(note_id, "")
            entries.append(
                {
                    "note_id": note_id,
                    "disposition": "no_change",
                    "reason": f"considered capture {index}",
                    "evidence_quote": (
                        preview[:20] if grounded else "fabricated claim never delivered"
                    ),
                    "client_refs": [],
                }
            )
        return {
            "summary": "empty",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [],
            "note_dispositions": entries,
        }

    def close(self) -> None:
        return None


def test_batch_evidence_grounding_warn_mode_stamps_attestation(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_a = _note(client, admin_auth_headers, project_id, "Gel lane 2 looked clean today.")
    note_b = _note(client, admin_auth_headers, project_id, "Rig 4 hums at startup.")
    client.app.state.graph_draft_client_factory = lambda settings: _QuotingClient()

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    run = response.json()["data"]
    assert run["status"] == "ready"
    draft = client.get(f"/batches/{run['change_set_id']}", headers=admin_auth_headers)
    ledger = {entry["note_id"]: entry for entry in draft.json()["data"]["note_dispositions"]}
    verified_flags = {
        note_id: entry["attestation_verified"] for note_id, entry in ledger.items()
    }
    # Exactly one quote is verbatim from its note's delivered preview.
    assert sorted(verified_flags.values()) == [False, True]
    assert set(verified_flags) == {note_a, note_b}


def test_batch_evidence_grounding_enforce_mode_rejects_fabricated_quotes(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "Only note in this window.")
    _note(client, admin_auth_headers, project_id, "Second note in this window.")
    monkeypatch.setattr(
        client.app.state.settings, "graph_draft_evidence_grounding", "enforce"
    )
    quoting = _QuotingClient()
    client.app.state.graph_draft_client_factory = lambda settings: quoting

    response = client.post(
        "/batches/run-now",
        json={"project_id": project_id},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    failed_run = response.json()["data"]
    assert failed_run["status"] == "failed"
    assert failed_run["error_metadata"]["category"] == "coverage_error"
    assert "not a verbatim snippet" in failed_run["error_metadata"]["message"]
    # The violation was retried with a repair hint before failing.
    assert len(quoting.calls) > 1
    assert "not a verbatim snippet" in (quoting.calls[1]["user_hint"] or "")
