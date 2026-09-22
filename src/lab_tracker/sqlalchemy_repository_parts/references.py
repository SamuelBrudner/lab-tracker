"""SQL probes behind :mod:`lab_tracker.reference_registry`.

Each probe answers "does any live record still reference this entity?" for one
:class:`~lab_tracker.reference_registry.ReferenceProbe`. FK-less JSON payloads
(manifest note ids, evidence refs, graph-draft source notes) are portable JSON
columns on SQLite and PostgreSQL, so those probes scan the owning project's
rows in Python; every other probe is a single ``EXISTS``-style query.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any, Final
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session as OrmSession

from lab_tracker.collection_db_models import AcquisitionCollectionModel
from lab_tracker.db_models import (
    AnalysisDatasetModel,
    ClaimDatasetModel,
    ClaimEdgeModel,
    ClaimQuestionModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    ExperimentDatasetModel,
    ExperimentModel,
    ExperimentSessionModel,
    ExplorationNodeEdgeModel,
    ExplorationNodeModel,
    GoalLinkModel,
    GraphChangeSetModel,
    NoteTargetModel,
    ProvenanceLinkModel,
    QuestionModel,
    QuestionParentModel,
    QuestionRefactorModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.models import (
    DatasetStatus,
    EntityType,
    ProvenanceLinkStatus,
    QuestionLinkRole,
)
from lab_tracker.reference_registry import (
    BlockingReference,
    DeletableEntity,
    ReferenceProbe,
    references_for,
)

Probe = Callable[[OrmSession, DeletableEntity, str, str], bool]


def _exists(session: OrmSession, statement: Any) -> bool:
    return session.scalar(statement.limit(1)) is not None


def _polymorphic_type(entity: DeletableEntity) -> EntityType:
    entity_type = entity.entity_type
    if entity_type is None:
        raise ValueError(f"{entity.value} has no polymorphic entity reference type.")
    return entity_type


def _note_targets(session: OrmSession, entity: DeletableEntity, entity_id: str, _: str) -> bool:
    statement = select(NoteTargetModel.note_id).where(
        NoteTargetModel.entity_type == _polymorphic_type(entity),
        NoteTargetModel.entity_id == entity_id,
    )
    if entity is DeletableEntity.NOTE:
        statement = statement.where(NoteTargetModel.note_id != entity_id)
    return _exists(session, statement)


def _exploration_targets(
    session: OrmSession, entity: DeletableEntity, entity_id: str, _: str
) -> bool:
    return _exists(
        session,
        select(ExplorationNodeModel.node_id).where(
            ExplorationNodeModel.target_entity_type == _polymorphic_type(entity),
            ExplorationNodeModel.target_entity_id == entity_id,
        ),
    )


def _json_ref_matches(ref: object, entity_type: str, entity_id: str) -> bool:
    if not isinstance(ref, dict):
        return False
    return (
        str(ref.get("entity_type")) == entity_type
        and str(ref.get("entity_id")).lower() == entity_id.lower()
    )


def _exploration_evidence(
    session: OrmSession, entity: DeletableEntity, entity_id: str, project_id: str
) -> bool:
    entity_type = _polymorphic_type(entity).value
    evidence_lists = session.scalars(
        select(ExplorationNodeModel.evidence_refs).where(
            ExplorationNodeModel.project_id == project_id
        )
    )
    return any(
        _json_ref_matches(ref, entity_type, entity_id)
        for evidence_refs in evidence_lists
        for ref in evidence_refs or []
    )


def _provenance_link_clause(entity: DeletableEntity, entity_id: str) -> Any:
    entity_type = _polymorphic_type(entity)
    return or_(
        (ProvenanceLinkModel.source_entity_type == entity_type)
        & (ProvenanceLinkModel.source_entity_id == entity_id),
        (ProvenanceLinkModel.target_entity_type == entity_type)
        & (ProvenanceLinkModel.target_entity_id == entity_id),
    )


def _accepted_provenance_links(
    session: OrmSession, entity: DeletableEntity, entity_id: str, _: str
) -> bool:
    return _exists(
        session,
        select(ProvenanceLinkModel.link_id).where(
            _provenance_link_clause(entity, entity_id),
            ProvenanceLinkModel.status == ProvenanceLinkStatus.ACCEPTED,
        ),
    )


def _unaccepted_provenance_links(
    session: OrmSession, entity: DeletableEntity, entity_id: str, _: str
) -> bool:
    return _exists(
        session,
        select(ProvenanceLinkModel.link_id).where(
            _provenance_link_clause(entity, entity_id),
            ProvenanceLinkModel.status != ProvenanceLinkStatus.ACCEPTED,
        ),
    )


def _goal_links(session: OrmSession, entity: DeletableEntity, entity_id: str, _: str) -> bool:
    return _exists(
        session,
        select(GoalLinkModel.link_id).where(
            GoalLinkModel.entity_type == _polymorphic_type(entity),
            GoalLinkModel.entity_id == entity_id,
        ),
    )


def _column_probe(column: Any, *criteria: Any) -> Probe:
    def probe(session: OrmSession, _entity: DeletableEntity, entity_id: str, __: str) -> bool:
        return _exists(session, select(column).where(column == entity_id, *criteria))

    return probe


def _question_refactors(
    session: OrmSession, _: DeletableEntity, entity_id: str, __: str
) -> bool:
    return _exists(
        session,
        select(QuestionRefactorModel.refactor_id).where(
            or_(
                QuestionRefactorModel.source_question_id == entity_id,
                QuestionRefactorModel.replacement_question_id == entity_id,
            )
        ),
    )


def _question_supersession(
    session: OrmSession, _: DeletableEntity, entity_id: str, __: str
) -> bool:
    return _exists(
        session,
        select(QuestionModel.question_id).where(
            QuestionModel.question_id != entity_id,
            or_(
                QuestionModel.superseded_by_question_id == entity_id,
                QuestionModel.supersedes_question_id == entity_id,
            ),
        ),
    )


def _graph_draft_sources(
    session: OrmSession, _: DeletableEntity, entity_id: str, project_id: str
) -> bool:
    if _exists(
        session,
        select(GraphChangeSetModel.change_set_id).where(
            GraphChangeSetModel.source_note_id == entity_id
        ),
    ):
        return True
    source_lists = session.scalars(
        select(GraphChangeSetModel.source_note_ids).where(
            GraphChangeSetModel.project_id == project_id
        )
    )
    return any(
        str(note_id).lower() == entity_id.lower()
        for note_ids in source_lists
        for note_id in note_ids or []
    )


def _dataset_manifest_notes(
    session: OrmSession, _: DeletableEntity, entity_id: str, project_id: str
) -> bool:
    manifests = session.scalars(
        select(DatasetModel.manifest_note_ids).where(DatasetModel.project_id == project_id)
    )
    return any(
        str(note_id).lower() == entity_id.lower()
        for note_ids in manifests
        for note_id in note_ids or []
    )


_PROBES: Final[Mapping[ReferenceProbe, Probe]] = MappingProxyType(
    {
        ReferenceProbe.NOTE_TARGETS: _note_targets,
        ReferenceProbe.EXPLORATION_TARGETS: _exploration_targets,
        ReferenceProbe.EXPLORATION_EVIDENCE: _exploration_evidence,
        ReferenceProbe.ACCEPTED_PROVENANCE_LINKS: _accepted_provenance_links,
        ReferenceProbe.UNACCEPTED_PROVENANCE_LINKS: _unaccepted_provenance_links,
        ReferenceProbe.GOAL_LINKS: _goal_links,
        ReferenceProbe.CHILD_QUESTIONS: _column_probe(QuestionParentModel.parent_question_id),
        ReferenceProbe.QUESTION_REFACTORS: _question_refactors,
        ReferenceProbe.QUESTION_SUPERSESSION: _question_supersession,
        ReferenceProbe.EXPERIMENT_PRIMARY_QUESTION: _column_probe(
            ExperimentModel.primary_question_id
        ),
        ReferenceProbe.DATASET_PRIMARY_QUESTION: _column_probe(DatasetModel.primary_question_id),
        ReferenceProbe.DATASET_SECONDARY_QUESTION_LINKS: _column_probe(
            DatasetQuestionLinkModel.question_id,
            DatasetQuestionLinkModel.role != QuestionLinkRole.PRIMARY,
        ),
        ReferenceProbe.SESSION_PRIMARY_QUESTION: _column_probe(SessionModel.primary_question_id),
        ReferenceProbe.CLAIM_ANSWERS: _column_probe(ClaimQuestionModel.question_id),
        ReferenceProbe.EXPERIMENT_DATASETS: _column_probe(ExperimentDatasetModel.dataset_id),
        ReferenceProbe.CLAIM_DATASETS: _column_probe(ClaimDatasetModel.dataset_id),
        ReferenceProbe.ANALYSIS_DATASETS: _column_probe(AnalysisDatasetModel.dataset_id),
        ReferenceProbe.GRAPH_DRAFT_SOURCES: _graph_draft_sources,
        ReferenceProbe.DATASET_MANIFEST_NOTES: _dataset_manifest_notes,
        ReferenceProbe.EXPERIMENT_SESSIONS: _column_probe(ExperimentSessionModel.session_id),
        ReferenceProbe.NON_STAGED_DATASET_SOURCE_SESSIONS: _column_probe(
            DatasetModel.manifest_source_session_id,
            DatasetModel.status != DatasetStatus.STAGED,
        ),
        ReferenceProbe.STAGED_DATASET_SOURCE_SESSIONS: _column_probe(
            DatasetModel.manifest_source_session_id,
            DatasetModel.status == DatasetStatus.STAGED,
        ),
        ReferenceProbe.ACQUISITION_COLLECTIONS: _column_probe(
            AcquisitionCollectionModel.session_id
        ),
        ReferenceProbe.INCOMING_CLAIM_EDGES: _column_probe(ClaimEdgeModel.target_claim_id),
        ReferenceProbe.VISUALIZATION_CLAIMS: _column_probe(VisualizationClaimModel.claim_id),
        ReferenceProbe.PIVOT_INVALIDATED_CLAIMS: _column_probe(
            ExplorationNodeModel.invalidates_claim_id
        ),
        ReferenceProbe.DEPENDENT_EXPLORATION_NODES: _column_probe(
            ExplorationNodeEdgeModel.source_node_id
        ),
        ReferenceProbe.PIVOT_INVALIDATED_NODES: _column_probe(
            ExplorationNodeModel.invalidates_node_id
        ),
    }
)


def implemented_probes() -> frozenset[ReferenceProbe]:
    return frozenset(_PROBES)


def _cascaded_child_ids(
    session: OrmSession,
    parent: DeletableEntity,
    child: DeletableEntity,
    parent_id: str,
) -> list[str]:
    if parent is DeletableEntity.ANALYSIS and child is DeletableEntity.VISUALIZATION:
        return [
            str(value)
            for value in session.scalars(
                select(VisualizationModel.viz_id)
                .where(VisualizationModel.analysis_id == parent_id)
                .order_by(VisualizationModel.viz_id)
            )
        ]
    raise ValueError(f"No cascade query from {parent.value} to {child.value}.")


def find_blocking_references(
    session: OrmSession,
    entity: DeletableEntity,
    entity_id: UUID,
    *,
    project_id: UUID,
) -> list[BlockingReference]:
    """Return every blocking referrer of the entity and of its cascaded children."""

    session.flush()
    project_value = str(project_id)
    registry = references_for(entity)
    found: list[BlockingReference] = [
        BlockingReference(referrer=referrer)
        for referrer in registry.blocking
        if _probe(referrer.probe)(session, entity, str(entity_id), project_value)
    ]
    for child in registry.cascades_to:
        child_references = references_for(child.entity)
        for child_id in _cascaded_child_ids(session, entity, child.entity, str(entity_id)):
            found.extend(
                BlockingReference(referrer=referrer, via=child)
                for referrer in child_references.blocking
                if _probe(referrer.probe)(session, child.entity, child_id, project_value)
            )
    return found


def _probe(probe: ReferenceProbe | None) -> Probe:
    if probe is None:
        raise ValueError("Referrer has no probe.")
    return _PROBES[probe]


def remove_unaccepted_provenance_links(
    session: OrmSession,
    entity: DeletableEntity,
    entity_ids: Iterable[UUID],
) -> None:
    """Delete proposed/rejected provenance links that name a deleted entity."""

    for entity_id in sorted(set(entity_ids), key=str):
        session.execute(
            delete(ProvenanceLinkModel)
            .where(
                _provenance_link_clause(entity, str(entity_id)),
                ProvenanceLinkModel.status != ProvenanceLinkStatus.ACCEPTED,
            )
        )
