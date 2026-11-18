"""Plain-text details for one proposal the recipient is authorized to review."""

from __future__ import annotations

import json
from collections.abc import Iterator
from textwrap import shorten
from typing import Any
from uuid import UUID

from lab_tracker.models import EntityType, GraphChangeOp, GraphChangeOperation, GraphChangeSet
from lab_tracker.repository import LabTrackerRepository

_QUESTION_FIELDS = {
    "primary_question_id": "Primary question",
    "secondary_question_ids": "Secondary question",
    "answers_question_ids": "Question the claim addresses",
    "parent_question_ids": "Parent question",
    "child_question_ids_to_reparent": "Child question to reparent",
    "linked_question_id": "Linked question",
    "question_id": "Question",
}


def _references(operation: GraphChangeOperation) -> Iterator[tuple[str, Any]]:
    if operation.entity_type == EntityType.QUESTION and operation.target_entity_id:
        yield "Current question", operation.target_entity_id
    for field, label in _QUESTION_FIELDS.items():
        value = operation.payload.get(field)
        for ref in value if isinstance(value, list) else [value]:
            if ref is not None:
                yield label, ref
    for link in operation.payload.get("question_links") or []:
        if isinstance(link, dict):
            role = link.get("role")
            label = (
                f"{role.capitalize()} question" if role in ("primary", "secondary") else "Question"
            )
            yield label, link.get("question_id")
    for target in operation.payload.get("targets") or []:
        if isinstance(target, dict) and target.get("entity_type") == EntityType.QUESTION:
            yield "Question to link", target.get("entity_id")


def _question_context(
    draft: GraphChangeSet, proposal: GraphChangeOperation, repository: LabTrackerRepository
) -> list[str]:
    refs: list[tuple[str, str | UUID]] = []
    for label, value in _references(proposal):
        if isinstance(value, dict) and set(value) == {"$ref"} and isinstance(value["$ref"], str):
            refs.append((label, value["$ref"]))
        elif isinstance(value, (str, UUID)):
            try:
                refs.append((label, UUID(str(value))))
            except ValueError:
                continue
    if not refs:
        return []
    ids = {ref for _, ref in refs if isinstance(ref, UUID)}
    titles: dict[str | UUID, str] = {}
    if ids:
        questions, _ = repository.query_questions(project_id=draft.project_id, question_ids=ids)
        titles.update(
            (q.question_id, q.text) for q in questions if q.project_id == draft.project_id
        )
    for dependency in draft.operations:
        if (
            dependency.client_ref
            and dependency.change_set_id == draft.change_set_id
            and dependency.sequence < proposal.sequence
            and dependency.op == GraphChangeOp.CREATE
            and dependency.entity_type == EntityType.QUESTION
            and str(dependency.payload.get("project_id")) == str(draft.project_id)
            and isinstance(dependency.payload.get("text"), str)
        ):
            titles[dependency.client_ref] = dependency.payload["text"]
    lines = []
    if proposal.entity_type == EntityType.NOTE and proposal.target_entity_id:
        notes, _ = repository.query_notes(
            project_id=draft.project_id, note_ids={proposal.target_entity_id}, limit=1
        )
        if (
            notes
            and notes[0].note_id == proposal.target_entity_id
            and notes[0].project_id == draft.project_id
            and notes[0].raw_asset is None
        ):
            excerpt = shorten(notes[0].raw_content, width=240, placeholder="…")
            if excerpt:
                lines.append(f"Note being updated (excerpt): {excerpt}")
    for label, ref in dict.fromkeys(refs):
        source = f"draft reference {ref}" if isinstance(ref, str) else str(ref)
        text = titles.get(ref) or "Question text unavailable"
        lines.append(f"{label} ({source}): {text}")
    return lines


def proposal_detail_lines(
    draft: GraphChangeSet, proposal: GraphChangeOperation, repository: LabTrackerRepository
) -> list[str]:
    """Describe the authorized operation and resolve only its explicit question links."""

    lines = [f"Proposed change: {proposal.op.value} {proposal.entity_type.value}"]
    if proposal.target_entity_id:
        lines.append(f"Record: {proposal.target_entity_id}")
    lines.extend(_question_context(draft, proposal, repository))
    for field, value in proposal.payload.items():
        if field == "project_id" and str(value) == str(draft.project_id):
            continue
        display = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        lines.append(f"{field}: {display}")
    return lines
