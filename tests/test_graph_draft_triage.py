from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from test_graph_draft_batches import FakeBatchDraftClient, _batch_patch, _note
from test_graph_drafts import (
    FakeDraftClient,
    _autonomy_request_api,
    _draft_patch,
    _image_note,
    _project,
)

from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import AuthError
from lab_tracker.graph_draft_triage import review_triage
from lab_tracker.models import (
    DEFERRED_AT_KEY,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftSemanticType,
)
from lab_tracker.services.graph_draft_batch_policy import BatchReviewer


def _operation(sequence=0, **kwargs):
    return GraphChangeOperation(
        operation_id=uuid4(),
        change_set_id=uuid4(),
        sequence=sequence,
        op=kwargs.pop("op", GraphChangeOp.CREATE),
        entity_type=kwargs.pop("entity_type", EntityType.QUESTION),
        semantic_type=kwargs.pop("semantic_type", GraphDraftSemanticType.SUGGEST_NEW_QUESTION),
        payload=kwargs.pop("payload", {"text": "A research question"}),
        **kwargs,
    )


def _draft(operations):
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=uuid4(),
        source_note_id=uuid4(),
        model="fixture",
        prompt_version="fixture",
        operations=operations,
        context_packet={
            "batch_notes": [
                {"id": str(op.target_entity_id), "targets": []}
                for op in operations
                if op.entity_type == EntityType.NOTE
            ]
        },
    )


def test_triage_groups_dependencies_but_not_unrelated_claims_from_the_same_capture():
    question = _operation(client_ref="question")
    evidence = _operation(
        1,
        entity_type=EntityType.NOTE,
        payload={
            "raw_content": "An observation",
            "targets": [{"entity_id": {"$ref": "question"}}],
        },
    )
    separate = _operation(2, source_refs=question.source_refs)
    triage = review_triage(_draft([question, evidence, separate]))
    assert triage["summary"]["judgment"] == 2
    assert triage["groups"][0]["operation_ids"] == [
        str(question.operation_id),
        str(evidence.operation_id),
    ]


def test_consequence_precedes_links_and_confidence_does_not_demote_a_decision():
    target = uuid4()
    link = _operation(
        entity_type=EntityType.NOTE,
        op=GraphChangeOp.UPDATE,
        semantic_type=GraphDraftSemanticType.LINK_NOTE_TO_QUESTION,
        target_entity_id=uuid4(),
        payload={"targets": [{"entity_type": "question", "entity_id": str(target)}]},
        confidence=1.0,
    )
    pivot = _operation(1, semantic_type=GraphDraftSemanticType.RECORD_PIVOT, confidence=0.01)
    groups = review_triage(_draft([link, pivot]))["groups"]
    assert groups[0]["operation_ids"] == [str(pivot.operation_id)]
    assert groups[0]["lane"] == "judgment"
    assert groups[1]["lane"] == "confirmations"
    # A note-content change masquerading as a link requires judgment.
    link.payload["raw_content"] = "A changed interpretation"
    assert review_triage(_draft([link]))["groups"][0]["lane"] == "judgment"


def test_deferred_decision_is_preserved_and_can_be_identified_for_resumption():
    operation = _operation(error_metadata={DEFERRED_AT_KEY: "2026-10-13T12:00:00+00:00"})
    triage = review_triage(_draft([operation]))
    assert triage["summary"]["judgment"] == 0
    assert triage["summary"]["deferred"] == 1
    assert triage["groups"][0]["deferred_operation_ids"] == [str(operation.operation_id)]


def _create_review(client, headers):
    project_id = _project(client, headers)
    note_id = _image_note(client, headers, project_id)
    patch = _draft_patch(project_id)
    patch["clarification_requests"] = ["Which session does this evidence belong to?"]
    patch["operations"].append({**patch["operations"][0], "client_ref": "unrelated"})
    client.app.state.graph_draft_client_factory = lambda settings: FakeDraftClient(patch)
    response = client.post(f"/notes/{note_id}/graph-drafts", headers=headers)
    assert response.status_code == 201
    return response.json()["data"]


