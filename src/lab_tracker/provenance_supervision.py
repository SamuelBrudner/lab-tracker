"""Load only the supervision edges a provenance document can use.

Supervision edges form one global, non-project-scoped table. A provenance
builder only reads the edges whose supervisee is a person already in its
document, so loading every edge in the database per request is wasted work
that grows with the whole deployment. Build the document once without edges
to learn its people, load their edges, then build it again with them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from lab_tracker.models import SupervisionEdge

ProvenanceDocument = dict[str, object]


class SupervisionEdgeQuery(Protocol):
    def query_supervision_edges(
        self,
        *,
        supervisee_user_ids: set[UUID] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[SupervisionEdge], int]: ...


def build_with_people_supervision(
    repository: SupervisionEdgeQuery,
    build: Callable[[list[SupervisionEdge]], ProvenanceDocument],
) -> ProvenanceDocument:
    """Build ``build``'s document with the supervision edges of its people.

    Builders attach supervision only to people they already emit (never
    recursively to a supervisor they add), so the people of the edge-free
    document are exactly the supervisees the full document can reference.
    """

    unsupervised = build([])
    supervisee_user_ids = provenance_person_user_ids(unsupervised)
    if not supervisee_user_ids:
        return unsupervised
    edges, _ = repository.query_supervision_edges(
        supervisee_user_ids=supervisee_user_ids,
        limit=None,
        offset=0,
    )
    if not edges:
        return unsupervised
    return build(edges)


def provenance_person_user_ids(document: object) -> set[UUID]:
    """Return the UUID ``userId`` of every ``prov:Person`` node in a document.

    Person ids that are not UUIDs (legacy free-text creators) are skipped,
    exactly as the builders skip them when matching supervision edges.
    """

    user_ids: set[UUID] = set()
    pending: list[object] = [document]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
            continue
        if not isinstance(item, dict):
            continue
        pending.extend(item.values())
        node_type = item.get("@type")
        node_types = node_type if isinstance(node_type, list) else [node_type]
        user_id = item.get("userId")
        if "prov:Person" not in node_types or not isinstance(user_id, str):
            continue
        try:
            user_ids.add(UUID(user_id))
        except ValueError:
            continue
    return user_ids
