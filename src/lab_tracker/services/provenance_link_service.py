"""Deterministic, human-gated provenance-link proposals.

When two captured artifacts share a content hash, every batch execution
(synchronous, queued worker, or due dispatch) proposes a ``was_derived_from``
link for a human to accept or reject. Carriers are notes, through their
indexed ``evidence_content_hash``, and datasets, through the checksum of an
uploaded dataset file; the earliest capture of a hash is the antecedent.
Nothing is ever auto-committed: the detector only writes PROPOSED links, and
there is no public create endpoint. Only accepted links render in PROV-O
export.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, OpaqueTargetNotFoundError, ValidationError
from lab_tracker.models import (
    MIN_CARRIERS_PER_HASH,
    AcceptanceMode,
    ContentHashCarrier,
    EntityType,
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


def group_content_hash_carriers(
    carriers: list[ContentHashCarrier],
) -> dict[str, list[ContentHashCarrier]]:
    """Group carriers by hash, one entry per entity, dropping singleton groups.

    The repository already orders carriers by hash and capture time, so the
    first entry of each surviving group is the antecedent. A dataset whose
    uploaded files repeat one checksum is a single carrier, so it can never
    pair with itself.
    """

    groups: dict[str, list[ContentHashCarrier]] = defaultdict(list)
    seen_entities: set[tuple[str, EntityType, UUID]] = set()
    for carrier in carriers:
        key = (carrier.content_hash, carrier.entity.entity_type, carrier.entity.entity_id)
        if key in seen_entities:
            continue
        seen_entities.add(key)
        groups[carrier.content_hash].append(carrier)
    return {
        content_hash: group
        for content_hash, group in groups.items()
        if len(group) >= MIN_CARRIERS_PER_HASH
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
        """Propose was_derived_from links between same-content-hash carriers.

        Deterministic and idempotent: a pair already linked in any status
        (including rejected) is never re-proposed, so a declined link is not
        re-nagged. Star topology — every later capture links to the single
        earliest antecedent — so a duplicate group of N yields N-1 links, not
        O(N^2). Endpoints may be notes or datasets. Always writes PROPOSED;
        never accepts or commits.
        """

        self.authorization.require_contributor(project_id, actor=actor)
        carriers = self.repository.provenance_links.list_content_hash_carriers(project_id)
        groups = group_content_hash_carriers(carriers)
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
                    key = (
                        derived.entity.entity_id,
                        antecedent.entity.entity_id,
                        ProvenanceLinkRelation.WAS_DERIVED_FROM,
                    )
                    if key in seen_pairs:
                        continue
                    link = ProvenanceLink(
                        link_id=uuid4(),
                        project_id=project_id,
                        source=derived.entity,
                        target=antecedent.entity,
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

    def get_provenance_link_for_read(
        self,
        link_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> ProvenanceLink:
        try:
            link = self.get_provenance_link(link_id)
        except NotFoundError as exc:
            raise OpaqueTargetNotFoundError("Provenance link does not exist.") from exc
        if not self.authorization.can_read(link.project_id, actor=actor):
            raise OpaqueTargetNotFoundError("Provenance link does not exist.")
        return link

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
