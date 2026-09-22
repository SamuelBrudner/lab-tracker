"""The single registry of records that can reference a deletable entity.

The schema mixes real foreign keys (some ``CASCADE``, some ``SET NULL``) with
FK-less GUID and JSON references: note targets, dataset manifest ``note_ids``
and ``source_session_id``, exploration-node targets and evidence refs,
provenance links and goal links. A database cascade silently strips the
referrer's meaning and an FK-less reference silently dangles, so neither may
decide what a delete does.

Every delete path asks this registry instead. For each deletable entity it
enumerates every referrer, FK and non-FK, with one policy:

``BLOCK``
    Deletion is refused with a :class:`~lab_tracker.errors.ValidationError`
    that names every blocking referrer
    (``"<Entity> cannot be deleted while <referrer>; <referrer>."``).
``CLEANUP``
    The delete path removes these rows explicitly in the same transaction
    (goal links, unaccepted provenance-link proposals).
``OWNED``
    Rows that belong to the deleted entity itself: its own outgoing links,
    files, acquisition outputs and cascaded child records. The FK cascade is
    intended. A cascaded child *entity* (an analysis's visualizations) is
    listed in ``cascades_to`` so its own referrers are checked too.

History tables that deliberately keep identifiers of deleted records (entity
versions, graph-change operations, graph-draft batch runs, evidence-bundle
replay results, usage events) are not referrers.

Guards run inside the delete transaction after
``LabTrackerRepository.lock_project_references`` so that reference-adding
writers that take the same project lock cannot slip a new referrer past the
check. Today those writers are claim create/update, analysis create/commit,
claim-edge create, exploration-node create/update and the question-DAG
writers. Other reference-adding writers (note targets, visualization
``claim_ids``, provenance-link acceptance, dataset manifest ``note_ids`` and
``source_session_id``) do not yet take the lock: the guards are exact for
sequential callers, but on PostgreSQL one of those writes that commits while a
delete is between its guard and its commit can still leave a dangling
reference. ``tests/test_reference_registry.py`` enforces that every foreign key
targeting a deletable table is classified here and that every probe named here
has a repository implementation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final

from lab_tracker.errors import ValidationError
from lab_tracker.models import EntityType


class DeletableEntity(str, Enum):
    """Entities whose delete path is guarded by this registry."""

    QUESTION = "question"
    DATASET = "dataset"
    NOTE = "note"
    SESSION = "session"
    ANALYSIS = "analysis"
    CLAIM = "claim"
    VISUALIZATION = "visualization"
    EXPLORATION_NODE = "exploration_node"

    @property
    def entity_type(self) -> EntityType | None:
        """The polymorphic ``EntityType`` used by FK-less references, if any."""

        if self is DeletableEntity.EXPLORATION_NODE:
            return None
        return EntityType(self.value)


class ReferencePolicy(str, Enum):
    BLOCK = "block"
    CLEANUP = "cleanup"
    OWNED = "owned"


class ReferenceProbe(str, Enum):
    """Repository queries that find live referrers of one entity."""

    NOTE_TARGETS = "note_targets"
    EXPLORATION_TARGETS = "exploration_targets"
    EXPLORATION_EVIDENCE = "exploration_evidence"
    ACCEPTED_PROVENANCE_LINKS = "accepted_provenance_links"
    UNACCEPTED_PROVENANCE_LINKS = "unaccepted_provenance_links"
    GOAL_LINKS = "goal_links"
    CHILD_QUESTIONS = "child_questions"
    QUESTION_REFACTORS = "question_refactors"
    QUESTION_SUPERSESSION = "question_supersession"
    EXPERIMENT_PRIMARY_QUESTION = "experiment_primary_question"
    DATASET_PRIMARY_QUESTION = "dataset_primary_question"
    DATASET_SECONDARY_QUESTION_LINKS = "dataset_secondary_question_links"
    SESSION_PRIMARY_QUESTION = "session_primary_question"
    CLAIM_ANSWERS = "claim_answers"
    EXPERIMENT_DATASETS = "experiment_datasets"
    CLAIM_DATASETS = "claim_datasets"
    ANALYSIS_DATASETS = "analysis_datasets"
    GRAPH_DRAFT_SOURCES = "graph_draft_sources"
    DATASET_MANIFEST_NOTES = "dataset_manifest_notes"
    EXPERIMENT_SESSIONS = "experiment_sessions"
    NON_STAGED_DATASET_SOURCE_SESSIONS = "non_staged_dataset_source_sessions"
    STAGED_DATASET_SOURCE_SESSIONS = "staged_dataset_source_sessions"
    ACQUISITION_COLLECTIONS = "acquisition_collections"
    INCOMING_CLAIM_EDGES = "incoming_claim_edges"
    VISUALIZATION_CLAIMS = "visualization_claims"
    PIVOT_INVALIDATED_CLAIMS = "pivot_invalidated_claims"
    DEPENDENT_EXPLORATION_NODES = "dependent_exploration_nodes"
    PIVOT_INVALIDATED_NODES = "pivot_invalidated_nodes"


@dataclass(frozen=True)
class Referrer:
    """One way a record can point at a deletable entity.

    ``sources`` names the referencing ``table.column`` values (``[json]`` marks
    a JSON payload); FK columns listed here are what the schema test matches.
    ``phrase`` completes ``"<Entity> cannot be deleted while ..."`` for
    ``BLOCK`` referrers, with ``{it}`` naming the deleted record.
    """

    sources: tuple[str, ...]
    policy: ReferencePolicy
    probe: ReferenceProbe | None = None
    phrase: str | None = None

    def __post_init__(self) -> None:
        if self.policy is ReferencePolicy.OWNED:
            if self.probe is not None or self.phrase is not None:
                raise ValueError("Owned referrers are cascaded, not probed.")
            return
        if self.probe is None:
            raise ValueError("Blocking and cleanup referrers need a probe.")
        if (self.policy is ReferencePolicy.BLOCK) != (self.phrase is not None):
            raise ValueError("Exactly the blocking referrers carry a phrase.")


@dataclass(frozen=True)
class CascadedChild:
    """A child entity that the parent's delete cascades into."""

    entity: DeletableEntity
    source: str
    description: str


