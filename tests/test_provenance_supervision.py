"""Provenance documents load only their own people's supervision edges (L40)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from provenance_example_fixture import (
    BASE_URL,
    build_example_document,
    example_dataset,
    example_supervision_edges,
)

from lab_tracker.models import SupervisionEdge
from lab_tracker.provenance import build_dataset_provenance_document
from lab_tracker.provenance_supervision import (
    build_with_people_supervision,
    provenance_person_user_ids,
)


class _EdgeTable:
    def __init__(self, edges: list[SupervisionEdge]) -> None:
        self.edges = edges
        self.requested: list[set[UUID] | None] = []

    def query_supervision_edges(
        self,
        *,
        supervisee_user_ids: set[UUID] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SupervisionEdge], int]:
        assert limit is None and offset == 0
        self.requested.append(supervisee_user_ids)
        matches = [
            edge
            for edge in self.edges
            if supervisee_user_ids is None or edge.supervisee_user_id in supervisee_user_ids
        ]
        return matches, len(matches)


def test_document_matches_the_all_edges_build_while_querying_only_its_people() -> None:
    unrelated = SupervisionEdge(
        edge_id=uuid4(),
        supervisor_user_id=uuid4(),
        supervisee_user_id=uuid4(),
        started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    table = _EdgeTable([unrelated, *example_supervision_edges()])
    dataset = example_dataset()

    document = build_with_people_supervision(
        table,
        lambda edges: build_dataset_provenance_document(
            BASE_URL,
            dataset,
            supervision_edges=edges,
        ),
    )

    assert document == build_example_document()
    assert table.requested == [{dataset.created_by_user_id}]


def test_documents_without_uuid_people_skip_the_edge_query() -> None:
    table = _EdgeTable(example_supervision_edges())
    dataset = example_dataset().model_copy(
        update={"created_by_user_id": None, "created_by": "legacy-operator"}
    )

    document = build_with_people_supervision(
        table,
        lambda edges: build_dataset_provenance_document(BASE_URL, dataset, supervision_edges=edges),
    )

    assert table.requested == []
    assert provenance_person_user_ids(document) == set()


def test_person_user_ids_cover_nested_and_multi_typed_person_nodes() -> None:
    first, second = uuid4(), uuid4()
    document = {
        "@graph": [
            {"@type": "prov:Person", "userId": str(first)},
            {"@type": "lab:Layer", "@graph": [{"@type": ["prov:Person"], "userId": str(second)}]},
            {"@type": "prov:Person", "userId": "not-a-uuid"},
            {"@type": "lab:Dataset", "userId": str(uuid4())},
        ]
    }

    assert provenance_person_user_ids(document) == {first, second}
