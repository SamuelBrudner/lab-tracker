from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.db_models import GraphChangeSetModel, GraphDraftBatchRunModel
from lab_tracker.errors import ConflictError, ValidationError
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import (
    GraphChangeOperationStatus,
    GraphChangeSetStatus,
    GraphDraftBatchRunStatus,
    GraphDraftPurpose,
)
from lab_tracker.services.graph_draft_context import SOURCE_ARTIFACT_TEXT_LIMIT_CHARS

SYSTEM_ACTOR = system_auth_context()


def _question_patch(
    project_id: str,
    *,
    status: str = "staged",
    summary: str = "Proposed a starter question.",
    question_text: str = "Which grant aim should the first experiment de-risk?",
) -> dict[str, Any]:
    return {
        "summary": summary,
        "uncertain_fields": [],
        "clarification_requests": [],
        "operations": [
            {
                "client_ref": "starter-question-1",
                "op": "create",
                "entity_type": "question",
                "semantic_type": "suggest_new_question",
                "target_entity_id": None,
                "payload_json": json.dumps(
                    {
                        "project_id": project_id,
                        "text": question_text,
                        "question_type": "descriptive",
                        "status": status,
                    }
                ),
                "rationale": "The imported grant motivates this question.",
                "confidence": 0.9,
                "source_refs": [],
            }
        ],
    }


class _DraftClient:
    provider = "fake"
    model = "fake-onboarding-hardening"

    def __init__(
        self,
        patch: dict[str, Any],
        *,
        error: str | None = None,
    ) -> None:
        self.patch = patch
        self.error = error
        self.note_calls: list[dict[str, Any]] = []
        self.batch_calls: list[dict[str, Any]] = []

    def draft_from_note(self, **kwargs: Any) -> dict[str, Any]:
        self.note_calls.append(kwargs)
        if self.error is not None:
            raise GraphDraftingError(self.error)
        return self.patch

    def draft_from_batch(self, **kwargs: Any) -> dict[str, Any]:
        self.batch_calls.append(kwargs)
        if self.error is not None:
            raise GraphDraftingError(self.error)
        return self.patch

    def close(self) -> None:
        return None


class _InterruptingDraftClient(_DraftClient):
    def draft_from_note(self, **kwargs: Any) -> dict[str, Any]:
        self.note_calls.append(kwargs)
        raise RuntimeError("simulated process interruption")


def _starter_draft(api, note_id, client: _DraftClient, **kwargs: Any):
    return api.create_graph_draft_from_note(
        note_id,
        draft_client=client,
        purpose=GraphDraftPurpose.STARTER_QUESTIONS,
        external_provider_acknowledged=True,
        actor=SYSTEM_ACTOR,
        **kwargs,
    )


def test_starter_grant_context_is_complete_past_preview_and_retry_idempotent() -> None:
    api = repository_backed_api()
    project = api.create_project("Onboarding grant", actor=SYSTEM_ACTOR)
    late_aim = "SPECIFIC AIM 3: validate the longitudinal intervention."
    raw_content = f"{'Background. ' * 120}\n{late_aim}"
    note = api.create_note(
        project.project_id,
        raw_content,
        client_capture_id="guided-setup-grant-v1",
        metadata={
            "source": "guided_setup",
            "context_type": "onboarding_grant_or_project_brief",
        },
        actor=SYSTEM_ACTOR,
    )
    duplicate_note = api.create_note(
        project.project_id,
        raw_content,
        client_capture_id="guided-setup-grant-v1",
        metadata={
            "source": "guided_setup",
            "context_type": "onboarding_grant_or_project_brief",
        },
        actor=SYSTEM_ACTOR,
    )
    assert duplicate_note.note_id == note.note_id

    client = _DraftClient(
        _question_patch(
            str(project.project_id),
            question_text=(
                "How should Specific Aim 3 validate the longitudinal intervention?"
            ),
        )
    )
    created = _starter_draft(
        api,
        note.note_id,
        client,
        idempotency_key="guided-setup-starter-v1",
    )
    retried = _starter_draft(
        api,
        note.note_id,
        client,
        idempotency_key="guided-setup-starter-v1",
    )

    assert created.status == GraphChangeSetStatus.READY
    assert retried.change_set_id == created.change_set_id
    assert len(client.note_calls) == 1
    artifact = client.note_calls[0]["source_artifacts"][0]
    assert late_aim in artifact["raw_content_text"]
    assert late_aim not in artifact["raw_content_preview"]
    assert artifact["raw_content_char_count"] == len(raw_content)
    assert artifact["raw_content_truncated"] is False
    assert created.source_context_truncated is False
    assert created.operations[0].status == GraphChangeOperationStatus.PROPOSED
    assert "Specific Aim 3" in created.operations[0].payload["text"]
    assert api.list_questions(project_id=project.project_id) == []
    persisted_note = api.get_note(note.note_id)
    assert persisted_note.metadata["scheduled_graph_draft_policy"] == "exclude"
    replayed_note = api.create_note(
        project.project_id,
        raw_content,
        client_capture_id="guided-setup-grant-v1",
        metadata={
            "source": "guided_setup",
            "context_type": "onboarding_grant_or_project_brief",
        },
        actor=SYSTEM_ACTOR,
    )
    assert replayed_note.note_id == note.note_id
    with pytest.raises(ConflictError, match="metadata"):
        api.create_note(
            project.project_id,
            raw_content,
            client_capture_id="guided-setup-grant-v1",
            metadata={
                "source": "guided_setup",
                "context_type": "onboarding_grant_or_project_brief",
                "scheduled_graph_draft_policy": "include",
            },
            actor=SYSTEM_ACTOR,
        )

    with pytest.raises(ValidationError, match="conflicting graph-draft fields"):
        _starter_draft(
            api,
            note.note_id,
            client,
            idempotency_key="guided-setup-starter-v1",
            user_hint="A conflicting retry payload.",
        )


