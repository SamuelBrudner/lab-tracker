"""Real PostgreSQL serialization coverage for member-onboarding AI links."""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

pytestmark = pytest.mark.postgres


class FencedFakeOnboardingDraftClient:
    """Deterministic provider double with the production timeout contract."""

    provider = "fake"
    model = "fake-member-alignment"
    timeout_seconds = 1

    def __init__(self, patch: dict[str, Any]) -> None:
        self.patch = patch
        self.calls = 0

    def draft_from_note(self, **_: Any) -> dict[str, Any]:
        self.calls += 1
        return self.patch

    def close(self) -> None:
        return None


def _create_project(client: TestClient, headers: dict[str, str]) -> UUID:
    response = client.post(
        "/projects",
        json={"name": f"Concurrent onboarding {uuid4().hex[:8]}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["project_id"])


def _create_checkpoint(
    client: TestClient,
    headers: dict[str, str],
    project_id: UUID,
) -> UUID:
    response = client.put(
        f"/projects/{project_id}/member-onboarding/checkpoint",
        json={
            "current_output_or_decision": "We selected the low-light assay.",
            "live_questions": ["Does the assay preserve response fidelity?"],
            "strongest_recent_context": "Pilot 4 was stable across two runs.",
            "next_move": "Repeat with the blinded batch.",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["checkpoint"]["note_id"])


def _create_question(
    client: TestClient,
    headers: dict[str, str],
    project_id: UUID,
) -> UUID:
    response = client.post(
        "/questions",
        json={
            "project_id": str(project_id),
            "text": "Does the assay preserve response fidelity?",
            "question_type": "other",
            "status": "staged",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["question_id"])


def _link_patch(checkpoint_id: UUID, question_id: UUID) -> dict[str, Any]:
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
                "target_entity_id": str(checkpoint_id),
                "payload_json": json.dumps(
                    {
                        "targets": [
                            {
                                "entity_type": "question",
                                "entity_id": str(question_id),
                            }
                        ]
                    }
                ),
                "rationale": "The existing question matches the member's live work.",
                "confidence": 0.9,
                "source_refs": [{"source_note_ids": [str(checkpoint_id)]}],
            }
        ],
    }


def _create_submitted_ai_link_draft(
    client: TestClient,
    headers: dict[str, str],
    *,
    project_id: UUID,
    checkpoint_id: UUID,
    question_id: UUID,
) -> tuple[UUID, UUID]:
    fake = FencedFakeOnboardingDraftClient(_link_patch(checkpoint_id, question_id))
    client.app.state.graph_draft_client_factory = lambda _settings: fake
    aligned = client.post(
        f"/projects/{project_id}/member-onboarding/ai-alignment",
        json={"external_provider_acknowledged": True},
        headers=headers,
    )
    assert aligned.status_code == 200, aligned.text
    assert fake.calls == 1
    draft = aligned.json()["data"]["alignment"]["draft"]
    operation = draft["operations"][0]
    accepted = client.patch(
        f"/graph-drafts/{draft['change_set_id']}/operations/"
        f"{operation['operation_id']}",
        json={"status": "accepted"},
        headers=headers,
    )
    assert accepted.status_code == 200, accepted.text
    submitted = client.post(
        f"/graph-drafts/{draft['change_set_id']}/submit",
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "submitted"
    return UUID(draft["change_set_id"]), UUID(operation["operation_id"])


def _backend_pid(repository: SQLAlchemyLabTrackerRepository) -> int:
    value = repository._session.scalar(text("SELECT pg_backend_pid()"))  # noqa: SLF001
    assert value is not None
    return int(value)


def _blocking_pids(client: TestClient, blocked_pid: int) -> list[int]:
    with client.app.state.db_engine.connect() as connection:
        result = connection.scalar(
            text("SELECT pg_blocking_pids(:blocked_pid)"),
            {"blocked_pid": blocked_pid},
        )
    return [int(value) for value in result or []]


def _wait_until_blocked(
    client: TestClient,
    *,
    blocked_pid: int,
    expected_blocker_pid: int,
) -> None:
    deadline = monotonic() + 10
    poll_interval = Event()
    while monotonic() < deadline:
        if expected_blocker_pid in _blocking_pids(client, blocked_pid):
            return
        poll_interval.wait(timeout=0.01)
    pytest.fail(
        f"Backend {blocked_pid} was not blocked by {expected_blocker_pid} "
        "before the deadline."
    )


def _future_result(future: Future[Any]) -> Any:
    return future.result(timeout=20)


def _mutate_question(
    client: TestClient,
    headers: dict[str, str],
    question_id: UUID,
    mutation: str,
) -> Any:
    if mutation == "delete":
        return client.delete(f"/questions/{question_id}", headers=headers)
    if mutation == "terminal_status":
        return client.patch(
            f"/questions/{question_id}",
            json={
                "status": "abandoned",
                "terminal_reason": "The concurrent work retired this question.",
            },
            headers=headers,
        )
    if mutation == "refactor":
        return client.post(
            f"/questions/{question_id}/refactor",
            json={
                "replacement": {
                    "text": "Does the revised assay preserve response fidelity?",
                    "question_type": "other",
                    "status": "staged",
                },
                "reason": "The concurrent work narrowed the live question.",
            },
            headers=headers,
        )
    raise AssertionError(f"Unsupported mutation: {mutation}")


@pytest.mark.parametrize(
    ("mutation", "mutation_status", "commit_status", "final_question_status"),
    [
        ("delete", 200, 404, None),
        ("terminal_status", 200, 422, "abandoned"),
        ("refactor", 201, 422, "superseded"),
    ],
)
def test_accepted_ai_link_waits_for_question_mutation_and_never_dangles(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    mutation_status: int,
    commit_status: int,
    final_question_status: str | None,
) -> None:
    """A winning question mutation invalidates a waiting accepted AI link."""

    project_id = _create_project(postgres_client, postgres_admin_auth_headers)
    checkpoint_id = _create_checkpoint(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
    )
    question_id = _create_question(
        postgres_client,
        postgres_admin_auth_headers,
        project_id,
    )
    draft_id, operation_id = _create_submitted_ai_link_draft(
        postgres_client,
        postgres_admin_auth_headers,
        project_id=project_id,
        checkpoint_id=checkpoint_id,
        question_id=question_id,
    )

    original_lock = SQLAlchemyLabTrackerRepository.lock_project_question_dag
    state_lock = Lock()
    mutation_locked = Event()
    commit_entered = Event()
    release_mutation = Event()
    call_count = 0
    backend_pids: dict[int, int] = {}

    def coordinated_lock(
        repository: SQLAlchemyLabTrackerRepository,
        locked_project_id: UUID,
    ) -> None:
        nonlocal call_count
        assert locked_project_id == project_id
        with state_lock:
            call_index = call_count
            call_count += 1
            backend_pids[call_index] = _backend_pid(repository)
        if call_index == 0:
            original_lock(repository, locked_project_id)
            mutation_locked.set()
            if not release_mutation.wait(timeout=20):
                raise RuntimeError("Timed out holding the question-mutation DAG lock.")
            return
        commit_entered.set()
        original_lock(repository, locked_project_id)

    monkeypatch.setattr(
        SQLAlchemyLabTrackerRepository,
        "lock_project_question_dag",
        coordinated_lock,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        mutation_future = executor.submit(
            _mutate_question,
            postgres_client,
            postgres_admin_auth_headers,
            question_id,
            mutation,
        )
        assert mutation_locked.wait(timeout=10)
        commit_future = executor.submit(
            postgres_client.post,
            f"/graph-drafts/{draft_id}/commit",
            json={"message": "Commit the accepted onboarding AI map."},
            headers=postgres_admin_auth_headers,
        )
        try:
            assert commit_entered.wait(timeout=10)
            assert backend_pids[0] != backend_pids[1]
            _wait_until_blocked(
                postgres_client,
                blocked_pid=backend_pids[1],
                expected_blocker_pid=backend_pids[0],
            )
        finally:
            release_mutation.set()
        mutation_response = _future_result(mutation_future)
        commit_response = _future_result(commit_future)

    assert mutation_response.status_code == mutation_status, mutation_response.text
    assert commit_response.status_code == commit_status, commit_response.text
    assert commit_response.json()["error"]["code"] in {
        "not_found",
        "validation_error",
    }

    checkpoint = postgres_client.get(
        f"/notes/{checkpoint_id}",
        headers=postgres_admin_auth_headers,
    )
    assert checkpoint.status_code == 200, checkpoint.text
    assert {
        (target["entity_type"], target["entity_id"])
        for target in checkpoint.json()["data"]["targets"]
    } == {("project", str(project_id))}

    draft = postgres_client.get(
        f"/graph-drafts/{draft_id}",
        headers=postgres_admin_auth_headers,
    )
    assert draft.status_code == 200, draft.text
    draft_data = draft.json()["data"]
    assert draft_data["status"] == "submitted"
    operation = next(
        item
        for item in draft_data["operations"]
        if item["operation_id"] == str(operation_id)
    )
    assert operation["status"] == "accepted"
    assert operation["result_entity_id"] is None

    question = postgres_client.get(
        f"/questions/{question_id}",
        headers=postgres_admin_auth_headers,
    )
    if final_question_status is None:
        assert question.status_code == 404, question.text
    else:
        assert question.status_code == 200, question.text
        assert question.json()["data"]["status"] == final_question_status
        if mutation == "refactor":
            assert (
                question.json()["data"]["superseded_by_question_id"]
                == mutation_response.json()["data"]["replacement_question"]["question_id"]
            )
