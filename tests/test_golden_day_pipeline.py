from __future__ import annotations

import json
from typing import Any

import pytest
from api_helpers import repository_backed_api
from test_graph_draft_batches import FakeBatchDraftClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.golden_day import (
    GOLDEN_DAY_CLARIFICATION,
    GOLDEN_DAY_QUESTIONS,
    IDENTIFIER_CAPTURE_SLUG,
    GoldenDayGraph,
    ScriptedGoldenDayDraftClient,
    capture_slug,
    expected_link_pairs,
    golden_day_expected_patch,
    golden_day_question_texts,
    score_golden_day,
    seed_golden_day,
)
from lab_tracker.graph_drafting import BATCH_PROMPT_VERSION
from lab_tracker.models import (
    ExplorationNodeType,
    GraphChangeOperationStatus,
    GraphChangeSetStatus,
    GraphDraftSemanticType,
    Note,
    QuestionStatus,
)
from lab_tracker.services.shared import is_meeting_note

ADMIN = AuthContext(user_id=LOCAL_AUTH_USER_ID, role=Role.ADMIN)


@pytest.fixture()
def api() -> LabTrackerAPI:
    return repository_backed_api()


@pytest.fixture()
def golden_day(api: LabTrackerAPI) -> tuple[GoldenDayGraph, list[Note]]:
    project = api.create_project("Golden day", actor=ADMIN)
    return seed_golden_day(api, project_id=project.project_id, actor=ADMIN)


def _identifier_note(notes: list[Note]) -> Note:
    [note] = [note for note in notes if capture_slug(note) == IDENTIFIER_CAPTURE_SLUG]
    return note


