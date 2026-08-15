from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, insert
from sqlalchemy.orm import Session

from lab_tracker.db import Base
from lab_tracker.db_models import (
    ExplorationNodeModel,
    ProjectModel,
    QuestionModel,
    QuestionParentModel,
)
from lab_tracker.graph_query import GraphQueryService
from lab_tracker.models import (
    EntityType,
    ExplorationNodeStatus,
    ExplorationNodeType,
    Project,
    ProjectStatus,
    QuestionStatus,
    QuestionType,
)


def test_25k_node_search_and_neighborhood_keep_hydration_and_responses_bounded() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    project_id = UUID(int=1)
    question_ids = [UUID(int=index + 100) for index in range(25_000)]
    project = Project(
        project_id=project_id,
        name="Scale graph",
        status=ProjectStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    with engine.begin() as connection:
        connection.execute(
            insert(ProjectModel),
            [
                {
                    "project_id": project_id,
                    "name": project.name,
                    "description": "",
                    "status": ProjectStatus.ACTIVE,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        connection.execute(
            insert(QuestionModel),
            [
                {
                    "question_id": question_id,
                    "project_id": project_id,
                    "text": f"bounded scale needle {index:05d}",
                    "question_type": QuestionType.DESCRIPTIVE,
                    "status": QuestionStatus.ACTIVE,
                    "created_at": now,
                    "updated_at": now,
                }
                for index, question_id in enumerate(question_ids)
            ],
        )
        connection.execute(
            insert(QuestionParentModel),
            [
                {
                    "question_id": child_id,
                    "parent_question_id": question_ids[0],
                }
                for child_id in question_ids[1:1_001]
            ],
        )

    statements: list[str] = []

    def count_statement(*_args) -> None:
        statements.append(str(_args[2]))

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        with Session(engine) as session:
            service = GraphQueryService(session)
            search = service.search(
                project,
                query="needle",
                entity_types=["question"],
                statuses=None,
                limit=20,
                offset=0,
            )
            search_statement_count = len(statements)
            statements.clear()
            neighborhood = service.neighborhood(
                project,
                anchor_type="question",
                anchor_id=question_ids[0],
                direction="outgoing",
                relationships=["question_parent"],
                node_types=["question"],
                depth=2,
                max_nodes=50,
                max_edges=100,
                include_anchor_content=False,
            )
            neighborhood_statement_count = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)
        engine.dispose()

    assert len(search.items) == 20
    assert search.has_more is True
    assert search.next_offset == 20
    assert search_statement_count == 2
    assert len(neighborhood.nodes) <= 49
    assert len(neighborhood.edges) <= 100
    assert neighborhood.truncation.node_limit_reached is True
    assert neighborhood_statement_count <= 5


@pytest.mark.postgres
def test_postgres_search_and_json_relationship_traversal_queries(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_engine(migrated_postgres_database_url, future=True)
    now = datetime.now(timezone.utc)
    project_id = UUID(int=70_001)
    root_id = UUID(int=70_002)
    child_id = UUID(int=70_003)
    exploration_id = UUID(int=70_004)
    project = Project(
        project_id=project_id,
        name="Postgres graph",
        status=ProjectStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    try:
        with Session(engine) as session:
            session.add(
                ProjectModel(
                    project_id=project_id,
                    name=project.name,
                    description="",
                    status=ProjectStatus.ACTIVE,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.add_all(
                [
                    QuestionModel(
                        question_id=root_id,
                        project_id=project_id,
                        text="Unicode calcium Δ response",
                        question_type=QuestionType.DESCRIPTIVE,
                        status=QuestionStatus.ACTIVE,
                        created_at=now,
                        updated_at=now,
                    ),
                    QuestionModel(
                        question_id=child_id,
                        project_id=project_id,
                        text="Child response",
                        question_type=QuestionType.DESCRIPTIVE,
                        status=QuestionStatus.ACTIVE,
                        created_at=now,
                        updated_at=now,
                    ),
                    ExplorationNodeModel(
                        node_id=exploration_id,
                        project_id=project_id,
                        node_type=ExplorationNodeType.DECISION,
                        title="Inspect the calcium response",
                        target_entity_type=EntityType.QUESTION,
                        target_entity_id=child_id,
                        status=ExplorationNodeStatus.COMMITTED,
                        evidence_refs=[
                            {
                                "entity_type": EntityType.QUESTION.value,
                                "entity_id": str(root_id),
                            }
                        ],
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.flush()
            session.add(
                QuestionParentModel(
                    question_id=child_id,
                    parent_question_id=root_id,
                )
            )
            session.commit()

            service = GraphQueryService(session)
            search = service.search(
                project,
                query="calcium",
                entity_types=["question", "exploration_node"],
                statuses=None,
                limit=10,
                offset=0,
            )
            neighborhood = service.neighborhood(
                project,
                anchor_type="question",
                anchor_id=root_id,
                direction="outgoing",
                relationships=None,
                node_types=None,
                depth=1,
                max_nodes=20,
                max_edges=20,
                include_anchor_content=False,
            )
    finally:
        engine.dispose()

    assert {item.node.entity_type for item in search.items} == {
        "question",
        "exploration_node",
    }
    assert {edge.relationship for edge in neighborhood.edges} >= {
        "question_parent",
        "exploration_evidence",
    }
