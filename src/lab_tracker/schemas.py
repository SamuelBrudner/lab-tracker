"""Pydantic schemas for API requests and responses.

The API returns domain models (defined in :mod:`lab_tracker.models`) directly inside
Envelope/ListEnvelope wrappers. Request payloads use purpose-built schemas below.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from lab_tracker.auth import Role
from lab_tracker.goals_attributes import validate_goal_attributes
from lab_tracker.models import (
    Analysis,
    AnalysisStatus,
    Claim,
    ClaimConfidence,
    ClaimInput,
    ClaimRelation,
    ClaimStatus,
    DatasetCommitManifestInput,
    DatasetStatus,
    DataStore,
    EntityRef,
    EntityType,
    ExplorationNode,
    ExplorationNodeStatus,
    ExplorationNodeType,
    ExternalArtifactReference,
    Goal,
    GoalLink,
    GoalLinkStatus,
    GoalRelation,
    GoalStatus,
    GoalType,
    GraphChangeOperationStatus,
    GraphChangeSetStatus,
    GraphDraftBatchRunStatus,
    GraphDraftMode,
    GroupMembership,
    Note,
    NoteArchiveReason,
    NoteMetadataScalar,
    NoteStatus,
    OwnershipReassignment,
    ProjectGroup,
    ProjectGroupKind,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
    ProvenanceLink,
    ProvenanceLinkStatus,
    Question,
    QuestionLink,
    QuestionRefactor,
    QuestionStatus,
    QuestionType,
    SessionStatus,
    SessionType,
    StoreCapability,
    StoreKind,
    SupervisionEdge,
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


class AuthInvitationRead(BaseModel):
    invitation_id: UUID
    email: str
    role: Role
    status: str
    invite_url: str | None = None
    mailto_url: str | None = None
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None
    revoked_at: datetime | None = None
    warning: str | None = None


class AuthBootstrapStatus(BaseModel):
    has_users: bool
    bootstrap_admin_configured: bool
    first_admin_available: bool
    bootstrap_token: str | None = None
    bootstrap_token_warning: str | None = None


class AuthTokenRead(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: AuthUserRead


class PersonalAccessTokenCreate(RequestModel):
    label: Annotated[str, Field(min_length=1, max_length=150), AfterValidator(_non_blank_string)]
    role: Role = Role.VIEWER
    read_only: bool = True
    # "all" keeps the role-based service policy; "batch_run_due" narrows the token
    # to POST /batches/run-due only (the daily-review scheduler credential).
    scope: Literal["all", "batch_run_due"] = "all"
    expires_at: datetime


class PersonalAccessTokenRead(BaseModel):
    token_id: UUID
    label: str
    role: Role
    read_only: bool
    scope: str
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class PersonalAccessTokenIssuedRead(PersonalAccessTokenRead):
    secret: str


class AuthRegisterRequest(RequestModel):
    username: NonBlankStr
    password: NonBlankStr
    role: Role = Role.VIEWER
    bootstrap_token: NonBlankStr | None = None
    invite_token: NonBlankStr | None = None


class AuthUserUpdate(RequestModel):
    password: NonBlankStr | None = None
    role: Role | None = None


class AuthInvitationCreate(RequestModel):
    email: NonBlankStr
    role: Role = Role.EDITOR


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
    group_id: UUID | None = None
    client_capture_id: str | None = None


class ProjectUpdate(RequestModel):
    name: NonBlankStr | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    group_id: UUID | None = None


class ProjectGroupCreate(RequestModel):
    name: NonBlankStr
    description: str | None = None
    kind: ProjectGroupKind | None = None
    group_read_all: bool | None = None


class ProjectGroupUpdate(RequestModel):
    name: NonBlankStr | None = None
    description: str | None = None
    kind: ProjectGroupKind | None = None
    group_read_all: bool | None = None


class GroupMembershipCreate(RequestModel):
    user_id: UUID | None = None
    username: str | None = Field(default=None, min_length=1)
    role: ProjectMembershipRole


class GroupMembershipUpdate(RequestModel):
    role: ProjectMembershipRole


class GroupProjectMembershipBulkCreate(RequestModel):
    user_id: UUID | None = None
    username: str | None = Field(default=None, min_length=1)
    role: ProjectMembershipRole


ProjectGroupRead = ProjectGroup
GroupMembershipRead = GroupMembership


class ProjectMembershipCreate(RequestModel):
    user_id: UUID | None = None
    username: str | None = Field(default=None, min_length=1)
    role: ProjectMembershipRole


class ProjectMembershipUpdate(RequestModel):
    role: ProjectMembershipRole


ProjectMembershipRead = ProjectMembership


class SupervisionEdgeCreate(RequestModel):
    supervisor_user_id: UUID
    supervisee_user_id: UUID
    started_at: datetime | None = None
    ended_at: datetime | None = None


class SupervisionEdgeUpdate(RequestModel):
    supervisor_user_id: UUID | None = None
    supervisee_user_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


SupervisionEdgeRead = SupervisionEdge


class OwnershipReassignmentCreate(RequestModel):
    from_user_id: UUID
    to_user_id: UUID
    reason: str | None = None


OwnershipReassignmentRead = OwnershipReassignment


class QuestionCreate(RequestModel):
    project_id: UUID
    text: NonBlankStr
    question_type: QuestionType
    hypothesis: str | None = None
    status: QuestionStatus | None = None
    client_capture_id: str | None = None
    terminal_reason: NonBlankStr | None = None
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
    terminal_reason: NonBlankStr | None = None
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
    terminal_reason: NonBlankStr | None = None

    @field_validator("secondary_question_ids")
    @classmethod
    def _secondary_question_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class DatasetUpdate(RequestModel):
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    status: DatasetStatus | None = None
    terminal_reason: NonBlankStr | None = None
    question_links: list[QuestionLink] | None = None


class NoteCreate(RequestModel):
    project_id: UUID
    raw_content: NonBlankStr
    transcribed_text: str | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, NoteMetadataScalar] | None = None
    client_capture_id: str | None = None
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


class NoteArchiveRequest(RequestModel):
    reason: NoteArchiveReason = NoteArchiveReason.ARCHIVED_UNREVIEWED


class GraphDraftOperationUpdate(RequestModel):
    payload: dict[str, Any] | None = None
    status: GraphChangeOperationStatus | None = None
    review_note: str | None = None


class GraphDraftCreateRequest(RequestModel):
    mode: GraphDraftMode = GraphDraftMode.GRAPH_CONTEXT
    user_hint: NonBlankStr | None = None


class GraphDraftCommitRequest(RequestModel):
    message: NonBlankStr


class GraphDraftReviewRequest(RequestModel):
    status: GraphChangeSetStatus
    note: NonBlankStr | None = None


class GraphChangeSetSummary(BaseModel):
    change_set_id: UUID
    project_id: UUID
    source_note_id: UUID
    source_note_ids: list[UUID] = Field(default_factory=list)
    source_checksum: str | None = None
    source_content_type: str | None = None
    source_filename: str | None = None
    source_note_count: int
    batch_key: str | None = None
    batch_window_start: datetime | None = None
    batch_window_end: datetime | None = None
    provider: str
    model: str
    prompt_version: str
    draft_mode: GraphDraftMode
    summary: str = ""
    uncertain_fields: list[str] = Field(default_factory=list)
    clarification_requests: list[str] = Field(default_factory=list)
    status: GraphChangeSetStatus
    commit_message: str | None = None
    error_metadata: dict[str, Any] = Field(default_factory=dict)
    operation_count: int = 0
    created_at: datetime
    created_by: str | None = None
    created_by_user_id: UUID | None = None
    created_by_username: str | None = None
    review_assignee: str | None = None
    review_assignee_user_id: UUID | None = None
    review_assignee_username: str | None = None
    updated_at: datetime
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


class GraphDraftListFilters(BaseModel):
    project_id: UUID | None = None
    status: GraphChangeSetStatus | None = None
    source_note_id: UUID | None = None


class GraphDraftBatchSettingsUpdate(RequestModel):
    enabled: bool | None = None
    cadence_minutes: int | None = Field(default=None, ge=60)
    run_at_local_time: str | None = None
    timezone_name: str | None = None
    user_id: UUID | None = None


class GraphDraftBatchRunRequest(RequestModel):
    project_id: UUID
    since: datetime | None = None
    until: datetime | None = None
    user_hint: str | None = None


class GraphDraftBatchRunFilters(BaseModel):
    project_id: UUID | None = None
    status: GraphDraftBatchRunStatus | None = None


class AssistantDecisionContextRequest(RequestModel):
    task_kind: NonBlankStr
    query: NonBlankStr
    project_id: UUID | None = None
    question_id: UUID | None = None
    dataset_id: UUID | None = None
    analysis_id: UUID | None = None
    claim_id: UUID | None = None
    visualization_id: UUID | None = None
    created_by: UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
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
    external_artifacts: list[ExternalArtifactReference] | None = None
    status: AnalysisStatus | None = None
    terminal_reason: NonBlankStr | None = None

    @field_validator("dataset_ids")
    @classmethod
    def _dataset_ids_unique(cls, value: list[UUID]) -> list[UUID]:
        return _unique_uuid_list(value) or []


class AnalysisUpdate(RequestModel):
    status: AnalysisStatus | None = None
    environment_hash: str | None = None
    external_artifacts: list[ExternalArtifactReference] | None = None
    terminal_reason: NonBlankStr | None = None


class ClaimCreate(RequestModel):
    project_id: UUID
    statement: NonBlankStr
    confidence: ClaimConfidence
    status: ClaimStatus | None = None
    terminal_reason: NonBlankStr | None = None
    falsification_criteria: NonBlankStr | None = None
    verification_plan: NonBlankStr | None = None
    refuting_outcome: NonBlankStr | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None
    answers_question_ids: list[UUID] | None = None
    external_citations: list[ExternalArtifactReference] | None = None

    @field_validator(
        "supported_by_dataset_ids", "supported_by_analysis_ids", "answers_question_ids"
    )
    @classmethod
    def _claim_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class ClaimUpdate(RequestModel):
    statement: NonBlankStr | None = None
    confidence: ClaimConfidence | None = None
    status: ClaimStatus | None = None
    terminal_reason: NonBlankStr | None = None
    falsification_criteria: NonBlankStr | None = None
    verification_plan: NonBlankStr | None = None
    refuting_outcome: NonBlankStr | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None
    answers_question_ids: list[UUID] | None = None
    external_citations: list[ExternalArtifactReference] | None = None

    @field_validator(
        "supported_by_dataset_ids", "supported_by_analysis_ids", "answers_question_ids"
    )
    @classmethod
    def _claim_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class ClaimEdgeCreate(RequestModel):
    target_claim_id: UUID
    relation: ClaimRelation


class ExplorationNodeCreate(RequestModel):
    project_id: UUID
    node_type: ExplorationNodeType
    title: NonBlankStr
    target: EntityRef
    status: ExplorationNodeStatus | None = None
    choice: NonBlankStr | None = None
    alternatives_considered: list[NonBlankStr] | None = None
    rationale: NonBlankStr | None = None
    evidence_refs: list[EntityRef] | None = None
    hypothesis: NonBlankStr | None = None
    failure_mode: NonBlankStr | None = None
    lesson: NonBlankStr | None = None
    tooling_context: NonBlankStr | None = None
    trigger: NonBlankStr | None = None
    invalidates_node_id: UUID | None = None
    invalidates_claim_id: UUID | None = None
    parent_node_ids: list[UUID] | None = None
    also_depends_on_node_ids: list[UUID] | None = None

    @field_validator("parent_node_ids", "also_depends_on_node_ids")
    @classmethod
    def _node_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


class ExplorationNodeUpdate(RequestModel):
    title: NonBlankStr | None = None
    status: ExplorationNodeStatus | None = None
    choice: NonBlankStr | None = None
    alternatives_considered: list[NonBlankStr] | None = None
    rationale: NonBlankStr | None = None
    evidence_refs: list[EntityRef] | None = None
    hypothesis: NonBlankStr | None = None
    failure_mode: NonBlankStr | None = None
    lesson: NonBlankStr | None = None
    tooling_context: NonBlankStr | None = None
    trigger: NonBlankStr | None = None
    invalidates_node_id: UUID | None = None
    invalidates_claim_id: UUID | None = None
    parent_node_ids: list[UUID] | None = None
    also_depends_on_node_ids: list[UUID] | None = None

    @field_validator("parent_node_ids", "also_depends_on_node_ids")
    @classmethod
    def _node_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


ExplorationNodeRead = ExplorationNode


class ProvenanceLinkStatusUpdate(RequestModel):
    status: ProvenanceLinkStatus

    @field_validator("status")
    @classmethod
    def _accept_or_reject_only(cls, value: ProvenanceLinkStatus) -> ProvenanceLinkStatus:
        if value not in {ProvenanceLinkStatus.ACCEPTED, ProvenanceLinkStatus.REJECTED}:
            raise ValueError("status must be 'accepted' or 'rejected'.")
        return value


ProvenanceLinkRead = ProvenanceLink


class GoalCreateFields(RequestModel):
    goal_type: GoalType
    title: NonBlankStr
    summary: str | None = None
    status: GoalStatus | None = None
    target_date: date | None = None
    external_ref: str | None = None
    attributes: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _attributes_match_goal_type(self) -> GoalCreateFields:
        self.attributes = validate_goal_attributes(self.goal_type, self.attributes)
        return self


class GoalLinkCreate(RequestModel):
    entity_type: EntityType
    entity_id: UUID
    relation: GoalRelation
    link_status: GoalLinkStatus | None = None
    slot: str | None = Field(default=None, max_length=120)


class GoalCreate(GoalCreateFields):
    project_id: UUID | None = None
    links: list[GoalLinkCreate] | None = None


class GoalUpdate(RequestModel):
    goal_type: GoalType | None = None
    title: NonBlankStr | None = None
    summary: str | None = None
    status: GoalStatus | None = None
    target_date: date | None = None
    external_ref: str | None = None
    attributes: dict[str, Any] | None = None
    links: list[GoalLinkCreate] | None = None

    @model_validator(mode="after")
    def _attributes_match_goal_type(self) -> GoalUpdate:
        if self.goal_type is not None and self.attributes is not None:
            self.attributes = validate_goal_attributes(self.goal_type, self.attributes)
        return self


class GoalLinkUpdate(RequestModel):
    relation: GoalRelation | None = None
    link_status: GoalLinkStatus | None = None
    slot: str | None = Field(default=None, max_length=120)


GoalRead = Goal
GoalLinkRead = GoalLink


class DataStoreCreate(RequestModel):
    project_id: UUID | None = None
    group_id: UUID | None = None
    name: NonBlankStr
    kind: StoreKind
    root: NonBlankStr
    capabilities: list[StoreCapability] | None = None
    endpoint: str | None = None
    credential_ref: str | None = None
    is_default: bool = False

    @model_validator(mode="after")
    def _exactly_one_scope(self) -> DataStoreCreate:
        if (self.project_id is None) == (self.group_id is None):
            raise ValueError("Provide exactly one of project_id or group_id.")
        return self


DataStoreRead = DataStore


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


class EvidenceBundleUploadIntent(RequestModel):
    checksum_sha256: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{64}$")]
    size_bytes: int = Field(..., gt=0)
    filename: NonBlankStr
    content_type: NonBlankStr

    @field_validator("checksum_sha256")
    @classmethod
    def _normalize_checksum(cls, value: str) -> str:
        return value.lower()


class EvidenceBundleExistingDataset(RequestModel):
    kind: Literal["existing"]
    dataset_id: UUID


class EvidenceBundleCreateDataset(RequestModel):
    kind: Literal["create"]
    primary_question_id: UUID | None = None
    secondary_question_ids: list[UUID] | None = None
    commit_manifest: DatasetCommitManifestInput | None = None
    commit_hash: str | None = None
    status: DatasetStatus = DatasetStatus.STAGED
    terminal_reason: NonBlankStr | None = None

    @field_validator("secondary_question_ids")
    @classmethod
    def _secondary_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


EvidenceBundleDataset = Annotated[
    EvidenceBundleExistingDataset | EvidenceBundleCreateDataset,
    Field(discriminator="kind"),
]


class EvidenceBundleExistingAnalysis(RequestModel):
    kind: Literal["existing"]
    analysis_id: UUID


class EvidenceBundleCreateAnalysis(RequestModel):
    kind: Literal["create"]
    dataset_ids: list[UUID] | None = None
    method_hash: NonBlankStr
    code_version: NonBlankStr
    environment_hash: str | None = None
    external_artifacts: list[ExternalArtifactReference] | None = None
    status: AnalysisStatus = AnalysisStatus.STAGED
    terminal_reason: NonBlankStr | None = None
    derive_code_provenance: bool = False

    @field_validator("dataset_ids")
    @classmethod
    def _analysis_dataset_ids_unique(
        cls,
        value: list[UUID] | None,
    ) -> list[UUID] | None:
        return _unique_uuid_list(value)


EvidenceBundleAnalysis = Annotated[
    EvidenceBundleExistingAnalysis | EvidenceBundleCreateAnalysis,
    Field(discriminator="kind"),
]


class EvidenceBundleExistingClaim(RequestModel):
    kind: Literal["existing"]
    claim_id: UUID


class EvidenceBundleCreateClaim(RequestModel):
    kind: Literal["create"]
    statement: NonBlankStr
    confidence: ClaimConfidence
    status: ClaimStatus = ClaimStatus.PROPOSED
    terminal_reason: NonBlankStr | None = None
    falsification_criteria: NonBlankStr | None = None
    verification_plan: NonBlankStr | None = None
    refuting_outcome: NonBlankStr | None = None
    supported_by_dataset_ids: list[UUID] | None = None
    supported_by_analysis_ids: list[UUID] | None = None
    answers_question_ids: list[UUID] | None = None
    external_citations: list[ExternalArtifactReference] | None = None

    @field_validator(
        "supported_by_dataset_ids",
        "supported_by_analysis_ids",
        "answers_question_ids",
    )
    @classmethod
    def _claim_link_ids_unique(cls, value: list[UUID] | None) -> list[UUID] | None:
        return _unique_uuid_list(value)


EvidenceBundleClaim = Annotated[
    EvidenceBundleExistingClaim | EvidenceBundleCreateClaim,
    Field(discriminator="kind"),
]


class EvidenceBundleExistingVisualization(RequestModel):
    kind: Literal["existing"]
    viz_id: UUID
    upload_intent: EvidenceBundleUploadIntent | None = None


class EvidenceBundleCreateVisualization(RequestModel):
    kind: Literal["create"]
    analysis_id: UUID | None = None
    viz_type: NonBlankStr
    file_path: NonBlankStr
    caption: str | None = None
    related_claim_ids: list[UUID] | None = None
    upload_intent: EvidenceBundleUploadIntent | None = None

    @field_validator("related_claim_ids")
    @classmethod
    def _bundle_claim_ids_unique(
        cls,
        value: list[UUID] | None,
    ) -> list[UUID] | None:
        return _unique_uuid_list(value)


EvidenceBundleVisualization = Annotated[
    EvidenceBundleExistingVisualization | EvidenceBundleCreateVisualization,
    Field(discriminator="kind"),
]


class EvidenceBundleExistingSourceNote(RequestModel):
    kind: Literal["existing"]
    note_id: UUID


class EvidenceBundleCreateSourceNote(RequestModel):
    kind: Literal["create"]
    raw_content: NonBlankStr
    transcribed_text: str | None = None
    targets: list[EntityRef] | None = None
    metadata: dict[str, NoteMetadataScalar] | None = None
    status: NoteStatus = NoteStatus.STAGED

    @field_validator("metadata")
    @classmethod
    def _bundle_note_metadata_normalized(
        cls,
        value: dict[str, NoteMetadataScalar] | None,
    ) -> dict[str, str] | None:
        return _normalize_note_metadata_for_request(value)


EvidenceBundleSourceNote = Annotated[
    EvidenceBundleExistingSourceNote | EvidenceBundleCreateSourceNote,
    Field(discriminator="kind"),
]


class EvidenceBundleRequest(RequestModel):
    project_id: UUID
    primary_question_id: UUID | None = None
    dataset: EvidenceBundleDataset | SkipJsonSchema[None] = None
    analysis: EvidenceBundleAnalysis | SkipJsonSchema[None] = None
    claim: EvidenceBundleClaim | SkipJsonSchema[None] = None
    visualization: EvidenceBundleVisualization | SkipJsonSchema[None] = None
    source_note: EvidenceBundleSourceNote | SkipJsonSchema[None] = None
    dry_run: bool = True
    idempotency_key: Annotated[
        str,
        Field(min_length=1, max_length=200),
        AfterValidator(_non_blank_string),
    ] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null_components(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        for field_name in ("dataset", "analysis", "claim", "visualization", "source_note"):
            if field_name in value and value[field_name] is None:
                raise ValueError(f"{field_name} must be omitted rather than null")
        return value

    @model_validator(mode="after")
    def _validate_bundle_boundary(self) -> EvidenceBundleRequest:
        if not any(
            component is not None
            for component in (
                self.dataset,
                self.analysis,
                self.claim,
                self.visualization,
                self.source_note,
            )
        ):
            raise ValueError("At least one evidence-bundle component is required.")
        if not self.dry_run and self.idempotency_key is None:
            raise ValueError("idempotency_key is required when dry_run is false.")
        return self


class EvidenceBundleComponentIds(BaseModel):
    dataset_id: UUID | None = None
    analysis_id: UUID | None = None
    claim_id: UUID | None = None
    visualization_id: UUID | None = None
    source_note_id: UUID | None = None


class EvidenceBundlePlanStep(BaseModel):
    action: Literal["create", "reuse"]
    entity_type: Literal["dataset", "analysis", "claim", "visualization", "source_note"]
    entity_id: UUID | None = None
    reason: str | None = None
    details: dict[str, Any] | None = None


class EvidenceBundleResultRead(BaseModel):
    outcome: Literal["preview", "created", "reused"]
    dry_run: bool
    project_id: UUID
    idempotency_key: str | None = None
    component_ids: EvidenceBundleComponentIds
    plan: list[EvidenceBundlePlanStep]
    warnings: list[str] = Field(default_factory=list)


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


class PortfolioProjectOwner(BaseModel):
    user_id: UUID
    username: str | None = None


class PortfolioTriageFlag(BaseModel):
    key: Literal[
        "stale_project",
        "unanswered_questions",
        "datasets_without_analyses",
        "analyses_without_claims",
        "unreviewed_claims",
        "overdue_goals",
    ]
    label: str
    count: int = Field(..., ge=0)
    severity: Literal["info", "warning", "critical"] = "warning"


class PortfolioProjectSummary(BaseModel):
    project_id: UUID
    name: str
    status: ProjectStatus
    open_question_count: int = Field(..., ge=0)
    draft_dataset_count: int = Field(..., ge=0)
    committed_dataset_count: int = Field(..., ge=0)
    staged_analysis_count: int = Field(..., ge=0)
    unreviewed_claim_count: int = Field(..., ge=0)
    last_activity_at: datetime | None = None
    owners: list[PortfolioProjectOwner] = Field(default_factory=list)
    triage_flags: list[PortfolioTriageFlag] = Field(default_factory=list)


class PortfolioProjectGroupSummary(BaseModel):
    project_group: ProjectGroup | None = None
    project_count: int = Field(..., ge=0)
    projects: list[PortfolioProjectSummary] = Field(default_factory=list)


class AnalysisCommitRequest(RequestModel):
    environment_hash: str | None = None
    external_artifacts: list[ExternalArtifactReference] | None = None
    claims: list[ClaimInput] | None = None
    visualizations: list[VisualizationInput] | None = None


class AnalysisCommitResult(BaseModel):
    analysis: Analysis
    claims: list[Claim]
    visualizations: list[Visualization]
