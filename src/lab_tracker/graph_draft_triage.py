"""Derived review groups, independent of model confidence or provider output.

A group is a human decision, not an acceptance. Reference-dependent edits stay
together; routine links are eligible for a compact explicit confirmation only
when they add targets without changing capture content or existing meaning.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from lab_tracker.models import (
    DEFERRED_AT_KEY,
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    utc_now,
)

TRIAGE_SUMMARY_KEY = "review_triage_summary"
TRIAGE_CLARIFICATIONS_KEY = "review_triage_clarifications"
_LINK_LABELS = {
    "link_note_to_question",
    "link_note_to_session",
    "link_note_to_dataset",
    "link_note_to_analysis",
}
_OPTIONAL_LABELS = {"suggest_followup", "suggest_new_goal", "link_node_to_goal"}
_URGENT_LABELS = {
    "resolve_prediction",
    "record_pivot",
    "record_dead_end",
    "record_decision",
    "abandon_question",
    "merge_questions",
    "retire_note",
}


def payload_refs(value: Any) -> set[str]:
    if isinstance(value, list):
        return set().union(*(payload_refs(item) for item in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    if set(value) == {"$ref"} and isinstance(value["$ref"], str):
        return {value["$ref"]}
    return set().union(*(payload_refs(item) for item in value.values())) if value else set()


def _label(operation: GraphChangeOperation) -> str:
    return operation.semantic_type.value if operation.semantic_type else ""


def _lane(operation: GraphChangeOperation, note_targets: dict[str, list[dict[str, Any]]]) -> str:
    label = _label(operation)
    if operation.error_metadata.get("message") or label == "request_clarification":
        return "judgment"
    if (
        label in _LINK_LABELS
        and operation.op == GraphChangeOp.UPDATE
        and operation.entity_type == EntityType.NOTE
        and operation.target_entity_id is not None
        and set(operation.payload) == {"targets"}
        and isinstance(operation.payload["targets"], list)
        and operation.payload["targets"]
        and not payload_refs(operation.payload)
        and str(operation.target_entity_id) in note_targets
        and all(
            target in operation.payload["targets"]
            for target in note_targets[str(operation.target_entity_id)]
        )
    ):
        return "confirmations"
    if label in _OPTIONAL_LABELS:
        return "later"
    return "judgment"


def clarification_id(question: str) -> str:
    return "clarification-" + sha256(question.encode()).hexdigest()[:20]


def review_triage(change_set: GraphChangeSet) -> dict[str, Any]:
    """Return stable dependency groups and counts from the current operations."""
    operations = sorted(change_set.operations, key=lambda op: (op.sequence, str(op.operation_id)))
    note_targets: dict[str, list[dict[str, Any]]] = {}
    for key in ("batch_notes", "recent_notes", "source_artifacts"):
        for note in change_set.context_packet.get(key, []):
            if isinstance(note, dict) and isinstance(note.get("targets"), list):
                note_targets[str(note.get("id") or note.get("note_id"))] = note["targets"]
    source = change_set.context_packet.get("source_note", {})
    if isinstance(source, dict) and isinstance(source.get("targets"), list):
        note_targets[str(source.get("id"))] = source["targets"]
    for project in change_set.context_packet.get("projects", []):
        for note in project.get("recent_notes", []):
            if isinstance(note, dict) and isinstance(note.get("targets"), list):
                note_targets[str(note.get("id"))] = note["targets"]
    parent = list(range(len(operations)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def join(a: int, b: int) -> None:
        parent[root(b)] = root(a)

    by_ref = {op.client_ref: index for index, op in enumerate(operations) if op.client_ref}
    # The same note's additive link edits form one confirmation. Shared source
    # notes alone never merge unrelated scientific claims into one verdict.
    by_note: dict[str, int] = {}
    for index, operation in enumerate(operations):
        for ref in payload_refs(operation.payload):
            if ref in by_ref:
                join(index, by_ref[ref])
        if _lane(operation, note_targets) == "confirmations":
            key = str(operation.target_entity_id)
            if key in by_note:
                join(index, by_note[key])
            by_note[key] = index
    components: dict[int, list[GraphChangeOperation]] = {}
    for index, operation in enumerate(operations):
        components.setdefault(root(index), []).append(operation)

    groups: list[dict[str, Any]] = []
    transferred = bool(change_set.review_metadata.get("review_triage_carryover_to"))
    answers = change_set.review_metadata.get(TRIAGE_CLARIFICATIONS_KEY, {})
    for question in dict.fromkeys(change_set.clarification_requests):
        group_id = clarification_id(question)
        answer = answers.get(group_id, {}) if isinstance(answers, dict) else {}
        deferred = answer.get("status") == "deferred"
        groups.append(
            {
                "group_id": group_id,
                "kind": "clarification",
                "operation_ids": [],
                "pending_operation_ids": [],
                "deferred_operation_ids": [],
                "lane": "later" if deferred else "judgment",
                "urgent": True,
                "deferred": deferred,
                "resolved": transferred or answer.get("status") == "answered",
                "question": question,
                "statement": "",
                "answer": answer.get("answer", ""),
                "reason": "Your answer supplies missing context; it does not accept a graph edit.",
            }
        )
    for members in components.values():
        proposed = [op for op in members if op.status == GraphChangeOperationStatus.PROPOSED]
        active = [op for op in proposed if DEFERRED_AT_KEY not in op.error_metadata]
        lanes = {_lane(op, note_targets) for op in proposed or members}
        lane = (
            "judgment"
            if "judgment" in lanes
            else ("confirmations" if "confirmations" in lanes else "later")
        )
        deferred = bool(proposed) and not active
        if deferred:
            lane = "later"
        urgent = any(
            _label(op) in _URGENT_LABELS
            or (op.entity_type == EntityType.CLAIM and op.op == GraphChangeOp.UPDATE)
            for op in members
        )
        primary = next((op for op in members if _lane(op, note_targets) == "judgment"), members[0])
        statement = next(
            (
                str(primary.payload[key])
                for key in ("statement", "text", "title", "raw_content")
                if isinstance(primary.payload.get(key), str) and primary.payload[key].strip()
            ),
            "",
        )
        groups.append(
            {
                "group_id": str(members[0].operation_id),
                "kind": "operations",
                "operation_ids": [str(op.operation_id) for op in members],
                "pending_operation_ids": [str(op.operation_id) for op in active],
                "deferred_operation_ids": [
                    str(op.operation_id) for op in proposed if DEFERRED_AT_KEY in op.error_metadata
                ],
                "lane": lane,
                "urgent": urgent,
                "deferred": deferred,
                "resolved": transferred or not proposed,
                "question": (
                    "Are these capture links correct?"
                    if lane == "confirmations"
                    else "Is this the change you intended?"
                    if urgent
                    else "Do you want to keep this follow-up?"
                    if lane == "later"
                    else "Does this accurately reflect the work?"
                ),
                "statement": statement[:600],
                "reason": (
                    "Changes an interpretation, decision, or existing research path."
                    if urgent
                    else "Adds capture links to existing records."
                    if lane == "confirmations"
                    else "Optional follow-up; can be reviewed later."
                    if lane == "later" and not deferred
                    else "You deferred this decision."
                    if deferred
                    else "Needs a person to confirm the meaning or evidence."
                ),
            }
        )
    lane_order = {"judgment": 0, "confirmations": 1, "later": 2}
    groups.sort(key=lambda g: (g["resolved"], lane_order[g["lane"]], not g["urgent"]))
    by_id = {str(operation.operation_id): operation for operation in operations}
    for group in groups:
        group["revision"] = sha256(
            json.dumps(
                [
                    by_id[operation_id].model_dump(
                        mode="json",
                        include={
                            "operation_id",
                            "payload",
                            "status",
                            "updated_at",
                            "error_metadata",
                        },
                    )
                    for operation_id in group["operation_ids"]
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
    summary = {
        lane: sum(not group["resolved"] and group["lane"] == lane for group in groups)
        for lane in lane_order
    }
    summary["deferred"] = sum(group["deferred"] for group in groups)
    summary["resolved"] = sum(group["resolved"] for group in groups)
    summary["clarifications"] = sum(
        group.get("kind") == "clarification" and not group["resolved"] for group in groups
    )
    return {"groups": groups, "summary": summary}


def carryover_triage(
    change_set: GraphChangeSet, resolved_refs: dict[str, UUID]
) -> GraphChangeSet | None:
    """Preserve unfinished triage after a partial commit, inside its transaction."""
    if not change_set.review_metadata.get("review_triage_used"):
        return None
    proposed = [
        op for op in change_set.operations if op.status == GraphChangeOperationStatus.PROPOSED
    ]
    answers = change_set.review_metadata.get(TRIAGE_CLARIFICATIONS_KEY, {})
    questions = [
        question
        for question in change_set.clarification_requests
        if answers.get(clarification_id(question), {}).get("status") != "answered"
    ]
    if not proposed and not questions:
        return None

    def resolve(value: Any) -> Any:
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            if set(value) == {"$ref"} and value["$ref"] in resolved_refs:
                return str(resolved_refs[value["$ref"]])
            return {key: resolve(item) for key, item in value.items()}
        return value

    new_id = uuid4()
    now = utc_now()
    remaining = [
        op.model_copy(
            deep=True,
            update={
                "operation_id": uuid4(),
                "change_set_id": new_id,
                "payload": resolve(op.payload),
                "created_at": now,
                "updated_at": now,
            },
        )
        for op in proposed
    ]
    context = {
        key: value
        for key, value in change_set.context_packet.items()
        if key != "delegated_curation"
    }
    metadata = dict(change_set.review_metadata)
    metadata.pop("first_triage_decision_at", None)
    metadata.pop("last_triage_decision_at", None)
    metadata["review_triage_carryover_from"] = str(change_set.change_set_id)
    return change_set.model_copy(
        deep=True,
        update={
            "change_set_id": new_id,
            "batch_key": f"triage-carryover:{change_set.change_set_id}",
            "operations": remaining,
            "clarification_requests": questions,
            "status": GraphChangeSetStatus.READY,
            "context_packet": context,
            "review_metadata": metadata,
            "summary": "Unfinished decisions from an earlier review. " + change_set.summary,
            "error_metadata": {},
            "commit_message": None,
            "created_at": now,
            "updated_at": now,
            "submitted_at": None,
            "submitted_by": None,
            "submitted_by_username": None,
            "reviewed_at": None,
            "reviewed_by": None,
            "reviewed_by_username": None,
            "review_note": None,
            "committed_at": None,
            "committed_by": None,
            "committed_by_username": None,
            "generation_claim_token": None,
            "generation_claimed_at": None,
            "generation_lease_expires_at": None,
        },
    )
