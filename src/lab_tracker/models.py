"""Core domain models for lab tracker.

The project intentionally keeps a single "domain model" representation that is used
throughout the in-memory API layer and for API responses (Pydantic models). SQLAlchemy
models remain the persistence representation.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from enum import Enum
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_link_code(link_code: str) -> str:
    cleaned = re.sub(r"[\s-]+", "", link_code or "")
    return cleaned.upper()


def encode_session_link_code(session_id: UUID) -> str:
    return base64.b32encode(session_id.bytes).decode("ascii").rstrip("=")


def decode_session_link_code(link_code: str) -> UUID:
    normalized = normalize_link_code(link_code)
    if not normalized:
        raise ValueError("link_code must not be empty.")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        decoded = base64.b32decode(normalized + padding, casefold=True)
    except binascii.Error as exc:
        raise ValueError("Invalid link_code characters.") from exc
    if len(decoded) != 16:
        raise ValueError("Invalid link_code length.")
    return UUID(bytes=decoded)


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectReviewPolicy(str, Enum):
    NONE = "none"
    ALL = "all"


class QuestionStatus(str, Enum):
    STAGED = "staged"
    ACTIVE = "active"
    ANSWERED = "answered"
    ABANDONED = "abandoned"


class QuestionType(str, Enum):
    DESCRIPTIVE = "descriptive"
    HYPOTHESIS_DRIVEN = "hypothesis_driven"
    METHOD_DEV = "method_dev"
    OTHER = "other"


class QuestionSource(str, Enum):
    MANUAL = "manual"
    MEETING_CAPTURE = "meeting_capture"
    IMPORTED = "imported"
    API = "api"


class DatasetStatus(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    ARCHIVED = "archived"


class DatasetReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class NoteStatus(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    ARCHIVED = "archived"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class SessionType(str, Enum):
    SCIENTIFIC = "scientific"
    OPERATIONAL = "operational"


class AnalysisStatus(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    ARCHIVED = "archived"


class ClaimStatus(str, Enum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    REJECTED = "rejected"


class QuestionLinkRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class OutcomeStatus(str, Enum):
    UNKNOWN = "unknown"
    SUPPORTS = "supports"
    REFUTES = "refutes"
    INCONCLUSIVE = "inconclusive"


class TagSuggestionStatus(str, Enum):
    STAGED = "staged"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EntityType(str, Enum):
    PROJECT = "project"
    QUESTION = "question"
    DATASET = "dataset"
    NOTE = "note"
    SESSION = "session"
    ANALYSIS = "analysis"
    CLAIM = "claim"
    VISUALIZATION = "visualization"


class _DomainModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EntityRef(_DomainModel):
    entity_type: EntityType
    entity_id: UUID


class ExtractedEntity(_DomainModel):
    label: str
    confidence: float
    provenance: str


class EntityTagSuggestion(_DomainModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    suggestion_id: UUID
    entity_label: str
    vocabulary: str
    term_id: str
    term_label: str
    confidence: float
    provenance: str
    status: TagSuggestionStatus = TagSuggestionStatus.STAGED
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class QuestionLink(_DomainModel):
    question_id: UUID
    role: QuestionLinkRole
    outcome_status: OutcomeStatus = OutcomeStatus.UNKNOWN


class DatasetFile(_DomainModel):
    file_id: UUID | None = None
    path: str
    checksum: str
    size_bytes: int | None = None


class DatasetCommitManifestInput(_DomainModel):
    files: list[DatasetFile] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    nwb_metadata: dict[str, str] = Field(default_factory=dict)
    bids_metadata: dict[str, str] = Field(default_factory=dict)
    note_ids: list[UUID] = Field(default_factory=list)
    extraction_provenance: list[str] = Field(default_factory=list)
    source_session_id: UUID | None = None


class DatasetCommitManifest(_DomainModel):
    files: list[DatasetFile] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    nwb_metadata: dict[str, str] = Field(default_factory=dict)
    bids_metadata: dict[str, str] = Field(default_factory=dict)
    note_ids: list[UUID] = Field(default_factory=list)
    extraction_provenance: list[str] = Field(default_factory=list)
    question_links: list[QuestionLink] = Field(default_factory=list)
    source_session_id: UUID | None = None


class NoteRawAsset(_DomainModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str


class Project(_DomainModel):
    project_id: UUID
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    review_policy: ProjectReviewPolicy = ProjectReviewPolicy.NONE
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class Question(_DomainModel):
    question_id: UUID
    project_id: UUID
    text: str
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus = QuestionStatus.STAGED
    parent_question_ids: list[UUID] = Field(default_factory=list)
    created_from: QuestionSource = QuestionSource.MANUAL
    source_provenance: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class QuestionExtractionCandidate(_DomainModel):
    """Candidate question extracted from a note for human review."""

    text: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    suggested_question_type: QuestionType = QuestionType.OTHER
    provenance: str | None = None


class Dataset(_DomainModel):
    dataset_id: UUID
    project_id: UUID
    commit_hash: str
    primary_question_id: UUID
    question_links: list[QuestionLink]
    commit_manifest: DatasetCommitManifest
    status: DatasetStatus = DatasetStatus.STAGED
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class DatasetReview(_DomainModel):
    review_id: UUID
    dataset_id: UUID
    reviewer_user_id: UUID | None = None
    status: DatasetReviewStatus = DatasetReviewStatus.PENDING
    comments: str | None = None
    requested_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class Note(_DomainModel):
    note_id: UUID
    project_id: UUID
    raw_content: str
    raw_asset: NoteRawAsset | None = None
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntity] = Field(default_factory=list)
    tag_suggestions: list[EntityTagSuggestion] = Field(default_factory=list)
    targets: list[EntityRef] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    status: NoteStatus = NoteStatus.STAGED
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class Session(_DomainModel):
    session_id: UUID
    project_id: UUID
    session_type: SessionType
    status: SessionStatus = SessionStatus.ACTIVE
    primary_question_id: UUID | None = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @computed_field(return_type=str)
    @property
    def link_code(self) -> str:
        return encode_session_link_code(self.session_id)


class AcquisitionOutput(_DomainModel):
    output_id: UUID
    session_id: UUID
    file_path: str
    checksum: str
    size_bytes: int | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Analysis(_DomainModel):
    analysis_id: UUID
    project_id: UUID
    dataset_ids: list[UUID]
    method_hash: str
    code_version: str
    environment_hash: str | None = None
    executed_by: str | None = None
    executed_at: datetime = Field(default_factory=utc_now)
    status: AnalysisStatus = AnalysisStatus.STAGED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ClaimInput(_DomainModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    statement: str
    confidence: float
    status: ClaimStatus = ClaimStatus.PROPOSED
    supported_by_dataset_ids: list[UUID] = Field(default_factory=list)
    supported_by_analysis_ids: list[UUID] = Field(default_factory=list)


class Claim(_DomainModel):
    claim_id: UUID
    project_id: UUID
    statement: str
    confidence: float
    status: ClaimStatus = ClaimStatus.PROPOSED
    supported_by_dataset_ids: list[UUID] = Field(default_factory=list)
    supported_by_analysis_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class VisualizationInput(_DomainModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID] = Field(default_factory=list)


class Visualization(_DomainModel):
    viz_id: UUID
    analysis_id: UUID
    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
