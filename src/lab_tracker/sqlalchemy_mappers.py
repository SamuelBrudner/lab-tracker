"""Mapping helpers between domain dataclasses and SQLAlchemy models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from lab_tracker.db_models import (
    AcquisitionOutputModel,
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    NoteModel,
    NoteTargetModel,
    ProjectModel,
    QuestionModel,
    QuestionParentModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimStatus,
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    DatasetStatus,
    EntityRef,
    EntityType,
    Note,
    NoteRawAsset,
    NoteStatus,
    OutcomeStatus,
    Project,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    Session,
    SessionStatus,
    SessionType,
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


def _dataset_files_to_json(files: Iterable[DatasetFile]) -> list[dict[str, object]]:
    return [file.model_dump(mode="json") for file in files]


def _dataset_files_from_json(raw_files: Iterable[object] | None) -> list[DatasetFile]:
    if not raw_files:
        return []
    return [DatasetFile.model_validate(item) for item in raw_files]


def project_to_model(project: Project) -> ProjectModel:
    return ProjectModel(
        project_id=_uuid_str(project.project_id),
        name=project.name,
        description=project.description,
        status=project.status.value,
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
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def apply_project_to_model(row: ProjectModel, project: Project) -> None:
    row.name = project.name
    row.description = project.description
    row.status = project.status.value
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
    row.created_by = question.created_by
    row.created_at = question.created_at
    row.updated_at = question.updated_at


def dataset_to_model(dataset: Dataset) -> DatasetModel:
    manifest = dataset.commit_manifest
    return DatasetModel(
        dataset_id=_uuid_str(dataset.dataset_id),
        project_id=_uuid_str(dataset.project_id),
        commit_hash=dataset.commit_hash,
        primary_question_id=_uuid_str(dataset.primary_question_id),
        manifest_files=_dataset_files_to_json(manifest.files),
        manifest_metadata=dict(manifest.metadata),
        manifest_nwb_metadata=dict(manifest.nwb_metadata),
        manifest_bids_metadata=dict(manifest.bids_metadata),
        manifest_note_ids=[str(note_id) for note_id in manifest.note_ids],
        manifest_source_session_id=(
            _uuid_str(manifest.source_session_id)
            if manifest.source_session_id is not None
            else None
        ),
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
    manifest = DatasetCommitManifest(
        files=_dataset_files_from_json(getattr(row, "manifest_files", None)),
        metadata=dict(getattr(row, "manifest_metadata", {}) or {}),
        nwb_metadata=dict(getattr(row, "manifest_nwb_metadata", {}) or {}),
        bids_metadata=dict(getattr(row, "manifest_bids_metadata", {}) or {}),
        note_ids=[_uuid(note_id) for note_id in getattr(row, "manifest_note_ids", []) or []],
        question_links=links,
        source_session_id=(
            _uuid(row.manifest_source_session_id)
            if getattr(row, "manifest_source_session_id", None)
            else None
        ),
    )
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
    manifest = dataset.commit_manifest
    row.project_id = _uuid_str(dataset.project_id)
    row.commit_hash = dataset.commit_hash
    row.primary_question_id = _uuid_str(dataset.primary_question_id)
    row.manifest_files = _dataset_files_to_json(manifest.files)
    row.manifest_metadata = dict(manifest.metadata)
    row.manifest_nwb_metadata = dict(manifest.nwb_metadata)
    row.manifest_bids_metadata = dict(manifest.bids_metadata)
    row.manifest_note_ids = [str(note_id) for note_id in manifest.note_ids]
    row.manifest_source_session_id = (
        _uuid_str(manifest.source_session_id) if manifest.source_session_id is not None else None
    )
    row.status = dataset.status.value
    row.created_by = dataset.created_by
    row.created_at = dataset.created_at
    row.updated_at = dataset.updated_at


def note_to_model(note: Note) -> NoteModel:
    return NoteModel(
        note_id=_uuid_str(note.note_id),
        project_id=_uuid_str(note.project_id),
        raw_content=note.raw_content,
        raw_storage_id=(
            _uuid_str(note.raw_asset.storage_id) if note.raw_asset is not None else None
        ),
        raw_filename=note.raw_asset.filename if note.raw_asset is not None else None,
        raw_content_type=note.raw_asset.content_type if note.raw_asset is not None else None,
        raw_size_bytes=note.raw_asset.size_bytes if note.raw_asset is not None else None,
        raw_checksum=note.raw_asset.checksum if note.raw_asset is not None else None,
        transcribed_text=note.transcribed_text,
        note_metadata=dict(note.metadata),
        status=note.status.value,
        created_by=note.created_by,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def note_from_model(
    row: NoteModel,
    *,
    targets: Iterable[EntityRef] = (),
) -> Note:
    raw_asset = None
    if row.raw_storage_id:
        raw_asset = NoteRawAsset(
            storage_id=_uuid(row.raw_storage_id),
            filename=row.raw_filename or "",
            content_type=row.raw_content_type or "",
            size_bytes=row.raw_size_bytes or 0,
            checksum=row.raw_checksum or "",
        )
    return Note(
        note_id=_uuid(row.note_id),
        project_id=_uuid(row.project_id),
        raw_content=row.raw_content,
        raw_asset=raw_asset,
        transcribed_text=row.transcribed_text,
        targets=list(targets),
        metadata=dict(getattr(row, "note_metadata", {}) or {}),
        status=NoteStatus(row.status),
        created_by=row.created_by,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


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
    row.raw_storage_id = (
        _uuid_str(note.raw_asset.storage_id) if note.raw_asset is not None else None
    )
    row.raw_filename = note.raw_asset.filename if note.raw_asset is not None else None
    row.raw_content_type = note.raw_asset.content_type if note.raw_asset is not None else None
    row.raw_size_bytes = note.raw_asset.size_bytes if note.raw_asset is not None else None
    row.raw_checksum = note.raw_asset.checksum if note.raw_asset is not None else None
    row.transcribed_text = note.transcribed_text
    row.note_metadata = dict(note.metadata)
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


def acquisition_output_to_model(output: AcquisitionOutput) -> AcquisitionOutputModel:
    return AcquisitionOutputModel(
        output_id=_uuid_str(output.output_id),
        session_id=_uuid_str(output.session_id),
        file_path=output.file_path,
        checksum=output.checksum,
        size_bytes=output.size_bytes,
        created_at=output.created_at,
        updated_at=output.updated_at,
    )


def acquisition_output_from_model(row: AcquisitionOutputModel) -> AcquisitionOutput:
    return AcquisitionOutput(
        output_id=_uuid(row.output_id),
        session_id=_uuid(row.session_id),
        file_path=row.file_path,
        checksum=row.checksum,
        size_bytes=row.size_bytes,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def apply_acquisition_output_to_model(
    row: AcquisitionOutputModel,
    output: AcquisitionOutput,
) -> None:
    row.session_id = _uuid_str(output.session_id)
    row.file_path = output.file_path
    row.checksum = output.checksum
    row.size_bytes = output.size_bytes
    row.created_at = output.created_at
    row.updated_at = output.updated_at


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
