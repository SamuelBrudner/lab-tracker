from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from lab_tracker.models import EntityType, GraphChangeOp, GraphChangeOperation, GraphChangeSet
from lab_tracker.proposal_email_content import proposal_detail_lines


def _draft():
    return GraphChangeSet(
        change_set_id=uuid4(),
        project_id=uuid4(),
        source_note_id=uuid4(),
        model="test",
        prompt_version="v1",
    )


def _operation(draft, entity_type, payload, **fields):
    operation = GraphChangeOperation(
        change_set_id=draft.change_set_id,
        operation_id=uuid4(),
        sequence=len(draft.operations),
        op=GraphChangeOp.UPDATE if "target_entity_id" in fields else GraphChangeOp.CREATE,
        entity_type=entity_type,
        payload=payload,
        **fields,
    )
    draft.operations.append(operation)
    return operation


def test_question_link_resolves_only_the_target_note_and_its_question():
    draft = _draft()
    note_id, question_id = uuid4(), uuid4()
    proposal = _operation(
        draft,
        EntityType.NOTE,
        {
            "targets": [{"entity_type": "question", "entity_id": str(question_id)}],
        },
        target_entity_id=note_id,
    )
    repo = Mock()
    repo.query_questions.return_value = (
        [
            SimpleNamespace(
                question_id=question_id,
                project_id=draft.project_id,
                text="Which samples changed?",
            )
        ],
        1,
    )
    repo.query_notes.return_value = (
        [
            SimpleNamespace(
                project_id=draft.project_id,
                note_id=note_id,
                raw_asset=None,
                raw_content="We counted the samples. " * 20 + "OMITTED TAIL",
            )
        ],
        1,
    )
    text = "\n".join(proposal_detail_lines(draft, proposal, repo))
    assert f"Record: {note_id}" in text
    assert f"Question to link ({question_id}): Which samples changed?" in text
    assert 'targets: [{"entity_type": "question", "entity_id": "' + str(question_id) + '"}]' in text
    assert "Note being updated (excerpt): We counted the samples." in text
    assert "OMITTED TAIL" not in text
    repo.query_notes.assert_called_once_with(
        project_id=draft.project_id, note_ids={note_id}, limit=1
    )
    repo.query_questions.assert_called_once_with(
        project_id=draft.project_id, question_ids={question_id}
    )


def test_dataset_links_name_draft_questions_and_keep_their_original_references():
    draft = _draft()
    for ref, text, project_id in (
        ("q1", "What was the duration?", draft.project_id),
        ("q2", "Unrelated content", draft.project_id),
        ("foreign", "Cross-project content", uuid4()),
    ):
        _operation(
            draft,
            EntityType.QUESTION,
            {
                "project_id": str(project_id),
                "text": text,
            },
            client_ref=ref,
        )
    proposal = _operation(
        draft,
        EntityType.DATASET,
        {
            "primary_question_id": {"$ref": "q1"},
            "secondary_question_ids": [{"$ref": "foreign"}],
            "status": "staged",
        },
    )
    repo = Mock()
    text = "\n".join(proposal_detail_lines(draft, proposal, repo))
    assert "Primary question (draft reference q1): What was the duration?" in text
    assert 'primary_question_id: {"$ref": "q1"}' in text
    assert "Secondary question (draft reference foreign): Question text unavailable" in text
    assert "status: staged" in text
    assert "Unrelated content" not in text and "Cross-project content" not in text
    assert not repo.mock_calls


def test_other_projects_and_attached_content_never_supply_excerpts():
    draft = _draft()
    note_id, question_id = uuid4(), uuid4()
    proposal = _operation(
        draft,
        EntityType.NOTE,
        {
            "targets": [{"entity_type": "question", "entity_id": str(question_id)}],
        },
        target_entity_id=note_id,
    )
    repo = Mock()
    repo.query_questions.return_value = (
        [
            SimpleNamespace(
                question_id=question_id,
                project_id=uuid4(),
                text="Private question from another project",
            )
        ],
        1,
    )
    repo.query_notes.return_value = (
        [
            SimpleNamespace(
                note_id=note_id,
                project_id=draft.project_id,
                raw_asset=object(),
                raw_content="Asset contents",
            )
        ],
        1,
    )
    text = "\n".join(proposal_detail_lines(draft, proposal, repo))
    assert f"Question to link ({question_id}): Question text unavailable" in text
    assert "Private question" not in text and "Asset contents" not in text
