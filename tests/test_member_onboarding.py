from __future__ import annotations

import json
from datetime import timedelta
from threading import Event, Thread
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.auth import AuthContext, PrincipalType, Role
from lab_tracker.db_models import NoteModel
from lab_tracker.errors import AuthError, ValidationError
from lab_tracker.member_onboarding import (
    ALIGNMENT_MODE_KEY,
    ALIGNMENT_PAYLOAD_HASH_KEY,
    ALIGNMENT_RESOLUTIONS_KEY,
    ALIGNMENT_RESOLVED_AT_KEY,
    FIRST_CAPTURE_NOTE_ID_KEY,
)
from lab_tracker.models import (
    EntityOrigin,
    EntityRef,
    EntityType,
    GraphChangeSetStatus,
    Note,
    NoteStatus,
    utc_now,
)
from lab_tracker.schemas import MemberOnboardingCheckpointRequest
from lab_tracker.services.graph_draft_batch_policy import staged_notes_in_window
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


def _checkpoint_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "current_output_or_decision": "We selected the low-light assay.",
        "live_questions": ["Does the assay preserve response fidelity?"],
        "strongest_recent_context": "Pilot 4 was stable across two runs.",
        "next_move": "Repeat with the blinded batch.",
        "source_text": "Full historical handoff text.",
    }
    payload.update(overrides)
    return payload


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(
        "/projects",
        json={"name": f"Ongoing assay {uuid4().hex[:8]}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _checkpoint(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    **overrides: Any,
) -> dict[str, Any]:
    response = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json=_checkpoint_payload(**overrides),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _add_member(
    client: TestClient,
    owner_headers: dict[str, str],
    project_id: str,
    user_id: str,
    role: str,
) -> None:
    response = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": user_id, "role": role},
        headers=owner_headers,
    )
    assert response.status_code == 201, response.text


def _register_user(client: TestClient, prefix: str) -> tuple[dict[str, str], str]:
    username = f"{prefix}-{uuid4().hex[:8]}"
    password = "secret"
    user = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.VIEWER,
    )
    login = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login.status_code == 200
    return (
        {"Authorization": f"Bearer {login.json()['data']['access_token']}"},
        str(user.user_id),
    )


def _pair_device(
    client: TestClient,
    user_headers: dict[str, str],
) -> dict[str, str]:
    enrollment = client.post(
        "/auth/devices/enrollment",
        json={},
        headers=user_headers,
    )
    assert enrollment.status_code == 201, enrollment.text
    consumed = client.post(
        "/auth/devices/consume",
        json={
            "offer_token": enrollment.json()["data"]["offer_token"],
            "label": "Onboarding capture device",
        },
    )
    assert consumed.status_code == 201, consumed.text
    return {"Authorization": f"Bearer {consumed.json()['data']['secret']}"}


class FakeOnboardingDraftClient:
    provider = "fake"
    model = "fake-member-alignment"
    timeout_seconds = 1

    def __init__(self, patch: dict[str, Any] | Exception) -> None:
        self.patch = patch
        self.calls = 0

    def draft_from_note(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        if isinstance(self.patch, Exception):
            raise self.patch
        return self.patch

    def close(self) -> None:
        return None


def _create_question_patch(project_id: str, checkpoint_id: str) -> dict[str, Any]:
    return {
        "summary": "Align the live question.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "live_question_0",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": "Does the assay preserve response fidelity?",
                        "question_type": "other",
                        "status": "staged",
                    }
                ),
                "rationale": "The member named this as live.",
                "confidence": 0.8,
                "source_refs": [{"source_note_ids": [checkpoint_id]}],
            }
        ],
    }


def _link_question_patch(checkpoint_id: str, question_id: str) -> dict[str, Any]:
    return {
        "summary": "Link the checkpoint to the existing live question.",
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "live_question_0",
                "op": "update",
                "entity_type": "note",
                "semantic_type": "link_note_to_question",
                "target_entity_id": checkpoint_id,
                "payload_json": json.dumps(
                    {"targets": [{"entity_type": "question", "entity_id": question_id}]}
                ),
                "rationale": "The existing question matches the member's live work.",
                "confidence": 0.9,
                "source_refs": [{"source_note_ids": [checkpoint_id]}],
            }
        ],
    }


