from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event
from test_graph_draft_batches import _register_project_member
from test_graph_draft_delegation import _grant
from test_review_email_delivery import _admin_user_id, _BatchDraftClient, _project_and_note

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, PrincipalType, Role
from lab_tracker.errors import ConflictError, PermissionDeniedError, ValidationError
from lab_tracker.models import AcceptanceMode, GraphChangeOperationStatus, GraphChangeSetStatus
from lab_tracker.services.proposal_email_decisions import (
    EmailReviewCommand,
    apply_proposal_email_decision,
    correctable_fields,
    operation_fingerprint,
    parse_review_reply,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


@pytest.fixture
def assigned(client, admin_auth_headers):
    with _assigned_batch(client, admin_auth_headers) as batch:
        yield batch


@contextmanager
def _assigned_batch(client, admin_auth_headers):
    project_id, _ = _project_and_note(client, admin_auth_headers)
    headers = _register_project_member(
        client,
        owner_headers=admin_auth_headers,
        project_id=project_id,
        role="contributor",
        global_role=Role.VIEWER,
    )
    user_id = UUID(_admin_user_id(client, headers))
    actor = AuthContext(user_id=user_id, role=Role.VIEWER)
    note = client.post(
        "/notes",
        headers=headers,
        json={"project_id": project_id, "raw_content": "A lab observation", "status": "staged"},
    )
    assert note.status_code == 201, note.text
    with client.app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=client.app.state.settings,
            raw_storage=client.app.state.raw_note_storage,
        )
        run = api.run_graph_draft_batch_for_project(
            UUID(project_id), draft_client=_BatchDraftClient(project_id), actor=actor
        )
        draft = api.get_graph_change_set(run.change_set_id)
        operation = draft.operations[0]
        assert draft.review_assignee_user_id == user_id
        yield api, actor, operation, project_id


def _apply(api, actor, operation, reply):
    return apply_proposal_email_decision(
        api.graph_drafts.review,
        actor=actor,
        change_set_id=operation.change_set_id,
        operation_id=operation.operation_id,
        expected_revision=operation_fingerprint(operation),
        command=parse_review_reply(reply),
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "looks good",
        "Approve?",
        "> Approve",
        "approved but change its question",
        "Approve\nCorrect: actually wait",
        "Correct:   ",
        "Correct: change it\nApprove",
        "Correct: " + "x" * 4000,
        "Correct: a\x00b",
    ],
)
def test_ambiguous_reply_does_not_create_command(text):
    with pytest.raises(ValidationError):
        parse_review_reply(text)


def test_email_commands_accept_case_and_surrounding_whitespace():
    assert parse_review_reply("  aPPrOvE\r\n") == EmailReviewCommand("approve")
    assert parse_review_reply("CORRECT TEXT: A better question?") == EmailReviewCommand(
        "correct", "text", "A better question?"
    )


def test_field_correction_then_approval_requires_current_version(assigned):
    api, actor, first, _ = assigned
    assert correctable_fields(first) == ("text",)
    assert _apply(api, actor, first, "Correct text: Which trial was this?") == "corrected"
    revised = api.get_graph_change_set(first.change_set_id).operations[0]
    assert revised.payload["text"] == "Which trial was this?"
    assert revised.error_metadata["edited_by"] == str(actor.user_id)
    assert revised.status == GraphChangeOperationStatus.PROPOSED
    with pytest.raises(ConflictError):
        _apply(api, actor, first, "Approve")
    assert _apply(api, actor, revised, "Approve") == "approved"
    draft = api.get_graph_change_set(first.change_set_id)
    accepted = draft.operations[0]
    assert draft.status == GraphChangeSetStatus.READY
    assert accepted.status == GraphChangeOperationStatus.ACCEPTED
    assert accepted.accepted_by_user_id == actor.user_id
    assert accepted.acceptance_mode == AcceptanceMode.HUMAN_SELECTED
    assert accepted.result_entity_id is None
    with pytest.raises(ConflictError):
        _apply(api, actor, revised, "Approve")