def test_golden_day_fixture_shape(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day

    assert len(graph.questions) == len(GOLDEN_DAY_QUESTIONS) == 12
    project_questions = api.list_questions(project_id=graph.project_id)
    # The refactor adds the replacement, so the project holds one more than the created set.
    assert len(project_questions) == 13
    superseded = [q for q in project_questions if q.status == QuestionStatus.SUPERSEDED]
    assert [q.question_id for q in superseded] == [graph.superseded_question_id]
    assert superseded[0].superseded_by_question_id == graph.replacement_question_id
    assert len(graph.sessions) == 2
    assert len(api.list_sessions(project_id=graph.project_id)) == 2
    dead_ends = api.list_exploration_nodes(
        project_id=graph.project_id, node_type=ExplorationNodeType.DEAD_END
    )
    assert [node.node_id for node in dead_ends] == [graph.dead_end_node_id]
    assert [goal.goal_id for goal in api.list_goals(project_id=graph.project_id)] == [
        graph.goal_id
    ]
    assert len(notes) == 14
    assert sum(1 for note in notes if is_meeting_note(note)) == 1
    assert _identifier_note(notes).raw_content == "M7-0925-03"


def test_golden_day_batch_links_targets_and_requests_clarification(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    fake = FakeBatchDraftClient(golden_day_expected_patch(graph, notes))

    change_set = api.create_batch_graph_draft(notes, draft_client=fake, actor=ADMIN)

    assert change_set.status == GraphChangeSetStatus.READY
    assert change_set.provider == "fake"
    assert change_set.prompt_version == BATCH_PROMPT_VERSION
    note_ids = {note.note_id for note in notes}
    question_ids = set(graph.questions.values()) | {graph.replacement_question_id}
    linked_pairs: set[tuple[Any, Any]] = set()
    for operation in change_set.operations:
        if operation.semantic_type != GraphDraftSemanticType.LINK_NOTE_TO_QUESTION:
            continue
        assert operation.target_entity_id in note_ids
        for target in operation.payload["targets"]:
            assert target["entity_type"] == "question"
            target_id = target["entity_id"]
            assert str(target_id) != str(graph.superseded_question_id)
            assert any(str(target_id) == str(qid) for qid in question_ids)
            linked_pairs.add((str(operation.target_entity_id), str(target_id)))
    assert linked_pairs == {
        (str(note_id), str(question_id))
        for note_id, question_id in expected_link_pairs(graph, notes)
    }
    clarifications = [
        operation
        for operation in change_set.operations
        if operation.semantic_type == GraphDraftSemanticType.REQUEST_CLARIFICATION
    ]
    assert len(clarifications) == 1
    assert clarifications[0].target_entity_id == _identifier_note(notes).note_id
    assert change_set.clarification_requests == [GOLDEN_DAY_CLARIFICATION]

    [call] = fake.calls
    batch_context = call["batch_context"]
    assert batch_context["context_summary"]["counts"]["meeting_notes"] == 1
    superseded_aliases = [
        alias
        for project in batch_context["projects"]
        for alias in project["known_aliases"]
        if alias.get("relationship") == "superseded_alias_for_replacement"
    ]
    assert [alias["superseded_entity_id"] for alias in superseded_aliases] == [
        str(graph.superseded_question_id)
    ]
    # The caller owns the client on the direct path; only the scheduler closes it.
    assert fake.closed is False


def test_score_is_perfect_for_the_expected_patch(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    client = ScriptedGoldenDayDraftClient(golden_day_expected_patch(graph, notes))

    change_set = api.create_batch_graph_draft(notes, draft_client=client, actor=ADMIN)
    score = score_golden_day(change_set, graph, notes)

    assert change_set.status == GraphChangeSetStatus.READY
    assert score.provider == "golden-day"
    assert score.model == "scripted-v1"
    assert score.prompt_version == BATCH_PROMPT_VERSION
    assert score.link_precision == 1.0
    assert score.link_recall == 1.0
    assert score.duplicate_create_rate == 0.0
    assert score.clarification_rate == pytest.approx(1 / 14)
    assert score.operation_count == len(change_set.operations) > 0


def test_score_detects_duplicate_and_missing_links(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    link_ops = [
        op for op in patch["operations"] if op["semantic_type"] == "link_note_to_question"
    ]
    dropped = link_ops[0]
    patch["operations"].remove(dropped)
    [create_op] = [op for op in patch["operations"] if op["op"] == "create"]
    payload = json.loads(create_op["payload_json"])
    payload["text"] = golden_day_question_texts()["dose_response"].upper() + "  "
    create_op["payload_json"] = json.dumps(payload)

    change_set = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    score = score_golden_day(change_set, graph, notes)

    assert change_set.status == GraphChangeSetStatus.READY
    assert score.duplicate_create_rate == 1.0
    assert score.link_precision == 1.0
    assert score.link_recall < 1.0


def test_golden_day_change_set_feeds_the_draft_quality_ledger(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    fake = FakeBatchDraftClient(golden_day_expected_patch(graph, notes))
    change_set = api.create_batch_graph_draft(notes, draft_client=fake, actor=ADMIN)
    question_links = [
        op
        for op in change_set.operations
        if op.semantic_type == GraphDraftSemanticType.LINK_NOTE_TO_QUESTION
    ]
    for operation in question_links[:2]:
        api.update_graph_change_operation(
            change_set.change_set_id,
            operation.operation_id,
            status=GraphChangeOperationStatus.ACCEPTED,
            actor=ADMIN,
        )
    api.update_graph_change_operation(
        change_set.change_set_id,
        question_links[2].operation_id,
        status=GraphChangeOperationStatus.REJECTED,
        actor=ADMIN,
    )

    ledger = api.draft_quality_ledger(graph.project_id, actor=ADMIN)

    assert ledger.change_set_count == 1
    [group] = ledger.groups
    assert (group.provider, group.model, group.prompt_version) == (
        "fake",
        "fake-batch-model",
        BATCH_PROMPT_VERSION,
    )
    assert group.change_set_count == 1
    assert group.clarification_request_count == 1
    assert group.change_sets_with_clarifications == 1
    assert group.median_seconds_to_first_accept is not None
    assert group.median_seconds_to_first_accept >= 0.0
    assert group.median_seconds_to_review is None
    cells = {cell.semantic_type: cell for cell in ledger.cells}
    link_cell = cells[GraphDraftSemanticType.LINK_NOTE_TO_QUESTION]
    assert link_cell.proposed == len(question_links) == 11
    assert link_cell.accepted_total == link_cell.accepted_human_selected == 2
    assert link_cell.accepted_bulk_accepted == 0
    assert link_cell.edited_before_accept == 0
    assert link_cell.rejected == 1
    assert link_cell.left_proposed_at_commit == 0
    assert cells[GraphDraftSemanticType.REQUEST_CLARIFICATION].proposed == 1
    assert cells[GraphDraftSemanticType.SUGGEST_NEW_QUESTION].proposed == 1