def test_checkpoint_exact_replay_changed_conflict_and_reserved_note_guards(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    created = _checkpoint(client, admin_auth_headers, project_id)
    checkpoint = created["checkpoint"]

    replay = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json=_checkpoint_payload(),
        headers=admin_auth_headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["checkpoint"]["note_id"] == checkpoint["note_id"]

    changed = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json=_checkpoint_payload(next_move="Changed next move"),
        headers=admin_auth_headers,
    )
    assert changed.status_code == 409

    forged = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Not a checkpoint",
            "metadata": {"member_onboarding_role": "checkpoint"},
        },
        headers=admin_auth_headers,
    )
    assert forged.status_code == 422
    ordinary = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Ordinary capture"},
        headers=admin_auth_headers,
    )
    assert ordinary.status_code == 201
    ordinary_id = ordinary.json()["data"]["note_id"]
    forged_update = client.patch(
        f"/notes/{ordinary_id}",
        json={"metadata": {"scheduled_graph_draft_policy": "exclude"}},
        headers=admin_auth_headers,
    )
    assert forged_update.status_code == 422

    for method, path, body in (
        ("patch", f"/notes/{checkpoint['note_id']}", {"metadata": {}}),
        (
            "post",
            f"/notes/{checkpoint['note_id']}/archive",
            {"reason": "archived_unreviewed"},
        ),
        ("delete", f"/notes/{checkpoint['note_id']}", None),
    ):
        response = client.request(method.upper(), path, json=body, headers=admin_auth_headers)
        assert response.status_code == 422, response.text


def test_viewer_and_noninteractive_principal_are_read_only(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    viewer_user,
) -> None:
    project_id = _project(client, admin_auth_headers)
    _add_member(
        client,
        admin_auth_headers,
        project_id,
        viewer_user.user_id,
        "viewer",
    )
    readable = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=viewer_user.headers,
    )
    assert readable.status_code == 200
    assert readable.json()["data"]["capabilities"]["can_create_checkpoint"] is False
    denied = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json=_checkpoint_payload(),
        headers=viewer_user.headers,
    )
    assert denied.status_code == 401

    actor = AuthContext(
        user_id=uuid4(),
        role=Role.ADMIN,
        principal_type=PrincipalType.SERVICE,
    )
    with pytest.raises(AuthError):
        client.app.state.lab_tracker_api.member_onboarding.put_checkpoint(
            UUID(project_id),
            MemberOnboardingCheckpointRequest.model_validate(_checkpoint_payload()),
            actor=actor,
        )


def test_device_capabilities_match_capture_only_middleware(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    checkpoint = _checkpoint(client, admin_auth_headers, project_id)["checkpoint"]
    device_headers = _pair_device(client, admin_auth_headers)

    readable = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=device_headers,
    )
    assert readable.status_code == 200, readable.text
    assert readable.json()["data"]["capabilities"] == {
        "can_read": True,
        "can_create_checkpoint": False,
        "can_align": False,
        "can_capture": True,
        "can_commit": False,
    }

    forbidden_requests = (
        (
            "PUT",
            f"/projects/{project_id}/member-onboarding/checkpoint",
            _checkpoint_payload(),
        ),
        (
            "PUT",
            f"/projects/{project_id}/member-onboarding/manual-alignment",
            {"resolutions": [{"question_index": 0, "action": "checkpoint_only"}]},
        ),
        (
            "POST",
            f"/projects/{project_id}/member-onboarding/ai-alignment",
            {"external_provider_acknowledged": True},
        ),
        (
            "POST",
            f"/graph-drafts/{uuid4()}/commit",
            {"message": "Device commit must remain forbidden."},
        ),
    )
    for method, path, payload in forbidden_requests:
        denied = client.request(method, path, json=payload, headers=device_headers)
        assert denied.status_code == 403, denied.text

    capture = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Forward capture from the paired device.",
            "targets": [{"entity_type": "note", "entity_id": checkpoint["note_id"]}],
        },
        headers=device_headers,
    )
    assert capture.status_code == 201, capture.text
    current = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=device_headers,
    )
    assert current.status_code == 200, current.text
    assert current.json()["data"]["first_capture"]["note_id"] == capture.json()["data"]["note_id"]


