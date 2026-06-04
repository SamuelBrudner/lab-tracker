"""Pydantic schemas for API requests and responses.

The API returns domain models (defined in :mod:`lab_tracker.models`) directly inside
Envelope/ListEnvelope wrappers. Request payloads use purpose-built schemas below.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

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
    GraphChangeOperationStatus,
    GraphChangeSetStatus,
    GraphDraftMode,
    Note,
    NoteMetadataScalar,
    NoteStatus,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
    Question,
    QuestionLink,
    QuestionRefactor,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    Visualization,
    VisualizationInput,
)

T = TypeVar("T")


def _non_blank_string(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


NonBlankStr = Annotated[str, Field(min_length=1), AfterValidator(_non_blank_string)]


def _unique_uuid_list(value: list[UUID] | None) -> list[UUID] | None:
    if value is None:
        return None
    if len(set(value)) != len(value):
        raise ValueError("Duplicate id in list.")
    return value


def _normalize_note_metadata_for_request(
    metadata: dict[str, NoteMetadataScalar] | None,
) -> dict[str, str] | None:
    if metadata is None:
        return None
    cleaned: dict[str, str] = {}
    for key, value in metadata.items():
        cleaned_key = str(key).strip()
        if not cleaned_key:
            raise ValueError("metadata key must not be empty")
        cleaned[cleaned_key] = value.strip() if isinstance(value, str) else str(value)
    return cleaned


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
    username: NonBlankStr
    password: NonBlankStr
    role: Role = Role.VIEWER
    bootstrap_token: NonBlankStr | None = None


class AuthLoginRequest(RequestModel):
    username: NonBlankStr
    password: NonBlankStr


class DeviceEnrollmentCreate(RequestModel):
    ttl_minutes: int | None = Field(default=None, ge=1, le=60)


class DeviceEnrollmentRead(BaseModel):
    enrollment_id: UUID
    offer_token: str
    expires_at: datetime
    enrollment_url: str
    enrollment_qr_svg: str


class DeviceConsumeRequest(RequestModel):
    offer_token: NonBlankStr
    label: Annotated[str, Field(min_length=1, max_length=150), AfterValidator(_non_blank_string)]


class DeviceTokenRead(BaseModel):
    device_token_id: UUID
    label: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class DeviceConsumeRead(BaseModel):
    device_token_id: UUID
    secret: str
    label: str
    created_at: datetime


class NoteRawDownloadRead(BaseModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str
    content_base64: str


class ProjectCreate(RequestModel):
    name: NonBlankStr
    description: str | None = None
    status: ProjectStatus | None = None


class ProjectUpdate(RequestModel):
    name: NonBlankStr | None = None
    description: str | None = None
    status: ProjectStatus | None = None


class ProjectMembershipCreate(RequestModel):
    user_id: UUID | None = None
    username: str | None = Field(default=None, min_length=1)
    role: ProjectMembershipRole


class ProjectMembershipUpdate(RequestModel):
    role: ProjectMembershipRole


ProjectMembershipRead = ProjectMembership


class QuestionCreate(RequestModel):
    project_id: UUID
    text: NonBlankStr
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    parent_question_ids: list[UUID] | None = None

    @field_validator("parent_question_ids")
    @classmethod
    def _parent_question_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class QuestionUpdate(RequestModel):
    text: NonBlankStr | None = None
    question_type: QuestionType | None = None
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    parent_question_ids: list[UUID] | None = None

    @field_validator("parent_question_ids")
    @classmethod
    def _parent_question_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class QuestionRefactorReplacement(RequestModel):
    text: NonBlankStr
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus
    parent_question_ids: list[UUID] | None = None

    @field_validator("parent_question_ids")
    @classmethod
    def _parent_question_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class QuestionRefactorRequest(RequestModel):
    replacement: QuestionRefactorReplacement
    reason: NonBlankStr
    child_question_ids_to_reparent: list[UUID] = Field(default_factory=list)
    note_ids_to_retarget: list[UUID] = Field(default_factory=list)

    @field_validator("child_question_ids_to_reparent", "note_ids_to_retarget")
    @classmethod
    def _target_ids_unique(cls, value: list[UUID]) -> list[UUID]:
        return _unique_uuid_list(value) or []


class QuestionRefactorResult(BaseModel):
    source_question: Question
    replacement_question: Question
    refactor: QuestionRefactor


class DatasetCreate(RequestModel):
    project_id: UUID
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    primary_question_id: UUID
    secondary_question_ids: list[UUID] | None = None
    status: DatasetStatus | None = None

    @field_validator("secondary_question_ids")
    @classmethod
    def _secondary_question_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class DatasetUpdate(RequestModel):
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    status: DatasetStatus | None = None
    question_links: list[QuestionLink] | None = None


class NoteCreate(RequestModel):
    project_id: UUID
    raw_content: NonBlankStr
    transcribed_text: str | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, NoteMetadataScalar] | None = None
    status: NoteStatus | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_normalized(
        cls,
        value: dict[str, NoteMetadataScalar] | None,
    ) -> dict[str, str] | None:
        return _normalize_note_metadata_for_request(value)


class NoteUpdate(RequestModel):
    transcribed_text: str | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, NoteMetadataScalar] | None = None
    status: NoteStatus | None = None

    @field_validator("metadata")
    @classmethod
    def _metadata_normalized(
        cls,
        value: dict[str, NoteMetadataScalar] | None,
    ) -> dict[str, str] | None:
        return _normalize_note_metadata_for_request(value)


class NoteTranscriptRequest(RequestModel):
    prompt: NonBlankStr | None = None


class GraphDraftOperationUpdate(RequestModel):
    payload: dict[str, Any] | None = None
    status: GraphChangeOperationStatus | None = None


class GraphDraftCreateRequest(RequestModel):
    mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT
    user_hint: NonBlankStr | None = None


class GraphDraftCommitRequest(RequestModel):
    message: NonBlankStr


class GraphDraftReviewRequest(RequestModel):
    status: GraphChangeSetStatus
    note: NonBlankStr | None = None


class GraphDraftListFilters(BaseModel):
    project_id: UUID | None = None
    status: GraphChangeSetStatus | None = None
    source_note_id: UUID | None = None


class AssistantDecisionContextRequest(RequestModel):
    task_kind: NonBlankStr
    query: NonBlankStr
    project_id: UUID | None = None
    question_id: UUID | None = None
    dataset_id: UUID | None = None
    analysis_id: UUID | None = None
    claim_id: UUID | None = None
    visualization_id: UUID | None = None
    limit: int = Field(default=20, ge=1, le=100)


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

    @field_validator("secondary_question_ids")
    @classmethod
    def _secondary_question_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class AcquisitionOutputCreate(RequestModel):
    file_path: NonBlankStr
    checksum: NonBlankStr
    size_bytes: int | None = Field(default=None, ge=0)


class AnalysisCreate(RequestModel):
    project_id: UUID
    dataset_ids: list[UUID] = Field(..., min_length=1)
    method_hash: NonBlankStr
    code_version: NonBlankStr
    environment_hash: str | None = None
    status: AnalysisStatus | None = None

    @field_validator("dataset_ids")
    @classmethod
    def _dataset_ids_unique(cls, value: list[UUID]) -> list[UUID]:
        return _unique_uuid_list(value) or []


class AnalysisUpdate(RequestModel):
    status: AnalysisStatus | None = None
    environment_hash: str | None = None


class ClaimCreate(RequestModel):
    project_id: UUID
    statement: NonBlankStr
    confidence: float = Field(..., ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None
    answers_question_ids: list[UUID] | None = None

    @field_validator(
        "supported_by_dataset_ids", "supported_by_analysis_ids", "answers_question_ids"
    )
    @classmethod
    def _claim_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class ClaimUpdate(RequestModel):
    statement: NonBlankStr | None = None
    confidence: float | None = Field(None, ge=0.0, le=100.0)
    status: ClaimStatus | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None
    answers_question_ids: list[UUID] | None = None

    @field_validator(
        "supported_by_dataset_ids", "supported_by_analysis_ids", "answers_question_ids"
    )
    @classmethod
    def _claim_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class VisualizationCreate(RequestModel):
    analysis_id: UUID
    viz_type: NonBlankStr
    file_path: NonBlankStr
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None

    @field_validator("related_claim_ids")
    @classmethod
    def _related_claim_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class VisualizationUpdate(RequestModel):
    viz_type: NonBlankStr | None = None
    file_path: NonBlankStr | None = None
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None

    @field_validator("related_claim_ids")
    @classmethod
    def _related_claim_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


ProjectGraphView = Literal["evidence", "questions", "full"]


class ProjectGraphNode(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    label: str
    detail: str | None = None
    status: str | None = None
    route: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectGraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str
    relationship: str


class ProjectGraphRead(BaseModel):
    project_id: UUID
    view: ProjectGraphView
    nodes: list[ProjectGraphNode]
    edges: list[ProjectGraphEdge]


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
