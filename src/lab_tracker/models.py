"""Core domain models for lab tracker.

Domain models are the service/repository contract and the response payload type for
retained-v1 routes. SQLAlchemy models remain persistence rows, and request schemas
remain explicit Pydantic wire/input models.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

NoteMetadataScalar = str | bool | int | float
_EXTERNAL_ARTIFACT_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")


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


def external_artifact_uri_validation_error(uri: str) -> str | None:
    cleaned = (uri or "").strip()
    if not cleaned:
        return "External artifact URI must not be empty."
    if any(ch.isspace() or ord(ch) < 0x20 for ch in cleaned):
        return "External artifact URI must not contain spaces or control characters."
    parsed = urlsplit(cleaned)
    if not parsed.scheme or not _EXTERNAL_ARTIFACT_URI_SCHEME_RE.fullmatch(parsed.scheme):
        return "External artifact URI must be a well-formed IRI with a scheme."
    if not parsed.netloc and not parsed.path:
        return "External artifact URI must identify a resource."
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        return "External artifact HTTP(S) URI must include a host."
    return None


def external_artifact_uri_is_valid(uri: str) -> bool:
    return external_artifact_uri_validation_error(uri) is None


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectMembershipRole(str, Enum):
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    OWNER = "owner"


class ProjectGroupKind(str, Enum):
    LAB = "lab"


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


class NoteArchiveReason(str, Enum):
    """Why a captured note was set aside.

    Archiving requires naming a reason so captures are never silently dropped:
    a skipped review degrades visible *coverage*, not silent *trust*.
    ``ARCHIVED_UNREVIEWED`` records that a capture was set aside without ever
    being reviewed against the graph.
    """

    REVIEWED_NOT_RELEVANT = "reviewed_not_relevant"
    ARCHIVED_UNREVIEWED = "archived_unreviewed"
    SUPERSEDED = "superseded"


class UsageEventVerb(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    SUBMIT = "submit"
    REVIEW = "review"
    COMMIT = "commit"
    SEARCH = "search"
    VIEW = "view"
    EXPORT = "export"
    TRANSCRIBE = "transcribe"
    UPLOAD = "upload"
    CLOSE = "close"


class UsageEventResourceType(str, Enum):
    PROJECT = "project"
    PROJECT_GROUP = "project_group"
    GROUP_MEMBERSHIP = "group_membership"
    PROJECT_MEMBERSHIP = "project_membership"
    QUESTION = "question"
    QUESTION_REFACTOR = "question_refactor"
    NOTE = "note"
    SESSION = "session"
    DATASET = "dataset"
    ANALYSIS = "analysis"
    CLAIM = "claim"
    CLAIM_EDGE = "claim_edge"
    EXPLORATION_NODE = "exploration_node"
    PROVENANCE_LINK = "provenance_link"
    VISUALIZATION = "visualization"
    GOAL = "goal"
    GOAL_LINK = "goal_link"
    GRAPH_CHANGE_SET = "graph_change_set"
    GRAPH_DRAFT_BATCH_SETTINGS = "graph_draft_batch_settings"
    GRAPH_DRAFT_BATCH_RUN = "graph_draft_batch_run"
    RECORD_EXPORT = "record_export"
    SEARCH = "search"
    SUPERVISION_EDGE = "supervision_edge"
    ACQUISITION_OUTPUT = "acquisition_output"
    USAGE_EVENT = "usage_event"


class UsageEventOutcome(str, Enum):
    OK = "ok"
    ERROR = "error"


class UsageEventSurface(str, Enum):
    HTTP = "http"
    MCP = "mcp"
    CLI = "cli"


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
    TESTING = "testing"
    SUPPORTED = "supported"
    REJECTED = "rejected"


class ClaimRelation(str, Enum):
    EXTENDS = "extends"
    CONTRADICTS = "contradicts"
    REFUTES = "refutes"
    DEPENDS_ON = "depends_on"
    SUPERSEDES = "supersedes"


class ExplorationNodeType(str, Enum):
    DECISION = "decision"
    DEAD_END = "dead_end"
    PIVOT = "pivot"


class ExplorationNodeStatus(str, Enum):
    STAGED = "staged"
    COMMITTED = "committed"
    ARCHIVED = "archived"


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


class StoreKind(str, Enum):
    """Backend family of a registered data store."""

    LOCAL_FS = "local_fs"
    SSH = "ssh"
    S3 = "s3"
    GCS = "gcs"
    AZURE_BLOB = "azure_blob"
    DROPBOX = "dropbox"
    GDRIVE = "gdrive"
    BOX = "box"
    ONEDRIVE = "onedrive"
    OBJECT_TABLE = "object_table"
    DATABASE = "database"
    HTTP = "http"
    RCLONE = "rclone"
    GIT = "git"


class StoreCapability(str, Enum):
    """What a store backend supports, so resolvers dispatch by capability."""

    BYTES_BY_PATH = "bytes_by_path"
    BYTE_RANGE = "byte_range"
    LIST = "list"
    VERSIONED_SNAPSHOT = "versioned_snapshot"
    QUERY = "query"


_PATH_STORE_CAPABILITIES = [
    StoreCapability.BYTES_BY_PATH,
    StoreCapability.BYTE_RANGE,
    StoreCapability.LIST,
]
_VERSIONED_PATH_STORE_CAPABILITIES = [*_PATH_STORE_CAPABILITIES, StoreCapability.VERSIONED_SNAPSHOT]


def default_store_capabilities(kind: StoreKind) -> list[StoreCapability]:
    """Default capability set for a store kind when none is supplied."""

    if kind in {StoreKind.S3, StoreKind.GCS, StoreKind.AZURE_BLOB}:
        return list(_VERSIONED_PATH_STORE_CAPABILITIES)
    if kind is StoreKind.OBJECT_TABLE:
        return [StoreCapability.VERSIONED_SNAPSHOT, StoreCapability.LIST]
    if kind is StoreKind.DATABASE:
        return [StoreCapability.QUERY]
    if kind is StoreKind.HTTP:
        return [StoreCapability.BYTES_BY_PATH, StoreCapability.BYTE_RANGE]
    if kind is StoreKind.GIT:
        # A commit is an immutable snapshot, so a git pin is versioned; bytes are
        # addressed by path within a commit and support ranged reads.
        return [
            StoreCapability.BYTES_BY_PATH,
            StoreCapability.BYTE_RANGE,
            StoreCapability.VERSIONED_SNAPSHOT,
        ]
    return list(_PATH_STORE_CAPABILITIES)


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


class EntityOrigin(str, Enum):
    USER = "user"
    AI_SUGGESTED = "ai_suggested"
    AI_EXECUTED = "ai_executed"
    USER_REVISED = "user_revised"


class GraphChangeSetStatus(str, Enum):
    DRAFTING = "drafting"
    READY = "ready"
    SUBMITTED = "submitted"
    CHANGES_REQUESTED = "changes_requested"
    COMMITTING = "committing"
    REJECTED = "rejected"
    FAILED = "failed"
    COMMITTED = "committed"


class GraphChangeOperationStatus(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"


class AcceptanceMode(str, Enum):
    """How an AI-proposed operation came to be accepted.

    Recorded so the committed graph stays honest about its own curation: a
    per-operation human accept (``HUMAN_SELECTED``) is durably distinguished
    from a single rubber-stamp over the whole batch (``BULK_ACCEPTED``) and from
    any future non-interactive accept (``AUTO_ACCEPTED``). Without this, a
    bulk-accepted AI guess is indistinguishable from a human-authored edge.
    """

    HUMAN_SELECTED = "human_selected"
    BULK_ACCEPTED = "bulk_accepted"
    AUTO_ACCEPTED = "auto_accepted"


class GraphChangeOp(str, Enum):
    CREATE = "create"
    UPDATE = "update"


class GraphDraftMode(str, Enum):
    GRAPH_CONTEXT = "graph_context"
    IMAGE_ONLY = "image_only"
    GRAPH_BATCH = "graph_batch"


class GraphDraftBatchRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SKIPPED = "skipped"
    READY = "ready"
    FAILED = "failed"


class GraphDraftBatchTrigger(str, Enum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"


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


class ExternalArtifactKind(str, Enum):
    ENTITY = "entity"
    ACTIVITY = "activity"


def _normalize_external_json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("External artifact metadata floats must be finite.")
        return value
    if isinstance(value, list):
        return [_normalize_external_json_value(item) for item in value]
    if isinstance(value, dict):
        return _normalize_external_metadata_mapping(value)
    raise ValueError("External artifact metadata values must be JSON-compatible.")


def _normalize_external_metadata_mapping(value: Mapping[object, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, metadata_value in value.items():
        cleaned_key = str(key).strip()
        if not cleaned_key:
            raise ValueError("External artifact metadata keys must not be blank.")
        cleaned[cleaned_key] = _normalize_external_json_value(metadata_value)
    return cleaned


class ExternalArtifactReference(_DomainModel):
    """Pointer to an artifact owned by an external storage or versioning substrate."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    kind: ExternalArtifactKind = ExternalArtifactKind.ENTITY
    source_system: str
    uri: str
    content_hash: str
    store_name: str | None = None
    locator: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_system", "uri", "content_hash")
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("External artifact reference fields must not be blank.")
        return cleaned

    @field_validator("metadata", mode="before")
    @classmethod
    def _normalize_metadata(cls, value: Mapping[object, Any] | None) -> dict[str, Any]:
        if value is None:
            return {}
        return _normalize_external_metadata_mapping(value)

    @model_validator(mode="after")
    def _require_paired_store_fields(self) -> ExternalArtifactReference:
        if (self.store_name is None) != (self.locator is None):
            raise ValueError(
                "store_name and locator must be provided together on an external "
                "artifact reference."
            )
        return self

    @classmethod
    def for_store(
        cls,
        *,
        store_name: str,
        locator: str,
        content_hash: str,
        kind: ExternalArtifactKind = ExternalArtifactKind.ENTITY,
        source_system: str = "store",
        metadata: Mapping[str, Any] | None = None,
    ) -> ExternalArtifactReference:
        """Build a store-relative reference from explicit fields.

        The ``store://<store_name>/<locator>`` URI is derived for display and
        back-compatibility, while the structured ``store_name``/``locator`` fields
        are what resolution reads — no URI string parsing required.
        """

        clean_locator = locator.strip().lstrip("/")
        return cls(
            kind=kind,
            source_system=source_system,
            uri=f"store://{store_name}/{clean_locator}",
            content_hash=content_hash,
            store_name=store_name,
            locator=clean_locator,
            metadata=dict(metadata or {}),
        )


