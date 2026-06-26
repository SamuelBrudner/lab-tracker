"""Deterministic, human-gated provenance-link proposals.

When two captured artifacts share a content hash, the daily/batch run proposes a
``was_derived_from`` link for a human to accept or reject. Nothing is ever
auto-committed: the detector only writes PROPOSED links, and there is no public
create endpoint. Only accepted links render in PROV-O export.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AcceptanceMode,
    EntityRef,
    EntityType,
    Note,
    ProvenanceLink,
    ProvenanceLinkBasis,
    ProvenanceLinkOrigin,
    ProvenanceLinkRelation,
    ProvenanceLinkStatus,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.shared import actor_user_fk, actor_user_id

_PROVENANCE_LINK_TRANSITIONS: dict[ProvenanceLinkStatus, set[ProvenanceLinkStatus]] = {
    ProvenanceLinkStatus.PROPOSED: {
        ProvenanceLinkStatus.ACCEPTED,
        ProvenanceLinkStatus.REJECTED,
    },
    ProvenanceLinkStatus.ACCEPTED: set(),
    ProvenanceLinkStatus.REJECTED: set(),
}


def notes_sharing_content_hash(notes: list[Note]) -> dict[str, list[Note]]:
    """Group notes by ``evidence_content_hash``; drop empty hashes and singletons.

    Each surviving group's notes are sorted by ``created_at`` ascending so the
    earliest capture is the antecedent.
    """

    groups: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        content_hash = (note.metadata or {}).get("evidence_content_hash")
        if content_hash:
            groups[content_hash].append(note)
    return {
        content_hash: sorted(group, key=lambda note: note.created_at)
        for content_hash, group in groups.items()
        if len(group) >= 2
    }


class ProvenanceLinkService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.authorization = authorization

    def propose_links_from_content_hash(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> int:
        """Propose was_derived_from links for same-content-hash note pairs.

        Deterministic and idempotent: a pair already linked in any status
        (including rejected) is never re-proposed, so a declined link is not
        re-nagged. Star topology — every later capture links to the single
        earliest antecedent — so a duplicate group of N yields N-1 links, not
        O(N^2). Always writes PROPOSED; never accepts or commits.
        """

        self.authorization.require_contributor(project_id, actor=actor)
        notes, _ = self.repository.query_notes(project_id=project_id, limit=None, offset=0)
        groups = notes_sharing_content_hash(notes)
        if not groups:
            return 0
        existing = self.repository.provenance_links.list_by_project(project_id)
        seen_pairs = {
            (link.source.entity_id, link.target.entity_id, link.relation) for link in existing
        }
        created = 0
        with self.unit_of_work() as repository:
            created_by = actor_user_id(actor)
            created_by_user_id = actor_user_fk(actor, repository)
            for content_hash, group in groups.items():
                antecedent = group[0]
                for derived in group[1:]:
                    if derived.note_id == antecedent.note_id:
                        continue
                    key = (
                        derived.note_id,
                        antecedent.note_id,
                        ProvenanceLinkRelation.WAS_DERIVED_FROM,
                    )
                    if key in seen_pairs:
                        continue
                    link = ProvenanceLink(
                        link_id=uuid4(),
                        project_id=project_id,
                        source=EntityRef(entity_type=EntityType.NOTE, entity_id=derived.note_id),
                        target=EntityRef(
                            entity_type=EntityType.NOTE, entity_id=antecedent.note_id
                        ),
                        relation=ProvenanceLinkRelation.WAS_DERIVED_FROM,
                        basis=ProvenanceLinkBasis.CONTENT_HASH_MATCH,
                        content_hash=content_hash,
                        status=ProvenanceLinkStatus.PROPOSED,
                        origin=ProvenanceLinkOrigin.SYSTEM_DETECTED,
                        created_by=created_by,
                        created_by_user_id=created_by_user_id,
                        created_at=utc_now(),
                    )
                    repository.provenance_links.save(link)
                    seen_pairs.add(key)
                    created += 1
        return created

    def get_provenance_link(self, link_id: UUID) -> ProvenanceLink:
        return self.get_from_repository(
            entity_id=link_id,
            label="Provenance link",
            loader=lambda repository: repository.provenance_links.get(link_id),
        )

    def list_provenance_links(
        self,
        *,
        project_id: UUID | None = None,
        status: ProvenanceLinkStatus | None = None,
    ) -> list[ProvenanceLink]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_provenance_links(
                project_id=project_id,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
        )

    def update_status(
        self,
        link_id: UUID,
        new_status: ProvenanceLinkStatus,
        *,
        actor: AuthContext | None = None,
    ) -> ProvenanceLink:
        link = self.get_provenance_link(link_id)
        self.authorization.require_contributor(link.project_id, actor=actor)
        self._ensure_status_transition(link.status, new_status)
        if new_status == ProvenanceLinkStatus.ACCEPTED:
            link.acceptance_mode = AcceptanceMode.HUMAN_SELECTED
            link.accepted_by = actor_user_id(actor)
            link.accepted_by_user_id = actor_user_fk(actor, self.repository)
            link.accepted_at = utc_now()
        else:
            link.acceptance_mode = None
            link.accepted_by = None
            link.accepted_by_user_id = None
            link.accepted_at = None
        link.status = new_status
        link.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.provenance_links.save(link)
        return link

    def _ensure_status_transition(
        self,
        current_status: ProvenanceLinkStatus,
        next_status: ProvenanceLinkStatus,
    ) -> None:
        allowed = _PROVENANCE_LINK_TRANSITIONS.get(current_status, set())
        if next_status not in allowed:
            raise ValidationError(
                "Provenance link status cannot transition "
                f"from {current_status.value} to {next_status.value}."
            )