@dataclass(frozen=True)
class EntityReferences:
    label: str
    referrers: tuple[Referrer, ...]
    cascades_to: tuple[CascadedChild, ...] = ()

    @property
    def blocking(self) -> tuple[Referrer, ...]:
        return tuple(
            referrer for referrer in self.referrers if referrer.policy is ReferencePolicy.BLOCK
        )

    @property
    def cleanup(self) -> tuple[Referrer, ...]:
        return tuple(
            referrer for referrer in self.referrers if referrer.policy is ReferencePolicy.CLEANUP
        )


def _block(probe: ReferenceProbe, phrase: str, *sources: str) -> Referrer:
    return Referrer(sources=sources, policy=ReferencePolicy.BLOCK, probe=probe, phrase=phrase)


def _cleanup(probe: ReferenceProbe, *sources: str) -> Referrer:
    return Referrer(sources=sources, policy=ReferencePolicy.CLEANUP, probe=probe)


def _owned(*sources: str) -> Referrer:
    return Referrer(sources=sources, policy=ReferencePolicy.OWNED)


_NOTE_TARGETS = _block(
    ReferenceProbe.NOTE_TARGETS,
    "notes target {it}",
    "note_targets.entity_id",
)
_EXPLORATION_TARGETS = _block(
    ReferenceProbe.EXPLORATION_TARGETS,
    "exploration nodes target {it}",
    "exploration_nodes.target_entity_id",
)
_EXPLORATION_EVIDENCE = _block(
    ReferenceProbe.EXPLORATION_EVIDENCE,
    "exploration nodes cite {it} as evidence",
    "exploration_nodes.evidence_refs[json]",
)
_ACCEPTED_PROVENANCE_LINKS = _block(
    ReferenceProbe.ACCEPTED_PROVENANCE_LINKS,
    "accepted provenance links reference {it}",
    "provenance_links.source_entity_id",
    "provenance_links.target_entity_id",
)
_UNACCEPTED_PROVENANCE_LINKS = _cleanup(
    ReferenceProbe.UNACCEPTED_PROVENANCE_LINKS,
    "provenance_links.source_entity_id",
    "provenance_links.target_entity_id",
)
_GOAL_LINKS = _cleanup(ReferenceProbe.GOAL_LINKS, "goal_links.entity_id")


