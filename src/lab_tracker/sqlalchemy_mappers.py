"""Mapping helpers between domain dataclasses and SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from lab_tracker.db_models import (
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    DatasetReviewModel,
    NoteExtractedEntityModel,
    NoteModel,
    NoteTagSuggestionModel,
    NoteTargetModel,
    ProjectModel,
    QuestionModel,
    QuestionParentModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetReview,
    DatasetReviewStatus,
    DatasetStatus,
    EntityRef,
    EntityTagSuggestion,
    EntityType,
    ExtractedEntity,
    Note,
    NoteStatus,
    OutcomeStatus,
    Project,
    ProjectReviewPolicy,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    Session,
    SessionStatus,
    SessionType,
    TagSuggestionStatus,
    Visualization,
)


def _uuid(raw: str) -> UUID:
    return UUID(raw)


def _uuid_str(value: UUID) -> str:
    return str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_utc_optional(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _as_utc(value)


def project_to_model(project: Project) -> ProjectModel:
    return ProjectModel(
        project_id=_uuid_str(project.project_id),
        name=project.name,
        description=project.description,
        status=project.status.value,
        review_policy=project.review_policy.value,
        created_by=project.created_by,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_from_model(row: ProjectModel) -> Project:
    return Project(
        project_id=_uuid(row.project_id),
        name=row.name,
        description=row.description,
        status=ProjectStatus(row.status),
        review_policy=ProjectReviewPolicy(getattr(row, "review_policy", "none")),
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def apply_project_to_model(row: ProjectModel, project: Project) -> None:
    row.name = project.name
    row.description = project.description
    row.status = project.status.value
    row.review_policy = project.review_policy.value
    row.created_by = project.created_by
    row.created_at = project.created_at
    row.updated_at = project.updated_at


def question_to_model(question: Question) -> QuestionModel:
    return QuestionModel(
        question_id=_uuid_str(question.question_id),
        project_id=_uuid_str(question.project_id),
        text=question.text,
        question_type=question.question_type.value,
        hypothesis=question.hypothesis,
        status=question.status.value,
        created_from=question.created_from.value,
        created_by=question.created_by,
        created_at=question.created_at,
        updated_at=question.updated_at,
    )


def question_from_model(
    row: QuestionModel,
    *,
    parent_question_ids: Iterable[UUID] = (),
) -> Question:
    return Question(
        question_id=_uuid(row.question_id),
        project_id=_uuid(row.project_id),
        text=row.text,
        question_type=QuestionType(row.question_type),
        hypothesis=row.hypothesis,
        status=QuestionStatus(row.status),
        parent_question_ids=list(parent_question_ids),
        created_from=QuestionSource(row.created_from),
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def question_parent_models(question: Question) -> list[QuestionParentModel]:
    return [
        QuestionParentModel(
            question_id=_uuid_str(question.question_id),
            parent_question_id=_uuid_str(parent_id),
        )
        for parent_id in question.parent_question_ids
    ]


def apply_question_to_model(row: QuestionModel, question: Question) -> None:
    row.project_id = _uuid_str(question.project_id)
    row.text = question.text
    row.question_type = question.question_type.value
    row.hypothesis = question.hypothesis
    row.status = question.status.value
    row.created_from = question.created_from.value
    row.created_by = question.created_by
    row.created_at = question.created_at
    row.updated_at = question.updated_at


def dataset_to_model(dataset: Dataset) -> DatasetModel:
    return DatasetModel(
        dataset_id=_uuid_str(dataset.dataset_id),
        project_id=_uuid_str(dataset.project_id),
        commit_hash=dataset.commit_hash,
        primary_question_id=_uuid_str(dataset.primary_question_id),
        status=dataset.status.value,
        created_by=dataset.created_by,
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
    )


def dataset_from_model(
    row: DatasetModel,
    *,
    question_links: Iterable[QuestionLink] = (),
) -> Dataset:
    links = list(question_links)
    if not any(link.role == QuestionLinkRole.PRIMARY for link in links):
        links.insert(
            0,
            QuestionLink(
                question_id=_uuid(row.primary_question_id),
                role=QuestionLinkRole.PRIMARY,
            ),
        )
    manifest = DatasetCommitManifest(question_links=links)
    return Dataset(
        dataset_id=_uuid(row.dataset_id),
        project_id=_uuid(row.project_id),
        commit_hash=row.commit_hash,
        primary_question_id=_uuid(row.primary_question_id),
        question_links=links,
        commit_manifest=manifest,
        status=DatasetStatus(row.status),
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def dataset_question_link_from_model(row: DatasetQuestionLinkModel) -> QuestionLink:
    return QuestionLink(
        question_id=_uuid(row.question_id),
        role=QuestionLinkRole(row.role),
        outcome_status=OutcomeStatus(row.outcome_status),
    )


def dataset_question_link_models(dataset: Dataset) -> list[DatasetQuestionLinkModel]:
    return [
        DatasetQuestionLinkModel(
            dataset_id=_uuid_str(dataset.dataset_id),
            question_id=_uuid_str(link.question_id),
            role=link.role.value,
            outcome_status=link.outcome_status.value,
        )
        for link in dataset.question_links
    ]


def apply_dataset_to_model(row: DatasetModel, dataset: Dataset) -> None:
    row.project_id = _uuid_str(dataset.project_id)
    row.commit_hash = dataset.commit_hash
    row.primary_question_id = _uuid_str(dataset.primary_question_id)
    row.status = dataset.status.value
    row.created_by = dataset.created_by
    row.created_at = dataset.created_at
    row.updated_at = dataset.updated_at


def dataset_review_to_model(review: DatasetReview) -> DatasetReviewModel:
    return DatasetReviewModel(
        review_id=_uuid_str(review.review_id),
        dataset_id=_uuid_str(review.dataset_id),
        reviewer_user_id=_uuid_str(review.reviewer_user_id) if review.reviewer_user_id else None,
        status=review.status.value,
        comments=review.comments,
        requested_at=review.requested_at,
        resolved_at=review.resolved_at,
    )


def dataset_review_from_model(row: DatasetReviewModel) -> DatasetReview:
    return DatasetReview(
        review_id=_uuid(row.review_id),
        dataset_id=_uuid(row.dataset_id),
        reviewer_user_id=_uuid(row.reviewer_user_id) if row.reviewer_user_id else None,
        status=DatasetReviewStatus(row.status),
        comments=row.comments,
        requested_at=_as_utc(row.requested_at),
        resolved_at=_as_utc_optional(row.resolved_at),
    )


def apply_dataset_review_to_model(row: DatasetReviewModel, review: DatasetReview) -> None:
    row.dataset_id = _uuid_str(review.dataset_id)
    row.reviewer_user_id = _uuid_str(review.reviewer_user_id) if review.reviewer_user_id else None
    row.status = review.status.value
    row.comments = review.comments
    row.requested_at = review.requested_at
    row.resolved_at = review.resolved_at


def note_to_model(note: Note) -> NoteModel:
    return NoteModel(
        note_id=_uuid_str(note.note_id),
        project_id=_uuid_str(note.project_id),
        raw_content=note.raw_content,
        transcribed_text=note.transcribed_text,
        status=note.status.value,
        created_by=note.created_by,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def note_from_model(
    row: NoteModel,
    *,
    extracted_entities: Iterable[ExtractedEntity] = (),
    tag_suggestions: Iterable[EntityTagSuggestion] = (),
    targets: Iterable[EntityRef] = (),
) -> Note:
    return Note(
        note_id=_uuid(row.note_id),
        project_id=_uuid(row.project_id),
        raw_content=row.raw_content,
        transcribed_text=row.transcribed_text,
        extracted_entities=list(extracted_entities),
        tag_suggestions=list(tag_suggestions),
        targets=list(targets),
        status=NoteStatus(row.status),
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def extracted_entity_from_model(row: NoteExtractedEntityModel) -> ExtractedEntity:
    return ExtractedEntity(
        label=row.label,
        confidence=row.confidence,
        provenance=row.provenance,
    )


def note_extracted_entity_models(note: Note) -> list[NoteExtractedEntityModel]:
    return [
        NoteExtractedEntityModel(
            note_id=_uuid_str(note.note_id),
            label=entity.label,
            confidence=entity.confidence,
            provenance=entity.provenance,
        )
        for entity in note.extracted_entities
    ]


def tag_suggestion_from_model(row: NoteTagSuggestionModel) -> EntityTagSuggestion:
    return EntityTagSuggestion(
        suggestion_id=_uuid(row.suggestion_id),
        entity_label=row.entity_label,
        vocabulary=row.vocabulary,
        term_id=row.term_id,
        term_label=row.term_label,
        confidence=row.confidence,
        provenance=row.provenance,
        status=TagSuggestionStatus(row.status),
        reviewed_by=row.reviewed_by,
        reviewed_at=_as_utc_optional(row.reviewed_at),
    )


def note_tag_suggestion_models(note: Note) -> list[NoteTagSuggestionModel]:
    return [
        NoteTagSuggestionModel(
            suggestion_id=_uuid_str(suggestion.suggestion_id),
            note_id=_uuid_str(note.note_id),
            entity_label=suggestion.entity_label,
            vocabulary=suggestion.vocabulary,
            term_id=suggestion.term_id,
            term_label=suggestion.term_label,
            confidence=suggestion.confidence,
            provenance=suggestion.provenance,
            status=suggestion.status.value,
            reviewed_by=suggestion.reviewed_by,
            reviewed_at=suggestion.reviewed_at,
        )
        for suggestion in note.tag_suggestions
    ]


def entity_ref_from_model(row: NoteTargetModel) -> EntityRef:
    return EntityRef(
        entity_type=EntityType(row.entity_type),
        entity_id=_uuid(row.entity_id),
    )


def note_target_models(note: Note) -> list[NoteTargetModel]:
    return [
        NoteTargetModel(
            note_id=_uuid_str(note.note_id),
            entity_type=target.entity_type.value,
            entity_id=_uuid_str(target.entity_id),
        )
        for target in note.targets
    ]


def apply_note_to_model(row: NoteModel, note: Note) -> None:
    row.project_id = _uuid_str(note.project_id)
    row.raw_content = note.raw_content
    row.transcribed_text = note.transcribed_text
    row.status = note.status.value
    row.created_by = note.created_by
    row.created_at = note.created_at
    row.updated_at = note.updated_at


def session_to_model(session: Session) -> SessionModel:
    return SessionModel(
        session_id=_uuid_str(session.session_id),
        project_id=_uuid_str(session.project_id),
        session_type=session.session_type.value,
        status=session.status.value,
        primary_question_id=(
            _uuid_str(session.primary_question_id)
            if session.primary_question_id is not None
            else None
        ),
        started_at=session.started_at,
        ended_at=session.ended_at,
        created_by=session.created_by,
        updated_at=session.updated_at,
    )


def session_from_model(row: SessionModel) -> Session:
    return Session(
        session_id=_uuid(row.session_id),
        project_id=_uuid(row.project_id),
        session_type=SessionType(row.session_type),
        status=SessionStatus(row.status),
        primary_question_id=(
            _uuid(row.primary_question_id) if row.primary_question_id is not None else None
        ),
        started_at=_as_utc(row.started_at),
        ended_at=_as_utc_optional(row.ended_at),
        created_by=row.created_by,
        updated_at=_as_utc(row.updated_at),
    )


def apply_session_to_model(row: SessionModel, session: Session) -> None:
    row.project_id = _uuid_str(session.project_id)
    row.session_type = session.session_type.value
    row.status = session.status.value
    row.primary_question_id = (
        _uuid_str(session.primary_question_id) if session.primary_question_id is not None else None
    )
    row.started_at = session.started_at
    row.ended_at = session.ended_at
    row.created_by = session.created_by
    row.updated_at = session.updated_at


def analysis_to_model(analysis: Analysis) -> AnalysisModel:
    return AnalysisModel(
        analysis_id=_uuid_str(analysis.analysis_id),
        project_id=_uuid_str(analysis.project_id),
        method_hash=analysis.method_hash,
        code_version=analysis.code_version,
        environment_hash=analysis.environment_hash,
        executed_by=analysis.executed_by,
        executed_at=analysis.executed_at,
        status=analysis.status.value,
        created_at=analysis.created_at,
        updated_at=analysis.updated_at,
    )


def analysis_from_model(
    row: AnalysisModel,
    *,
    dataset_ids: Iterable[UUID] = (),
) -> Analysis:
    return Analysis(
        analysis_id=_uuid(row.analysis_id),
        project_id=_uuid(row.project_id),
        dataset_ids=list(dataset_ids),
        method_hash=row.method_hash,
        code_version=row.code_version,
        environment_hash=row.environment_hash,
        executed_by=row.executed_by,
        executed_at=_as_utc(row.executed_at),
        status=AnalysisStatus(row.status),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def analysis_dataset_models(analysis: Analysis) -> list[AnalysisDatasetModel]:
    return [
        AnalysisDatasetModel(
            analysis_id=_uuid_str(analysis.analysis_id),
            dataset_id=_uuid_str(dataset_id),
        )
        for dataset_id in analysis.dataset_ids
    ]


def apply_analysis_to_model(row: AnalysisModel, analysis: Analysis) -> None:
    row.project_id = _uuid_str(analysis.project_id)
    row.method_hash = analysis.method_hash
    row.code_version = analysis.code_version
    row.environment_hash = analysis.environment_hash
    row.executed_by = analysis.executed_by
    row.executed_at = analysis.executed_at
    row.status = analysis.status.value
    row.created_at = analysis.created_at
    row.updated_at = analysis.updated_at


def claim_to_model(claim: Claim) -> ClaimModel:
    return ClaimModel(
        claim_id=_uuid_str(claim.claim_id),
        project_id=_uuid_str(claim.project_id),
        statement=claim.statement,
        confidence=claim.confidence,
        status=claim.status.value,
        created_at=claim.created_at,
        updated_at=claim.updated_at,
    )


def claim_from_model(
    row: ClaimModel,
    *,
    supported_by_dataset_ids: Iterable[UUID] = (),
    supported_by_analysis_ids: Iterable[UUID] = (),
) -> Claim:
    return Claim(
        claim_id=_uuid(row.claim_id),
        project_id=_uuid(row.project_id),
        statement=row.statement,
        confidence=row.confidence,
        status=ClaimStatus(row.status),
        supported_by_dataset_ids=list(supported_by_dataset_ids),
        supported_by_analysis_ids=list(supported_by_analysis_ids),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def claim_dataset_models(claim: Claim) -> list[ClaimDatasetModel]:
    return [
        ClaimDatasetModel(
            claim_id=_uuid_str(claim.claim_id),
            dataset_id=_uuid_str(dataset_id),
        )
        for dataset_id in claim.supported_by_dataset_ids
    ]


def claim_analysis_models(claim: Claim) -> list[ClaimAnalysisModel]:
    return [
        ClaimAnalysisModel(
            claim_id=_uuid_str(claim.claim_id),
            analysis_id=_uuid_str(analysis_id),
        )
        for analysis_id in claim.supported_by_analysis_ids
    ]


def apply_claim_to_model(row: ClaimModel, claim: Claim) -> None:
    row.project_id = _uuid_str(claim.project_id)
    row.statement = claim.statement
    row.confidence = claim.confidence
    row.status = claim.status.value
    row.created_at = claim.created_at
    row.updated_at = claim.updated_at


def visualization_to_model(visualization: Visualization) -> VisualizationModel:
    return VisualizationModel(
        viz_id=_uuid_str(visualization.viz_id),
        analysis_id=_uuid_str(visualization.analysis_id),
        viz_type=visualization.viz_type,
        file_path=visualization.file_path,
        caption=visualization.caption,
        created_at=visualization.created_at,
        updated_at=visualization.updated_at,
    )


def visualization_from_model(
    row: VisualizationModel,
    *,
    related_claim_ids: Iterable[UUID] = (),
) -> Visualization:
    return Visualization(
        viz_id=_uuid(row.viz_id),
        analysis_id=_uuid(row.analysis_id),
        viz_type=row.viz_type,
        file_path=row.file_path,
        caption=row.caption,
        related_claim_ids=list(related_claim_ids),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def visualization_claim_models(visualization: Visualization) -> list[VisualizationClaimModel]:
    return [
        VisualizationClaimModel(
            viz_id=_uuid_str(visualization.viz_id),
            claim_id=_uuid_str(claim_id),
        )
        for claim_id in visualization.related_claim_ids
    ]


def apply_visualization_to_model(row: VisualizationModel, visualization: Visualization) -> None:
    row.analysis_id = _uuid_str(visualization.analysis_id)
    row.viz_type = visualization.viz_type
    row.file_path = visualization.file_path
    row.caption = visualization.caption
    row.created_at = visualization.created_at
    row.updated_at = visualization.updated_at
