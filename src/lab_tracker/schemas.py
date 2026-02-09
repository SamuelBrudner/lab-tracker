"""Pydantic schemas for API requests and responses.

The API returns domain models (defined in :mod:`lab_tracker.models`) directly inside
Envelope/ListEnvelope wrappers. Request payloads use purpose-built schemas below.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from lab_tracker.auth import Role
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimInput,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetStatus,
    EntityRef,
    ExtractedEntity,
    Note,
    NoteStatus,
    ProjectStatus,
    ProjectReviewPolicy,
    Question,
    QuestionLink,
    QuestionSource,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    TagSuggestionStatus,
    Visualization,
    VisualizationInput,
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


class AuthUserRead(BaseModel):
    user_id: UUID
    username: str
    role: Role
    created_at: datetime


class AuthTokenRead(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AuthUserRead


class AuthRegisterRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    role: Role = Role.VIEWER
    bootstrap_token: str | None = Field(default=None, min_length=1)


class AuthLoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class NoteRawDownloadRead(BaseModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str
    content_base64: str


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    status: ProjectStatus | None = None
    review_policy: ProjectReviewPolicy | None = None
    created_by: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    review_policy: ProjectReviewPolicy | None = None


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
    question_links: list[QuestionLink] | None = None


class DatasetReviewRequest(BaseModel):
    comments: str | None = None


class DatasetReviewAction(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class DatasetReviewUpdate(BaseModel):
    action: DatasetReviewAction
    comments: str | None = None

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned = value.strip().lower().replace("-", "_")
        mapping = {
            "approved": DatasetReviewAction.APPROVE.value,
            "changes_requested": DatasetReviewAction.REQUEST_CHANGES.value,
            "rejected": DatasetReviewAction.REJECT.value,
        }
        return mapping.get(cleaned, cleaned)


class NoteCreate(BaseModel):
    project_id: UUID
    raw_content: str = Field(..., min_length=1)
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntity] | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None
    created_by: str | None = None


class NoteUpload(BaseModel):
    project_id: UUID
    filename: str = Field(..., min_length=1)
    content_type: str = Field(..., min_length=1)
    content_base64: str = Field(..., min_length=1)
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntity] | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None
    created_by: str | None = None


class NoteUpdate(BaseModel):
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntity] | None = None
    targets: list[EntityRef] | None = None
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


class AcquisitionOutputCreate(BaseModel):
    file_path: str = Field(..., min_length=1)
    checksum: str = Field(..., min_length=1)
    size_bytes: int | None = Field(default=None, ge=0)


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


class SearchResults(BaseModel):
    questions: list[Question] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)


class AnalysisCommitRequest(BaseModel):
    environment_hash: str | None = None
    claims: list[ClaimInput] | None = None
    visualizations: list[VisualizationInput] | None = None


class AnalysisCommitResult(BaseModel):
    analysis: Analysis
    claims: list[Claim]
    visualizations: list[Visualization]
