"""Pydantic schemas for API requests and responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from lab_tracker.models import (
    AnalysisStatus,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    NoteStatus,
    OutcomeStatus,
    ProjectStatus,
    QuestionLinkRole,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    TagSuggestionStatus,
)

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: dict[str, Any] | None = None


class PaginationMeta(BaseModel):
    limit: int = Field(..., ge=1)
    offset: int = Field(..., ge=0)
    total: int = Field(..., ge=0)


class ListEnvelope(BaseModel, Generic[T]):
    data: list[T]
    meta: PaginationMeta


class ErrorIssue(BaseModel):
    field: str | None = None
    message: str


class ErrorInfo(BaseModel):
    code: str
    message: str
    issues: list[ErrorIssue] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorInfo


class _BaseReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EntityRefRead(_BaseReadModel):
    entity_type: EntityType
    entity_id: UUID


class ExtractedEntityRead(_BaseReadModel):
    label: str
    confidence: float
    provenance: str


class EntityTagSuggestionRead(_BaseReadModel):
    suggestion_id: UUID
    entity_label: str
    vocabulary: str
    term_id: str
    term_label: str
    confidence: float
    provenance: str
    status: TagSuggestionStatus
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class QuestionLinkRead(_BaseReadModel):
    question_id: UUID
    role: QuestionLinkRole
    outcome_status: OutcomeStatus


class DatasetFileRead(_BaseReadModel):
    path: str
    checksum: str


class DatasetCommitManifestRead(_BaseReadModel):
    files: list[DatasetFileRead]
    metadata: dict[str, str]
    note_ids: list[UUID]
    extraction_provenance: list[str]
    question_links: list[QuestionLinkRead]
    source_session_id: UUID | None = None


class NoteRawAssetRead(_BaseReadModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str


class NoteRawDownloadRead(BaseModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str
    content_base64: str


class ProjectRead(_BaseReadModel):
    project_id: UUID
    name: str
    description: str
    status: ProjectStatus
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime


class QuestionRead(_BaseReadModel):
    question_id: UUID
    project_id: UUID
    text: str
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus
    parent_question_ids: list[UUID]
    created_from: QuestionSource
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime


class DatasetRead(_BaseReadModel):
    dataset_id: UUID
    project_id: UUID
    commit_hash: str
    primary_question_id: UUID
    question_links: list[QuestionLinkRead]
    commit_manifest: DatasetCommitManifestRead
    status: DatasetStatus
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime


class NoteRead(_BaseReadModel):
    note_id: UUID
    project_id: UUID
    raw_content: str
    raw_asset: NoteRawAssetRead | None = None
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntityRead]
    tag_suggestions: list[EntityTagSuggestionRead]
    targets: list[EntityRefRead]
    metadata: dict[str, str]
    status: NoteStatus
    created_at: datetime
    created_by: str | None = None
    updated_at: datetime


class SessionRead(_BaseReadModel):
    session_id: UUID
    project_id: UUID
    session_type: SessionType
    status: SessionStatus
    primary_question_id: UUID | None = None
    started_at: datetime
    ended_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime


class AnalysisRead(_BaseReadModel):
    analysis_id: UUID
    project_id: UUID
    dataset_ids: list[UUID]
    method_hash: str
    code_version: str
    environment_hash: str | None = None
    executed_by: str | None = None
    executed_at: datetime
    status: AnalysisStatus
    created_at: datetime
    updated_at: datetime


class ClaimRead(_BaseReadModel):
    claim_id: UUID
    project_id: UUID
    statement: str
    confidence: float
    status: ClaimStatus
    supported_by_dataset_ids: list[UUID]
    supported_by_analysis_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


class VisualizationRead(_BaseReadModel):
    viz_id: UUID
    analysis_id: UUID
    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


class EntityRefInput(BaseModel):
    entity_type: EntityType
    entity_id: UUID


class ExtractedEntityInput(BaseModel):
    label: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    provenance: str = Field(..., min_length=1)


class QuestionLinkInput(BaseModel):
    question_id: UUID
    role: QuestionLinkRole
    outcome_status: OutcomeStatus = OutcomeStatus.UNKNOWN


class DatasetFileInput(BaseModel):
    path: str = Field(..., min_length=1)
    checksum: str = Field(..., min_length=1)


class DatasetCommitManifestInput(BaseModel):
    files: list[DatasetFileInput] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    note_ids: list[UUID] = Field(default_factory=list)
    extraction_provenance: list[str] = Field(default_factory=list)
    source_session_id: UUID | None = None


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    status: ProjectStatus | None = None
    created_by: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None


class QuestionCreate(BaseModel):
    project_id: UUID
    text: str = Field(..., min_length=1)
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    parent_question_ids: list[UUID] | None = None
    created_from: QuestionSource | None = None
    created_by: str | None = None


class QuestionUpdate(BaseModel):
    text: str | None = None
    question_type: QuestionType | None = None
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    parent_question_ids: list[UUID] | None = None


class DatasetCreate(BaseModel):
    project_id: UUID
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    primary_question_id: UUID
    secondary_question_ids: list[UUID] | None = None
    status: DatasetStatus | None = None
    created_by: str | None = None


class DatasetUpdate(BaseModel):
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    status: DatasetStatus | None = None
    question_links: list[QuestionLinkInput] | None = None


class NoteCreate(BaseModel):
    project_id: UUID
    raw_content: str = Field(..., min_length=1)
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntityInput] | None = None
    targets: list[EntityRefInput] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None
    created_by: str | None = None


class NoteUpload(BaseModel):
    project_id: UUID
    filename: str = Field(..., min_length=1)
    content_type: str = Field(..., min_length=1)
    content_base64: str = Field(..., min_length=1)
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntityInput] | None = None
    targets: list[EntityRefInput] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None
    created_by: str | None = None


class NoteUpdate(BaseModel):
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntityInput] | None = None
    targets: list[EntityRefInput] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None


class SessionCreate(BaseModel):
    project_id: UUID
    session_type: SessionType
    primary_question_id: UUID | None = None
    status: SessionStatus | None = None
    created_by: str | None = None


class SessionUpdate(BaseModel):
    status: SessionStatus | None = None
    ended_at: datetime | None = None


class SessionPromotionRequest(BaseModel):
    primary_question_id: UUID
    secondary_question_ids: list[UUID] | None = None
    commit_manifest: DatasetCommitManifestInput | None = None
    status: DatasetStatus | None = None
    created_by: str | None = None


class AnalysisCreate(BaseModel):
    project_id: UUID
    dataset_ids: list[UUID] = Field(..., min_length=1)
    method_hash: str = Field(..., min_length=1)
    code_version: str = Field(..., min_length=1)
    environment_hash: str | None = None
    status: AnalysisStatus | None = None
    executed_by: str | None = None


class AnalysisUpdate(BaseModel):
    status: AnalysisStatus | None = None
    environment_hash: str | None = None


class ClaimCreate(BaseModel):
    project_id: UUID
    statement: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None


class ClaimUpdate(BaseModel):
    statement: str | None = Field(None, min_length=1)
    confidence: float | None = Field(None, ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None


class VisualizationCreate(BaseModel):
    analysis_id: UUID
    viz_type: str = Field(..., min_length=1)
    file_path: str = Field(..., min_length=1)
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None


class VisualizationUpdate(BaseModel):
    viz_type: str | None = Field(None, min_length=1)
    file_path: str | None = Field(None, min_length=1)
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None


class TagSuggestionRequest(BaseModel):
    provenance: str | None = None


class TagSuggestionReviewRequest(BaseModel):
    status: TagSuggestionStatus
    reviewed_by: str | None = None


class QuestionExtractionRequest(BaseModel):
    question_type: QuestionType | None = None
    created_from: QuestionSource | None = None
    provenance: str | None = None


class ClaimCommit(BaseModel):
    statement: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None


class VisualizationCommit(BaseModel):
    viz_type: str = Field(..., min_length=1)
    file_path: str = Field(..., min_length=1)
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None


class AnalysisCommitRequest(BaseModel):
    environment_hash: str | None = None
    claims: list[ClaimCommit] | None = None
    visualizations: list[VisualizationCommit] | None = None


class AnalysisCommitResult(BaseModel):
    analysis: AnalysisRead
    claims: list[ClaimRead]
    visualizations: list[VisualizationRead]