class DatasetCommitManifestInput(_DomainModel):
    files: list[DatasetFile] = Field(default_factory=list)
    external_artifacts: list[ExternalArtifactReference] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    nwb_metadata: dict[str, str] = Field(default_factory=dict)
    bids_metadata: dict[str, str] = Field(default_factory=dict)
    note_ids: list[UUID] = Field(default_factory=list)
    source_session_id: UUID | None = None


class DatasetCommitManifest(_DomainModel):
    files: list[DatasetFile] = Field(default_factory=list)
    external_artifacts: list[ExternalArtifactReference] = Field(default_factory=list)
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
    review_note: str | None = None
    acceptance_mode: AcceptanceMode | None = None
    accepted_by: str | None = None
    accepted_by_user_id: UUID | None = None
    accepted_at: datetime | None = None
    result_entity_id: UUID | None = None
    error_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphChangeSet(_DomainModel):
    change_set_id: UUID
    project_id: UUID
    source_note_id: UUID
    source_note_ids: list[UUID] = Field(default_factory=list)
    source_checksum: str | None = None
    source_content_type: str | None = None
    source_filename: str | None = None
    batch_key: str | None = None
    batch_window_start: datetime | None = None
    batch_window_end: datetime | None = None
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
    operation_count: int = 0
    operations: list[GraphChangeOperation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    created_by_username: str | None = None
    review_assignee: str | None = None
    review_assignee_user_id: UUID | None = None
    review_assignee_username: str | None = None
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

    @computed_field
    @property
    def source_note_count(self) -> int:
        return len(self.source_note_ids or [self.source_note_id])

    @computed_field
    @property
    def meeting_note_count(self) -> int:
        """Number of meeting-tagged notes in this draft's batch context.

        Derived from the stored context packet's summary so review surfaces can
        nudge ("a meeting is waiting to be fleshed out") without re-reading the
        source notes. Zero for note-scoped drafts and batches without meetings.
        """
        summary = self.context_packet.get("context_summary") if self.context_packet else None
        counts = summary.get("counts") if isinstance(summary, dict) else None
        value = counts.get("meeting_notes") if isinstance(counts, dict) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)


