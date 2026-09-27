from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from api_helpers import repository_backed_api
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_graph_draft_batches import FakeBatchDraftClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.db_models import UsageEventModel
from lab_tracker.draft_quality import (
    ACCEPTED_OPERATION_STATUSES,
    REVIEW_AUDIT_METADATA_KEYS,
    DraftQualityRow,
    aggregate_draft_quality,
    seconds_between,
)
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.graph_drafting import BATCH_PROMPT_VERSION
from lab_tracker.models import (
    EDITED_AT_KEY,
    EDITED_BY_KEY,
    REVIEW_NOTE_KEY,
    REVIEWED_AT_KEY,
    REVIEWED_BY_KEY,
    AcceptanceMode,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftMode,
    GraphDraftSemanticType,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.sqlalchemy_repository_parts.graph_drafts import (
    SQLAlchemyGraphChangeSetRepository,
)

ADMIN = AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
FAKE_GROUP = ("fake", "fake-batch-model", BATCH_PROMPT_VERSION)


# --- pure aggregator ---------------------------------------------------------


def _row(
    *,
    change_set_id: UUID,
    change_set_status: GraphChangeSetStatus = GraphChangeSetStatus.READY,
    created_at: datetime = T0,
    reviewed_at: datetime | None = None,
    clarifications: int = 0,
    semantic_type: GraphDraftSemanticType | None = GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
    operation_status: GraphChangeOperationStatus | None = GraphChangeOperationStatus.PROPOSED,
    acceptance_mode: AcceptanceMode | None = None,
    accepted_at: datetime | None = None,
    edited: bool = False,
    provider: str = "openai",
    model: str = "gpt-x",
    prompt_version: str = "v1",
) -> DraftQualityRow:
    return DraftQualityRow(
        change_set_id=change_set_id,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        change_set_status=change_set_status,
        change_set_created_at=created_at,
        reviewed_at=reviewed_at,
        clarification_request_count=clarifications,
        semantic_type=semantic_type,
        operation_status=operation_status,
        acceptance_mode=acceptance_mode,
        accepted_at=accepted_at,
        edited_before_accept=edited,
    )


def test_review_audit_keys_are_the_shared_model_constants() -> None:
    assert frozenset({EDITED_AT_KEY, EDITED_BY_KEY}) == REVIEW_AUDIT_METADATA_KEYS
    assert (EDITED_AT_KEY, EDITED_BY_KEY, REVIEWED_AT_KEY, REVIEWED_BY_KEY, REVIEW_NOTE_KEY) == (
        "edited_at",
        "edited_by",
        "reviewed_at",
        "reviewed_by",
        "review_note",
    )
    assert frozenset(
        {GraphChangeOperationStatus.ACCEPTED, GraphChangeOperationStatus.APPLIED}
    ) == ACCEPTED_OPERATION_STATUSES


def test_seconds_between_rejects_negative_spans() -> None:
    assert seconds_between(T0, T0 + timedelta(seconds=30)) == 30.0
    assert seconds_between(T0, T0) == 0.0
    with pytest.raises(ValueError):
        seconds_between(T0, T0 - timedelta(microseconds=1))


def test_aggregate_counts_each_cell_and_split_by_acceptance_mode() -> None:
    committed = uuid4()
    ready = uuid4()
    accepted = GraphChangeOperationStatus.ACCEPTED
    rows = [
        _row(
            change_set_id=committed,
            change_set_status=GraphChangeSetStatus.COMMITTED,
            operation_status=GraphChangeOperationStatus.APPLIED,
            acceptance_mode=AcceptanceMode.HUMAN_SELECTED,
            accepted_at=T0 + timedelta(seconds=5),
            edited=True,
        ),
        _row(
            change_set_id=committed,
            change_set_status=GraphChangeSetStatus.COMMITTED,
            operation_status=GraphChangeOperationStatus.APPLIED,
            acceptance_mode=AcceptanceMode.HUMAN_SELECTED,
            accepted_at=T0 + timedelta(seconds=9),
        ),
        _row(
            change_set_id=ready,
            operation_status=accepted,
            acceptance_mode=AcceptanceMode.BULK_ACCEPTED,
            accepted_at=T0 + timedelta(seconds=2),
        ),
        _row(change_set_id=committed, change_set_status=GraphChangeSetStatus.COMMITTED,
             operation_status=GraphChangeOperationStatus.REJECTED),
        _row(change_set_id=committed, change_set_status=GraphChangeSetStatus.COMMITTED),
        _row(change_set_id=ready),
    ]

    ledger = aggregate_draft_quality(uuid4(), None, rows)

    assert ledger.change_set_count == 2
    [cell] = ledger.cells
    assert cell.semantic_type == GraphDraftSemanticType.LINK_NOTE_TO_QUESTION
    assert cell.proposed == 6
    assert cell.accepted_total == 3
    assert cell.accepted_human_selected == 2
    assert cell.accepted_bulk_accepted == 1
    assert cell.edited_before_accept == 1
    assert cell.rejected == 1
    assert cell.left_proposed_at_commit == 1


def test_aggregate_groups_change_set_stats_and_medians() -> None:
    sets = [uuid4(), uuid4(), uuid4()]
    rows: list[DraftQualityRow] = []
    for change_set_id, offset in zip(sets, (10, 30, 50), strict=True):
        rows.append(
            _row(
                change_set_id=change_set_id,
                reviewed_at=T0 + timedelta(seconds=offset),
                clarifications=2,
                operation_status=GraphChangeOperationStatus.ACCEPTED,
                acceptance_mode=AcceptanceMode.HUMAN_SELECTED,
                accepted_at=T0 + timedelta(seconds=offset + 1),
            )
        )
        rows.append(
            _row(
                change_set_id=change_set_id,
                reviewed_at=T0 + timedelta(seconds=offset),
                clarifications=2,
                operation_status=GraphChangeOperationStatus.ACCEPTED,
                acceptance_mode=AcceptanceMode.HUMAN_SELECTED,
                accepted_at=T0 + timedelta(seconds=offset),
            )
        )
    quiet = uuid4()
    rows.append(_row(change_set_id=quiet, provider="anthropic", clarifications=0))

    ledger = aggregate_draft_quality(uuid4(), None, rows)

    assert [(g.provider, g.model, g.prompt_version) for g in ledger.groups] == [
        ("anthropic", "gpt-x", "v1"),
        ("openai", "gpt-x", "v1"),
    ]
    quiet_group, busy_group = ledger.groups
    assert busy_group.change_set_count == 3
    assert busy_group.clarification_request_count == 6
    assert busy_group.change_sets_with_clarifications == 3
    assert busy_group.median_seconds_to_first_accept == 30.0
    assert busy_group.median_seconds_to_review == 30.0
    assert quiet_group.change_set_count == 1
    assert quiet_group.clarification_request_count == 0
    assert quiet_group.change_sets_with_clarifications == 0
    assert quiet_group.median_seconds_to_first_accept is None
    assert quiet_group.median_seconds_to_review is None


def test_aggregate_is_deterministically_ordered() -> None:
    rows = [
        _row(change_set_id=uuid4(), provider=provider, semantic_type=semantic_type)
        for provider in ("openai", "anthropic")
        for semantic_type in (
            GraphDraftSemanticType.SUGGEST_NEW_QUESTION,
            None,
            GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        )
    ]
    project_id = uuid4()
    ordered = aggregate_draft_quality(project_id, None, rows)

    shuffled = list(rows)
    random.Random(7).shuffle(shuffled)
    assert aggregate_draft_quality(project_id, None, shuffled) == ordered
    assert [(cell.provider, cell.semantic_type) for cell in ordered.cells] == [
        ("anthropic", None),
        ("anthropic", GraphDraftSemanticType.LINK_NOTE_TO_QUESTION),
        ("anthropic", GraphDraftSemanticType.SUGGEST_NEW_QUESTION),
        ("openai", None),
        ("openai", GraphDraftSemanticType.LINK_NOTE_TO_QUESTION),
        ("openai", GraphDraftSemanticType.SUGGEST_NEW_QUESTION),
    ]


def test_aggregate_counts_operationless_change_sets() -> None:
    empty = uuid4()
    rows = [
        _row(
            change_set_id=empty,
            clarifications=3,
            semantic_type=None,
            operation_status=None,
        )
    ]

    ledger = aggregate_draft_quality(uuid4(), T0, rows)

    assert ledger.since == T0
    assert ledger.change_set_count == 1
    assert ledger.cells == []
    [group] = ledger.groups
    assert group.change_set_count == 1
    assert group.clarification_request_count == 3
    assert group.change_sets_with_clarifications == 1


# --- repository projection ---------------------------------------------------


def test_sqlalchemy_repository_satisfies_draft_quality_protocol() -> None:
    assert callable(SQLAlchemyLabTrackerRepository.query_draft_quality_rows)
    assert callable(SQLAlchemyGraphChangeSetRepository.draft_quality_rows)


def _saved_change_set(
    api: LabTrackerAPI,
    *,
    project_id: UUID,
    note_id: UUID,
    created_at: datetime,
    operations: int,
) -> GraphChangeSet:
    change_set_id = uuid4()
    change_set = GraphChangeSet(
        change_set_id=change_set_id,
        project_id=project_id,
        source_note_id=note_id,
        source_note_ids=[note_id],
        provider="fake",
        model="fake-batch-model",
        prompt_version=BATCH_PROMPT_VERSION,
        draft_mode=GraphDraftMode.GRAPH_BATCH,
        status=GraphChangeSetStatus.READY,
        clarification_requests=["Which animal?"],
        created_at=created_at,
        updated_at=created_at,
        operations=[
            GraphChangeOperation(
                operation_id=uuid4(),
                change_set_id=change_set_id,
                sequence=sequence,
                op=GraphChangeOp.UPDATE,
                entity_type=EntityType.NOTE,
                semantic_type=GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
                target_entity_id=note_id,
                status=GraphChangeOperationStatus.ACCEPTED,
                acceptance_mode=AcceptanceMode.HUMAN_SELECTED,
                accepted_at=created_at + timedelta(seconds=sequence),
                error_metadata={EDITED_AT_KEY: created_at.isoformat()} if sequence == 1 else {},
            )
            for sequence in range(1, operations + 1)
        ],
    )
    api.graph_drafts.records.save_graph_change_set(change_set)
    return change_set


def _assert_rows_join_operations_and_filter_since(api: LabTrackerAPI) -> None:
    project = api.create_project("Ledger rows", actor=ADMIN)
    note = api.create_note(project_id=project.project_id, raw_content="capture", actor=ADMIN)
    other_project = api.create_project("Other", actor=ADMIN)
    other_note = api.create_note(
        project_id=other_project.project_id, raw_content="elsewhere", actor=ADMIN
    )
    early = _saved_change_set(
        api, project_id=project.project_id, note_id=note.note_id, created_at=T0, operations=2
    )
    late = _saved_change_set(
        api,
        project_id=project.project_id,
        note_id=note.note_id,
        created_at=T0 + timedelta(days=1),
        operations=0,
    )
    _saved_change_set(
        api,
        project_id=other_project.project_id,
        note_id=other_note.note_id,
        created_at=T0,
        operations=1,
    )
    repository = api.draft_quality.repository

    rows = repository.query_draft_quality_rows(project_id=project.project_id, since=None)

    assert [row.change_set_id for row in rows] == [
        early.change_set_id,
        early.change_set_id,
        late.change_set_id,
    ]
    first, second, empty = rows
    assert first.operation_status == GraphChangeOperationStatus.ACCEPTED
    assert first.acceptance_mode == AcceptanceMode.HUMAN_SELECTED
    assert first.accepted_at == T0 + timedelta(seconds=1)
    assert first.edited_before_accept is True
    assert second.edited_before_accept is False
    assert first.clarification_request_count == 1
    assert first.change_set_created_at == T0
    assert first.change_set_status == GraphChangeSetStatus.READY
    assert first.semantic_type == GraphDraftSemanticType.LINK_NOTE_TO_QUESTION
    assert (first.provider, first.model, first.prompt_version) == FAKE_GROUP
    assert empty.operation_status is None
    assert empty.semantic_type is None
    assert empty.acceptance_mode is None
    assert empty.accepted_at is None
    assert empty.edited_before_accept is False
    assert empty.clarification_request_count == 1

    since_rows = repository.query_draft_quality_rows(
        project_id=project.project_id, since=T0 + timedelta(hours=12)
    )
    assert [row.change_set_id for row in since_rows] == [late.change_set_id]


def test_repository_rows_join_operations_and_filter_since() -> None:
    _assert_rows_join_operations_and_filter_since(repository_backed_api())


@pytest.mark.postgres
def test_repository_rows_join_operations_and_filter_since_on_postgres(
    postgres_client: TestClient,
) -> None:
    with postgres_client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=postgres_client.app.state.settings,
        )
        _assert_rows_join_operations_and_filter_since(api)


