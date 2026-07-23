"""Seed deterministic graph drafts for browser race-condition coverage."""

from __future__ import annotations

from uuid import UUID

from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.demo_seed import DEMO_PROJECT_NAME
from lab_tracker.models import (
    EntityType,
    GraphChangeOp,
    GraphChangeOperation,
    GraphChangeOperationStatus,
    GraphChangeSet,
    GraphChangeSetStatus,
    GraphDraftSemanticType,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

DRAFT_A_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DRAFT_B_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
OPERATION_A_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaa01")
OPERATION_B_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbb01")


def _draft(
    *,
    change_set_id: UUID,
    operation_id: UUID,
    project_id: UUID,
    source_note_id: UUID,
    label: str,
) -> GraphChangeSet:
    operation = GraphChangeOperation(
        operation_id=operation_id,
        change_set_id=change_set_id,
        sequence=1,
        op=GraphChangeOp.CREATE,
        entity_type=EntityType.QUESTION,
        semantic_type=GraphDraftSemanticType.SUGGEST_NEW_QUESTION,
        payload={
            "project_id": str(project_id),
            "text": f"Question proposed by draft {label}.",
            "question_type": "descriptive",
            "status": "staged",
        },
        rationale=f"Deterministic rationale for draft {label}.",
        confidence=0.8,
        status=GraphChangeOperationStatus.PROPOSED,
    )
    return GraphChangeSet(
        change_set_id=change_set_id,
        project_id=project_id,
        source_note_id=source_note_id,
        source_note_ids=[source_note_id],
        provider="e2e-fixture",
        model="deterministic-e2e",
        prompt_version="e2e-v1",
        summary=f"Draft {label} summary.",
        status=GraphChangeSetStatus.READY,
        operation_count=1,
        operations=[operation],
    )


def main() -> None:
    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_session_factory(settings, engine=engine)
    try:
        with session_factory() as session:
            repository = SQLAlchemyLabTrackerRepository(session)
            project = next(
                (
                    candidate
                    for candidate in repository.projects.list()
                    if candidate.name == DEMO_PROJECT_NAME
                ),
                None,
            )
            if project is None:
                raise RuntimeError("The E2E demo project must be seeded before graph drafts.")
            source_note = next(
                (
                    candidate
                    for candidate in repository.notes.list()
                    if candidate.project_id == project.project_id
                ),
                None,
            )
            if source_note is None:
                raise RuntimeError("The E2E demo project must contain a source note.")

            repository.graph_change_sets.save(
                _draft(
                    change_set_id=DRAFT_A_ID,
                    operation_id=OPERATION_A_ID,
                    project_id=project.project_id,
                    source_note_id=source_note.note_id,
                    label="A",
                )
            )
            repository.graph_change_sets.save(
                _draft(
                    change_set_id=DRAFT_B_ID,
                    operation_id=OPERATION_B_ID,
                    project_id=project.project_id,
                    source_note_id=source_note.note_id,
                    label="B",
                )
            )
            repository.commit()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