def test_manual_alignment_requires_every_resolution_and_separate_same_author_capture(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    state = _checkpoint(
        client,
        admin_auth_headers,
        project_id,
        live_questions=["Question one?", "Question two?"],
    )
    checkpoint_id = state["checkpoint"]["note_id"]
    partial = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json={"resolutions": [{"question_index": 0, "action": "checkpoint_only"}]},
        headers=admin_auth_headers,
    )
    assert partial.status_code == 422
    aligned = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json={
            "resolutions": [
                {"question_index": 0, "action": "create_staged"},
                {"question_index": 1, "action": "checkpoint_only"},
            ]
        },
        headers=admin_auth_headers,
    )
    assert aligned.status_code == 200, aligned.text
    body = aligned.json()["data"]
    assert body["state"] == "capture_pending"
    assert body["member_complete"] is False
    assert body["map_items"][0]["status"] == "staged"
    assert body["map_items"][1]["status"] == "checkpoint_only"

    capture = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "First genuine forward capture.",
            "targets": [{"entity_type": "note", "entity_id": checkpoint_id}],
            "client_capture_id": "first-forward-capture",
        },
        headers=admin_auth_headers,
    )
    assert capture.status_code == 201, capture.text
    replay = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "First genuine forward capture.",
            "targets": [{"entity_type": "note", "entity_id": checkpoint_id}],
            "client_capture_id": "first-forward-capture",
        },
        headers=admin_auth_headers,
    )
    assert replay.status_code == 200
    completed = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=admin_auth_headers,
    ).json()["data"]
    assert completed["state"] == "complete"
    assert completed["first_capture"]["note_id"] == capture.json()["data"]["note_id"]

    for method, path, body in (
        (
            "patch",
            f"/notes/{capture.json()['data']['note_id']}",
            {"targets": [{"entity_type": "project", "entity_id": project_id}]},
        ),
        (
            "post",
            f"/notes/{capture.json()['data']['note_id']}/archive",
            {"reason": "archived_unreviewed"},
        ),
        ("delete", f"/notes/{capture.json()['data']['note_id']}", None),
    ):
        protected = client.request(
            method.upper(),
            path,
            json=body,
            headers=admin_auth_headers,
        )
        assert protected.status_code == 422, protected.text


