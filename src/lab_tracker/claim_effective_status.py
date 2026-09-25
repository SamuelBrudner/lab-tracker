"""Derived claim interpretation: effective status and pre-registration.

Pure functions over domain models plus one repository loader. Nothing here
mutates a claim: stored ``Claim.status`` stays the record of what a person
committed, and ``ClaimEffectiveStatus`` is recomputed on every read from

* claim edges (``supersedes`` marks the target superseded; ``refutes`` and
  ``contradicts`` mark it contested; edges from a REJECTED source are ignored),
* committed ``pivot`` exploration nodes whose ``invalidates_claim_id`` names
  the claim, and
* evidence timestamps (committed dataset ``created_at`` and committed analysis
  ``executed_at``) for ``pre_registered``.

Only direct edges are interpreted; readers follow the emitted ``lab:ClaimRelation``
nodes for anything transitive.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimEdge,
    ClaimEffectiveStatus,
    ClaimRelation,
    ClaimStatus,
    Dataset,
    DatasetStatus,
    ExplorationNode,
    ExplorationNodeStatus,
    ExplorationNodeType,
)

CONTESTING_RELATIONS: Final = frozenset({ClaimRelation.REFUTES, ClaimRelation.CONTRADICTS})
SUPERSEDING_RELATION: Final = ClaimRelation.SUPERSEDES
STATUS_AFFECTING_RELATIONS: Final = CONTESTING_RELATIONS | {SUPERSEDING_RELATION}
OPEN_PREDICTION_STATUSES: Final = frozenset({ClaimStatus.PROPOSED, ClaimStatus.TESTING})
# The loader issues exactly this many project-scoped queries per distinct project.
INTERPRETATION_QUERIES_PER_PROJECT: Final = 5


class ClaimInterpretationError(ValueError):
    """The inputs cannot support an honest derivation (for example a missing edge source)."""


@dataclass(frozen=True)
class ClaimInterpretation:
    """Read-time derivation for one claim; every field is recomputed, never stored."""

    effective_status: ClaimEffectiveStatus
    superseded_by_claim_id: UUID | None
    contested_by_claim_ids: tuple[UUID, ...]
    invalidated_by_node_id: UUID | None
    pre_registered: bool


def effective_status_for(
    claim: Claim,
    *,
    superseded_by: UUID | None,
    contested_by: Sequence[UUID],
    invalidated_by: UUID | None,
) -> ClaimEffectiveStatus:
    """Precedence: stored REJECTED, then invalidated, superseded, contested, stored."""

    if claim.status == ClaimStatus.REJECTED:
        return ClaimEffectiveStatus.REJECTED
    if invalidated_by is not None:
        return ClaimEffectiveStatus.INVALIDATED
    if superseded_by is not None:
        return ClaimEffectiveStatus.SUPERSEDED
    if contested_by:
        return ClaimEffectiveStatus.CONTESTED
    return ClaimEffectiveStatus(claim.status.value)


def evidence_times_for(
    claim: Claim,
    *,
    committed_datasets: Sequence[Dataset],
    committed_analyses: Sequence[Analysis],
) -> list[datetime]:
    """When evidence for the claim entered the record, earliest first.

    A committed dataset counts when the claim cites it or it lands under a
    question the claim answers (primary or linked); a committed analysis counts
    when the claim cites it or it consumed one of those datasets. Datasets carry
    no ``committed_at``, so ``created_at`` (the moment data entered the system)
    is the conservative stand-in.
    """

    question_ids = set(claim.answers_question_ids)
    supporting_dataset_ids = set(claim.supported_by_dataset_ids)
    evidence_dataset_ids: set[UUID] = set()
    times: list[datetime] = []
    for dataset in committed_datasets:
        if dataset.status != DatasetStatus.COMMITTED:
            continue
        under_question = dataset.primary_question_id in question_ids or any(
            link.question_id in question_ids for link in dataset.question_links
        )
        if dataset.dataset_id in supporting_dataset_ids or under_question:
            evidence_dataset_ids.add(dataset.dataset_id)
            times.append(dataset.created_at)
    supporting_analysis_ids = set(claim.supported_by_analysis_ids)
    for analysis in committed_analyses:
        if analysis.status != AnalysisStatus.COMMITTED:
            continue
        if analysis.analysis_id in supporting_analysis_ids or evidence_dataset_ids.intersection(
            analysis.dataset_ids
        ):
            times.append(analysis.executed_at)
    return sorted(times)


def is_pre_registered(claim: Claim, evidence_times: Sequence[datetime]) -> bool:
    """True only when the claim was written before its earliest evidence existed."""

    return bool(evidence_times) and claim.created_at < min(evidence_times)


def _edge_order(edge: ClaimEdge) -> tuple[datetime, str]:
    return (edge.created_at, str(edge.edge_id))


def _node_order(node: ExplorationNode) -> tuple[datetime, str]:
    return (node.created_at, str(node.node_id))


def _committed_invalidating_pivots(
    exploration_nodes: Sequence[ExplorationNode],
) -> list[ExplorationNode]:
    return sorted(
        (
            node
            for node in exploration_nodes
            if node.node_type == ExplorationNodeType.PIVOT
            and node.status == ExplorationNodeStatus.COMMITTED
            and node.invalidates_claim_id is not None
        ),
        key=_node_order,
    )


def _status_affecting_edges(
    edges: Sequence[ClaimEdge],
    *,
    claims_by_id: Mapping[UUID, Claim],
    interpreted_ids: set[UUID],
) -> list[ClaimEdge]:
    """Edges that can change an interpreted claim's status, oldest first.

    Fails loud when such an edge's source claim was not supplied: without the
    source's status the derivation cannot tell a live contest from a rejected one.
    """

    relevant: list[ClaimEdge] = []
    for edge in sorted(edges, key=_edge_order):
        if edge.relation not in STATUS_AFFECTING_RELATIONS:
            continue
        if edge.target_claim_id not in interpreted_ids:
            continue
        source = claims_by_id.get(edge.claim_id)
        if source is None:
            raise ClaimInterpretationError(
                f"Claim edge {edge.edge_id} names source claim {edge.claim_id}, which was "
                "not supplied; interpretation needs every source claim's status."
            )
        if source.status == ClaimStatus.REJECTED:
            continue
        relevant.append(edge)
    return relevant


def interpret_claims(
    claims: Sequence[Claim],
    *,
    claims_by_id: Mapping[UUID, Claim],
    edges: Sequence[ClaimEdge],
    exploration_nodes: Sequence[ExplorationNode],
    datasets: Sequence[Dataset],
    analyses: Sequence[Analysis],
) -> dict[UUID, ClaimInterpretation]:
    """Derive an interpretation for each claim; output order and ties are deterministic.

    ``exploration_nodes``, ``datasets`` and ``analyses`` may be supersets: only
    committed invalidating pivots, committed datasets and committed analyses count.
    """

    interpreted_ids = {claim.claim_id for claim in claims}
    relevant_edges = _status_affecting_edges(
        edges, claims_by_id=claims_by_id, interpreted_ids=interpreted_ids
    )
    pivots = _committed_invalidating_pivots(exploration_nodes)
    committed_datasets = [item for item in datasets if item.status == DatasetStatus.COMMITTED]
    committed_analyses = [item for item in analyses if item.status == AnalysisStatus.COMMITTED]
    interpretations: dict[UUID, ClaimInterpretation] = {}
    for claim in claims:
        incoming = [edge for edge in relevant_edges if edge.target_claim_id == claim.claim_id]
        superseders = [edge for edge in incoming if edge.relation == SUPERSEDING_RELATION]
        superseded_by = superseders[-1].claim_id if superseders else None
        contested_by = tuple(
            dict.fromkeys(
                edge.claim_id for edge in incoming if edge.relation in CONTESTING_RELATIONS
            )
        )
        invalidators = [node for node in pivots if node.invalidates_claim_id == claim.claim_id]
        invalidated_by = invalidators[-1].node_id if invalidators else None
        evidence_times = evidence_times_for(
            claim,
            committed_datasets=committed_datasets,
            committed_analyses=committed_analyses,
        )
        interpretations[claim.claim_id] = ClaimInterpretation(
            effective_status=effective_status_for(
                claim,
                superseded_by=superseded_by,
                contested_by=contested_by,
                invalidated_by=invalidated_by,
            ),
            superseded_by_claim_id=superseded_by,
            contested_by_claim_ids=contested_by,
            invalidated_by_node_id=invalidated_by,
            pre_registered=is_pre_registered(claim, evidence_times),
        )
    return interpretations


class ClaimInterpretationRepository(Protocol):
    """The five project-scoped reads the loader needs (signatures match the repository)."""

    def query_claims(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        dataset_id: UUID | None = None,
        analysis_id: UUID | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Claim], int]: ...

    def query_claim_edges(
        self,
        *,
        project_id: UUID | None = None,
        claim_id: UUID | None = None,
        target_claim_id: UUID | None = None,
        relation: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[ClaimEdge], int]: ...

    def query_exploration_nodes(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        node_type: str | None = None,
        status: str | None = None,
        target_entity_type: str | None = None,
        target_entity_id: UUID | None = None,
        created_by: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[ExplorationNode], int]: ...

    def query_datasets(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Dataset], int]: ...

    def query_analyses(
        self,
        *,
        project_id: UUID | None = None,
        project_ids: set[UUID] | None = None,
        dataset_id: UUID | None = None,
        question_id: UUID | None = None,
        status: str | None = None,
        created_by: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        recent_first: bool = False,
    ) -> tuple[list[Analysis], int]: ...


def load_claim_interpretations(
    repository: ClaimInterpretationRepository,
    claims: Sequence[Claim],
) -> dict[UUID, ClaimInterpretation]:
    """Batch-load the interpretation inputs per project and interpret the claims.

    Exactly ``INTERPRETATION_QUERIES_PER_PROJECT`` repository reads per distinct
    project, however many claims it holds.
    """

    claims_by_project: dict[UUID, list[Claim]] = {}
    for claim in claims:
        claims_by_project.setdefault(claim.project_id, []).append(claim)
    interpretations: dict[UUID, ClaimInterpretation] = {}
    for project_id in sorted(claims_by_project, key=str):
        project_claims, _ = repository.query_claims(project_id=project_id, limit=None, offset=0)
        edges, _ = repository.query_claim_edges(project_id=project_id, limit=None, offset=0)
        pivots, _ = repository.query_exploration_nodes(
            project_id=project_id,
            node_type=ExplorationNodeType.PIVOT.value,
            status=ExplorationNodeStatus.COMMITTED.value,
            limit=None,
            offset=0,
        )
        datasets, _ = repository.query_datasets(
            project_id=project_id,
            status=DatasetStatus.COMMITTED.value,
            limit=None,
            offset=0,
        )
        analyses, _ = repository.query_analyses(
            project_id=project_id,
            status=AnalysisStatus.COMMITTED.value,
            limit=None,
            offset=0,
        )
        claims_by_id = {claim.claim_id: claim for claim in project_claims}
        claims_by_id.update({claim.claim_id: claim for claim in claims_by_project[project_id]})
        interpretations.update(
            interpret_claims(
                claims_by_project[project_id],
                claims_by_id=claims_by_id,
                edges=edges,
                exploration_nodes=pivots,
                datasets=datasets,
                analyses=analyses,
            )
        )
    return interpretations
