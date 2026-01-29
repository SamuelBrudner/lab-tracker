"""Core domain models for lab tracker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


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


@dataclass(frozen=True)
class EntityRef:
    entity_type: EntityType
    entity_id: UUID


@dataclass(frozen=True)
class ExtractedEntity:
    label: str
    confidence: float
    provenance: str


@dataclass(frozen=True)
class EntityTagSuggestion:
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


@dataclass(frozen=True)
class QuestionLink:
    question_id: UUID
    role: QuestionLinkRole
    outcome_status: OutcomeStatus = OutcomeStatus.UNKNOWN


@dataclass(frozen=True)
class DatasetFile:
    path: str
    checksum: str


@dataclass(frozen=True)
class DatasetCommitManifestInput:
    files: list[DatasetFile] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    note_ids: list[UUID] = field(default_factory=list)
    extraction_provenance: list[str] = field(default_factory=list)
    source_session_id: UUID | None = None


@dataclass(frozen=True)
class DatasetCommitManifest:
    files: list[DatasetFile] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    note_ids: list[UUID] = field(default_factory=list)
    extraction_provenance: list[str] = field(default_factory=list)
    question_links: list[QuestionLink] = field(default_factory=list)
    source_session_id: UUID | None = None


@dataclass
class Project:
    project_id: UUID
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    created_at: datetime = field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Question:
    question_id: UUID
    project_id: UUID
    text: str
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus = QuestionStatus.STAGED
    parent_question_ids: list[UUID] = field(default_factory=list)
    created_from: QuestionSource = QuestionSource.MANUAL
    created_at: datetime = field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Dataset:
    dataset_id: UUID
    project_id: UUID
    commit_hash: str
    primary_question_id: UUID
    question_links: list[QuestionLink]
    commit_manifest: DatasetCommitManifest
    status: DatasetStatus = DatasetStatus.STAGED
    created_at: datetime = field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Note:
    note_id: UUID
    project_id: UUID
    raw_content: str
    transcribed_text: str | None = None
    extracted_entities: list[ExtractedEntity] = field(default_factory=list)
    tag_suggestions: list[EntityTagSuggestion] = field(default_factory=list)
    targets: list[EntityRef] = field(default_factory=list)
    status: NoteStatus = NoteStatus.STAGED
    created_at: datetime = field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Session:
    session_id: UUID
    project_id: UUID
    session_type: SessionType
    status: SessionStatus = SessionStatus.ACTIVE
    primary_question_id: UUID | None = None
    started_at: datetime = field(default_factory=utc_now)
    ended_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Analysis:
    analysis_id: UUID
    project_id: UUID
    dataset_ids: list[UUID]
    method_hash: str
    code_version: str
    environment_hash: str | None = None
    executed_by: str | None = None
    executed_at: datetime = field(default_factory=utc_now)
    status: AnalysisStatus = AnalysisStatus.STAGED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Claim:
    claim_id: UUID
    project_id: UUID
    statement: str
    confidence: float
    status: ClaimStatus = ClaimStatus.PROPOSED
    supported_by_dataset_ids: list[UUID] = field(default_factory=list)
    supported_by_analysis_ids: list[UUID] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class Visualization:
    viz_id: UUID
    analysis_id: UUID
    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