REFERENCE_REGISTRY: Final[Mapping[DeletableEntity, EntityReferences]] = MappingProxyType(
    {
        DeletableEntity.QUESTION: EntityReferences(
            label="Question",
            referrers=(
                _NOTE_TARGETS,
                _block(
                    ReferenceProbe.EXPERIMENT_PRIMARY_QUESTION,
                    "Experiments use {it} as their primary question",
                    "experiments.primary_question_id",
                ),
                _block(
                    ReferenceProbe.DATASET_PRIMARY_QUESTION,
                    "datasets use {it} as their primary question",
                    "datasets.primary_question_id",
                ),
                _block(
                    ReferenceProbe.DATASET_SECONDARY_QUESTION_LINKS,
                    "datasets link to {it}",
                    "dataset_question_links.question_id",
                ),
                _block(
                    ReferenceProbe.SESSION_PRIMARY_QUESTION,
                    "sessions use {it} as their primary question",
                    "sessions.primary_question_id",
                ),
                _block(
                    ReferenceProbe.CLAIM_ANSWERS,
                    "claims answer {it}",
                    "claim_questions.question_id",
                ),
                _block(
                    ReferenceProbe.CHILD_QUESTIONS,
                    "child questions list {it} as a parent",
                    "question_parents.parent_question_id",
                ),
                _block(
                    ReferenceProbe.QUESTION_REFACTORS,
                    "question refactors record {it}",
                    "question_refactors.source_question_id",
                    "question_refactors.replacement_question_id",
                ),
                _block(
                    ReferenceProbe.QUESTION_SUPERSESSION,
                    "other questions' supersession links point to {it}",
                    "questions.superseded_by_question_id",
                    "questions.supersedes_question_id",
                ),
                _EXPLORATION_TARGETS,
                _EXPLORATION_EVIDENCE,
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                _owned("question_parents.question_id"),
            ),
        ),
        DeletableEntity.DATASET: EntityReferences(
            label="Dataset",
            referrers=(
                _block(
                    ReferenceProbe.EXPERIMENT_DATASETS,
                    "Experiments reference {it}",
                    "experiment_datasets.dataset_id",
                ),
                _block(
                    ReferenceProbe.CLAIM_DATASETS,
                    "claims reference {it}",
                    "claim_datasets.dataset_id",
                ),
                _block(
                    ReferenceProbe.ANALYSIS_DATASETS,
                    "analyses reference {it}",
                    "analysis_datasets.dataset_id",
                ),
                _NOTE_TARGETS,
                _EXPLORATION_TARGETS,
                _EXPLORATION_EVIDENCE,
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                _owned("dataset_files.dataset_id", "dataset_question_links.dataset_id"),
            ),
        ),
        DeletableEntity.NOTE: EntityReferences(
            label="Note",
            referrers=(
                _block(
                    ReferenceProbe.GRAPH_DRAFT_SOURCES,
                    "graph drafts reference {it}",
                    "graph_change_sets.source_note_id",
                    "graph_change_sets.source_note_ids[json]",
                ),
                _block(
                    ReferenceProbe.DATASET_MANIFEST_NOTES,
                    "dataset manifests cite {it}",
                    "datasets.manifest_note_ids[json]",
                ),
                _NOTE_TARGETS,
                _EXPLORATION_EVIDENCE,
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                _owned("note_targets.note_id"),
            ),
        ),
        DeletableEntity.SESSION: EntityReferences(
            label="Session",
            referrers=(
                _block(
                    ReferenceProbe.EXPERIMENT_SESSIONS,
                    "Experiments reference {it}",
                    "experiment_sessions.session_id",
                ),
                _block(
                    ReferenceProbe.NON_STAGED_DATASET_SOURCE_SESSIONS,
                    "non-staged datasets reference {it}",
                    "datasets.manifest_source_session_id",
                ),
                # A staged dataset re-validates its source session on commit,
                # so a deleted source would make it uncommittable.
                _block(
                    ReferenceProbe.STAGED_DATASET_SOURCE_SESSIONS,
                    "staged datasets reference {it}",
                    "datasets.manifest_source_session_id",
                ),
                _block(
                    ReferenceProbe.ACQUISITION_COLLECTIONS,
                    "acquisition collections capture {it}",
                    "acquisition_collections.session_id",
                ),
                _NOTE_TARGETS,
                _EXPLORATION_EVIDENCE,
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                _owned("acquisition_outputs.session_id"),
            ),
        ),
        DeletableEntity.ANALYSIS: EntityReferences(
            label="Analysis",
            referrers=(
                _NOTE_TARGETS,
                _EXPLORATION_TARGETS,
                _EXPLORATION_EVIDENCE,
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                # Claim support links are governed by AnalysisService's
                # last-support rule: proposed/rejected claims and claims with
                # other support may lose this link, other claims block.
                _owned("analysis_datasets.analysis_id", "claim_analyses.analysis_id"),
            ),
            cascades_to=(
                CascadedChild(
                    entity=DeletableEntity.VISUALIZATION,
                    source="visualizations.analysis_id",
                    description="its visualizations",
                ),
            ),
        ),
        DeletableEntity.CLAIM: EntityReferences(
            label="Claim",
            referrers=(
                _block(
                    ReferenceProbe.INCOMING_CLAIM_EDGES,
                    "claim edges point to {it}",
                    "claim_edges.target_claim_id",
                ),
                _block(
                    ReferenceProbe.VISUALIZATION_CLAIMS,
                    "visualizations reference {it}",
                    "visualization_claims.claim_id",
                ),
                _NOTE_TARGETS,
                _EXPLORATION_TARGETS,
                _EXPLORATION_EVIDENCE,
                _block(
                    ReferenceProbe.PIVOT_INVALIDATED_CLAIMS,
                    "exploration pivots invalidate {it}",
                    "exploration_nodes.invalidates_claim_id",
                ),
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                # Only proposed claims are deletable, so their own evidence
                # links and outgoing edges are the claim's own assertions.
                _owned(
                    "claim_edges.claim_id",
                    "claim_datasets.claim_id",
                    "claim_analyses.claim_id",
                    "claim_questions.claim_id",
                ),
            ),
        ),
        DeletableEntity.VISUALIZATION: EntityReferences(
            label="Visualization",
            referrers=(
                _NOTE_TARGETS,
                _EXPLORATION_EVIDENCE,
                _ACCEPTED_PROVENANCE_LINKS,
                _UNACCEPTED_PROVENANCE_LINKS,
                _GOAL_LINKS,
                _owned("visualization_claims.viz_id"),
            ),
        ),
        DeletableEntity.EXPLORATION_NODE: EntityReferences(
            label="Exploration node",
            referrers=(
                _block(
                    ReferenceProbe.DEPENDENT_EXPLORATION_NODES,
                    "exploration nodes depend on {it}",
                    "exploration_node_edges.source_node_id",
                ),
                _block(
                    ReferenceProbe.PIVOT_INVALIDATED_NODES,
                    "exploration pivots invalidate {it}",
                    "exploration_nodes.invalidates_node_id",
                ),
                _owned("exploration_node_edges.target_node_id"),
            ),
        ),
    }
)


@dataclass(frozen=True)
class BlockingReference:
    """A live referrer found for the deleted entity or one of its cascaded children."""

    referrer: Referrer
    via: CascadedChild | None = None


def references_for(entity: DeletableEntity) -> EntityReferences:
    return REFERENCE_REGISTRY[entity]


def deletion_blocked_message(
    entity: DeletableEntity,
    blocking: Sequence[BlockingReference],
) -> str:
    phrases: list[str] = []
    for reference in blocking:
        phrase = reference.referrer.phrase
        if phrase is None:
            raise ValueError("Only blocking referrers can refuse a delete.")
        rendered = phrase.format(it=reference.via.description if reference.via else "it")
        if rendered not in phrases:
            phrases.append(rendered)
    label = REFERENCE_REGISTRY[entity].label
    return f"{label} cannot be deleted while {'; '.join(phrases)}."


def ensure_no_blocking_references(
    entity: DeletableEntity,
    blocking: Sequence[BlockingReference],
) -> None:
    """Fail loudly, naming every referrer, when anything still points here."""

    if blocking:
        raise ValidationError(deletion_blocked_message(entity, blocking))