def test_repository_rows_ignore_other_projects() -> None:
    api = repository_backed_api()
    project = api.create_project("Mine", actor=ADMIN)
    other = api.create_project("Theirs", actor=ADMIN)
    other_note = api.create_note(project_id=other.project_id, raw_content="x", actor=ADMIN)
    _saved_change_set(
        api, project_id=other.project_id, note_id=other_note.note_id, created_at=T0, operations=3
    )

    assert api.draft_quality.repository.query_draft_quality_rows(
        project_id=project.project_id, since=None
    ) == []


# --- service -----------------------------------------------------------------


def test_service_raises_not_found_for_unreadable_project(monkeypatch) -> None:
    api = repository_backed_api()
    project = api.create_project("Private", actor=ADMIN)
    outsider = AuthContext(user_id=uuid4(), role=Role.VIEWER)

    def never_called(self, *, project_id, since):
        raise AssertionError("repository must not be read before authorization succeeds")

    monkeypatch.setattr(SQLAlchemyLabTrackerRepository, "query_draft_quality_rows", never_called)

    with pytest.raises(NotFoundError):
        api.draft_quality_ledger(project.project_id, actor=outsider)
    with pytest.raises(NotFoundError):
        api.draft_quality_ledger(uuid4(), actor=ADMIN)