def test_starter_grant_context_reports_explicit_bound_when_source_is_too_large() -> None:
    api = repository_backed_api()
    project = api.create_project("Large onboarding grant", actor=SYSTEM_ACTOR)
    raw_content = "G" * (SOURCE_ARTIFACT_TEXT_LIMIT_CHARS + 137)
    note = api.create_note(project.project_id, raw_content, actor=SYSTEM_ACTOR)
    client = _DraftClient(_question_patch(str(project.project_id)))

    created = _starter_draft(api, note.note_id, client)

    artifact = created.context_packet["source_artifacts"][0]
    assert len(artifact["raw_content_text"]) == SOURCE_ARTIFACT_TEXT_LIMIT_CHARS
    assert artifact["raw_content_char_count"] == len(raw_content)
    assert artifact["raw_content_limit_chars"] == SOURCE_ARTIFACT_TEXT_LIMIT_CHARS
    assert artifact["raw_content_truncated"] is True
    assert created.source_context_truncated is True
    assert any(
        "bounded provider-context limit" in warning.lower()
        for warning in created.context_packet["context_summary"]["warnings"]
    )


def test_starter_requires_provider_ack_and_enforces_question_only_contract() -> None:
    api = repository_backed_api()
    project = api.create_project("Question-only onboarding", actor=SYSTEM_ACTOR)
    note = api.create_note(
        project.project_id,
        "Grant context.",
        actor=SYSTEM_ACTOR,
    )
    client = _DraftClient(_question_patch(str(project.project_id), status="active"))

    with pytest.raises(ValidationError, match="external drafting provider"):
        api.create_graph_draft_from_note(
            note.note_id,
            draft_client=client,
            purpose=GraphDraftPurpose.STARTER_QUESTIONS,
            actor=SYSTEM_ACTOR,
        )
    assert client.note_calls == []
    assert (
        api.get_note(note.note_id).metadata.get("scheduled_graph_draft_policy")
        is None
    )

    failed = _starter_draft(api, note.note_id, client)
    assert failed.status == GraphChangeSetStatus.FAILED
    assert "staged question status" in failed.error_metadata["message"]
    assert api.list_questions(project_id=project.project_id) == []

    operation = _question_patch(str(project.project_id))["operations"][0]
    too_many_operations = []
    for index in range(13):
        item = dict(operation)
        item["client_ref"] = f"starter-question-{index}"
        payload = json.loads(item["payload_json"])
        payload["text"] = f"Starter question {index}?"
        item["payload_json"] = json.dumps(payload)
        too_many_operations.append(item)
    too_many = _starter_draft(
        api,
        note.note_id,
        _DraftClient(
            {
                "summary": "Too many questions.",
                "uncertain_fields": [],
                "clarification_requests": [],
                "operations": too_many_operations,
            }
        ),
    )
    assert too_many.status == GraphChangeSetStatus.FAILED
    assert "maximum is 12" in too_many.error_metadata["message"]


