"""Pydantic schemas for API requests and responses.

The API returns domain models (defined in :mod:`lab_tracker.models`) directly inside
Envelope/ListEnvelope wrappers. Request payloads use purpose-built schemas below.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    Note,
    NoteStatus,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    Visualization,
    VisualizationInput,
)

T = TypeVar("T")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class AuthRegisterRequest(RequestModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    role: Role = Role.VIEWER
    bootstrap_token: str | None = Field(default=None, min_length=1)


class AuthLoginRequest(RequestModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class NoteRawDownloadRead(BaseModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str
    content_base64: str


class ProjectCreate(RequestModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    status: ProjectStatus | None = None


class ProjectUpdate(RequestModel):
    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None


class QuestionCreate(RequestModel):
    project_id: UUID
    text: str = Field(..., min_length=1)
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    parent_question_ids: list[UUID] | None = None


class QuestionUpdate(RequestModel):
    text: str | None = None
    question_type: QuestionType | None = None
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    parent_question_ids: list[UUID] | None = None


class DatasetCreate(RequestModel):
    project_id: UUID
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    primary_question_id: UUID
    secondary_question_ids: list[UUID] | None = None
    status: DatasetStatus | None = None


class DatasetUpdate(RequestModel):
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    status: DatasetStatus | None = None
    question_links: list[QuestionLink] | None = None


class NoteCreate(RequestModel):
    project_id: UUID
    raw_content: str = Field(..., min_length=1)
    transcribed_text: str | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None


class NoteUpdate(RequestModel):
    transcribed_text: str | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, str] | None = None
    status: NoteStatus | None = None


class SessionCreate(RequestModel):
    project_id: UUID
    session_type: SessionType
    primary_question_id: UUID | None = None


class SessionUpdate(RequestModel):
    status: SessionStatus | None = None
    ended_at: datetime | None = None


class SessionPromotionRequest(RequestModel):
    """Promote an operational session into a scientific session by linking a primary question."""

    primary_question_id: UUID


class SessionDatasetPromotionRequest(RequestModel):
    primary_question_id: UUID
    secondary_question_ids: list[UUID] | None = None
    commit_manifest: DatasetCommitManifestInput | None = None
    status: DatasetStatus | None = None


class AcquisitionOutputCreate(RequestModel):
    file_path: str = Field(..., min_length=1)
    checksum: str = Field(..., min_length=1)
    size_bytes: int | None = Field(default=None, ge=0)


class AnalysisCreate(RequestModel):
    project_id: UUID
    dataset_ids: list[UUID] = Field(..., min_length=1)
    method_hash: str = Field(..., min_length=1)
    code_version: str = Field(..., min_length=1)
    environment_hash: str | None = None
    status: AnalysisStatus | None = None


class AnalysisUpdate(RequestModel):
    status: AnalysisStatus | None = None
    environment_hash: str | None = None


class ClaimCreate(RequestModel):
    project_id: UUID
    statement: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None


class ClaimUpdate(RequestModel):
    statement: str | None = Field(None, min_length=1)
    confidence: float | None = Field(None, ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None


class VisualizationCreate(RequestModel):
    analysis_id: UUID
    viz_type: str = Field(..., min_length=1)
    file_path: str = Field(..., min_length=1)
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None


class VisualizationUpdate(RequestModel):
    viz_type: str | None = Field(None, min_length=1)
    file_path: str | None = Field(None, min_length=1)
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None


class SearchResults(BaseModel):
    questions: list[Question] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)


class AnalysisCommitRequest(RequestModel):
    environment_hash: str | None = None
    claims: list[ClaimInput] | None = None
    visualizations: list[VisualizationInput] | None = None


class AnalysisCommitResult(BaseModel):
    analysis: Analysis
    claims: list[Claim]
    visualizations: list[Visualization]