class ReadyEdition(_DomainModel):
    """A contentless summary of one ready daily-review edition for a reviewer.

    This is the payload of the daily-review *cue* (the email/push knock). It
    deliberately carries no science: no summary, no proposed operations, no
    excerpts. ``project_name`` is ``None`` unless the caller explicitly opts in,
    and ``decidable_count`` is ``None`` (with ``sensitivity_suppressed`` true)
    when any source note in the edition is sensitivity-tagged, so even activity
    volume for a sensitive program cannot be inferred off-app. ``deep_link`` is a
    signed, short-TTL, capability-free link into the in-app accept/reject queue.
    """

    change_set_id: UUID
    project_id: UUID
    project_name: str | None = None
    review_assignee: str | None = None
    review_assignee_user_id: UUID | None = None
    review_assignee_username: str | None = None
    decidable_count: int | None = None
    sensitivity_suppressed: bool = False
    created_at: datetime
    deep_link: str | None = None


class GraphDraftBatchSettings(_DomainModel):
    settings_id: UUID
    project_id: UUID
    user_id: UUID | None = None
    enabled: bool = True
    cadence_minutes: int = 24 * 60
    run_at_local_time: str = "18:00"
    timezone_name: str = "America/New_York"
    next_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    updated_by: str | None = None


class GraphDraftBatchRun(_DomainModel):
    run_id: UUID
    project_id: UUID
    trigger: GraphDraftBatchTrigger = GraphDraftBatchTrigger.SCHEDULED
    status: GraphDraftBatchRunStatus = GraphDraftBatchRunStatus.RUNNING
    window_start: datetime
    window_end: datetime
    note_count: int = 0
    source_note_ids: list[UUID] = Field(default_factory=list)
    batch_key: str
    user_hint: str | None = None
    change_set_id: UUID | None = None
    summary: str = ""
    error_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    review_assignee: str | None = None
    review_assignee_user_id: UUID | None = None