def test_group_accept_is_scoped_audited_and_rejects_partial_or_stale_selection(
    client, admin_auth_headers
):
    draft = _create_review(client, admin_auth_headers)
    url = f"/graph-drafts/{draft['change_set_id']}/triage-decision"
    group = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")
    ids = group["pending_operation_ids"]
    assert len(ids) == 2
    partial = client.post(
        url,
        json={
            "expected_revision": group["revision"],
            "operation_ids": ids[:1],
            "decision": "accepted",
        },
        headers=admin_auth_headers,
    )
    assert partial.status_code == 422
    accepted = client.post(
        url,
        json={"expected_revision": group["revision"], "operation_ids": ids, "decision": "accepted"},
        headers=admin_auth_headers,
    )
    assert accepted.status_code == 200
    operations = accepted.json()["data"]["operations"]
    assert [op["status"] for op in operations] == ["accepted", "accepted", "proposed"]
    assert all(op["acceptance_mode"] == "bulk_accepted" for op in operations[:2])
    assert (
        client.post(
            url,
            json={
                "expected_revision": group["revision"],
                "operation_ids": ids,
                "decision": "accepted",
            },
            headers=admin_auth_headers,
        ).status_code
        == 422
    )


def test_group_deferral_survives_reload_and_resumes_without_acceptance(client, admin_auth_headers):
    draft = _create_review(client, admin_auth_headers)
    url = f"/graph-drafts/{draft['change_set_id']}/triage-decision"
    ids = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")[
        "pending_operation_ids"
    ]
    group = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")
    assert (
        client.post(
            url,
            json={
                "expected_revision": group["revision"],
                "operation_ids": ids,
                "decision": "deferred",
            },
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    reloaded = client.get(
        f"/graph-drafts/{draft['change_set_id']}", headers=admin_auth_headers
    ).json()["data"]
    assert reloaded["triage_summary"]["deferred"] == 1
    resumed = client.post(
        url,
        json={
            "expected_revision": next(
                group for group in reloaded["triage"]["groups"] if group["deferred"]
            )["revision"],
            "operation_ids": ids,
            "decision": "resume",
        },
        headers=admin_auth_headers,
    ).json()["data"]
    assert resumed["triage_summary"]["deferred"] == 0
    assert all(
        op["status"] == "proposed" and op["acceptance_mode"] is None for op in resumed["operations"]
    )


def test_invalid_group_edit_rolls_back_the_whole_acceptance(client, admin_auth_headers):
    draft = _create_review(client, admin_auth_headers)
    ids = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")[
        "pending_operation_ids"
    ]
    group = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")
    bad_payload = {**draft["operations"][1]["payload"], "raw_content": {"invalid": True}}
    response = client.post(
        f"/graph-drafts/{draft['change_set_id']}/triage-decision",
        json={
            "expected_revision": group["revision"],
            "operation_ids": ids,
            "decision": "accepted",
            "edits": {ids[1]: {"payload": bad_payload}},
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 422
    current = client.get(
        f"/graph-drafts/{draft['change_set_id']}", headers=admin_auth_headers
    ).json()["data"]
    assert all(op["status"] == "proposed" for op in current["operations"])


def test_clarification_answer_is_audited_without_accepting_an_operation(client, admin_auth_headers):
    draft = _create_review(client, admin_auth_headers)
    question = draft["clarification_requests"][0]
    response = client.post(
        f"/graph-drafts/{draft['change_set_id']}/triage-clarification",
        json={
            "question": question,
            "decision": "answered",
            "answer": "Use the planned control cohort.",
        },
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    current = response.json()["data"]
    assert current["triage_summary"]["clarifications"] == 0
    assert all(operation["status"] == "proposed" for operation in current["operations"])
    annotation = next(iter(current["review_metadata"]["review_triage_clarifications"].values()))
    assert annotation["reviewed_by"] and annotation["reviewed_at"]
    assert annotation["answer"] == "Use the planned control cohort."


def test_saved_clarification_reaches_future_batch_and_note_context(client, admin_auth_headers):
    draft = _create_review(client, admin_auth_headers)
    question = draft["clarification_requests"][0]
    response = client.post(
        f"/graph-drafts/{draft['change_set_id']}/triage-clarification",
        json={"question": question, "decision": "answered", "answer": "The afternoon session."},
        headers=admin_auth_headers,
    )
    assert response.status_code == 200
    saved = response.json()["data"]
    reviewer_id = UUID(saved["created_by_user_id"])
    with _autonomy_request_api(client) as api:
        note = api.get_note(UUID(draft["source_note_id"]))
        batch = api.build_batch_graph_context(
            [note], context_owner=BatchReviewer(str(reviewer_id), reviewer_id)
        )
        note_context = api.build_graph_context_for_note(
            note.note_id, actor=AuthContext(user_id=reviewer_id, role=Role.ADMIN)
        )
    for packet in [batch, note_context]:
        history = packet["review_memory"]["recent_draft_context"]
        assert history[0]["change_set_id"] == draft["change_set_id"]
        assert history[0]["summary"] == draft["summary"]
        clarification = history[0]["clarifications"][0]
        assert clarification["question"] == question
        assert clarification["answer"] == "The afternoon session."
        assert clarification["reviewed_by"] == str(reviewer_id)
    assert batch["context_summary"]["counts"]["recent_draft_context"] == 1
    # Saving an answer leaves both the original provider input and graph edits intact.
    assert saved["context_packet"] == draft["context_packet"]
    assert all(op["status"] == "proposed" for op in saved["operations"])


def test_partial_commit_carries_unfinished_decisions_and_resolves_committed_refs(
    client, admin_auth_headers
):
    draft = _create_review(client, admin_auth_headers)
    url = f"/graph-drafts/{draft['change_set_id']}"
    parent, child, _ = draft["operations"]
    assert (
        client.patch(
            f"{url}/operations/{parent['operation_id']}",
            json={"status": "accepted"},
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{url}/triage-decision",
            json={
                "expected_revision": next(
                    group
                    for group in client.get(url, headers=admin_auth_headers).json()["data"][
                        "triage"
                    ]["groups"]
                    if child["operation_id"] in group["pending_operation_ids"]
                )["revision"],
                "operation_ids": [child["operation_id"]],
                "decision": "deferred",
            },
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    committed = client.post(
        f"{url}/commit",
        json={"message": "Keep the question; review the evidence later"},
        headers=admin_auth_headers,
    )
    assert committed.status_code == 200
    original = committed.json()["data"]
    followup_id = original["review_metadata"]["review_triage_carryover_to"]
    followup = client.get(f"/graph-drafts/{followup_id}", headers=admin_auth_headers).json()["data"]
    assert followup["status"] == "ready"
    assert followup["review_metadata"]["review_triage_carryover_from"] == draft["change_set_id"]
    assert followup["triage_summary"]["deferred"] == 1
    assert len(followup["operations"]) == 2
    assert (
        followup["operations"][0]["payload"]["targets"][0]["entity_id"]
        == original["operations"][0]["result_entity_id"]
    )
    deferred = next(group for group in followup["triage"]["groups"] if group["deferred"])
    assert (
        client.post(
            f"/graph-drafts/{followup_id}/triage-decision",
            json={
                "expected_revision": deferred["revision"],
                "operation_ids": deferred["deferred_operation_ids"],
                "decision": "resume",
            },
            headers=admin_auth_headers,
        ).status_code
        == 200
    )


def test_group_decisions_require_the_reviewers_project_access(
    client, admin_auth_headers, viewer_user
):
    draft = _create_review(client, admin_auth_headers)
    group = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")
    response = client.post(
        f"/graph-drafts/{draft['change_set_id']}/triage-decision",
        json={
            "expected_revision": group["revision"],
            "operation_ids": group["pending_operation_ids"],
            "decision": "accepted",
        },
        headers=viewer_user.headers,
    )
    assert response.status_code in {403, 404}


def test_review_engagement_counts_human_open_and_decision_but_not_prefetch(
    client, admin_auth_headers, viewer_user
):
    client.app.state.settings.usage_events = True
    project_id = _project(client, admin_auth_headers)
    _note(client, admin_auth_headers, project_id, "A staged comparison")
    client.app.state.graph_draft_client_factory = lambda settings: FakeBatchDraftClient(
        _batch_patch(project_id)
    )
    run = client.post(
        "/batches/run-now", json={"project_id": project_id}, headers=admin_auth_headers
    )
    assert run.status_code == 201
    draft_id = run.json()["data"]["change_set_id"]
    report_url = f"/projects/{project_id}/review-engagement"
    prefetched = client.get(
        f"/graph-drafts/{draft_id}?record_view=false", headers=admin_auth_headers
    )
    assert prefetched.status_code == 200
    report = client.get(report_url, headers=admin_auth_headers).json()["data"]["reviewers"][0]
    assert report["assigned"] == 1 and report["opened"] == 0 and report["decisions_started"] == 0
    visible = client.get(f"/graph-drafts/{draft_id}", headers=admin_auth_headers).json()["data"]
    group = next(group for group in visible["triage"]["groups"] if group["kind"] == "operations")
    assert (
        client.post(
            f"/graph-drafts/{draft_id}/triage-decision",
            json={
                "expected_revision": group["revision"],
                "operation_ids": group["pending_operation_ids"],
                "decision": "accepted",
            },
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    report = client.get(report_url, headers=admin_auth_headers).json()["data"]["reviewers"][0]
    assert report["opened"] == 1 and report["decisions_started"] == 1
    assert report["median_seconds_to_first_decision"] is not None
    assert client.get(report_url, headers=viewer_user.headers).status_code in {403, 404}


def test_stale_group_cannot_overwrite_a_newer_correction(client, admin_auth_headers):
    draft = _create_review(client, admin_auth_headers)
    group = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")
    operation = draft["operations"][0]
    newer = {**operation["payload"], "text": "A corrected research question"}
    url = f"/graph-drafts/{draft['change_set_id']}"
    assert (
        client.patch(
            f"{url}/operations/{operation['operation_id']}",
            json={"payload": newer},
            headers=admin_auth_headers,
        ).status_code
        == 200
    )
    stale = client.post(
        f"{url}/triage-decision",
        json={
            "operation_ids": group["pending_operation_ids"],
            "expected_revision": group["revision"],
            "decision": "accepted",
            "edits": {operation["operation_id"]: {"payload": operation["payload"]}},
        },
        headers=admin_auth_headers,
    )
    assert stale.status_code == 422
    current = client.get(url, headers=admin_auth_headers).json()["data"]
    assert current["operations"][0]["payload"]["text"] == "A corrected research question"
    assert current["operations"][0]["status"] == "proposed"


def test_system_actor_cannot_use_a_group_decision_to_bypass_human_review(
    client, admin_auth_headers
):
    draft = _create_review(client, admin_auth_headers)
    group = next(group for group in draft["triage"]["groups"] if group["kind"] == "operations")
    with _autonomy_request_api(client) as api, pytest.raises(AuthError):
        api.decide_graph_draft_group(
            UUID(draft["change_set_id"]),
            operation_ids=[UUID(item) for item in group["pending_operation_ids"]],
            decision="accepted",
            expected_revision=group["revision"],
            actor=system_auth_context(),
        )
