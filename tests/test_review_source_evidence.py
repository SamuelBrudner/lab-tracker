from uuid import uuid4

from lab_tracker.models import EntityType, GraphChangeOp, GraphChangeOperation, GraphChangeSet


def test_source_evidence_stays_within_cited_project_notes_and_saved_artifacts():
    project_id, draft_id, first, second, foreign, asset = [uuid4() for _ in range(6)]
    draft = GraphChangeSet(
        change_set_id=draft_id,
        project_id=project_id,
        source_note_id=first,
        source_note_ids=[first, second, foreign],
        model="fixture",
        prompt_version="fixture",
        context_packet={
            "source_artifacts": [
                {
                    "project_id": str(project_id),
                    "note_id": str(first),
                    "filename": "panel.png",
                    "artifact_id": str(asset),
                    "raw_content_preview": "Run one: sample loaded.",
                },
                {
                    "project_id": str(project_id),
                    "note_id": str(second),
                    "transcript_text": "Run two: sample loaded.",
                    "transcript_text_truncated": True,
                },
                {
                    "project_id": str(uuid4()),
                    "note_id": str(foreign),
                    "raw_content_preview": "Foreign observation",
                },
            ],
        },
    )
    operation = GraphChangeOperation(
        operation_id=uuid4(), change_set_id=draft_id, sequence=1,
        op=GraphChangeOp.CREATE, entity_type=EntityType.QUESTION,
        source_refs=[
            {"source_note_ids": [str(first)], "label": "panel", "region": {"x": 10}},
            {"source_note_ids": [str(first)], "quote": "Invented result"},
            {
                "source_note_ids": [str(first), str(second)],
                "source_note_ids_resolution": "explicit",
                "quote": "sample loaded",
            },
            {
                "source_note_ids": [str(first), str(second)],
                "source_note_ids_resolution": "explicit",
                "quote": "Run two",
            },
            {"source_note_ids": [str(foreign)], "quote": "Foreign observation"},
            {"quote": "Run one"},
        ],
    )
    draft.operations = [operation]
    locator, invented, shared, exact, out_of_scope, no_mapping = (
        draft.proposal_evidence[operation.operation_id]
    )
    assert locator.status == "stored_artifact"
    assert locator.artifact_id == asset and locator.note_id == first
    assert invented.status == "unverified" and invented.excerpt == "Run one: sample loaded."
    assert shared.status == no_mapping.status == "ambiguous"
    assert exact.status == "exact_excerpt" and exact.excerpt == "Run two"
    assert exact.note_id == second and exact.excerpt_kind == "transcript" and exact.text_shortened
    assert out_of_scope.status == "unverified" and out_of_scope.note_id is None

    draft.context_packet["review_attachment_evidence"] = {"status": "unavailable"}
    [unavailable] = draft.proposal_evidence[operation.operation_id]
    assert unavailable.status == "ambiguous" and unavailable.excerpt is None