class Project(_DomainModel):
    project_id: UUID
    group_id: UUID | None = None
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ProjectGroup(_DomainModel):
    group_id: UUID
    name: str
    description: str = ""
    kind: ProjectGroupKind = ProjectGroupKind.LAB
    group_read_all: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
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
    created_by_user_id: UUID | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class GroupMembership(_DomainModel):
    membership_id: UUID
    group_id: UUID
    user_id: UUID
    role: ProjectMembershipRole
    username: str | None = None
    user_global_role: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class SupervisionEdge(_DomainModel):
    edge_id: UUID
    supervisor_user_id: UUID
    supervisee_user_id: UUID
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class OwnershipReassignment(_DomainModel):
    reassignment_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    reason: str = ""
    record_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None

    @computed_field(return_type=int)
    @property
    def total_records(self) -> int:
        return sum(self.record_counts.values())


class Question(_DomainModel):
    question_id: UUID
    project_id: UUID
    text: str
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus = QuestionStatus.STAGED
    terminal_reason: str | None = None
    parent_question_ids: list[UUID] = Field(default_factory=list)
    superseded_by_question_id: UUID | None = None
    supersedes_question_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
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
    created_by_user_id: UUID | None = None


class Dataset(_DomainModel):
    dataset_id: UUID
    project_id: UUID
    commit_hash: str
    primary_question_id: UUID
    question_links: list[QuestionLink]
    commit_manifest: DatasetCommitManifest
    status: DatasetStatus = DatasetStatus.STAGED
    terminal_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class Note(_DomainModel):
    note_id: UUID
    project_id: UUID
    raw_content: str
    raw_asset: NoteRawAsset | None = None
    transcribed_text: str | None = None
    targets: list[EntityRef] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    client_capture_id: str | None = None
    status: NoteStatus = NoteStatus.STAGED
    archived_reason: NoteArchiveReason | None = None
    archived_at: datetime | None = None
    archived_by: str | None = None
    archived_by_user_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
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
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
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
    external_artifacts: list[ExternalArtifactReference] = Field(default_factory=list)
    executed_by: str | None = None
    executed_by_user_id: UUID | None = None
    executed_at: datetime = Field(default_factory=utc_now)
    status: AnalysisStatus = AnalysisStatus.STAGED
    terminal_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ClaimInput(_DomainModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    statement: str
    confidence: float | None = None
    status: ClaimStatus = ClaimStatus.PROPOSED
    terminal_reason: str | None = None
    falsification_criteria: str | None = None
    verification_plan: str | None = None
    refuting_outcome: str | None = None
    supported_by_dataset_ids: list[UUID] = Field(default_factory=list)
    supported_by_analysis_ids: list[UUID] = Field(default_factory=list)
    answers_question_ids: list[UUID] = Field(default_factory=list)
    external_citations: list[ExternalArtifactReference] = Field(default_factory=list)


class Claim(_DomainModel):
    claim_id: UUID
    project_id: UUID
    statement: str
    confidence: float | None = None
    status: ClaimStatus = ClaimStatus.PROPOSED
    terminal_reason: str | None = None
    falsification_criteria: str | None = None
    verification_plan: str | None = None
    refuting_outcome: str | None = None
    supported_by_dataset_ids: list[UUID] = Field(default_factory=list)
    supported_by_analysis_ids: list[UUID] = Field(default_factory=list)
    answers_question_ids: list[UUID] = Field(default_factory=list)
    external_citations: list[ExternalArtifactReference] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class ClaimEdge(_DomainModel):
    edge_id: UUID
    claim_id: UUID
    target_claim_id: UUID
    relation: ClaimRelation
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None


class ProvenanceLinkRelation(str, Enum):
    """PROV-O relation a provenance link expresses between two artifacts."""

    WAS_DERIVED_FROM = "was_derived_from"
    USED = "used"


class ProvenanceLinkBasis(str, Enum):
    """How a provenance link was detected/justified."""

    CONTENT_HASH_MATCH = "content_hash_match"


class ProvenanceLinkStatus(str, Enum):
    """Curation lifecycle of a proposed provenance link (human-gated)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ProvenanceLinkOrigin(str, Enum):
    """Who produced the link."""

    SYSTEM_DETECTED = "system_detected"


class ProvenanceLink(_DomainModel):
    """A human-gated lineage edge: ``source`` was derived from / used ``target``.

    Proposed by the deterministic content-hash detector during the daily/batch
    run; only a human accept makes it canonical (and only accepted links render
    in PROV-O export). Carries the same curation-provenance triple as accepted
    graph-draft operations.
    """

    link_id: UUID
    project_id: UUID
    source: EntityRef
    target: EntityRef
    relation: ProvenanceLinkRelation
    basis: ProvenanceLinkBasis
    content_hash: str | None = None
    status: ProvenanceLinkStatus = ProvenanceLinkStatus.PROPOSED
    origin: ProvenanceLinkOrigin = ProvenanceLinkOrigin.SYSTEM_DETECTED
    acceptance_mode: AcceptanceMode | None = None
    accepted_by: str | None = None
    accepted_by_user_id: UUID | None = None
    accepted_at: datetime | None = None
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime | None = None


class ExplorationNode(_DomainModel):
    node_id: UUID
    project_id: UUID
    node_type: ExplorationNodeType
    title: str
    target: EntityRef
    status: ExplorationNodeStatus = ExplorationNodeStatus.STAGED
    choice: str | None = None
    alternatives_considered: list[str] = Field(default_factory=list)
    rationale: str | None = None
    evidence_refs: list[EntityRef] = Field(default_factory=list)
    hypothesis: str | None = None
    failure_mode: str | None = None
    lesson: str | None = None
    tooling_context: str | None = None
    trigger: str | None = None
    invalidates_node_id: UUID | None = None
    invalidates_claim_id: UUID | None = None
    parent_node_ids: list[UUID] = Field(default_factory=list)
    also_depends_on_node_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class EntityVersion(_DomainModel):
    version_id: UUID
    entity_type: EntityType
    entity_id: UUID
    version_number: int = Field(..., ge=1)
    snapshot: dict[str, Any] = Field(default_factory=dict)
    change_set_id: UUID | None = None
    committed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None


class EntityVersionDiff(_DomainModel):
    entity_type: EntityType
    entity_id: UUID
    from_version: int
    to_version: int
    changed_fields: dict[str, dict[str, Any]] = Field(default_factory=dict)


class GoalLink(_DomainModel):
    link_id: UUID
    goal_id: UUID
    target: EntityRef
    relation: GoalRelation
    link_status: GoalLinkStatus = GoalLinkStatus.CANDIDATE
    slot: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None


class Goal(_DomainModel):
    goal_id: UUID
    project_id: UUID | None = None
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
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
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


class DataStore(_DomainModel):
    """A registered durable data-store location Lab Tracker resolves against.

    Lab Tracker stores *where* a store is (kind, root, endpoint) and a credential
    *reference* — never a secret. Artifacts are addressed relative to a store via
    ``store://<name>/<path>`` locators. A store is scoped to exactly one of a
    project or a group (lab); a group-scoped store is inherited by every project
    in that group.
    """

    store_id: UUID
    project_id: UUID | None = None
    group_id: UUID | None = None
    name: str
    kind: StoreKind
    capabilities: list[StoreCapability] = Field(default_factory=list)
    root: str
    endpoint: str | None = None
    credential_ref: str | None = None
    is_default: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> DataStore:
        if (self.project_id is None) == (self.group_id is None):
            raise ValueError("DataStore must be scoped to exactly one of project_id or group_id.")
        return self


class Visualization(_DomainModel):
    viz_id: UUID
    analysis_id: UUID
    dataset_ids: list[UUID] = Field(default_factory=list)
    viz_type: str
    file_path: str
    caption: str | None = None
    related_claim_ids: list[UUID] = Field(default_factory=list)
    asset: VisualizationAsset | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    origin: EntityOrigin = EntityOrigin.USER
    change_set_id: UUID | None = None
    origin_provider: str | None = None
    origin_model: str | None = None
    origin_prompt_version: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @computed_field(return_type=str | None)
    @property
    def asset_download_path(self) -> str | None:
        if self.asset is None:
            return None
        return f"/visualizations/{self.viz_id}/file/download"


class RecordExportRecords(_DomainModel):
    questions: list[Question] = Field(default_factory=list)
    datasets: list[Dataset] = Field(default_factory=list)
    sessions: list[Session] = Field(default_factory=list)
    analyses: list[Analysis] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    claim_edges: list[ClaimEdge] = Field(default_factory=list)
    exploration_nodes: list[ExplorationNode] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    visualizations: list[Visualization] = Field(default_factory=list)


class PublicationReadinessUnsupportedClaim(_DomainModel):
    claim_id: UUID
    statement: str
    status: ClaimStatus
    reason: str


class PublicationReadinessUngroundedQuestion(_DomainModel):
    question_id: UUID
    text: str
    status: QuestionStatus
    reason: str


class PublicationReadinessOrphanedEntity(_DomainModel):
    entity_type: EntityType
    entity_id: UUID
    relation: str
    missing_entity_type: EntityType
    missing_entity_id: UUID


class PublicationReadinessBrokenExternalRef(_DomainModel):
    entity_type: EntityType
    entity_id: UUID
    source_system: str
    uri: str
    reason: str


class PublicationReadinessReport(_DomainModel):
    project_id: UUID
    unsupported_claims: list[PublicationReadinessUnsupportedClaim] = Field(default_factory=list)
    ungrounded_questions: list[PublicationReadinessUngroundedQuestion] = Field(default_factory=list)
    orphaned_entities: list[PublicationReadinessOrphanedEntity] = Field(default_factory=list)
    broken_external_refs: list[PublicationReadinessBrokenExternalRef] = Field(default_factory=list)
    seal_level: Literal["blocked", "ara_l1"] = "blocked"


class RecordExportEvent(_DomainModel):
    export_id: UUID
    user_id: UUID
    group_id: UUID | None = None
    project_ids: list[UUID] = Field(default_factory=list)
    record_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str | None = None
    created_by_user_id: UUID | None = None


class RecordExport(_DomainModel):
    export_event_id: UUID | None = None
    user_id: UUID
    group_id: UUID | None = None
    project_ids: list[UUID] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)
    records: RecordExportRecords
    provenance: dict[str, Any] = Field(default_factory=dict)


class UsageEvent(_DomainModel):
    event_id: UUID
    occurred_at: datetime = Field(default_factory=utc_now)
    verb: UsageEventVerb
    resource_type: UsageEventResourceType
    resource_id: UUID | None = None
    actor_user_id: UUID | None = None
    actor_role: str | None = None
    principal_type: str | None = None
    surface: UsageEventSurface | None = None
    project_id: UUID | None = None
    outcome: UsageEventOutcome = UsageEventOutcome.OK
    duration_ms: int | None = None
    result_count: int | None = None


class UsageEventRollup(_DomainModel):
    rollup_id: UUID
    day: date
    verb: UsageEventVerb
    resource_type: UsageEventResourceType
    project_id: UUID | None = None
    actor_role: str | None = None
    principal_type: str | None = None
    surface: UsageEventSurface | None = None
    outcome: UsageEventOutcome = UsageEventOutcome.OK
    event_count: int = 0
    total_duration_ms: int = 0
    total_result_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