def test_service_rejects_naive_since() -> None:
    api = repository_backed_api()
    project = api.create_project("Naive", actor=ADMIN)

    with pytest.raises(ValidationError, match="timezone offset"):
        api.draft_quality_ledger(project.project_id, since=datetime(2026, 1, 1), actor=ADMIN)


def test_service_returns_empty_ledger_for_project_without_drafts() -> None:
    api = repository_backed_api()
    project = api.create_project("Empty", actor=ADMIN)

    ledger = api.draft_quality_ledger(project.project_id, since=T0, actor=ADMIN)

    assert ledger.project_id == project.project_id
    assert ledger.since == T0
    assert ledger.change_set_count == 0
    assert ledger.cells == []
    assert ledger.groups == []


# --- HTTP route --------------------------------------------------------------


def _post(client: TestClient, path: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code in {200, 201}, f"{path}: {response.text}"
    return response.json()["data"]


def _staged_note(client: TestClient, headers: dict[str, str], project_id: str, text: str) -> str:
    note = _post(
        client,
        "/notes",
        {"project_id": project_id, "raw_content": text, "status": "staged"},
        headers,
    )
    return str(note["note_id"])


def _link_op(note_id: str, question_id: str) -> dict[str, Any]:
    return {
        "client_ref": None,
        "op": "update",
        "entity_type": "note",
        "semantic_type": "link_note_to_question",
        "target_entity_id": note_id,
        "payload_json": json.dumps(
            {"targets": [{"entity_type": "question", "entity_id": question_id}]}
        ),
        "rationale": "The note is about this question.",
        "confidence": 0.9,
        "source_refs": [{"source_note_ids": [note_id]}],
    }


def _create_question_op(project_id: str, client_ref: str, text: str) -> dict[str, Any]:
    return {
        "client_ref": client_ref,
        "op": "create",
        "entity_type": "question",
        "semantic_type": "suggest_new_question",
        "target_entity_id": None,
        "payload_json": json.dumps(
            {
                "project_id": project_id,
                "text": text,
                "question_type": "descriptive",
                "status": "staged",
            }
        ),
        "rationale": "The notes raise a new question.",
        "confidence": 0.8,
        "source_refs": [],
    }


def _patch(
    client: TestClient,
    headers: dict[str, str],
    change_set_id: str,
    operation: dict[str, Any],
    body: dict[str, Any],
) -> None:
    response = client.patch(
        f"/graph-drafts/{change_set_id}/operations/{operation['operation_id']}",
        json=body,
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _run_batch(
    client: TestClient, headers: dict[str, str], project_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    client.app.state.graph_draft_client_factory = lambda _settings: FakeBatchDraftClient(patch)
    run = _post(client, "/batches/run-now", {"project_id": project_id}, headers)
    assert run["status"] == "ready", run
    detail = client.get(f"/batches/{run['change_set_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()["data"]


@dataclass(frozen=True)
class ReviewWalkthrough:
    project_id: str
    first_change_set_id: str
    second_change_set_id: str
    second_created_at: str
    edited_operation_id: str
    plain_operation_id: str
    rejected_operation_id: str


def _review_walkthrough(client: TestClient, headers: dict[str, str]) -> ReviewWalkthrough:
    """Two batches: one reviewed op-by-op (edit, reject, leave one) and committed, one bulk."""

    project_id = str(_post(client, "/projects", {"name": "Draft quality"}, headers)["project_id"])
    question_id = str(
        _post(
            client,
            "/questions",
            {"project_id": project_id, "text": "Existing?", "question_type": "descriptive"},
            headers,
        )["question_id"]
    )
    note_a = _staged_note(client, headers, project_id, "Gel photo A looked clean.")
    note_b = _staged_note(client, headers, project_id, "Voice memo: same gel, lane 2.")
    first = _run_batch(
        client,
        headers,
        project_id,
        {
            "summary": "first",
            "uncertain_fields": [],
            "clarification_requests": [],
            "operations": [
                _create_question_op(project_id, "q1", "Does lane 2 replicate lane 1?"),
                _link_op(note_a, question_id),
                _link_op(note_b, question_id),
                _create_question_op(project_id, "q2", "Should the gel be re-run?"),
            ],
        },
    )
    plain, edited, rejected, left = sorted(first["operations"], key=lambda op: op["sequence"])
    first_id = str(first["change_set_id"])
    _patch(client, headers, first_id, plain, {"status": "accepted"})
    _patch(
        client,
        headers,
        first_id,
        edited,
        {
            "payload": {**edited["payload"], "metadata": {"reviewed": True}},
            "status": "accepted",
        },
    )
    _patch(client, headers, first_id, rejected, {"status": "rejected", "review_note": "dup"})

    note_c = _staged_note(client, headers, project_id, "Second day: lane 3.")
    note_d = _staged_note(client, headers, project_id, "Second day: lane 4.")
    second = _run_batch(
        client,
        headers,
        project_id,
        {
            "summary": "second",
            "uncertain_fields": [],
            "clarification_requests": ["Which gel?"],
            "operations": [_link_op(note_c, question_id), _link_op(note_d, question_id)],
        },
    )
    second_id = str(second["change_set_id"])
    assert set(second["source_note_ids"]) == {note_c, note_d}
    accepted_all = client.post(f"/graph-drafts/{second_id}/accept-all", headers=headers)
    assert accepted_all.status_code == 200, accepted_all.text
    committed = client.post(
        f"/graph-drafts/{first_id}/commit", json={"message": "commit first"}, headers=headers
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["data"]["status"] == "committed"
    return ReviewWalkthrough(
        project_id=project_id,
        first_change_set_id=first_id,
        second_change_set_id=second_id,
        second_created_at=str(second["created_at"]),
        edited_operation_id=str(edited["operation_id"]),
        plain_operation_id=str(plain["operation_id"]),
        rejected_operation_id=str(rejected["operation_id"]),
    )


def _ledger(client: TestClient, headers: dict[str, str], project_id: str, **params: Any) -> Any:
    response = client.get(f"/projects/{project_id}/draft-quality", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_draft_quality_route_reports_review_outcomes(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    walk = _review_walkthrough(client, admin_auth_headers)

    ledger = _ledger(client, admin_auth_headers, walk.project_id)

    assert ledger["project_id"] == walk.project_id
    assert ledger["since"] is None
    assert ledger["change_set_count"] == 2
    cells = {cell["semantic_type"]: cell for cell in ledger["cells"]}
    assert set(cells) == {"link_note_to_question", "suggest_new_question"}
    for cell in cells.values():
        assert (cell["provider"], cell["model"], cell["prompt_version"]) == FAKE_GROUP
    assert cells["link_note_to_question"] == {
        "provider": "fake",
        "model": "fake-batch-model",
        "prompt_version": BATCH_PROMPT_VERSION,
        "semantic_type": "link_note_to_question",
        "proposed": 4,
        "accepted_total": 3,
        "accepted_human_selected": 1,
        "accepted_bulk_accepted": 2,
        "edited_before_accept": 1,
        "rejected": 1,
        "left_proposed_at_commit": 0,
    }
    assert cells["suggest_new_question"] == {
        "provider": "fake",
        "model": "fake-batch-model",
        "prompt_version": BATCH_PROMPT_VERSION,
        "semantic_type": "suggest_new_question",
        "proposed": 2,
        "accepted_total": 1,
        "accepted_human_selected": 1,
        "accepted_bulk_accepted": 0,
        "edited_before_accept": 0,
        "rejected": 0,
        "left_proposed_at_commit": 1,
    }
    [group] = ledger["groups"]
    assert (group["provider"], group["model"], group["prompt_version"]) == FAKE_GROUP
    assert group["change_set_count"] == 2
    assert group["clarification_request_count"] == 1
    assert group["change_sets_with_clarifications"] == 1
    assert group["median_seconds_to_first_accept"] >= 0.0
    assert group["median_seconds_to_review"] is None


def test_draft_quality_since_filters_by_change_set_created_at(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    walk = _review_walkthrough(client, admin_auth_headers)

    ledger = _ledger(client, admin_auth_headers, walk.project_id, since=walk.second_created_at)

    assert ledger["change_set_count"] == 1
    assert ledger["since"] is not None
    [cell] = ledger["cells"]
    assert cell["semantic_type"] == "link_note_to_question"
    assert cell["proposed"] == cell["accepted_bulk_accepted"] == 2
    [group] = ledger["groups"]
    assert group["clarification_request_count"] == 1

    naive = client.get(
        f"/projects/{walk.project_id}/draft-quality",
        params={"since": "2026-01-01T00:00:00"},
        headers=admin_auth_headers,
    )
    assert naive.status_code == 422, naive.text
    assert naive.json()["error"]["code"] == "validation_error"
    assert "timezone offset" in naive.json()["error"]["message"]


def _view_events(client: TestClient) -> list[UsageEventModel]:
    with client.app.state.db_session_factory() as session:
        return list(
            session.scalars(
                select(UsageEventModel).where(
                    UsageEventModel.verb == "view",
                    UsageEventModel.resource_type == "draft_quality",
                )
            )
        )


def test_draft_quality_records_one_view_usage_event(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    client.app.state.settings.usage_events = True
    project_id = str(
        _post(client, "/projects", {"name": "Viewed"}, admin_auth_headers)["project_id"]
    )

    _ledger(client, admin_auth_headers, project_id)

    [event] = _view_events(client)
    assert str(event.resource_id) == project_id
    assert str(event.project_id) == project_id
    assert event.outcome == "ok"


def test_draft_quality_is_opaque_404_for_hidden_project(
    client: TestClient, scoped_project_member
) -> None:
    client.app.state.settings.usage_events = True
    not_found = {
        "error": {"code": "not_found", "message": "Project does not exist.", "issues": None}
    }

    hidden = client.get(
        f"/projects/{scoped_project_member.hidden_project_id}/draft-quality",
        headers=scoped_project_member.member_headers,
    )
    missing = client.get(
        f"/projects/{uuid4()}/draft-quality",
        headers=scoped_project_member.member_headers,
    )

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json() == not_found
    assert _view_events(client) == []
    visible = client.get(
        f"/projects/{scoped_project_member.visible_project_id}/draft-quality",
        headers=scoped_project_member.member_headers,
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["change_set_count"] == 0


# --- commit keeps the review audit --------------------------------------------


def test_commit_preserves_edited_at_but_clears_other_operation_metadata(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    walk = _review_walkthrough(client, admin_auth_headers)

    detail = client.get(f"/graph-drafts/{walk.first_change_set_id}", headers=admin_auth_headers)
    assert detail.status_code == 200, detail.text
    operations = {op["operation_id"]: op for op in detail.json()["data"]["operations"]}

    edited = operations[walk.edited_operation_id]
    assert edited["status"] == "applied"
    assert set(edited["error_metadata"]) == {"edited_at", "edited_by"}
    assert edited["error_metadata"]["edited_at"]
    plain = operations[walk.plain_operation_id]
    assert plain["status"] == "applied"
    assert plain["error_metadata"] == {}
    rejected = operations[walk.rejected_operation_id]
    assert rejected["status"] == "rejected"
    assert rejected["error_metadata"]["review_note"] == "dup"
    assert rejected["error_metadata"]["reviewed_at"]