def test_feedback_is_recorded_as_deferred_review(assigned):
    api, actor, first, _ = assigned
    assert (
        _apply(api, actor, first, "Correct: Attribute this to session two.")
        == "correction_requested"
    )
    held = api.get_graph_change_set(first.change_set_id).operations[0]
    assert held.payload == first.payload
    assert held.status == GraphChangeOperationStatus.PROPOSED
    assert held.review_note == "Attribute this to session two."
    assert held.error_metadata["deferred_by"] == str(actor.user_id)
    with pytest.raises(ConflictError):
        _apply(api, actor, held, "Approve")


def test_delegation_rechecks_snapshot_after_email_correction(assigned, client, admin_auth_headers):
    api, actor, first, project_id = assigned
    generated = api.get_graph_change_set(first.change_set_id)
    assert _apply(api, actor, first, "Correct text: Which trial was recorded?") == "corrected"
    _grant(client, admin_auth_headers, project_id, "full")

    # Generation can hand the pass a snapshot captured before the scientist replied.
    current = api.apply_delegated_curation(generated)
    assert current.status == GraphChangeSetStatus.READY
    assert current.operations[0].status == GraphChangeOperationStatus.PROPOSED
    assert current.operations[0].payload["text"] == "Which trial was recorded?"
    assert current.operations[0].accepted_at is None


def test_sender_permissions_project_role_and_structured_ids(assigned, client, admin_auth_headers):
    api, actor, first, project_id = assigned
    with pytest.raises(PermissionDeniedError):
        _apply(api, AuthContext(user_id=uuid4(), role=Role.ADMIN), first, "Approve")
    with pytest.raises(PermissionDeniedError):
        _apply(
            api,
            AuthContext(
                user_id=actor.user_id,
                role=Role.ADMIN,
                principal_type=PrincipalType.SERVICE,
            ),
            first,
            "Approve",
        )
    with pytest.raises(ValidationError):
        _apply(api, actor, first, f"Correct project_id: {uuid4()}")
    demoted = client.patch(
        f"/projects/{project_id}/members/{actor.user_id}",
        headers=admin_auth_headers,
        json={"role": "viewer"},
    )
    assert demoted.status_code == 200, demoted.text
    with pytest.raises(PermissionDeniedError):
        _apply(api, actor, first, "Approve")
    current = api.get_graph_change_set(first.change_set_id).operations[0]
    assert operation_fingerprint(current) == operation_fingerprint(first)


def _exercise_simultaneous_web_edit(client, admin_auth_headers):
    """The second writer must see the reviewer's uncommitted approval after it waits."""
    with _assigned_batch(client, admin_auth_headers) as (api, actor, first, _):
        web_at_write = Event()

        def add_web_review_note():
            # The normal PATCH handler calls this API inside a request-managed transaction.
            with client.app.state.db_session_factory() as session:
                repo = SQLAlchemyLabTrackerRepository(session)
                web_api = client.app.state.lab_tracker_api.for_request(repo)

                def web_attempting_write(conn, cursor, statement, params, context, many):
                    sql = statement.lower()
                    if "graph_change_sets" in sql and (
                        "for update" in sql or sql.startswith("update")
                    ):
                        web_at_write.set()

                event.listen(session.connection(), "before_cursor_execute", web_attempting_write)
                web_api.update_graph_change_operation(
                    first.change_set_id,
                    first.operation_id,
                    review_note="Control checked in the notebook.",
                    actor=actor,
                )
                session.commit()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with api.graph_drafts.review.application_transaction():
                assert _apply(api, actor, first, "Approve") == "approved"
                future = executor.submit(add_web_review_note)
                assert web_at_write.wait(timeout=5)
            future.result(timeout=10)
        current = api.get_graph_change_set(first.change_set_id).operations[0]
        assert current.status == GraphChangeOperationStatus.ACCEPTED
        assert current.review_note == "Control checked in the notebook."


def test_sqlite_web_edit_preserves_concurrent_email_approval(client, admin_auth_headers):
    _exercise_simultaneous_web_edit(client, admin_auth_headers)


@pytest.mark.postgres
def test_postgres_web_edit_preserves_concurrent_email_approval(
    postgres_client,
    postgres_admin_auth_headers,
):
    _exercise_simultaneous_web_edit(postgres_client, postgres_admin_auth_headers)
