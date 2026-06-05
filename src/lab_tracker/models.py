"""Core domain models for lab tracker.

Domain models are the service/repository contract and the response payload type for
retained-v1 routes. SQLAlchemy models remain persistence rows, and request schemas
remain explicit Pydantic wire/input models.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

NoteMetadataScalar = str | bool | int | float


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


class ProjectMembershipRole(str, Enum):
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    OWNER = "owner"


class QuestionStatus(str, Enum):
    STAGED = "staged"
    ACTIVE = "active"
    ANSWERED = "answered"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class QuestionType(str, Enum):
    DESCRIPTIVE = "descriptive"
    HYPOTHESIS_DRIVEN = "hypothesis_driven"
    METHOD_DEV = "method_dev"
    OTHER = "other"


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


class GoalType(str, Enum):
    PAPER = "paper"
    GRANT = "grant"
    TALK = "talk"
    OTHER = "other"


class GoalStatus(str, Enum):
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    ABANDONED = "abandoned"


class GoalRelation(str, Enum):
    CONTRIBUTES_TO = "contributes_to"
    ADDRESSES = "addresses"
    CANDIDATE_FIGURE = "candidate_figure"
    SUPPORTING_EVIDENCE = "supporting_evidence"
    BACKGROUND = "background"
    METHODS = "methods"


class GoalLinkStatus(str, Enum):
    CANDIDATE = "candidate"
    COMMITTED = "committed"
    DROPPED = "dropped"


class QuestionLinkRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class OutcomeStatus(str, Enum):
    UNKNOWN = "unknown"
    SUPPORTS = "supports"
    REFUTES = "refutes"
    INCONCLUSIVE = "inconclusive"


class EntityType(str, Enum):
    PROJECT = "project"
    QUESTION = "question"
    DATASET = "dataset"
    NOTE = "note"
    SESSION = "session"
    ANALYSIS = "analysis"
    CLAIM = "claim"
    VISUALIZATION = "visualization"
    GOAL = "goal"


class GraphChangeSetStatus(str, Enum):
    DRAFTING = "drafting"
    READY = "ready"
    SUBMITTED = "submitted"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"
    FAILED = "failed"
    COMMITTED = "committed"


class GraphChangeOperationStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class GraphChangeOp(str, Enum):
    CREATE = "create"
    UPDATE = "update"


class GraphDraftMode(str, Enum):
    GRAPH_CONTEXT = "graph_context"
    IMAGE_ONLY = "image_only"


class GraphDraftSemanticType(str, Enum):
    CREATE_ENTITY = "create_entity"
    UPDATE_ENTITY = "update_entity"
    CREATE_NOTE = "create_note"
    LINK_NOTE_TO_QUESTION = "link_note_to_question"
    LINK_NOTE_TO_SESSION = "link_note_to_session"
    LINK_NOTE_TO_DATASET = "link_note_to_dataset"
    LINK_NOTE_TO_ANALYSIS = "link_note_to_analysis"
    SUGGEST_NEW_QUESTION = "suggest_new_question"
    SUGGEST_NEW_DATASET = "suggest_new_dataset"
    SUGGEST_NEW_GOAL = "suggest_new_goal"
    LINK_NODE_TO_GOAL = "link_node_to_goal"
    UPDATE_GOAL = "update_goal"
    SUGGEST_FOLLOWUP = "suggest_followup"
    REQUEST_CLARIFICATION = "request_clarification"


class _DomainModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EntityRef(_DomainModel):
    entity_type: EntityType
    entity_id: UUID

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
    source_session_id: UUID | None = None


class DatasetCommitManifest(_DomainModel):
    files: list[DatasetFile] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    nwb_metadata: dict[str, str] = Field(default_factory=dict)
    bids_metadata: dict[str, str] = Field(default_factory=dict)
    note_ids: list[UUID] = Field(default_factory=list)
    question_links: list[QuestionLink] = Field(default_factory=list)
    source_session_id: UUID | None = None


class NoteRawAsset(_DomainModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str


class GraphChangeOperation(_DomainModel):
    operation_id: UUID
    change_set_id: UUID
    sequence: int
    op: GraphChangeOp
    entity_type: EntityType
    semantic_type: GraphDraftSemanticType | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    target_entity_id: UUID | None = None
    client_ref: str | None = None
    rationale: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: GraphChangeOperationStatus = GraphChangeOperationStatus.PROPOSED
    result_entity_id: UUID | None = None
    error_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphChangeSet(_DomainModel):
    change_set_id: UUID
    project_id: UUID
    source_note_id: UUID
    source_checksum: str | None = None
    source_content_type: str | None = None
    source_filename: str | None = None
    provider: str = "openai"
    model: str
    prompt_version: str
    draft_mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT
    context_packet: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    uncertain_fields: list[str] = Field(default_factory=list)
    clarification_requests: list[str] = Field(default_factory=list)
    status: GraphChangeSetStatus = GraphChangeSetStatus.DRAFTING
    commit_message: str | None = None
    error_metadata: dict[str, Any] = Field(default_factory=dict)
    operations: list[GraphChangeOperation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_username: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    submitted_at: datetime | None = None
    submitted_by: str | None = None
    submitted_by_username: str | None = None
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_by_username: str | None = None
    review_note: str | None = None
    committed_at: datetime | None = None
    committed_by: str | None = None
    committed_by_username: str | None = None


class Project(_DomainModel):
    project_id: UUID
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectMembership(_DomainModel):
    membership_id: UUID
    project_id: UUID
    user_id: UUID
    role: ProjectMembershipRole
    username: str | None = None
    user_global_role: str | None = None
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
    superseded_by_question_id: UUID | None = None
    supersedes_question_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class QuestionRefactor(_DomainModel):
    refactor_id: UUID
    project_id: UUID
    source_question_id: UUID
    replacement_question_id: UUID
    reason: str
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    replacement_snapshot: dict[str, Any] = Field(default_factory=dict)
    relationship_changes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None


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


class Note(_DomainModel):
    note_id: UUID
    project_id: UUID
    raw_content: str
    raw_asset: NoteRawAsset | None = None
    transcribed_text: str | None = None
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
    answers_question_ids: list[UUID] = Field(default_factory=list)


class Claim(_DomainModel):
    claim_id: UUID
    project_id: UUID
    statement: str
    confidence: float
    status: ClaimStatus = ClaimStatus.PROPOSED
    supported_by_dataset_ids: list[UUID] = Field(default_factory=list)
    supported_by_analysis_ids: list[UUID] = Field(default_factory=list)
    answers_question_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GoalLink(_DomainModel):
    link_id: UUID
    goal_id: UUID
    target: EntityRef
    relation: GoalRelation
    link_status: GoalLinkStatus = GoalLinkStatus.CANDIDATE
    slot: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None


class Goal(_DomainModel):
    goal_id: UUID
    project_id: UUID
    goal_type: GoalType
    title: str
    summary: str = ""
    status: GoalStatus = GoalStatus.PLANNED
    target_date: date | None = None
    external_ref: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    links: list[GoalLink] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class VisualizationInput(_DomainModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID] = Field(default_factory=list)


class VisualizationAsset(_DomainModel):
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    checksum: str


class Visualization(_DomainModel):
    viz_id: UUID
    analysis_id: UUID
    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID] = Field(default_factory=list)
    asset: VisualizationAsset | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @computed_field(return_type=str | None)
    @property
    def asset_download_path(self) -> str | None:
        if self.asset is None:
            return None
        return f"/visualizations/{self.viz_id}/file/download"