def test_ai_alignment_consent_constraints_zero_accepted_and_owner_queue(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    state = _checkpoint(client, admin_auth_headers, project_id)
    checkpoint_id = state["checkpoint"]["note_id"]
    fake = FakeOnboardingDraftClient(_create_question_patch(project_id, checkpoint_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    aligned = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=admin_auth_headers,
    )
    assert aligned.status_code == 200, aligned.text
    draft = aligned.json()["data"]["alignment"]["draft"]
    assert draft["purpose"] == "member_checkpoint_alignment"
    consent = draft["context_packet"]["external_provider_acknowledgement"]
    assert consent["acknowledged"] is True
    assert consent["actor_user_id"]

    bulk = client.post(
        f"/graph-drafts/{draft['change_set_id']}/accept-all",
        headers=admin_auth_headers,
    )
    assert bulk.status_code == 422
    unresolved = client.post(
        f"/graph-drafts/{draft['change_set_id']}/submit",
        headers=admin_auth_headers,
    )
    assert unresolved.status_code == 422
    rejected = client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/"
        f"{draft['operations'][0]['operation_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 200, rejected.text
    submitted = client.post(
        f"/graph-drafts/{draft['change_set_id']}/submit",
        headers=admin_auth_headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "rejected"
    current = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=admin_auth_headers,
    ).json()["data"]
    assert current["owner_commit_pending"] is False
    assert current["state"] == "rejected"
    assert current["checkpoint"]["metadata"]["member_onboarding_alignment_mode"] == "ai"
    assert (
        current["checkpoint"]["metadata"]["member_onboarding_alignment_change_set_id"]
        == draft["change_set_id"]
    )
    assert (
        current["checkpoint"]["metadata"]["member_onboarding_alignment_resolution"]
        == "checkpoint_only"
    )
    assert current["checkpoint"]["metadata"]["member_onboarding_alignment_resolved_at"]
    queue = client.get(
        f"/projects/{project_id}/member-onboarding/owner-queue",
        headers=admin_auth_headers,
    )
    assert queue.status_code == 200
    assert queue.json()["data"] == []


def test_ai_runtime_failure_is_terminal_and_manual_fallback_remains_available(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _checkpoint(client, admin_auth_headers, project_id)
    fake = FakeOnboardingDraftClient(RuntimeError("provider timed out"))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    failed = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=admin_auth_headers,
    )
    assert failed.status_code == 200, failed.text
    draft = failed.json()["data"]["alignment"]["draft"]
    assert draft["status"] == "failed"
    assert fake.calls == 3
    fallback = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json={"resolutions": [{"question_index": 0, "action": "checkpoint_only"}]},
        headers=admin_auth_headers,
    )
    assert fallback.status_code == 200, fallback.text


def test_ai_accepted_path_is_author_reviewed_owner_committed_and_additive(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    author_headers, author_id = _register_user(client, "onboarding-author")
    other_headers, other_id = _register_user(client, "other-contributor")
    _add_member(client, admin_auth_headers, project_id, author_id, "contributor")
    _add_member(client, admin_auth_headers, project_id, other_id, "contributor")
    state = _checkpoint(client, author_headers, project_id)
    checkpoint_id = state["checkpoint"]["note_id"]
    existing_question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does the assay preserve response fidelity?",
            "question_type": "other",
            "status": "staged",
        },
        headers=author_headers,
    )
    assert existing_question.status_code == 201, existing_question.text
    question_id = existing_question.json()["data"]["question_id"]
    fake = FakeOnboardingDraftClient(_link_question_patch(checkpoint_id, question_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    aligned = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=author_headers,
    )
    assert aligned.status_code == 200, aligned.text
    draft = aligned.json()["data"]["alignment"]["draft"]
    draft_id = draft["change_set_id"]
    operation_id = draft["operations"][0]["operation_id"]

    ready_commit = client.post(
        f"/graph-drafts/{draft_id}/commit",
        json={"message": "must submit first"},
        headers=admin_auth_headers,
    )
    assert ready_commit.status_code == 422
    for headers in (other_headers, admin_auth_headers):
        denied = client.patch(
            f"/graph-drafts/{draft_id}/operations/{operation_id}",
            json={"status": "accepted"},
            headers=headers,
        )
        assert denied.status_code == 422
    accepted = client.patch(
        f"/graph-drafts/{draft_id}/operations/{operation_id}",
        json={"status": "accepted"},
        headers=author_headers,
    )
    assert accepted.status_code == 200, accepted.text
    submitted = client.post(
        f"/graph-drafts/{draft_id}/submit",
        headers=author_headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "submitted"
    contributor_commit = client.post(
        f"/graph-drafts/{draft_id}/commit",
        json={"message": "not owner"},
        headers=author_headers,
    )
    assert contributor_commit.status_code == 401
    queue = client.get(
        f"/projects/{project_id}/member-onboarding/owner-queue",
        headers=admin_auth_headers,
    )
    assert queue.status_code == 200
    assert [item["draft"]["change_set_id"] for item in queue.json()["data"]] == [draft_id]
    committed = client.post(
        f"/graph-drafts/{draft_id}/commit",
        json={"message": "Owner accepted the member map"},
        headers=admin_auth_headers,
    )
    assert committed.status_code == 200, committed.text
    applied = committed.json()["data"]["operations"][0]
    assert applied["status"] == "applied"
    assert applied["result_entity_id"] == checkpoint_id
    question = client.get(f"/questions/{question_id}", headers=author_headers)
    assert question.status_code == 200
    assert question.json()["data"]["status"] == "staged"
    checkpoint = client.get(f"/notes/{checkpoint_id}", headers=author_headers)
    assert checkpoint.status_code == 200
    checkpoint_data = checkpoint.json()["data"]
    target_pairs = {
        (target["entity_type"], target["entity_id"]) for target in checkpoint_data["targets"]
    }
    assert ("project", project_id) in target_pairs
    assert ("question", question_id) in target_pairs
    assert checkpoint_data["origin"] == "user"
    assert checkpoint_data["change_set_id"] is None


def test_checkpoint_is_excluded_from_generic_drafting_and_manual_replay_conflicts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    state = _checkpoint(client, admin_auth_headers, project_id)
    checkpoint_id = state["checkpoint"]["note_id"]
    fake = FakeOnboardingDraftClient(_create_question_patch(project_id, checkpoint_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    generic = client.post(
        f"/notes/{checkpoint_id}/graph-drafts",
        headers=admin_auth_headers,
    )
    analysis = client.post(
        f"/notes/{checkpoint_id}/analysis-graph-drafts",
        headers=admin_auth_headers,
    )
    assert generic.status_code == analysis.status_code == 422
    assert fake.calls == 0

    payload = {"resolutions": [{"question_index": 0, "action": "checkpoint_only"}]}
    first = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json=payload,
        headers=admin_auth_headers,
    )
    replay = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json=payload,
        headers=admin_auth_headers,
    )
    changed = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json={"resolutions": [{"question_index": 0, "action": "create_staged"}]},
        headers=admin_auth_headers,
    )
    assert first.status_code == replay.status_code == 200
    assert changed.status_code == 409


def test_first_capture_before_ai_submit_completes_on_submit(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    state = _checkpoint(client, admin_auth_headers, project_id)
    checkpoint_id = state["checkpoint"]["note_id"]
    capture = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Captured before alignment resolved.",
            "targets": [{"entity_type": "note", "entity_id": checkpoint_id}],
        },
        headers=admin_auth_headers,
    )
    assert capture.status_code == 201
    fake = FakeOnboardingDraftClient(_create_question_patch(project_id, checkpoint_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    aligned = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=admin_auth_headers,
    ).json()["data"]
    draft = aligned["alignment"]["draft"]
    client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/"
        f"{draft['operations'][0]['operation_id']}",
        json={"status": "rejected"},
        headers=admin_auth_headers,
    )
    submitted = client.post(
        f"/graph-drafts/{draft['change_set_id']}/submit",
        headers=admin_auth_headers,
    )
    assert submitted.status_code == 200, submitted.text
    complete = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=admin_auth_headers,
    ).json()["data"]
    assert complete["member_complete"] is True
    assert complete["state"] == "complete"
    assert complete["checkpoint"]["metadata"]["member_onboarding_completed_at"]


