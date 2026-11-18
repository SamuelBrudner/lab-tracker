"""Exercise machine authorship, scheduling and email with independent review operators."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import update
from test_proposal_email_round_trip import _prepare_assigned_mail, _reply, _send
from test_proposal_email_round_trip import mail_client as mail_client
from test_review_email_delivery import _BatchDraftClient
from test_review_mailbox import _email_user

from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.app_parts.runtime import _background_api
from lab_tracker.auth import AuthContext, Role
from lab_tracker.db_models import NoteModel
from lab_tracker.models import (
    EntityRef,
    EntityType,
    GraphChangeOperationStatus,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    Note,
    NoteStatus,
    Session,
    SessionType,
    utc_now,
)
from lab_tracker.services.graph_draft_context import _capture_placement


def _create_session(client, headers, project_id):
    response = client.post(
        "/sessions", json={"project_id": project_id, "session_type": "operational"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["data"]["session_id"])


def _machine_capture(api, project_id, sessions=()):
    return api.create_note(
        project_id,
        raw_content="Recording from the shared instrument",
        targets=[EntityRef(entity_type=EntityType.SESSION, entity_id=uid) for uid in sessions],
        status=NoteStatus.STAGED,
        metadata={"capture_channel": "ics"},
        origin_provider="ics",
        actor=system_auth_context(),
    )


def test_machine_capture_routes_only_to_assigned_reviewer_and_checks_reassignment(mail_client):
    client, admin = mail_client
    client, scientist, baseline_id, _ = _prepare_assigned_mail(
        client, admin, draft_client_factory=_BatchDraftClient
    )
    _send(client)
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        baseline = api.get_graph_change_set(baseline_id)
    project_id = baseline.project_id
    operator = baseline.review_assignee_user_id
    assert operator is not None
    session_id = _create_session(client, scientist, str(project_id))
    other_session = _create_session(client, scientist, str(project_id))

    other_id, _ = _email_user(client, "other-reviewer@example.edu")
    added = client.post(
        f"/projects/{project_id}/members",
        headers=admin,
        json={"user_id": str(other_id), "role": "contributor"},
    )
    assert added.status_code == 201, added.text
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        linked = _machine_capture(api, project_id, [session_id])
        ambiguous = _machine_capture(api, project_id, [session_id, other_session])
        unlinked = _machine_capture(api, project_id)
        # The reviewed window passed before these old captures were assigned.
        session.execute(
            update(NoteModel)
            .where(NoteModel.note_id.in_({linked.note_id, ambiguous.note_id, unlinked.note_id}))
            .values(created_at=utc_now() - timedelta(days=2))
        )
        session.commit()
        no_operator = api.run_graph_draft_batch_for_project(
            project_id,
            actor=AuthContext(operator, Role.EDITOR),
            draft_client=_BatchDraftClient(str(project_id)),
        )
        assert no_operator.status == GraphDraftBatchRunStatus.SKIPPED

    inbox = f"/projects/{project_id}/unassigned-machine-captures"
    assert client.get(inbox, headers=scientist).status_code == 403
    rows = client.get(inbox, headers=admin).json()["data"]
    assert {row["source"] for row in rows} == {"ics"}
    assert {row["note_id"]: row["route"]["status"] for row in rows} == {
        str(linked.note_id): "needs_operator",
        str(ambiguous.note_id): "ambiguous_session",
        str(unlinked.note_id): "needs_session",
    }
    setter = f"/review-operator/session/{session_id}"
    denied = client.put(setter, headers=scientist, json={"user_id": str(operator)})
    assert denied.status_code == 403, denied.text
    assigned_session = client.put(setter, headers=admin, json={"user_id": str(operator)})
    assert assigned_session.status_code == 200, assigned_session.text
    assigned_note = client.put(
        f"/review-operator/note/{unlinked.note_id}", headers=admin, json={"user_id": str(operator)}
    )
    assert assigned_note.status_code == 200, assigned_note.text
    assert [row["note_id"] for row in client.get(inbox, headers=admin).json()["data"]] == [
        str(ambiguous.note_id)
    ]

    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        default = GraphDraftBatchSettings(settings_id=uuid4(), project_id=project_id)
        reviewers = api.graph_drafts.scheduling.reservations.scheduled_reviewers_for_settings(
            default, until=utc_now()
        )
        review_ids = {reviewer.reviewer_user_id for reviewer in reviewers}
        assert operator in review_ids
        assert other_id not in review_ids
        run = api.run_graph_draft_batch_for_project(
            project_id,
            actor=AuthContext(operator, Role.EDITOR),
            draft_client=_BatchDraftClient(str(project_id)),
        )
        assert set(run.source_note_ids) == {linked.note_id, unlinked.note_id}
        assert run.status == GraphDraftBatchRunStatus.READY, run.error_metadata
        assert run.change_set_id is not None
        draft = api.get_graph_change_set(run.change_set_id)
        assert draft.review_assignee_user_id == operator
        assert api.get_note(linked.note_id).created_by == linked.created_by
        assert api.get_note(linked.note_id).capture_source == "ics"
        assert api.get_note(linked.note_id).origin_provider == "ics"

    invitations = _send(client)
    [proposal] = [msg for msg in invitations if "Private proposed question" in msg.get_content()]
    assert str(proposal["To"]) == "researcher@example.edu"

    reassign = client.put(setter, headers=admin, json={"user_id": str(other_id)})
    assert reassign.status_code == 200
    assert _reply(client, proposal, "Approve")["rejected"] == 1
    with client.app.state.db_session_factory() as session:
        api = _background_api(client.app, session)
        saved = api.get_graph_change_set(draft.change_set_id)
        assert all(op.status == GraphChangeOperationStatus.PROPOSED for op in saved.operations)
        new_run = api.run_graph_draft_batch_for_project(
            project_id,
            actor=AuthContext(other_id, Role.EDITOR),
            draft_client=_BatchDraftClient(str(project_id)),
        )
        assert new_run.source_note_ids == [linked.note_id]


def test_overlapping_session_clocks_are_reported_as_candidates():
    project = uuid4()
    now = utc_now()
    sessions = [
        Session(
            session_id=uuid4(),
            project_id=project,
            session_type=SessionType.OPERATIONAL,
            started_at=now - timedelta(minutes=minutes),
        )
        for minutes in (20, 10)
    ]
    note = Note(project_id=project, note_id=uuid4(), raw_content="A recording", created_at=now)
    placement = _capture_placement(note, sessions)
    assert placement["in_session"] is None
    assert placement["session_ambiguous"]
    assert {candidate["id"] for candidate in placement["session_candidates"]} == {
        str(session.session_id) for session in sessions
    }
