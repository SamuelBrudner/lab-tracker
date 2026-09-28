"""Deterministic, human-gated provenance-link proposals.

Two detectors run on every batch execution (synchronous, queued worker, or
due dispatch) and propose ``was_derived_from`` links for a human to accept or
reject:

* Content hash: when two captured artifacts share a content hash. Carriers
  are notes, through their indexed ``evidence_content_hash``, and datasets,
  through the checksum of an uploaded dataset file; the earliest capture of
  a hash is the antecedent.
* Exact id: when a note's own capture metadata names a session
  (``watch_session_id``, ``capture_session_id``, ``photo_session_id``) or a git commit
  (``run_git_commit``, ``repo_git_commit``, ``hpc_git_commit``,
  ``git_commit``) that resolves to exactly one session or committed analysis
  ``code_version`` in the project. The note is the source, the named entity
  the target; ambiguous prefixes and targets the note already carries propose
  nothing.
* Worktree tree: when a capture recorded the git tree of the working copy it
  ran in (``capture_git_worktree_tree``, ``run_git_worktree_tree``,
  ``hpc_git_worktree_tree``) and another note records that same tree as its
  commit's own (``repo_git_tree``), the capture derives from the earliest such
  commit note (see :mod:`lab_tracker.services.provenance_tree_matches`).

Nothing is ever auto-committed: the detectors only write PROPOSED links, a
pair already linked in any status (including rejected) is never re-proposed,
and there is no public create endpoint. Only accepted note-to-note links
render in PROV-O export.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, OpaqueTargetNotFoundError, ValidationError
from lab_tracker.models import (
    MIN_CARRIERS_PER_HASH,
    AcceptanceMode,
    AnalysisStatus,
    ContentHashCarrier,
    EntityRef,
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
from lab_tracker.services.provenance_id_matches import (
    ID_MATCH_COMMIT_METADATA_KEYS,
    ID_MATCH_SESSION_METADATA_KEYS,
    IdMatch,
    id_matches_for_notes,
)
from lab_tracker.services.provenance_tree_matches import (
    COMMIT_TREE_METADATA_KEY,
    WORKTREE_TREE_METADATA_KEYS,
    TreeMatch,
    commit_trees,
    tree_matches_for_notes,
)
from lab_tracker.services.shared import actor_user_fk, actor_user_id

ID_MATCH_METADATA_KEYS: tuple[str, ...] = (
    *ID_MATCH_SESSION_METADATA_KEYS,
    *ID_MATCH_COMMIT_METADATA_KEYS,
)


@dataclass(frozen=True)
class _Proposal:
    """A detector's candidate link before the existing-pair check and save."""

    source: EntityRef
    target: EntityRef
    basis: ProvenanceLinkBasis
    content_hash: str | None = None

    @property
    def pair(self) -> tuple[UUID, UUID, ProvenanceLinkRelation]:
        return (
            self.source.entity_id,
            self.target.entity_id,
            ProvenanceLinkRelation.WAS_DERIVED_FROM,
        )


def content_hash_proposals(
    groups: dict[str, list[ContentHashCarrier]],
) -> list[_Proposal]:
    """Star topology per hash: every later capture derives from the earliest one."""

    return [
        _Proposal(
            source=derived.entity,
            target=antecedent.entity,
            basis=ProvenanceLinkBasis.CONTENT_HASH_MATCH,
            content_hash=content_hash,
        )
        for content_hash, (antecedent, *rest) in groups.items()
        for derived in rest
    ]


def id_match_proposals(matches: list[IdMatch]) -> list[_Proposal]:
    return [
        _Proposal(
            source=EntityRef(entity_type=EntityType.NOTE, entity_id=match.note_id),
            target=match.target,
            basis=ProvenanceLinkBasis.EXACT_ID_MATCH,
        )
        for match in matches
    ]


