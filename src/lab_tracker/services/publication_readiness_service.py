"""Publication-readiness structural checks for Ara export."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    Analysis,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetStatus,
    EntityRef,
    EntityType,
    ExternalArtifactReference,
    Goal,
    Note,
    Project,
    PublicationReadinessBrokenExternalRef,
    PublicationReadinessOrphanedEntity,
    PublicationReadinessReport,
    PublicationReadinessUngroundedQuestion,
    PublicationReadinessUnsupportedClaim,
    Question,
    QuestionStatus,
    Session,
    Visualization,
    external_artifact_uri_validation_error,
)
from lab_tracker.services.base import BaseService, ServiceContext


class ProjectReadAccess(Protocol):
    """Opaque project lookup needed by publication-readiness checks."""

    def get_project_for_read(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> Project: ...


class PublicationReadinessService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectReadAccess,
    ) -> None:
        super().__init__(context)
        self.projects = projects

    def check(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> PublicationReadinessReport:
        self.projects.get_project_for_read(project_id, actor=actor)
        repository = self.repository
        questions, _ = repository.query_questions(project_id=project_id, limit=None, offset=0)
        datasets, _ = repository.query_datasets(project_id=project_id, limit=None, offset=0)
        analyses, _ = repository.query_analyses(project_id=project_id, limit=None, offset=0)
        claims, _ = repository.query_claims(project_id=project_id, limit=None, offset=0)
        visualizations, _ = repository.query_visualizations(
            project_id=project_id,
            limit=None,
            offset=0,
        )
        notes, _ = repository.query_notes(project_id=project_id, limit=None, offset=0)
        goals, _ = repository.query_goals(project_id=project_id, limit=None, offset=0)
        sessions, _ = repository.query_sessions(project_id=project_id, limit=None, offset=0)

        unsupported_claims = _unsupported_claims(claims)
        ungrounded_questions = _ungrounded_questions(questions, datasets)
        orphaned_entities = _orphaned_entities(
            project_id=project_id,
            questions=questions,
            datasets=datasets,
            analyses=analyses,
            claims=claims,
            visualizations=visualizations,
            notes=notes,
            goals=goals,
            sessions=sessions,
        )
        broken_external_refs: list[PublicationReadinessBrokenExternalRef] = []
        for dataset in datasets:
            broken_external_refs.extend(
                _broken_external_refs(
                    EntityType.DATASET,
                    dataset.dataset_id,
                    _dataset_refs(dataset),
                )
            )
        for analysis in analyses:
            broken_external_refs.extend(
                _broken_external_refs(
                    EntityType.ANALYSIS,
                    analysis.analysis_id,
                    analysis.external_artifacts,
                )
            )
        for claim in claims:
            broken_external_refs.extend(
                _broken_external_refs(
                    EntityType.CLAIM,
                    claim.claim_id,
                    claim.external_citations,
                )
            )
        seal_level = (
            "ara_l1"
            if not unsupported_claims
            and not ungrounded_questions
            and not orphaned_entities
            and not broken_external_refs
            else "blocked"
        )
        return PublicationReadinessReport(
            project_id=project_id,
            unsupported_claims=unsupported_claims,
            ungrounded_questions=ungrounded_questions,
            orphaned_entities=orphaned_entities,
            broken_external_refs=broken_external_refs,
            seal_level=seal_level,
        )


def _unsupported_claims(
    claims: list[Claim],
) -> list[PublicationReadinessUnsupportedClaim]:
    unsupported: list[PublicationReadinessUnsupportedClaim] = []
    for claim in claims:
        if claim.status != ClaimStatus.SUPPORTED:
            continue
        if not claim.supported_by_dataset_ids and not claim.supported_by_analysis_ids:
            unsupported.append(
                PublicationReadinessUnsupportedClaim(
                    claim_id=claim.claim_id,
                    statement=claim.statement,
                    status=claim.status,
                    reason="Supported claim has no dataset or analysis evidence.",
                )
            )
        if not claim.falsification_criteria:
            unsupported.append(
                PublicationReadinessUnsupportedClaim(
                    claim_id=claim.claim_id,
                    statement=claim.statement,
                    status=claim.status,
                    reason="Supported claim has no falsification criteria.",
                )
            )
    return unsupported


def _ungrounded_questions(
    questions: list[Question],
    datasets: list[Dataset],
) -> list[PublicationReadinessUngroundedQuestion]:
    committed_dataset_question_ids = {
        link.question_id
        for dataset in datasets
        if dataset.status == DatasetStatus.COMMITTED
        for link in dataset.question_links
    }
    committed_dataset_question_ids.update(
        dataset.primary_question_id
        for dataset in datasets
        if dataset.status == DatasetStatus.COMMITTED
    )
    return [
        PublicationReadinessUngroundedQuestion(
            question_id=question.question_id,
            text=question.text,
            status=question.status,
            reason="Answered question has no committed dataset evidence.",
        )
        for question in questions
        if question.status == QuestionStatus.ANSWERED
        and question.question_id not in committed_dataset_question_ids
    ]


def _orphaned_entities(
    *,
    project_id: UUID,
    questions: list[Question],
    datasets: list[Dataset],
    analyses: list[Analysis],
    claims: list[Claim],
    visualizations: list[Visualization],
    notes: list[Note],
    goals: list[Goal],
    sessions: list[Session],
) -> list[PublicationReadinessOrphanedEntity]:
    known_ids: dict[EntityType, set[UUID]] = {
        EntityType.PROJECT: {project_id},
        EntityType.QUESTION: {question.question_id for question in questions},
        EntityType.DATASET: {dataset.dataset_id for dataset in datasets},
        EntityType.ANALYSIS: {analysis.analysis_id for analysis in analyses},
        EntityType.CLAIM: {claim.claim_id for claim in claims},
        EntityType.VISUALIZATION: {visualization.viz_id for visualization in visualizations},
        EntityType.NOTE: {note.note_id for note in notes},
        EntityType.GOAL: {goal.goal_id for goal in goals},
        EntityType.SESSION: {session.session_id for session in sessions},
    }
    orphaned: list[PublicationReadinessOrphanedEntity] = []
    for dataset in datasets:
        _append_missing_ref(
            orphaned,
            entity_type=EntityType.DATASET,
            entity_id=dataset.dataset_id,
            relation="primary_question",
            ref=EntityRef(
                entity_type=EntityType.QUESTION,
                entity_id=dataset.primary_question_id,
            ),
            known_ids=known_ids,
        )
        for link in dataset.question_links:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.DATASET,
                entity_id=dataset.dataset_id,
                relation="question_link",
                ref=EntityRef(entity_type=EntityType.QUESTION, entity_id=link.question_id),
                known_ids=known_ids,
            )
    for analysis in analyses:
        for dataset_id in analysis.dataset_ids:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.ANALYSIS,
                entity_id=analysis.analysis_id,
                relation="dataset",
                ref=EntityRef(entity_type=EntityType.DATASET, entity_id=dataset_id),
                known_ids=known_ids,
            )
    for claim in claims:
        for dataset_id in claim.supported_by_dataset_ids:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.CLAIM,
                entity_id=claim.claim_id,
                relation="supports_dataset",
                ref=EntityRef(entity_type=EntityType.DATASET, entity_id=dataset_id),
                known_ids=known_ids,
            )
        for analysis_id in claim.supported_by_analysis_ids:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.CLAIM,
                entity_id=claim.claim_id,
                relation="supports_analysis",
                ref=EntityRef(entity_type=EntityType.ANALYSIS, entity_id=analysis_id),
                known_ids=known_ids,
            )
        for question_id in claim.answers_question_ids:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.CLAIM,
                entity_id=claim.claim_id,
                relation="answers_question",
                ref=EntityRef(entity_type=EntityType.QUESTION, entity_id=question_id),
                known_ids=known_ids,
            )
    for visualization in visualizations:
        _append_missing_ref(
            orphaned,
            entity_type=EntityType.VISUALIZATION,
            entity_id=visualization.viz_id,
            relation="analysis",
            ref=EntityRef(
                entity_type=EntityType.ANALYSIS,
                entity_id=visualization.analysis_id,
            ),
            known_ids=known_ids,
        )
        for claim_id in visualization.related_claim_ids:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.VISUALIZATION,
                entity_id=visualization.viz_id,
                relation="related_claim",
                ref=EntityRef(entity_type=EntityType.CLAIM, entity_id=claim_id),
                known_ids=known_ids,
            )
    for note in notes:
        for target in note.targets:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.NOTE,
                entity_id=note.note_id,
                relation="target",
                ref=target,
                known_ids=known_ids,
            )
    for goal in goals:
        for link in goal.links:
            _append_missing_ref(
                orphaned,
                entity_type=EntityType.GOAL,
                entity_id=goal.goal_id,
                relation=link.relation.value,
                ref=link.target,
                known_ids=known_ids,
            )
    return orphaned


def _append_missing_ref(
    orphaned: list[PublicationReadinessOrphanedEntity],
    *,
    entity_type: EntityType,
    entity_id: UUID,
    relation: str,
    ref: EntityRef,
    known_ids: dict[EntityType, set[UUID]],
) -> None:
    if ref.entity_id in known_ids.get(ref.entity_type, set()):
        return
    orphaned.append(
        PublicationReadinessOrphanedEntity(
            entity_type=entity_type,
            entity_id=entity_id,
            relation=relation,
            missing_entity_type=ref.entity_type,
            missing_entity_id=ref.entity_id,
        )
    )


def _dataset_refs(dataset: Dataset) -> list[ExternalArtifactReference]:
    return list(dataset.commit_manifest.external_artifacts)


def _broken_external_refs(
    entity_type: EntityType,
    entity_id: UUID,
    artifacts: list[ExternalArtifactReference],
) -> list[PublicationReadinessBrokenExternalRef]:
    broken: list[PublicationReadinessBrokenExternalRef] = []
    for artifact in artifacts:
        reason = external_artifact_uri_validation_error(artifact.uri)
        if reason is None:
            continue
        broken.append(
            PublicationReadinessBrokenExternalRef(
                entity_type=entity_type,
                entity_id=entity_id,
                source_system=artifact.source_system,
                uri=artifact.uri,
                reason=reason,
            )
        )
    return broken
