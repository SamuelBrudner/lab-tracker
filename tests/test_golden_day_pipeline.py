from __future__ import annotations

import json
from typing import Any

import pytest
from api_helpers import repository_backed_api
from test_graph_draft_batches import FakeBatchDraftClient

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import LOCAL_AUTH_USER_ID, AuthContext, Role
from lab_tracker.golden_day import (
    AMBIGUOUS_CAPTURE_SLUG,
    DOSE_QUESTION_SLUGS,
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
    assert [goal.goal_id for goal in api.list_goals(project_id=graph.project_id)] == [graph.goal_id]
    assert len(notes) == 15
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
    assert len(clarifications) == 2
    assert clarifications[0].target_entity_id == _identifier_note(notes).note_id
    assert change_set.clarification_requests[0] == GOLDEN_DAY_CLARIFICATION

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
    context_notes = {item["id"]: item for item in batch_context["batch_notes"]}
    for note in notes:
        context_note = context_notes[str(note.note_id)]
        if capture_slug(note) in {"bench_dose_1", "bench_dose_2", "figure"}:
            assert context_note["metadata"]["declared_question_id"] == str(
                graph.question_id("dose_response")
            )
            assert "Researcher-selected" in context_note["metadata"]["question_routing_basis"]
        elif capture_slug(note) == AMBIGUOUS_CAPTURE_SLUG:
            assert "declared_question_id" not in context_note["metadata"]
            assert "No question was selected" in context_note["preview"]
    supplied_question_ids = {
        item["id"]
        for project in batch_context["projects"]
        for item in project["active_or_staged_questions"]
    }
    assert all(
        str(graph.question_id(slug)) in supplied_question_ids for slug in DOSE_QUESTION_SLUGS
    )


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
    assert score.clarification_rate == pytest.approx(2 / 15)
    assert score.clarification_recall == score.proposal_precision == score.proposal_recall == 1.0
    assert score.ambiguity_link_rate == 0.0
    assert score.operation_count == len(change_set.operations) > 0


def test_score_detects_duplicate_and_missing_links(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    link_ops = [op for op in patch["operations"] if op["semantic_type"] == "link_note_to_question"]
    dropped = link_ops[0]
    patch["operations"].remove(dropped)
    for operation in patch["operations"]:
        if (
            operation["semantic_type"] == "link_note_to_session"
            and operation["target_entity_id"] == dropped["target_entity_id"]
        ):
            payload = json.loads(operation["payload_json"])
            payload["targets"] = [
                target for target in payload["targets"] if target["entity_type"] != "question"
            ]
            operation["payload_json"] = json.dumps(payload)
    create_op = next(op for op in patch["operations"] if op["op"] == "create")
    payload = json.loads(create_op["payload_json"])
    payload["text"] = golden_day_question_texts()["dose_response"].upper() + "  "
    create_op["payload_json"] = json.dumps(payload)

    change_set = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    score = score_golden_day(change_set, graph, notes)

    assert change_set.status == GraphChangeSetStatus.READY
    assert score.duplicate_create_rate == 0.5
    assert score.link_precision == 1.0
    assert score.link_recall < 1.0


def test_score_counts_question_targets_in_combined_and_generic_updates(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    for operation in patch["operations"]:
        if operation["semantic_type"] == "link_note_to_question":
            operation["semantic_type"] = "update_entity"
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.link_precision == 1.0
    assert score.link_recall == 1.0


def test_score_detects_a_later_target_update_removing_a_question_link(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    session_link = next(
        item for item in patch["operations"] if item["semantic_type"] == "link_note_to_session"
    )
    payload = json.loads(session_link["payload_json"])
    payload["targets"] = [item for item in payload["targets"] if item["entity_type"] == "session"]
    session_link["payload_json"] = json.dumps(payload)
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    count = len(expected_link_pairs(graph, notes))
    assert score.link_precision == 1.0
    assert score.link_recall == pytest.approx((count - 1) / count)


def test_score_grades_wrong_source_new_question_links_separately(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    create = next(item for item in patch["operations"] if item["op"] == "create")
    link = next(
        item for item in patch["operations"] if item["semantic_type"] == "link_note_to_question"
    )
    payload = json.loads(link["payload_json"])
    payload["targets"].append(
        {
            "entity_type": "question",
            "entity_id": {"$ref": create["client_ref"]},
        }
    )
    link["payload_json"] = json.dumps(payload)
    # A later session link carries targets forward; keep the unmatched prediction
    # in the final target state rather than immediately overwriting it.
    for operation in patch["operations"]:
        if (
            operation["semantic_type"] == "link_note_to_session"
            and operation["target_entity_id"] == link["target_entity_id"]
        ):
            session_payload = json.loads(operation["payload_json"])
            session_payload["targets"].append(payload["targets"][-1])
            operation["payload_json"] = json.dumps(session_payload)
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.link_precision == 1.0
    assert score.link_recall == 1.0
    assert score.proposal_precision == pytest.approx(4 / 5)


@pytest.mark.parametrize("question_slug", DOSE_QUESTION_SLUGS[1:])
def test_score_rejects_a_wrong_near_duplicate_despite_similar_text(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]], question_slug: str
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    note = next(note for note in notes if capture_slug(note) == "bench_dose_1")
    for operation in patch["operations"]:
        if operation["target_entity_id"] == str(note.note_id):
            payload = json.loads(operation["payload_json"])
            for target in payload.get("targets", []):
                if target["entity_type"] == "question":
                    target["entity_id"] = str(graph.question_id(question_slug))
            operation["payload_json"] = json.dumps(payload)
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.link_precision == score.link_recall == pytest.approx(11 / 12)


@pytest.mark.parametrize("question_slug", DOSE_QUESTION_SLUGS)
def test_specific_clarification_does_not_excuse_an_unjustified_link(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]], question_slug: str
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    clarification = patch["operations"][-1]
    payload = json.loads(clarification["payload_json"])
    payload["targets"] = [
        {"entity_type": "question", "entity_id": str(graph.question_id(question_slug))}
    ]
    clarification["payload_json"] = json.dumps(payload)
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.clarification_recall == 1.0
    assert score.ambiguity_link_rate == 1.0
    assert score.link_precision < 1.0


@pytest.mark.parametrize(
    "clarification_text", ["Please clarify?", "Which question?", "Which animal and session?"]
)
def test_ambiguity_requires_the_actual_question_choices(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]], clarification_text: str
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    patch["clarification_requests"] = [GOLDEN_DAY_CLARIFICATION, clarification_text]
    patch["operations"][-1]["payload_json"] = json.dumps(
        {"metadata": {"needs_clarification": clarification_text}}
    )
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.clarification_recall == 0.5
    assert score.ambiguity_link_rate == 0.0


@pytest.mark.parametrize(
    "change",
    [
        "novel_qualifier",
        "cross_case_concepts",
        "invented_number",
        "wrong_source",
        "missing_source_link",
        "no_proposals",
    ],
)
def test_new_question_grounding_and_placement_are_required(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]], change: str
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    create = next(operation for operation in patch["operations"] if operation["op"] == "create")
    if change == "novel_qualifier":
        payload = json.loads(create["payload_json"])
        payload["text"] = (
            "Does a dopamine inhibitor make a partial agonist saturate at a lower dose?"
        )
        create["payload_json"] = json.dumps(payload)
    elif change in {"cross_case_concepts", "invented_number"}:
        payload = json.loads(create["payload_json"])
        payload["text"] = (
            "Does focal drift make a partial agonist saturate at a lower dose?"
            if change == "cross_case_concepts"
            else "Does a partial agonist saturate at a lower dose of 7?"
        )
        create["payload_json"] = json.dumps(payload)
    elif change == "wrong_source":
        create["source_refs"] = [{"source_note_ids": [str(_identifier_note(notes).note_id)]}]
    elif change == "missing_source_link":
        patch["operations"] = [
            operation
            for operation in patch["operations"]
            if {"$ref": create["client_ref"]}
            not in [
                target.get("entity_id")
                for target in json.loads(operation["payload_json"]).get("targets", [])
            ]
        ]
    else:
        patch["operations"] = [
            operation
            for operation in patch["operations"]
            if operation["entity_type"] != "question"
            and not any(
                isinstance(target.get("entity_id"), dict)
                for target in json.loads(operation["payload_json"]).get("targets", [])
            )
        ]
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.link_precision == score.link_recall == 1.0
    assert score.proposal_recall < 1.0
    if change != "missing_source_link":
        assert score.proposal_precision < 1.0


@pytest.mark.parametrize(
    "texts",
    [
        [
            "Can a partial agonist reach calcium response saturation at a lower concentration?",
            "Is focal plane drift correlated with bath temperature during imaging?",
        ],
        [
            "Does the partial agonist reach response saturation at a lower dose "
            "than the intended comparison condition?",
            "Does focal drift covary with bath temperature during imaging?",
        ],
        [
            "Does a partial agonist saturate at a lower dose "
            "than the intended reference condition?",
            "Does focal drift track bath temperature during imaging?",
        ],
        [
            "Does a partial agonist reach a calcium-response plateau "
            "at a lower dose than the comparator?",
            "Does focal drift track bath temperature?",
        ],
        [
            "Does a partial agonist saturate at a lower dose than the comparator "
            "intended in the lab meeting (comparator to be specified)?",
            "Does focal drift track bath temperature?",
        ],
    ],
)
def test_supported_proposal_paraphrases_use_the_same_rubric(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]], texts: list[str]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    for operation, text in zip(
        [op for op in patch["operations"] if op["op"] == "create"], texts, strict=True
    ):
        payload = json.loads(operation["payload_json"])
        payload["text"] = text
        operation["payload_json"] = json.dumps(payload)
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    assert score.proposal_precision == score.proposal_recall == 1.0