def tree_match_proposals(matches: list[TreeMatch]) -> list[_Proposal]:
    """Capture note -> the commit note whose own tree the capture ran in."""

    return [
        _Proposal(
            source=EntityRef(entity_type=EntityType.NOTE, entity_id=match.note_id),
            target=EntityRef(entity_type=EntityType.NOTE, entity_id=match.commit_note_id),
            basis=ProvenanceLinkBasis.WORKTREE_TREE_MATCH,
        )
        for match in matches
    ]

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
        return self._save_new_proposals(project_id, content_hash_proposals(groups), actor=actor)

    def propose_links_from_id_matches(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> int:
        """Propose was_derived_from links from ids a note's own metadata names.

        A note whose capture metadata names a project session, or a git commit
        matching exactly one committed analysis ``code_version``, derives from
        that entity as a matter of record; the link still needs a person to
        accept it. Same idempotency rule as the content-hash detector, plus:
        a target the note already carries proposes nothing.
        """

        self.authorization.require_contributor(project_id, actor=actor)
        notes = self.repository.provenance_links.list_identifier_carriers(
            project_id, ID_MATCH_METADATA_KEYS
        )
        if not notes:
            return 0
        sessions, _total = self.repository.query_sessions(
            project_id=project_id, limit=None, offset=0
        )
        analyses, _total = self.repository.query_analyses(
            project_id=project_id,
            status=AnalysisStatus.COMMITTED.value,
            limit=None,
            offset=0,
        )
        matches = id_matches_for_notes(notes, sessions=sessions, analyses=analyses)
        return self._save_new_proposals(project_id, id_match_proposals(matches), actor=actor)

    def propose_links_from_tree_matches(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> int:
        """Propose was_derived_from links from a capture to the commit it ran.

        A note whose metadata records the git tree of its working copy
        derives from the earliest *other* note in the project whose
        ``repo_git_tree`` is the same tree id: the capture was made from
        exactly that commit's code. Only full tree ids match, the first
        worktree key that resolves wins, and the same idempotency rule as the
        other detectors applies (a pair linked in any status is never
        re-proposed). Always writes PROPOSED.
        """

        self.authorization.require_contributor(project_id, actor=actor)
        links = self.repository.provenance_links
        # Commit notes first (few); then only the captures whose recorded tree
        # is one of those commits' trees, so a project full of figure captures
        # never loads them all.
        commit_notes = links.list_identifier_carriers(project_id, (COMMIT_TREE_METADATA_KEY,))
        trees = sorted(commit_trees(commit_notes))
        if not trees:
            return 0
        captures = links.list_metadata_value_carriers(
            project_id,
            WORKTREE_TREE_METADATA_KEYS,
            [*trees, *(tree.upper() for tree in trees)],
        )
        notes = {note.note_id: note for note in (*commit_notes, *captures)}
        matches = tree_matches_for_notes(notes.values())
        if not matches:
            return 0
        return self._save_new_proposals(project_id, tree_match_proposals(matches), actor=actor)

    def _save_new_proposals(
        self,
        project_id: UUID,
        proposals: list[_Proposal],
        *,
        actor: AuthContext | None,
    ) -> int:
        """Write each proposal whose (source, target, relation) pair is new; return the count."""

        if not proposals:
            return 0
        existing = self.repository.provenance_links.list_by_project(project_id)
        seen_pairs = {
            (link.source.entity_id, link.target.entity_id, link.relation) for link in existing
        }
        created = 0
        with self.unit_of_work() as repository:
            created_by = actor_user_id(actor)
            created_by_user_id = actor_user_fk(actor, repository)
            for proposal in proposals:
                if proposal.pair in seen_pairs:
                    continue
                repository.provenance_links.save(
                    ProvenanceLink(
                        link_id=uuid4(),
                        project_id=project_id,
                        source=proposal.source,
                        target=proposal.target,
                        relation=ProvenanceLinkRelation.WAS_DERIVED_FROM,
                        basis=proposal.basis,
                        content_hash=proposal.content_hash,
                        status=ProvenanceLinkStatus.PROPOSED,
                        origin=ProvenanceLinkOrigin.SYSTEM_DETECTED,
                        created_by=created_by,
                        created_by_user_id=created_by_user_id,
                        created_at=utc_now(),
                    )
                )
                seen_pairs.add(proposal.pair)
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