def test_starter_revision_preserves_question_only_contract_and_purpose() -> None:
    api = repository_backed_api()
    project = api.create_project("Starter revision", actor=SYSTEM_ACTOR)
    note = api.create_note(
        project.project_id,
        "Grant context.",
        actor=SYSTEM_ACTOR,
    )
    created = _starter_draft(
        api,
        note.note_id,
        _DraftClient(_question_patch(str(project.project_id))),
    )
    invalid_revision = _DraftClient(
        _question_patch(str(project.project_id), status="active")
    )

    with pytest.raises(GraphDraftingError, match="staged question status"):
        api.graph_drafts.generation.propose_note_revision(
            created,
            user_hint="Revise the questions.",
            draft_client=invalid_revision,
            actor=SYSTEM_ACTOR,
            extra_images=[],
        )

    proposal = api.graph_drafts.generation.propose_note_revision(
        created,
        user_hint="Make the question more concrete.",
        draft_client=_DraftClient(_question_patch(str(project.project_id))),
        actor=SYSTEM_ACTOR,
        extra_images=[],
    )
    assert proposal.context_packet["draft_purpose"] == "starter_questions"
    assert proposal.context_packet["draft_contract"]["human_commit_required"] is True
    assert proposal.operations[0].status == GraphChangeOperationStatus.PROPOSED

    api.submit_graph_change_set(created.change_set_id, actor=SYSTEM_ACTOR)
    api.review_graph_change_set(
        created.change_set_id,
        status=GraphChangeSetStatus.CHANGES_REQUESTED,
        note="Make the question more concrete.",
        actor=SYSTEM_ACTOR,
    )
    revised = api.revise_graph_change_set(
        created.change_set_id,
        feedback="Make the question more concrete.",
        draft_client=_DraftClient(_question_patch(str(project.project_id))),
        actor=SYSTEM_ACTOR,
    )
    assert revised.draft_purpose == GraphDraftPurpose.STARTER_QUESTIONS
    assert revised.prompt_version == "starter-questions-v1"