@pytest.mark.parametrize(
    "clarification_text,expected",
    [
        (
            "What does M7-0925-03 identify, and which recording or dataset does it refer to? "
            "Its session is known.",
            1.0,
        ),
        ("Which question?", 0.5),
        ("Please clarify?", 0.5),
    ],
)
def test_identifier_clarification_uses_the_information_still_missing(
    api: LabTrackerAPI,
    golden_day: tuple[GoldenDayGraph, list[Note]],
    clarification_text: str,
    expected: float,
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    identifier_op = next(
        operation
        for operation in patch["operations"]
        if operation["target_entity_id"] == str(_identifier_note(notes).note_id)
    )
    identifier_op["payload_json"] = json.dumps(
        {"metadata": {"needs_clarification": clarification_text}}
    )
    patch["clarification_requests"][0] = clarification_text
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    assert score_golden_day(draft, graph, notes).clarification_recall == expected


@pytest.mark.parametrize(
    "source_scope", ["supporting_observation", "unrelated_extra", "missing_origin"]
)
def test_proposal_sources_allow_relevant_context_but_require_the_origin(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]], source_scope: str
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    create = next(
        operation
        for operation in patch["operations"]
        if operation["client_ref"] == "temperature_drift_followup"
    )
    by_slug = {capture_slug(note): note for note in notes}
    supporting_note = by_slug["imaging_drift"]
    if source_scope == "unrelated_extra":
        supporting_note = by_slug[IDENTIFIER_CAPTURE_SLUG]
    if source_scope == "missing_origin":
        create["source_refs"] = []
    create["source_refs"].append({"source_note_ids": [str(supporting_note.note_id)]})
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    score = score_golden_day(draft, graph, notes)
    expected = 1.0 if source_scope == "supporting_observation" else 0.5
    assert score.proposal_precision == score.proposal_recall == expected


def test_specific_top_level_ambiguity_request_can_name_the_unassigned_comparison(
    api: LabTrackerAPI, golden_day: tuple[GoldenDayGraph, list[Note]]
) -> None:
    graph, notes = golden_day
    patch = golden_day_expected_patch(graph, notes)
    patch["operations"].pop()
    patch["clarification_requests"][-1] = (
        "Which existing question should receive the final unassigned comparison: "
        + ", ".join(str(graph.question_id(slug)) for slug in DOSE_QUESTION_SLUGS)
        + "?"
    )
    draft = api.create_batch_graph_draft(
        notes, draft_client=ScriptedGoldenDayDraftClient(patch), actor=ADMIN
    )
    assert draft.status == GraphChangeSetStatus.READY
    assert score_golden_day(draft, graph, notes).clarification_recall == 1.0


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
    assert group.clarification_request_count == 2
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
    assert cells[GraphDraftSemanticType.REQUEST_CLARIFICATION].proposed == 2
    assert cells[GraphDraftSemanticType.SUGGEST_NEW_QUESTION].proposed == 1