def test_checkpoint_source_is_bounded_without_truncation(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    too_large = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json=_checkpoint_payload(source_text="x" * 64_000),
        headers=admin_auth_headers,
    )
    assert too_large.status_code == 422
    assert "never silently truncated" in too_large.json()["error"]["message"]


def test_ai_authored_note_does_not_count_as_first_capture(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    checkpoint = _checkpoint(client, admin_auth_headers, project_id)["checkpoint"]
    actor = AuthContext(user_id=UUID(checkpoint["created_by"]), role=Role.ADMIN)
    with client.app.state.db_session_factory() as session:
        api = client.app.state.session_api_factory(session, surface="background")
        generated = api.notes.create_note(
            UUID(project_id),
            "Generated graph-draft note.",
            targets=[
                EntityRef(
                    entity_type=EntityType.NOTE,
                    entity_id=UUID(checkpoint["note_id"]),
                )
            ],
            status=NoteStatus.STAGED,
            actor=actor,
            origin=EntityOrigin.AI_SUGGESTED,
        )
    assert generated.origin == EntityOrigin.AI_SUGGESTED
    current = client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=admin_auth_headers,
    ).json()["data"]
    assert current["first_capture"] is None
    assert current["member_complete"] is False


def test_owner_service_cannot_review_member_alignment(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    state = _checkpoint(client, admin_auth_headers, project_id)
    checkpoint_id = state["checkpoint"]["note_id"]
    fake = FakeOnboardingDraftClient(_create_question_patch(project_id, checkpoint_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    aligned = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=admin_auth_headers,
    ).json()["data"]
    draft = aligned["alignment"]["draft"]
    operation = draft["operations"][0]
    accepted = client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/{operation['operation_id']}",
        json={"status": "accepted"},
        headers=admin_auth_headers,
    )
    assert accepted.status_code == 200
    submitted = client.post(
        f"/graph-drafts/{draft['change_set_id']}/submit",
        headers=admin_auth_headers,
    )
    assert submitted.status_code == 200
    service_actor = AuthContext(
        user_id=UUID(state["checkpoint"]["created_by"]),
        role=Role.ADMIN,
        principal_type=PrincipalType.SERVICE,
    )
    with client.app.state.db_session_factory() as session:
        api = client.app.state.session_api_factory(session, surface="background")
        with pytest.raises(AuthError):
            api.graph_drafts.review_graph_change_set(
                UUID(draft["change_set_id"]),
                status=GraphChangeSetStatus.REJECTED,
                actor=service_actor,
            )


def test_question_link_cannot_be_deleted_while_checkpoint_targets_it(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    _checkpoint(client, admin_auth_headers, project_id)
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Durable linked question?",
            "question_type": "other",
            "status": "staged",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    aligned = client.put(
        f"/projects/{project_id}/member-onboarding/manual-alignment",
        json={
            "resolutions": [
                {
                    "question_index": 0,
                    "action": "link_existing",
                    "existing_question_id": question["question_id"],
                }
            ]
        },
        headers=admin_auth_headers,
    )
    assert aligned.status_code == 200
    deleted = client.delete(
        f"/questions/{question['question_id']}",
        headers=admin_auth_headers,
    )
    assert deleted.status_code == 422
    assert "notes target it" in deleted.json()["error"]["message"]
    checkpoint_id = aligned.json()["data"]["checkpoint"]["note_id"]
    refactored = client.post(
        f"/questions/{question['question_id']}/refactor",
        json={
            "replacement": {
                "text": "Replacement linked question?",
                "question_type": "other",
                "status": "staged",
            },
            "reason": "Try to retarget the immutable checkpoint.",
            "note_ids_to_retarget": [checkpoint_id],
        },
        headers=admin_auth_headers,
    )
    assert refactored.status_code == 422
    assert "checkpoint targets cannot be retargeted" in refactored.json()["error"]["message"]


def test_checkpoint_is_excluded_from_cadence_policy(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    checkpoint = Note.model_validate(
        _checkpoint(client, admin_auth_headers, project_id)["checkpoint"]
    )
    ordinary_response = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Cadence-eligible note."},
        headers=admin_auth_headers,
    )
    assert ordinary_response.status_code == 201
    ordinary = Note.model_validate(ordinary_response.json()["data"])
    eligible = staged_notes_in_window(
        [checkpoint, ordinary],
        since=min(checkpoint.created_at, ordinary.created_at) - timedelta(seconds=1),
        until=max(checkpoint.created_at, ordinary.created_at) + timedelta(seconds=1),
    )
    assert [note.note_id for note in eligible] == [ordinary.note_id]


def test_member_onboarding_usage_events_are_first_only_and_content_free(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    client.app.state.settings.usage_events = True
    project_id = _project(client, admin_auth_headers)
    state = _checkpoint(
        client,
        admin_auth_headers,
        project_id,
        strongest_recent_context="sensitive-research-sentinel",
    )
    checkpoint_id = state["checkpoint"]["note_id"]
    resolution = {"resolutions": [{"question_index": 0, "action": "checkpoint_only"}]}
    for _ in range(2):
        aligned = client.put(
            f"/projects/{project_id}/member-onboarding/manual-alignment",
            json=resolution,
            headers=admin_auth_headers,
        )
        assert aligned.status_code == 200
    capture_payload = {
        "project_id": project_id,
        "raw_content": "sensitive-forward-capture-sentinel",
        "targets": [{"entity_type": "note", "entity_id": checkpoint_id}],
        "client_capture_id": "usage-first-capture",
    }
    created = client.post(
        "/notes",
        json=capture_payload,
        headers=admin_auth_headers,
    )
    replay = client.post(
        "/notes",
        json=capture_payload,
        headers=admin_auth_headers,
    )
    assert created.status_code == 201
    assert replay.status_code == 200
    # Status reads are deliberately side-effect free.
    assert (
        client.get(
            f"/projects/{project_id}/member-onboarding",
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    events_response = client.get(
        "/usage-events",
        params={
            "project_id": project_id,
            "resource_type": "member_onboarding",
            "limit": 50,
        },
        headers=admin_auth_headers,
    )
    assert events_response.status_code == 200
    events = events_response.json()["data"]
    assert sorted(event["verb"] for event in events) == [
        "create",
        "review",
        "submit",
        "upload",
    ]
    assert all(event["resource_id"] == checkpoint_id for event in events)
    serialized = json.dumps(events)
    assert "sensitive-research-sentinel" not in serialized
    assert "sensitive-forward-capture-sentinel" not in serialized


def test_postgres_first_capture_serializes_with_manual_alignment(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    """A genuine capture cannot lose its marker to a simultaneous alignment."""

    project_id = _project(postgres_client, postgres_admin_auth_headers)
    checkpoint = _checkpoint(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
    )["checkpoint"]
    capture = postgres_client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Concurrent genuine capture."},
        headers=postgres_admin_auth_headers,
    )
    assert capture.status_code == 201
    checkpoint_id = UUID(checkpoint["note_id"])
    capture_id = UUID(capture.json()["data"]["note_id"])
    lock_held = Event()
    capture_attempting = Event()
    release_alignment = Event()
    errors: list[BaseException] = []
    marker_results: list[Note | None] = []

    def finalize_alignment() -> None:
        try:
            with postgres_client.app.state.db_session_factory() as session:
                session.scalar(
                    select(NoteModel)
                    .where(NoteModel.note_id == str(checkpoint_id))
                    .with_for_update()
                )
                repository = SQLAlchemyLabTrackerRepository(session)
                current = repository.notes.get(checkpoint_id)
                assert current is not None
                expected_updated_at = current.updated_at
                resolved_at = utc_now()
                current.metadata = {
                    **current.metadata,
                    ALIGNMENT_MODE_KEY: "manual",
                    ALIGNMENT_PAYLOAD_HASH_KEY: "concurrency-test",
                    ALIGNMENT_RESOLVED_AT_KEY: resolved_at.isoformat(),
                    ALIGNMENT_RESOLUTIONS_KEY: "[]",
                }
                current.updated_at = resolved_at
                lock_held.set()
                assert release_alignment.wait(timeout=5)
                finalized = repository.notes.try_finalize_member_onboarding_alignment(
                    current,
                    expected_updated_at=expected_updated_at,
                )
                assert finalized is not None
                session.commit()
        except BaseException as exc:  # assertions must reach the test thread
            errors.append(exc)
            lock_held.set()

    def mark_capture() -> None:
        try:
            assert lock_held.wait(timeout=5)
            with postgres_client.app.state.db_session_factory() as session:
                repository = SQLAlchemyLabTrackerRepository(session)
                capture_attempting.set()
                marker_results.append(
                    repository.notes.try_mark_member_onboarding_first_capture(
                        checkpoint_id,
                        capture_note_id=capture_id,
                        captured_at=utc_now(),
                        completed=False,
                    )
                )
                session.commit()
        except BaseException as exc:
            errors.append(exc)

    alignment_thread = Thread(target=finalize_alignment)
    capture_thread = Thread(target=mark_capture)
    alignment_thread.start()
    assert lock_held.wait(timeout=5)
    capture_thread.start()
    assert capture_attempting.wait(timeout=5)
    release_alignment.set()
    alignment_thread.join(timeout=10)
    capture_thread.join(timeout=10)
    assert not alignment_thread.is_alive()
    assert not capture_thread.is_alive()
    assert errors == []
    assert marker_results and marker_results[0] is not None
    current = postgres_client.get(
        f"/projects/{project_id}/member-onboarding",
        headers=postgres_admin_auth_headers,
    ).json()["data"]
    assert current["checkpoint"]["metadata"][ALIGNMENT_MODE_KEY] == "manual"
    assert current["checkpoint"]["metadata"][FIRST_CAPTURE_NOTE_ID_KEY] == str(capture_id)


def test_postgres_manual_link_serializes_against_question_delete(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    """A committed checkpoint edge wins the same DAG lock as question delete."""

    project_id = _project(postgres_client, postgres_admin_auth_headers)
    checkpoint = _checkpoint(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
    )["checkpoint"]
    question_response = postgres_client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Concurrent durable question?",
            "question_type": "other",
            "status": "staged",
        },
        headers=postgres_admin_auth_headers,
    )
    assert question_response.status_code == 201
    question_id = UUID(question_response.json()["data"]["question_id"])
    project_uuid = UUID(project_id)
    checkpoint_id = UUID(checkpoint["note_id"])
    lock_held = Event()
    delete_attempting = Event()
    release_link = Event()
    errors: list[BaseException] = []
    delete_errors: list[BaseException] = []

    def insert_link() -> None:
        try:
            with postgres_client.app.state.db_session_factory() as session:
                repository = SQLAlchemyLabTrackerRepository(session)
                repository.lock_project_question_dag(project_uuid)
                lock_held.set()
                assert release_link.wait(timeout=5)
                question = repository.questions.get(question_id)
                assert question is not None and question.status.value == "staged"
                assert (
                    repository.notes.add_member_onboarding_question_target(
                        checkpoint_id,
                        question_id=question_id,
                    )
                    is not None
                )
                session.commit()
        except BaseException as exc:
            errors.append(exc)
            lock_held.set()

    def delete_question() -> None:
        try:
            assert lock_held.wait(timeout=5)
            with postgres_client.app.state.db_session_factory() as session:
                api = postgres_client.app.state.session_api_factory(
                    session,
                    surface="background",
                )
                delete_attempting.set()
                api.questions.delete_question(
                    question_id,
                    actor=AuthContext(
                        user_id=UUID(checkpoint["created_by"]),
                        role=Role.ADMIN,
                    ),
                )
        except BaseException as exc:
            delete_errors.append(exc)

    link_thread = Thread(target=insert_link)
    delete_thread = Thread(target=delete_question)
    link_thread.start()
    assert lock_held.wait(timeout=5)
    delete_thread.start()
    assert delete_attempting.wait(timeout=5)
    release_link.set()
    link_thread.join(timeout=10)
    delete_thread.join(timeout=10)
    assert not link_thread.is_alive()
    assert not delete_thread.is_alive()
    assert errors == []
    assert len(delete_errors) == 1
    assert isinstance(delete_errors[0], ValidationError)
    assert "notes target it" in str(delete_errors[0])
    checkpoint_response = postgres_client.get(
        f"/notes/{checkpoint_id}",
        headers=postgres_admin_auth_headers,
    )
    assert checkpoint_response.status_code == 200
    assert {
        (target["entity_type"], target["entity_id"])
        for target in checkpoint_response.json()["data"]["targets"]
    } >= {("project", project_id), ("question", str(question_id))}