def test_failed_and_interrupted_note_drafts_reuse_identity_and_recover_lease() -> None:
    api = repository_backed_api()
    project = api.create_project("Recoverable onboarding", actor=SYSTEM_ACTOR)
    note = api.create_note(
        project.project_id,
        "Grant context.",
        actor=SYSTEM_ACTOR,
    )
    client = _DraftClient(
        _question_patch(str(project.project_id)),
        error="temporary provider failure",
    )

    failed = _starter_draft(
        api,
        note.note_id,
        client,
        idempotency_key="recoverable-starter-v1",
    )
    assert failed.status == GraphChangeSetStatus.FAILED
    client.error = None
    ready = _starter_draft(
        api,
        note.note_id,
        client,
        idempotency_key="recoverable-starter-v1",
    )
    assert ready.status == GraphChangeSetStatus.READY
    assert ready.change_set_id == failed.change_set_id
    assert ready.context_packet["generation_attempt"] == 2
    assert len(client.note_calls) == 2

    _engine, session = api._test_resources
    row = session.get(GraphChangeSetModel, str(ready.change_set_id))
    assert row is not None
    row.status = GraphChangeSetStatus.DRAFTING.value
    row.updated_at = datetime.now(timezone.utc)
    context_packet = dict(row.context_packet)
    context_packet["generation_lease_expires_at"] = (
        datetime.now(timezone.utc) + timedelta(minutes=5)
    ).isoformat()
    row.context_packet = context_packet
    session.commit()

    still_owned = api.graph_drafts.generation.create_graph_draft_from_note(
        note.note_id,
        draft_client=client,
        purpose=GraphDraftPurpose.STARTER_QUESTIONS,
        idempotency_key="recoverable-starter-v1",
        external_provider_acknowledged=True,
        actor=SYSTEM_ACTOR,
        generation_lease_seconds=300,
    )
    assert still_owned.status == GraphChangeSetStatus.DRAFTING
    assert len(client.note_calls) == 2

    row = session.get(GraphChangeSetModel, str(ready.change_set_id))
    assert row is not None
    row.updated_at = datetime.now(timezone.utc)
    context_packet = dict(row.context_packet)
    context_packet["generation_lease_expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    row.context_packet = context_packet
    session.commit()
    recovered = api.graph_drafts.generation.create_graph_draft_from_note(
        note.note_id,
        draft_client=client,
        purpose=GraphDraftPurpose.STARTER_QUESTIONS,
        idempotency_key="recoverable-starter-v1",
        external_provider_acknowledged=True,
        actor=SYSTEM_ACTOR,
        generation_lease_seconds=300,
    )
    assert recovered.status == GraphChangeSetStatus.READY
    assert recovered.change_set_id == ready.change_set_id
    assert recovered.context_packet["generation_attempt"] == 3
    assert recovered.context_packet["generation_recovered"] is True
    assert recovered.generation_lease_expires_at is None
    assert len(client.note_calls) == 3


def test_http_interruption_leaves_durable_starter_lease_for_same_id_retry(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_response = client.post(
        "/projects",
        json={"name": "HTTP interruption recovery"},
        headers=admin_auth_headers,
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["data"]["project_id"]
    note_response = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Imported grant context.",
            "status": "staged",
        },
        headers=admin_auth_headers,
    )
    assert note_response.status_code == 201
    note_id = note_response.json()["data"]["note_id"]
    request_payload = {
        "external_provider_acknowledged": True,
        "idempotency_key": "http-interruption-starter-v1",
        "mode": "graph_context",
        "purpose": "starter_questions",
    }
    interrupted_client = _InterruptingDraftClient(_question_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda _settings: interrupted_client

    with pytest.raises(RuntimeError, match="simulated process interruption"):
        client.post(
            f"/notes/{note_id}/graph-drafts",
            json=request_payload,
            headers=admin_auth_headers,
        )

    with client.app.state.db_session_factory() as session:
        row = session.scalar(
            select(GraphChangeSetModel).where(
                GraphChangeSetModel.source_note_id == note_id
            )
        )
        assert row is not None
        interrupted_change_set_id = str(row.change_set_id)
        assert row.status == GraphChangeSetStatus.DRAFTING.value
        assert row.context_packet["generation_lease_expires_at"] is not None
        row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        session.commit()

    retry_client = _DraftClient(_question_patch(project_id))
    client.app.state.graph_draft_client_factory = lambda _settings: retry_client
    retried = client.post(
        f"/notes/{note_id}/graph-drafts",
        json=request_payload,
        headers=admin_auth_headers,
    )
    assert retried.status_code == 201
    payload = retried.json()["data"]
    assert payload["change_set_id"] == interrupted_change_set_id
    assert payload["status"] == GraphChangeSetStatus.READY.value
    assert payload["context_packet"]["generation_attempt"] == 2
    assert len(retry_client.note_calls) == 1


def test_starter_source_is_excluded_from_scheduled_batch_cadence() -> None:
    api = repository_backed_api()
    project = api.create_project("Cadence-safe onboarding", actor=SYSTEM_ACTOR)
    starter_note = api.create_note(
        project.project_id,
        "Imported grant context.",
        metadata={
            "source": "guided_setup",
            "context_type": "onboarding_grant_or_project_brief",
        },
        actor=SYSTEM_ACTOR,
    )
    ordinary_note = api.create_note(
        project.project_id,
        "New observation after onboarding.",
        actor=SYSTEM_ACTOR,
    )
    starter_client = _DraftClient(_question_patch(str(project.project_id)))
    _starter_draft(api, starter_note.note_id, starter_client)
    batch_client = _DraftClient(
        _question_patch(
            str(project.project_id),
            summary="Proposed a question from the ordinary note.",
        )
    )

    now = datetime.now(timezone.utc)
    run = api.run_graph_draft_batch_for_project(
        project.project_id,
        draft_client=batch_client,
        since=now - timedelta(hours=1),
        until=now + timedelta(hours=1),
        actor=SYSTEM_ACTOR,
    )

    assert run.status == GraphDraftBatchRunStatus.READY
    assert run.source_note_ids == [ordinary_note.note_id]
    assert starter_note.note_id not in run.source_note_ids
    assert len(batch_client.batch_calls) == 1


def test_stale_running_batch_is_reclaimed_once_and_keeps_run_identity() -> None:
    api = repository_backed_api()
    project = api.create_project("Recoverable scheduled batch", actor=SYSTEM_ACTOR)
    api.create_note(
        project.project_id,
        "Observation awaiting the batch worker.",
        actor=SYSTEM_ACTOR,
    )
    now = datetime.now(timezone.utc)
    queued = api.enqueue_graph_draft_batch_for_project(
        project.project_id,
        since=now - timedelta(hours=1),
        until=now + timedelta(hours=1),
        actor=SYSTEM_ACTOR,
    )
    assert queued.status == GraphDraftBatchRunStatus.PENDING

    scheduling = api.graph_drafts.scheduling
    claimed = scheduling.claim_next_graph_draft_batch_run(lease_seconds=60)
    assert claimed is not None
    assert claimed.run_id == queued.run_id
    assert claimed.status == GraphDraftBatchRunStatus.RUNNING
    assert scheduling.claim_next_graph_draft_batch_run(lease_seconds=60) is None

    _engine, session = api._test_resources
    row = session.get(GraphDraftBatchRunModel, str(queued.run_id))
    assert row is not None
    row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    session.commit()

    reclaimed = scheduling.claim_next_graph_draft_batch_run(lease_seconds=1)
    assert reclaimed is not None
    assert reclaimed.run_id == queued.run_id
    assert reclaimed.error_metadata["lease_recovery_count"] == 1

    client = _DraftClient(_question_patch(str(project.project_id)))
    completed = api.execute_graph_draft_batch_run(
        reclaimed.run_id,
        draft_client=client,
        actor=SYSTEM_ACTOR,
    )
    assert completed.run_id == queued.run_id
    assert completed.status == GraphDraftBatchRunStatus.READY
    assert completed.change_set_id is not None
