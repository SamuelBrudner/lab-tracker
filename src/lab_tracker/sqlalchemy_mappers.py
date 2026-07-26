"""Mapping helpers between domain dataclasses and SQLAlchemy models."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import UUID

from lab_tracker.db_models import (
    AcquisitionOutputModel,
    AnalysisDatasetModel,
    AnalysisModel,
    ClaimAnalysisModel,
    ClaimDatasetModel,
    ClaimEdgeModel,
    ClaimModel,
    ClaimQuestionModel,
    DatasetModel,
    DatasetQuestionLinkModel,
    DataStoreModel,
    EntityVersionModel,
    EvidenceBundleModel,
    ExplorationNodeEdgeModel,
    ExplorationNodeModel,
    GoalLinkModel,
    GoalModel,
    NoteModel,
    NoteTargetModel,
    ProjectMembershipModel,
    ProvenanceLinkModel,
    QuestionModel,
    QuestionParentModel,
    QuestionRefactorModel,
    SessionModel,
    VisualizationClaimModel,
    VisualizationModel,
)
from lab_tracker.models import (
    AcquisitionOutput,
    Analysis,
    Claim,
    ClaimEdge,
    Dataset,
    DatasetCommitManifest,
    DatasetFile,
    DataStore,
    EntityOrigin,
    EntityRef,
    EntityVersion,
    EvidenceBundleRecord,
    ExplorationNode,
    ExternalArtifactReference,
    Goal,
    GoalLink,
    Note,
    NoteArchiveReason,
    NoteRawAsset,
    ProjectMembership,
    ProvenanceLink,
    Question,
    QuestionLink,
    QuestionLinkRole,
    QuestionRefactor,
    Session,
    StoreCapability,
    Visualization,
    VisualizationAsset,
)
from lab_tracker.provenance_ingestion import external_artifacts_from_metadata
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    apply_project_group_to_model as apply_project_group_to_model,
)
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    apply_project_to_model as apply_project_to_model,
)
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    project_from_model as project_from_model,
)
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    project_group_from_model as project_group_from_model,
)
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    project_group_to_model as project_group_to_model,
)
from lab_tracker.sqlalchemy_mapper_parts.projects import (
    project_to_model as project_to_model,
)

_logger = logging.getLogger(__name__)


def _uuid(raw: str | UUID) -> UUID:
    # Tolerant during the per-entity GUID migration: a migrated column's row
    # attribute is already a UUID, while un-migrated ones are still str.
    return raw if isinstance(raw, UUID) else UUID(raw)


def _uuid_str(value: UUID) -> str:
    return str(value)


def _origin_domain_kwargs(row) -> dict[str, object]:
    return {
        "origin": EntityOrigin(getattr(row, "origin", None) or EntityOrigin.USER.value),
        "change_set_id": (
            row.change_set_id if getattr(row, "change_set_id", None) else None
        ),
        "origin_provider": getattr(row, "origin_provider", None),
        "origin_model": getattr(row, "origin_model", None),
        "origin_prompt_version": getattr(row, "origin_prompt_version", None),
    }


def _origin_model_kwargs(entity) -> dict[str, object]:
    return {
        "origin": entity.origin.value,
        "change_set_id": (
            entity.change_set_id if entity.change_set_id is not None else None
        ),
        "origin_provider": entity.origin_provider,
        "origin_model": entity.origin_model,
        "origin_prompt_version": entity.origin_prompt_version,
    }


def _apply_origin_to_model(row, entity) -> None:
    row.origin = entity.origin.value
    row.change_set_id = (
        entity.change_set_id if entity.change_set_id is not None else None
    )
    row.origin_provider = entity.origin_provider
    row.origin_model = entity.origin_model
    row.origin_prompt_version = entity.origin_prompt_version


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


def _external_artifacts_to_json(
    artifacts: Iterable[ExternalArtifactReference],
) -> list[dict[str, object]]:
    return [artifact.model_dump(mode="json", exclude_none=True) for artifact in artifacts]


def _external_artifacts_from_json(
    raw_artifacts: Iterable[object] | None,
) -> list[ExternalArtifactReference]:
    if not raw_artifacts:
        return []
    return [ExternalArtifactReference.model_validate(item) for item in raw_artifacts]


def _legacy_external_artifacts_from_metadata(
    metadata: dict[str, str],
) -> list[ExternalArtifactReference]:
    try:
        return external_artifacts_from_metadata(metadata)
    except ValueError as exc:
        _logger.warning(
            "Ignoring malformed legacy external_artifacts dataset metadata: %s",
            exc,
        )
        return []


def project_membership_to_model(membership: ProjectMembership) -> ProjectMembershipModel:
    return ProjectMembershipModel(
        membership_id=membership.membership_id,
        project_id=membership.project_id,
        user_id=membership.user_id,
        role=membership.role.value,
        created_by=membership.created_by,
        created_by_user_id=(
            membership.created_by_user_id
            if membership.created_by_user_id is not None
            else None
        ),
        created_at=membership.created_at,
        updated_at=membership.updated_at,
    )


def project_membership_from_model(row: ProjectMembershipModel) -> ProjectMembership:
    username = getattr(row, "username", None)
    user_global_role = getattr(row, "user_global_role", None)
    return ProjectMembership(
        membership_id=row.membership_id,
        project_id=row.project_id,
        user_id=row.user_id,
        role=row.role,
        username=username,
        user_global_role=user_global_role,
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_project_membership_to_model(
    row: ProjectMembershipModel,
    membership: ProjectMembership,
) -> None:
    row.project_id = membership.project_id
    row.user_id = membership.user_id
    row.role = membership.role.value
    row.created_by = membership.created_by
    row.created_by_user_id = (
        membership.created_by_user_id
        if membership.created_by_user_id is not None
        else None
    )
    row.created_at = membership.created_at
    row.updated_at = membership.updated_at


def question_to_model(question: Question) -> QuestionModel:
    return QuestionModel(
        question_id=question.question_id,
        project_id=question.project_id,
        text=question.text,
        question_type=question.question_type.value,
        hypothesis=question.hypothesis,
        status=question.status.value,
        client_capture_id=question.client_capture_id,
        terminal_reason=question.terminal_reason,
        superseded_by_question_id=(
            question.superseded_by_question_id
            if question.superseded_by_question_id is not None
            else None
        ),
        supersedes_question_id=(
            question.supersedes_question_id
            if question.supersedes_question_id is not None
            else None
        ),
        created_by=question.created_by,
        created_by_user_id=(
            question.created_by_user_id
            if question.created_by_user_id is not None
            else None
        ),
        **_origin_model_kwargs(question),
        created_at=question.created_at,
        updated_at=question.updated_at,
    )


def question_from_model(
    row: QuestionModel,
    *,
    parent_question_ids: Iterable[UUID] = (),
) -> Question:
    return Question(
        question_id=row.question_id,
        project_id=row.project_id,
        text=row.text,
        question_type=row.question_type,
        hypothesis=row.hypothesis,
        status=row.status,
        client_capture_id=getattr(row, "client_capture_id", None),
        terminal_reason=getattr(row, "terminal_reason", None),
        parent_question_ids=list(parent_question_ids),
        superseded_by_question_id=(
            row.superseded_by_question_id if row.superseded_by_question_id else None
        ),
        supersedes_question_id=(
            row.supersedes_question_id if row.supersedes_question_id else None
        ),
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def question_parent_models(question: Question) -> list[QuestionParentModel]:
    return [
        QuestionParentModel(
            question_id=question.question_id,
            parent_question_id=_uuid_str(parent_id),
        )
        for parent_id in question.parent_question_ids
    ]


def apply_question_to_model(row: QuestionModel, question: Question) -> None:
    row.project_id = question.project_id
    row.text = question.text
    row.question_type = question.question_type.value
    row.hypothesis = question.hypothesis
    row.status = question.status.value
    row.client_capture_id = question.client_capture_id
    row.terminal_reason = question.terminal_reason
    row.superseded_by_question_id = (
        question.superseded_by_question_id
        if question.superseded_by_question_id is not None
        else None
    )
    row.supersedes_question_id = (
        question.supersedes_question_id
        if question.supersedes_question_id is not None
        else None
    )
    row.created_by = question.created_by
    row.created_by_user_id = (
        question.created_by_user_id if question.created_by_user_id is not None else None
    )
    _apply_origin_to_model(row, question)
    row.created_at = question.created_at
    row.updated_at = question.updated_at


def question_refactor_to_model(refactor: QuestionRefactor) -> QuestionRefactorModel:
    return QuestionRefactorModel(
        refactor_id=refactor.refactor_id,
        project_id=refactor.project_id,
        source_question_id=refactor.source_question_id,
        replacement_question_id=refactor.replacement_question_id,
        reason=refactor.reason,
        source_snapshot=dict(refactor.source_snapshot),
        replacement_snapshot=dict(refactor.replacement_snapshot),
        relationship_changes=dict(refactor.relationship_changes),
        created_by=refactor.created_by,
        created_by_user_id=(
            refactor.created_by_user_id
            if refactor.created_by_user_id is not None
            else None
        ),
        created_at=refactor.created_at,
    )


def question_refactor_from_model(row: QuestionRefactorModel) -> QuestionRefactor:
    return QuestionRefactor(
        refactor_id=row.refactor_id,
        project_id=row.project_id,
        source_question_id=row.source_question_id,
        replacement_question_id=row.replacement_question_id,
        reason=row.reason,
        source_snapshot=dict(row.source_snapshot or {}),
        replacement_snapshot=dict(row.replacement_snapshot or {}),
        relationship_changes=dict(row.relationship_changes or {}),
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        created_at=row.created_at,
    )


def apply_question_refactor_to_model(
    row: QuestionRefactorModel,
    refactor: QuestionRefactor,
) -> None:
    row.project_id = refactor.project_id
    row.source_question_id = refactor.source_question_id
    row.replacement_question_id = refactor.replacement_question_id
    row.reason = refactor.reason
    row.source_snapshot = dict(refactor.source_snapshot)
    row.replacement_snapshot = dict(refactor.replacement_snapshot)
    row.relationship_changes = dict(refactor.relationship_changes)
    row.created_by = refactor.created_by
    row.created_by_user_id = (
        refactor.created_by_user_id if refactor.created_by_user_id is not None else None
    )
    row.created_at = refactor.created_at


def dataset_to_model(dataset: Dataset) -> DatasetModel:
    manifest = dataset.commit_manifest
    return DatasetModel(
        dataset_id=dataset.dataset_id,
        project_id=dataset.project_id,
        commit_hash=dataset.commit_hash,
        primary_question_id=dataset.primary_question_id,
        manifest_files=_dataset_files_to_json(manifest.files),
        manifest_external_artifacts=_external_artifacts_to_json(manifest.external_artifacts),
        manifest_metadata=dict(manifest.metadata),
        manifest_nwb_metadata=dict(manifest.nwb_metadata),
        manifest_bids_metadata=dict(manifest.bids_metadata),
        manifest_note_ids=[str(note_id) for note_id in manifest.note_ids],
        manifest_source_session_id=(
            manifest.source_session_id
            if manifest.source_session_id is not None
            else None
        ),
        status=dataset.status.value,
        terminal_reason=dataset.terminal_reason,
        created_by=dataset.created_by,
        created_by_user_id=(
            dataset.created_by_user_id
            if dataset.created_by_user_id is not None
            else None
        ),
        **_origin_model_kwargs(dataset),
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
                question_id=row.primary_question_id,
                role=QuestionLinkRole.PRIMARY,
            ),
        )
    metadata = dict(getattr(row, "manifest_metadata", {}) or {})
    external_artifacts = _external_artifacts_from_json(
        getattr(row, "manifest_external_artifacts", None)
    )
    if not external_artifacts:
        external_artifacts = _legacy_external_artifacts_from_metadata(metadata)
    manifest = DatasetCommitManifest(
        files=_dataset_files_from_json(getattr(row, "manifest_files", None)),
        external_artifacts=external_artifacts,
        metadata=metadata,
        nwb_metadata=dict(getattr(row, "manifest_nwb_metadata", {}) or {}),
        bids_metadata=dict(getattr(row, "manifest_bids_metadata", {}) or {}),
        note_ids=[_uuid(note_id) for note_id in getattr(row, "manifest_note_ids", []) or []],
        question_links=links,
        source_session_id=(
            row.manifest_source_session_id
            if getattr(row, "manifest_source_session_id", None)
            else None
        ),
    )
    return Dataset(
        dataset_id=row.dataset_id,
        project_id=row.project_id,
        commit_hash=row.commit_hash,
        primary_question_id=row.primary_question_id,
        question_links=links,
        commit_manifest=manifest,
        status=row.status,
        terminal_reason=getattr(row, "terminal_reason", None),
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def dataset_question_link_from_model(row: DatasetQuestionLinkModel) -> QuestionLink:
    return QuestionLink(
        question_id=row.question_id,
        role=row.role,
        outcome_status=row.outcome_status,
    )


def dataset_question_link_models(dataset: Dataset) -> list[DatasetQuestionLinkModel]:
    return [
        DatasetQuestionLinkModel(
            dataset_id=dataset.dataset_id,
            question_id=link.question_id,
            role=link.role.value,
            outcome_status=link.outcome_status.value,
        )
        for link in dataset.question_links
    ]


def apply_dataset_to_model(row: DatasetModel, dataset: Dataset) -> None:
    manifest = dataset.commit_manifest
    row.project_id = dataset.project_id
    row.commit_hash = dataset.commit_hash
    row.primary_question_id = dataset.primary_question_id
    row.manifest_files = _dataset_files_to_json(manifest.files)
    row.manifest_external_artifacts = _external_artifacts_to_json(manifest.external_artifacts)
    row.manifest_metadata = dict(manifest.metadata)
    row.manifest_nwb_metadata = dict(manifest.nwb_metadata)
    row.manifest_bids_metadata = dict(manifest.bids_metadata)
    row.manifest_note_ids = [str(note_id) for note_id in manifest.note_ids]
    row.manifest_source_session_id = (
        manifest.source_session_id if manifest.source_session_id is not None else None
    )
    row.status = dataset.status.value
    row.terminal_reason = dataset.terminal_reason
    row.created_by = dataset.created_by
    row.created_by_user_id = (
        dataset.created_by_user_id if dataset.created_by_user_id is not None else None
    )
    _apply_origin_to_model(row, dataset)
    row.created_at = dataset.created_at
    row.updated_at = dataset.updated_at


def note_to_model(note: Note) -> NoteModel:
    return NoteModel(
        note_id=note.note_id,
        project_id=note.project_id,
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
        client_capture_id=note.client_capture_id,
        status=note.status.value,
        archived_reason=note.archived_reason.value if note.archived_reason is not None else None,
        archived_at=note.archived_at,
        archived_by=note.archived_by,
        archived_by_user_id=(
            note.archived_by_user_id if note.archived_by_user_id is not None else None
        ),
        created_by=note.created_by,
        created_by_user_id=(
            note.created_by_user_id if note.created_by_user_id is not None else None
        ),
        **_origin_model_kwargs(note),
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
            storage_id=row.raw_storage_id,
            filename=row.raw_filename or "",
            content_type=row.raw_content_type or "",
            size_bytes=row.raw_size_bytes or 0,
            checksum=row.raw_checksum or "",
        )
    return Note(
        note_id=row.note_id,
        project_id=row.project_id,
        raw_content=row.raw_content,
        raw_asset=raw_asset,
        transcribed_text=row.transcribed_text,
        targets=list(targets),
        metadata=dict(getattr(row, "note_metadata", {}) or {}),
        client_capture_id=getattr(row, "client_capture_id", None),
        status=row.status,
        archived_reason=(
            NoteArchiveReason(row.archived_reason) if row.archived_reason else None
        ),
        archived_at=row.archived_at if row.archived_at else None,
        archived_by=getattr(row, "archived_by", None),
        archived_by_user_id=(
            row.archived_by_user_id if row.archived_by_user_id else None
        ),
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def entity_ref_from_model(row: NoteTargetModel) -> EntityRef:
    return EntityRef(
        entity_type=row.entity_type,
        entity_id=row.entity_id,
    )


def note_target_models(note: Note) -> list[NoteTargetModel]:
    return [
        NoteTargetModel(
            note_id=note.note_id,
            entity_type=target.entity_type.value,
            entity_id=target.entity_id,
        )
        for target in note.targets
    ]


def apply_note_to_model(row: NoteModel, note: Note) -> None:
    row.project_id = note.project_id
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
    row.client_capture_id = note.client_capture_id
    row.status = note.status.value
    row.archived_reason = note.archived_reason.value if note.archived_reason is not None else None
    row.archived_at = note.archived_at
    row.archived_by = note.archived_by
    row.archived_by_user_id = (
        note.archived_by_user_id if note.archived_by_user_id is not None else None
    )
    row.created_by = note.created_by
    row.created_by_user_id = (
        note.created_by_user_id if note.created_by_user_id is not None else None
    )
    _apply_origin_to_model(row, note)
    row.created_at = note.created_at
    row.updated_at = note.updated_at


def session_to_model(session: Session) -> SessionModel:
    return SessionModel(
        session_id=session.session_id,
        project_id=session.project_id,
        session_type=session.session_type,
        status=session.status,
        primary_question_id=session.primary_question_id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        created_by=session.created_by,
        created_by_user_id=session.created_by_user_id,
        **_origin_model_kwargs(session),
        updated_at=session.updated_at,
    )


def session_from_model(row: SessionModel) -> Session:
    return Session(
        session_id=row.session_id,
        project_id=row.project_id,
        session_type=row.session_type,
        status=row.status,
        primary_question_id=row.primary_question_id,
        started_at=row.started_at,
        ended_at=row.ended_at,
        created_by=row.created_by,
        created_by_user_id=row.created_by_user_id,
        **_origin_domain_kwargs(row),
        updated_at=row.updated_at,
    )


def apply_session_to_model(row: SessionModel, session: Session) -> None:
    row.project_id = session.project_id
    row.session_type = session.session_type
    row.status = session.status
    row.primary_question_id = session.primary_question_id
    row.started_at = session.started_at
    row.ended_at = session.ended_at
    row.created_by = session.created_by
    row.created_by_user_id = session.created_by_user_id
    _apply_origin_to_model(row, session)
    row.updated_at = session.updated_at


def acquisition_output_to_model(output: AcquisitionOutput) -> AcquisitionOutputModel:
    return AcquisitionOutputModel(
        output_id=output.output_id,
        session_id=output.session_id,
        file_path=output.file_path,
        checksum=output.checksum,
        size_bytes=output.size_bytes,
        created_at=output.created_at,
        updated_at=output.updated_at,
    )


def acquisition_output_from_model(row: AcquisitionOutputModel) -> AcquisitionOutput:
    return AcquisitionOutput(
        output_id=row.output_id,
        session_id=row.session_id,
        file_path=row.file_path,
        checksum=row.checksum,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_acquisition_output_to_model(
    row: AcquisitionOutputModel,
    output: AcquisitionOutput,
) -> None:
    row.session_id = output.session_id
    row.file_path = output.file_path
    row.checksum = output.checksum
    row.size_bytes = output.size_bytes
    row.created_at = output.created_at
    row.updated_at = output.updated_at


def analysis_to_model(analysis: Analysis) -> AnalysisModel:
    return AnalysisModel(
        analysis_id=analysis.analysis_id,
        project_id=analysis.project_id,
        method_hash=analysis.method_hash,
        code_version=analysis.code_version,
        environment_hash=analysis.environment_hash,
        external_artifacts=_external_artifacts_to_json(analysis.external_artifacts),
        executed_by=analysis.executed_by,
        executed_by_user_id=(
            analysis.executed_by_user_id
            if analysis.executed_by_user_id is not None
            else None
        ),
        executed_at=analysis.executed_at,
        status=analysis.status.value,
        terminal_reason=analysis.terminal_reason,
        **_origin_model_kwargs(analysis),
        created_at=analysis.created_at,
        updated_at=analysis.updated_at,
    )


def analysis_from_model(
    row: AnalysisModel,
    *,
    dataset_ids: Iterable[UUID] = (),
) -> Analysis:
    return Analysis(
        analysis_id=row.analysis_id,
        project_id=row.project_id,
        dataset_ids=list(dataset_ids),
        method_hash=row.method_hash,
        code_version=row.code_version,
        environment_hash=row.environment_hash,
        external_artifacts=_external_artifacts_from_json(getattr(row, "external_artifacts", None)),
        executed_by=row.executed_by,
        executed_by_user_id=(row.executed_by_user_id if row.executed_by_user_id else None),
        executed_at=row.executed_at,
        status=row.status,
        terminal_reason=getattr(row, "terminal_reason", None),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def analysis_dataset_models(analysis: Analysis) -> list[AnalysisDatasetModel]:
    return [
        AnalysisDatasetModel(
            analysis_id=analysis.analysis_id,
            dataset_id=_uuid_str(dataset_id),
        )
        for dataset_id in analysis.dataset_ids
    ]


def apply_analysis_to_model(row: AnalysisModel, analysis: Analysis) -> None:
    row.project_id = analysis.project_id
    row.method_hash = analysis.method_hash
    row.code_version = analysis.code_version
    row.environment_hash = analysis.environment_hash
    row.external_artifacts = _external_artifacts_to_json(analysis.external_artifacts)
    row.executed_by = analysis.executed_by
    row.executed_by_user_id = (
        analysis.executed_by_user_id
        if analysis.executed_by_user_id is not None
        else None
    )
    row.executed_at = analysis.executed_at
    row.status = analysis.status.value
    row.terminal_reason = analysis.terminal_reason
    _apply_origin_to_model(row, analysis)
    row.created_at = analysis.created_at
    row.updated_at = analysis.updated_at


def claim_to_model(claim: Claim) -> ClaimModel:
    return ClaimModel(
        claim_id=claim.claim_id,
        project_id=claim.project_id,
        statement=claim.statement,
        confidence=claim.confidence,
        status=claim.status.value,
        terminal_reason=claim.terminal_reason,
        falsification_criteria=claim.falsification_criteria,
        verification_plan=claim.verification_plan,
        refuting_outcome=claim.refuting_outcome,
        external_citations=_external_artifacts_to_json(claim.external_citations),
        created_by=claim.created_by,
        created_by_user_id=(
            claim.created_by_user_id if claim.created_by_user_id is not None else None
        ),
        **_origin_model_kwargs(claim),
        created_at=claim.created_at,
        updated_at=claim.updated_at,
    )


def claim_from_model(
    row: ClaimModel,
    *,
    supported_by_dataset_ids: Iterable[UUID] = (),
    supported_by_analysis_ids: Iterable[UUID] = (),
    answers_question_ids: Iterable[UUID] = (),
) -> Claim:
    return Claim(
        claim_id=row.claim_id,
        project_id=row.project_id,
        statement=row.statement,
        confidence=row.confidence,
        status=row.status,
        terminal_reason=getattr(row, "terminal_reason", None),
        falsification_criteria=getattr(row, "falsification_criteria", None),
        verification_plan=getattr(row, "verification_plan", None),
        refuting_outcome=getattr(row, "refuting_outcome", None),
        supported_by_dataset_ids=list(supported_by_dataset_ids),
        supported_by_analysis_ids=list(supported_by_analysis_ids),
        answers_question_ids=list(answers_question_ids),
        external_citations=_external_artifacts_from_json(getattr(row, "external_citations", None)),
        created_by=getattr(row, "created_by", None),
        created_by_user_id=(
            row.created_by_user_id if getattr(row, "created_by_user_id", None) else None
        ),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def claim_dataset_models(claim: Claim) -> list[ClaimDatasetModel]:
    return [
        ClaimDatasetModel(
            claim_id=claim.claim_id,
            dataset_id=_uuid_str(dataset_id),
        )
        for dataset_id in claim.supported_by_dataset_ids
    ]


def claim_analysis_models(claim: Claim) -> list[ClaimAnalysisModel]:
    return [
        ClaimAnalysisModel(
            claim_id=claim.claim_id,
            analysis_id=_uuid_str(analysis_id),
        )
        for analysis_id in claim.supported_by_analysis_ids
    ]


def claim_question_models(claim: Claim) -> list[ClaimQuestionModel]:
    return [
        ClaimQuestionModel(
            claim_id=claim.claim_id,
            question_id=_uuid_str(question_id),
        )
        for question_id in claim.answers_question_ids
    ]


def apply_claim_to_model(row: ClaimModel, claim: Claim) -> None:
    row.project_id = claim.project_id
    row.statement = claim.statement
    row.confidence = claim.confidence
    row.status = claim.status.value
    row.terminal_reason = claim.terminal_reason
    row.falsification_criteria = claim.falsification_criteria
    row.verification_plan = claim.verification_plan
    row.refuting_outcome = claim.refuting_outcome
    row.external_citations = _external_artifacts_to_json(claim.external_citations)
    row.created_by = claim.created_by
    row.created_by_user_id = (
        claim.created_by_user_id if claim.created_by_user_id is not None else None
    )
    _apply_origin_to_model(row, claim)
    row.created_at = claim.created_at
    row.updated_at = claim.updated_at


def claim_edge_to_model(edge: ClaimEdge) -> ClaimEdgeModel:
    return ClaimEdgeModel(
        edge_id=edge.edge_id,
        claim_id=edge.claim_id,
        target_claim_id=edge.target_claim_id,
        relation=edge.relation.value,
        created_by=edge.created_by,
        created_by_user_id=(
            edge.created_by_user_id if edge.created_by_user_id is not None else None
        ),
        created_at=edge.created_at,
    )


def claim_edge_from_model(row: ClaimEdgeModel) -> ClaimEdge:
    return ClaimEdge(
        edge_id=row.edge_id,
        claim_id=row.claim_id,
        target_claim_id=row.target_claim_id,
        relation=row.relation,
        created_by=getattr(row, "created_by", None),
        created_by_user_id=(
            row.created_by_user_id if getattr(row, "created_by_user_id", None) else None
        ),
        created_at=row.created_at,
    )


def apply_claim_edge_to_model(row: ClaimEdgeModel, edge: ClaimEdge) -> None:
    row.claim_id = edge.claim_id
    row.target_claim_id = edge.target_claim_id
    row.relation = edge.relation.value
    row.created_by = edge.created_by
    row.created_by_user_id = (
        edge.created_by_user_id if edge.created_by_user_id is not None else None
    )
    row.created_at = edge.created_at


def provenance_link_to_model(link: ProvenanceLink) -> ProvenanceLinkModel:
    return ProvenanceLinkModel(
        link_id=link.link_id,
        project_id=link.project_id,
        source_entity_type=link.source.entity_type.value,
        source_entity_id=_uuid_str(link.source.entity_id),
        target_entity_type=link.target.entity_type.value,
        target_entity_id=_uuid_str(link.target.entity_id),
        relation=link.relation.value,
        basis=link.basis.value,
        content_hash=link.content_hash,
        status=link.status.value,
        origin=link.origin.value,
        acceptance_mode=link.acceptance_mode.value if link.acceptance_mode is not None else None,
        accepted_by=link.accepted_by,
        accepted_by_user_id=(
            link.accepted_by_user_id if link.accepted_by_user_id is not None else None
        ),
        accepted_at=link.accepted_at,
        created_by=link.created_by,
        created_by_user_id=(
            link.created_by_user_id if link.created_by_user_id is not None else None
        ),
        created_at=link.created_at,
        updated_at=link.updated_at,
    )


def provenance_link_from_model(row: ProvenanceLinkModel) -> ProvenanceLink:
    return ProvenanceLink(
        link_id=row.link_id,
        project_id=row.project_id,
        source=EntityRef(
            entity_type=row.source_entity_type,
            entity_id=row.source_entity_id,
        ),
        target=EntityRef(
            entity_type=row.target_entity_type,
            entity_id=row.target_entity_id,
        ),
        relation=row.relation,
        basis=row.basis,
        content_hash=row.content_hash,
        status=row.status,
        origin=row.origin,
        acceptance_mode=(
            row.acceptance_mode if row.acceptance_mode is not None else None
        ),
        accepted_by=row.accepted_by,
        accepted_by_user_id=(
            row.accepted_by_user_id if row.accepted_by_user_id else None
        ),
        accepted_at=row.accepted_at,
        created_by=row.created_by,
        created_by_user_id=(
            row.created_by_user_id if row.created_by_user_id else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_provenance_link_to_model(row: ProvenanceLinkModel, link: ProvenanceLink) -> None:
    row.project_id = link.project_id
    row.source_entity_type = link.source.entity_type.value
    row.source_entity_id = _uuid_str(link.source.entity_id)
    row.target_entity_type = link.target.entity_type.value
    row.target_entity_id = _uuid_str(link.target.entity_id)
    row.relation = link.relation.value
    row.basis = link.basis.value
    row.content_hash = link.content_hash
    row.status = link.status.value
    row.origin = link.origin.value
    row.acceptance_mode = link.acceptance_mode.value if link.acceptance_mode is not None else None
    row.accepted_by = link.accepted_by
    row.accepted_by_user_id = (
        link.accepted_by_user_id if link.accepted_by_user_id is not None else None
    )
    row.accepted_at = link.accepted_at
    row.created_by = link.created_by
    row.created_by_user_id = (
        link.created_by_user_id if link.created_by_user_id is not None else None
    )
    row.updated_at = link.updated_at


def _entity_refs_to_json(refs: Iterable[EntityRef]) -> list[dict[str, str]]:
    return [ref.model_dump(mode="json") for ref in refs]


def _entity_refs_from_json(raw_refs: Iterable[object] | None) -> list[EntityRef]:
    if not raw_refs:
        return []
    return [EntityRef.model_validate(ref) for ref in raw_refs]


def exploration_node_to_model(node: ExplorationNode) -> ExplorationNodeModel:
    return ExplorationNodeModel(
        node_id=node.node_id,
        project_id=node.project_id,
        node_type=node.node_type.value,
        title=node.title,
        target_entity_type=node.target.entity_type.value,
        target_entity_id=_uuid_str(node.target.entity_id),
        status=node.status.value,
        choice=node.choice,
        alternatives_considered=list(node.alternatives_considered),
        rationale=node.rationale,
        evidence_refs=_entity_refs_to_json(node.evidence_refs),
        hypothesis=node.hypothesis,
        failure_mode=node.failure_mode,
        lesson=node.lesson,
        tooling_context=node.tooling_context,
        trigger=node.trigger,
        invalidates_node_id=(
            node.invalidates_node_id
            if node.invalidates_node_id is not None
            else None
        ),
        invalidates_claim_id=(
            node.invalidates_claim_id
            if node.invalidates_claim_id is not None
            else None
        ),
        created_by=node.created_by,
        created_by_user_id=(
            node.created_by_user_id
            if node.created_by_user_id is not None
            else None
        ),
        **_origin_model_kwargs(node),
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def exploration_node_from_model(
    row: ExplorationNodeModel,
    *,
    parent_node_ids: Iterable[UUID] = (),
    also_depends_on_node_ids: Iterable[UUID] = (),
) -> ExplorationNode:
    return ExplorationNode(
        node_id=row.node_id,
        project_id=row.project_id,
        node_type=row.node_type,
        title=row.title,
        target=EntityRef(
            entity_type=row.target_entity_type,
            entity_id=row.target_entity_id,
        ),
        status=row.status,
        choice=getattr(row, "choice", None),
        alternatives_considered=list(getattr(row, "alternatives_considered", None) or []),
        rationale=getattr(row, "rationale", None),
        evidence_refs=_entity_refs_from_json(getattr(row, "evidence_refs", None)),
        hypothesis=getattr(row, "hypothesis", None),
        failure_mode=getattr(row, "failure_mode", None),
        lesson=getattr(row, "lesson", None),
        tooling_context=getattr(row, "tooling_context", None),
        trigger=getattr(row, "trigger", None),
        invalidates_node_id=(
            row.invalidates_node_id
            if getattr(row, "invalidates_node_id", None)
            else None
        ),
        invalidates_claim_id=(
            row.invalidates_claim_id
            if getattr(row, "invalidates_claim_id", None)
            else None
        ),
        parent_node_ids=list(parent_node_ids),
        also_depends_on_node_ids=list(also_depends_on_node_ids),
        created_by=getattr(row, "created_by", None),
        created_by_user_id=(
            row.created_by_user_id
            if getattr(row, "created_by_user_id", None)
            else None
        ),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_exploration_node_to_model(
    row: ExplorationNodeModel,
    node: ExplorationNode,
) -> None:
    row.project_id = node.project_id
    row.node_type = node.node_type.value
    row.title = node.title
    row.target_entity_type = node.target.entity_type.value
    row.target_entity_id = _uuid_str(node.target.entity_id)
    row.status = node.status.value
    row.choice = node.choice
    row.alternatives_considered = list(node.alternatives_considered)
    row.rationale = node.rationale
    row.evidence_refs = _entity_refs_to_json(node.evidence_refs)
    row.hypothesis = node.hypothesis
    row.failure_mode = node.failure_mode
    row.lesson = node.lesson
    row.tooling_context = node.tooling_context
    row.trigger = node.trigger
    row.invalidates_node_id = (
        node.invalidates_node_id if node.invalidates_node_id is not None else None
    )
    row.invalidates_claim_id = (
        node.invalidates_claim_id if node.invalidates_claim_id is not None else None
    )
    row.created_by = node.created_by
    row.created_by_user_id = (
        node.created_by_user_id if node.created_by_user_id is not None else None
    )
    _apply_origin_to_model(row, node)
    row.created_at = node.created_at
    row.updated_at = node.updated_at


def exploration_node_edge_models(node: ExplorationNode) -> list[ExplorationNodeEdgeModel]:
    rows: list[ExplorationNodeEdgeModel] = []
    for parent_id in node.parent_node_ids:
        rows.append(
            ExplorationNodeEdgeModel(
                source_node_id=_uuid_str(parent_id),
                target_node_id=node.node_id,
                relation="parent",
            )
        )
    for dependency_id in node.also_depends_on_node_ids:
        rows.append(
            ExplorationNodeEdgeModel(
                source_node_id=_uuid_str(dependency_id),
                target_node_id=node.node_id,
                relation="also_depends_on",
            )
        )
    return rows


def entity_version_to_model(version: EntityVersion) -> EntityVersionModel:
    return EntityVersionModel(
        version_id=version.version_id,
        entity_type=version.entity_type.value,
        entity_id=version.entity_id,
        version_number=version.version_number,
        snapshot=dict(version.snapshot),
        change_set_id=(
            version.change_set_id if version.change_set_id is not None else None
        ),
        committed_at=version.committed_at,
        created_at=version.created_at,
        created_by=version.created_by,
        created_by_user_id=(
            version.created_by_user_id
            if version.created_by_user_id is not None
            else None
        ),
    )


def entity_version_from_model(row: EntityVersionModel) -> EntityVersion:
    return EntityVersion(
        version_id=row.version_id,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        version_number=row.version_number,
        snapshot=dict(row.snapshot or {}),
        change_set_id=row.change_set_id if row.change_set_id else None,
        committed_at=row.committed_at,
        created_at=row.created_at,
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
    )


def apply_entity_version_to_model(
    row: EntityVersionModel,
    version: EntityVersion,
) -> None:
    row.entity_type = version.entity_type.value
    row.entity_id = version.entity_id
    row.version_number = version.version_number
    row.snapshot = dict(version.snapshot)
    row.change_set_id = (
        version.change_set_id if version.change_set_id is not None else None
    )
    row.committed_at = version.committed_at
    row.created_at = version.created_at
    row.created_by = version.created_by
    row.created_by_user_id = (
        version.created_by_user_id if version.created_by_user_id is not None else None
    )


def data_store_to_model(store: DataStore) -> DataStoreModel:
    return DataStoreModel(
        store_id=store.store_id,
        project_id=store.project_id if store.project_id is not None else None,
        group_id=store.group_id if store.group_id is not None else None,
        name=store.name,
        kind=store.kind.value,
        capabilities=[capability.value for capability in store.capabilities],
        root=store.root,
        endpoint=store.endpoint,
        credential_ref=store.credential_ref,
        is_default=store.is_default,
        created_by=store.created_by,
        created_by_user_id=(
            store.created_by_user_id
            if store.created_by_user_id is not None
            else None
        ),
        created_at=store.created_at,
        updated_at=store.updated_at,
    )


def data_store_from_model(row: DataStoreModel) -> DataStore:
    return DataStore(
        store_id=row.store_id,
        project_id=row.project_id if row.project_id else None,
        group_id=row.group_id if row.group_id else None,
        name=row.name,
        kind=row.kind,
        capabilities=[StoreCapability(value) for value in (row.capabilities or [])],
        root=row.root,
        endpoint=row.endpoint,
        credential_ref=row.credential_ref,
        is_default=bool(row.is_default),
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_data_store_to_model(row: DataStoreModel, store: DataStore) -> None:
    row.project_id = store.project_id if store.project_id is not None else None
    row.group_id = store.group_id if store.group_id is not None else None
    row.name = store.name
    row.kind = store.kind.value
    row.capabilities = [capability.value for capability in store.capabilities]
    row.root = store.root
    row.endpoint = store.endpoint
    row.credential_ref = store.credential_ref
    row.is_default = store.is_default
    row.created_by = store.created_by
    row.created_by_user_id = (
        store.created_by_user_id if store.created_by_user_id is not None else None
    )
    row.created_at = store.created_at
    row.updated_at = store.updated_at


def evidence_bundle_record_to_model(record: EvidenceBundleRecord) -> EvidenceBundleModel:
    return EvidenceBundleModel(
        bundle_id=record.bundle_id,
        project_id=record.project_id,
        created_by=record.created_by,
        idempotency_key=record.idempotency_key,
        request_fingerprint=record.request_fingerprint,
        result=dict(record.result),
        created_at=record.created_at,
    )


def evidence_bundle_record_from_model(row: EvidenceBundleModel) -> EvidenceBundleRecord:
    return EvidenceBundleRecord(
        bundle_id=row.bundle_id,
        project_id=row.project_id,
        created_by=row.created_by,
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        result=dict(row.result or {}),
        created_at=_as_utc(row.created_at),
    )


def goal_to_model(goal: Goal) -> GoalModel:
    return GoalModel(
        goal_id=goal.goal_id,
        project_id=goal.project_id if goal.project_id is not None else None,
        goal_type=goal.goal_type.value,
        title=goal.title,
        summary=goal.summary,
        status=goal.status.value,
        target_date=goal.target_date,
        external_ref=goal.external_ref,
        attributes=dict(goal.attributes),
        created_by=goal.created_by,
        created_by_user_id=(
            goal.created_by_user_id if goal.created_by_user_id is not None else None
        ),
        **_origin_model_kwargs(goal),
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )


def goal_from_model(
    row: GoalModel,
    *,
    links: Iterable[GoalLink] = (),
) -> Goal:
    return Goal(
        goal_id=row.goal_id,
        project_id=row.project_id if row.project_id is not None else None,
        goal_type=row.goal_type,
        title=row.title,
        summary=row.summary or "",
        status=row.status,
        target_date=row.target_date,
        external_ref=row.external_ref,
        attributes=dict(row.attributes or {}),
        links=list(links),
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def apply_goal_to_model(row: GoalModel, goal: Goal) -> None:
    row.project_id = goal.project_id if goal.project_id is not None else None
    row.goal_type = goal.goal_type.value
    row.title = goal.title
    row.summary = goal.summary
    row.status = goal.status.value
    row.target_date = goal.target_date
    row.external_ref = goal.external_ref
    row.attributes = dict(goal.attributes)
    row.created_by = goal.created_by
    row.created_by_user_id = (
        goal.created_by_user_id if goal.created_by_user_id is not None else None
    )
    _apply_origin_to_model(row, goal)
    row.created_at = goal.created_at
    row.updated_at = goal.updated_at


def goal_link_from_model(row: GoalLinkModel) -> GoalLink:
    return GoalLink(
        link_id=row.link_id,
        goal_id=row.goal_id,
        target=EntityRef(
            entity_type=row.entity_type,
            entity_id=row.entity_id,
        ),
        relation=row.relation,
        link_status=row.link_status,
        slot=row.slot or None,
        created_by=row.created_by,
        created_by_user_id=(row.created_by_user_id if row.created_by_user_id else None),
        created_at=row.created_at,
    )


def goal_link_to_model(link: GoalLink) -> GoalLinkModel:
    return GoalLinkModel(
        link_id=link.link_id,
        goal_id=link.goal_id,
        entity_type=link.target.entity_type.value,
        entity_id=_uuid_str(link.target.entity_id),
        relation=link.relation.value,
        link_status=link.link_status.value,
        slot=link.slot or "",
        created_by=link.created_by,
        created_by_user_id=(
            link.created_by_user_id if link.created_by_user_id is not None else None
        ),
        created_at=link.created_at,
    )


def goal_link_models(goal: Goal) -> list[GoalLinkModel]:
    return [goal_link_to_model(link) for link in goal.links]


def visualization_to_model(visualization: Visualization) -> VisualizationModel:
    return VisualizationModel(
        viz_id=visualization.viz_id,
        analysis_id=visualization.analysis_id,
        viz_type=visualization.viz_type,
        file_path=visualization.file_path,
        caption=visualization.caption,
        asset_storage_id=(
            _uuid_str(visualization.asset.storage_id) if visualization.asset is not None else None
        ),
        asset_filename=visualization.asset.filename if visualization.asset is not None else None,
        asset_content_type=(
            visualization.asset.content_type if visualization.asset is not None else None
        ),
        asset_size_bytes=(
            visualization.asset.size_bytes if visualization.asset is not None else None
        ),
        asset_checksum=visualization.asset.checksum if visualization.asset is not None else None,
        created_by=visualization.created_by,
        created_by_user_id=(
            visualization.created_by_user_id
            if visualization.created_by_user_id is not None
            else None
        ),
        **_origin_model_kwargs(visualization),
        created_at=visualization.created_at,
        updated_at=visualization.updated_at,
    )


def visualization_from_model(
    row: VisualizationModel,
    *,
    dataset_ids: Iterable[UUID] = (),
    related_claim_ids: Iterable[UUID] = (),
) -> Visualization:
    asset = None
    if row.asset_storage_id:
        asset = VisualizationAsset(
            storage_id=row.asset_storage_id,
            filename=row.asset_filename or "",
            content_type=row.asset_content_type or "",
            size_bytes=row.asset_size_bytes or 0,
            checksum=row.asset_checksum or "",
        )
    return Visualization(
        viz_id=row.viz_id,
        analysis_id=row.analysis_id,
        dataset_ids=list(dataset_ids),
        viz_type=row.viz_type,
        file_path=row.file_path,
        caption=row.caption,
        related_claim_ids=list(related_claim_ids),
        asset=asset,
        created_by=getattr(row, "created_by", None),
        created_by_user_id=(
            row.created_by_user_id if getattr(row, "created_by_user_id", None) else None
        ),
        **_origin_domain_kwargs(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def visualization_claim_models(visualization: Visualization) -> list[VisualizationClaimModel]:
    return [
        VisualizationClaimModel(
            viz_id=visualization.viz_id,
            claim_id=_uuid_str(claim_id),
        )
        for claim_id in visualization.related_claim_ids
    ]


def apply_visualization_to_model(row: VisualizationModel, visualization: Visualization) -> None:
    row.analysis_id = visualization.analysis_id
    row.viz_type = visualization.viz_type
    row.file_path = visualization.file_path
    row.caption = visualization.caption
    row.asset_storage_id = (
        _uuid_str(visualization.asset.storage_id) if visualization.asset is not None else None
    )
    row.asset_filename = visualization.asset.filename if visualization.asset is not None else None
    row.asset_content_type = (
        visualization.asset.content_type if visualization.asset is not None else None
    )
    row.asset_size_bytes = (
        visualization.asset.size_bytes if visualization.asset is not None else None
    )
    row.asset_checksum = visualization.asset.checksum if visualization.asset is not None else None
    row.created_by = visualization.created_by
    row.created_by_user_id = (
        visualization.created_by_user_id
        if visualization.created_by_user_id is not None
        else None
    )
    _apply_origin_to_model(row, visualization)
    row.created_at = visualization.created_at
    row.updated_at = visualization.updated_at
