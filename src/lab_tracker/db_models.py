"""SQLAlchemy models for lab tracker."""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from lab_tracker.db import Base
from lab_tracker.db_types import GUID, EnumType, UtcDateTime
from lab_tracker.models import (
    AcceptanceMode,
    AnalysisStatus,
    ClaimRelation,
    ClaimStatus,
    DatasetStatus,
    EntityType,
    ExplorationNodeStatus,
    ExplorationNodeType,
    GoalLinkStatus,
    GoalRelation,
    GoalStatus,
    GoalType,
    NoteStatus,
    OutcomeStatus,
    ProjectGroupKind,
    ProjectMembershipRole,
    ProjectStatus,
    ProvenanceLinkBasis,
    ProvenanceLinkOrigin,
    ProvenanceLinkRelation,
    ProvenanceLinkStatus,
    QuestionLinkRole,
    QuestionStatus,
    QuestionType,
    ReviewEmailDeliveryStatus,
    SessionStatus,
    SessionType,
    StoreKind,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_STORE_AUTHORITY_GRANT_ID_COLUMN = sa.column("authority_grant_id", String())
_STORE_AUTHORITY_FINGERPRINT_COLUMN = sa.column(
    "authority_grant_fingerprint",
    String(),
)
_STORE_AUTHORITY_GRANT_ID_FIRST_CHARACTER_PATTERN = (
    r"[ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789]"
)
_STORE_AUTHORITY_GRANT_ID_INVALID_CHARACTER_PATTERN = (
    r"[^ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]"
)
_DATA_STORE_AUTHORITY_GRANT_ID_FORMAT_EXPRESSION = sa.or_(
    _STORE_AUTHORITY_GRANT_ID_COLUMN.is_(None),
    sa.and_(
        sa.func.length(_STORE_AUTHORITY_GRANT_ID_COLUMN).between(1, 128),
        sa.func.substr(
            _STORE_AUTHORITY_GRANT_ID_COLUMN,
            1,
            1,
        ).regexp_match(_STORE_AUTHORITY_GRANT_ID_FIRST_CHARACTER_PATTERN),
        sa.not_(
            _STORE_AUTHORITY_GRANT_ID_COLUMN.regexp_match(
                _STORE_AUTHORITY_GRANT_ID_INVALID_CHARACTER_PATTERN
            )
        ),
    ),
)
_DATA_STORE_AUTHORITY_FINGERPRINT_FORMAT_EXPRESSION = sa.or_(
    _STORE_AUTHORITY_FINGERPRINT_COLUMN.is_(None),
    sa.and_(
        sa.func.length(_STORE_AUTHORITY_FINGERPRINT_COLUMN) == 78,
        sa.func.substr(_STORE_AUTHORITY_FINGERPRINT_COLUMN, 1, 14)
        == "sag-v1-sha256:",
        sa.not_(
            sa.func.substr(
                _STORE_AUTHORITY_FINGERPRINT_COLUMN,
                15,
            ).regexp_match(r"[^0123456789abcdef]")
        ),
    ),
)


class ProjectGroupModel(Base):
    __tablename__ = "project_groups"

    group_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="")
    kind: Mapped[ProjectGroupKind] = mapped_column(
        EnumType(ProjectGroupKind, length=30), nullable=False, default="lab"
    )
    group_read_all: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class ProjectModel(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint(
            "created_by",
            "client_capture_id",
            name="uq_projects_creator_client_capture",
        ),
        CheckConstraint(
            "client_capture_id IS NULL OR "
            "(created_by IS NOT NULL AND TRIM(created_by) <> '')",
            name="ck_projects_client_capture_creator",
        ),
    )

    project_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    group_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("project_groups.group_id", ondelete="SET NULL"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[ProjectStatus] = mapped_column(
        EnumType(ProjectStatus, length=20), default="active"
    )
    client_capture_id: Mapped[str | None] = mapped_column(String(120))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class QuestionModel(Base):
    __tablename__ = "questions"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "client_capture_id",
            name="uq_questions_project_client_capture",
        ),
    )

    question_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(
        EnumType(QuestionType, length=40), nullable=False
    )
    hypothesis: Mapped[str | None] = mapped_column(Text)
    status: Mapped[QuestionStatus] = mapped_column(
        EnumType(QuestionStatus, length=20), default="staged"
    )
    client_capture_id: Mapped[str | None] = mapped_column(String(120))
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    superseded_by_question_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="SET NULL"),
    )
    supersedes_question_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="SET NULL"),
    )
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class QuestionParentModel(Base):
    __tablename__ = "question_parents"

    question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )


class QuestionRefactorModel(Base):
    __tablename__ = "question_refactors"

    refactor_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
    )
    replacement_question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    replacement_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    relationship_changes: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class DatasetModel(Base):
    __tablename__ = "datasets"

    dataset_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    commit_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    primary_question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id"),
        nullable=False,
    )
    manifest_files: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    manifest_external_artifacts: Mapped[list[dict[str, object]]] = mapped_column(
        JSON,
        default=list,
    )
    manifest_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_nwb_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_bids_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_note_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    manifest_source_session_id: Mapped[UUID | None] = mapped_column(GUID)
    status: Mapped[DatasetStatus] = mapped_column(
        EnumType(DatasetStatus, length=20), default="staged"
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class DatasetQuestionLinkModel(Base):
    __tablename__ = "dataset_question_links"

    dataset_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )
    question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[QuestionLinkRole] = mapped_column(
        EnumType(QuestionLinkRole, length=20), nullable=False
    )
    outcome_status: Mapped[OutcomeStatus] = mapped_column(
        EnumType(OutcomeStatus, length=20), default="unknown"
    )


class DatasetFileModel(Base):
    __tablename__ = "dataset_files"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "path",
            name="uq_dataset_files_dataset_path",
        ),
    )

    file_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    dataset_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Opaque storage-backend key (not a UUID; the domain DatasetFile has no
    # storage_id field), so it stays a plain String rather than GUID.
    storage_id: Mapped[str] = mapped_column(String(36), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class NoteModel(Base):
    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "client_capture_id",
            name="uq_notes_project_client_capture",
        ),
    )

    note_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    raw_storage_id: Mapped[UUID | None] = mapped_column(GUID)
    raw_filename: Mapped[str | None] = mapped_column(String(255))
    raw_content_type: Mapped[str | None] = mapped_column(String(255))
    raw_size_bytes: Mapped[int | None] = mapped_column(Integer)
    raw_checksum: Mapped[str | None] = mapped_column(String(64))
    transcribed_text: Mapped[str | None] = mapped_column(Text)
    note_metadata: Mapped[dict[str, str]] = mapped_column("metadata", JSON, default=dict)
    client_capture_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[NoteStatus] = mapped_column(EnumType(NoteStatus, length=20), default="staged")
    archived_reason: Mapped[str | None] = mapped_column(String(32))
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    archived_by: Mapped[str | None] = mapped_column(String(255))
    archived_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey(
            "graph_change_sets.change_set_id",
            ondelete="SET NULL",
            name="fk_notes_change_set_id_graph_change_sets",
            use_alter=True,
        ),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class NoteTargetModel(Base):
    __tablename__ = "note_targets"

    note_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_type: Mapped[EntityType] = mapped_column(
        EnumType(EntityType, length=30), primary_key=True
    )
    entity_id: Mapped[UUID] = mapped_column(GUID, primary_key=True)


class GraphChangeSetModel(Base):
    __tablename__ = "graph_change_sets"
    __table_args__ = (UniqueConstraint("batch_key", name="uq_graph_change_sets_batch_key"),)

    change_set_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_note_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_note_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_checksum: Mapped[str | None] = mapped_column(String(64))
    source_content_type: Mapped[str | None] = mapped_column(String(255))
    source_filename: Mapped[str | None] = mapped_column(String(255))
    batch_key: Mapped[str | None] = mapped_column(String(120))
    batch_window_start: Mapped[datetime | None] = mapped_column(UtcDateTime)
    batch_window_end: Mapped[datetime | None] = mapped_column(UtcDateTime)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    draft_mode: Mapped[str] = mapped_column(String(30), nullable=False, default="graph_context")
    context_packet: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    uncertain_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    clarification_requests: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="drafting")
    commit_message: Mapped[str | None] = mapped_column(Text)
    error_metadata: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    review_assignee: Mapped[str | None] = mapped_column(String(255))
    review_assignee_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )
    committed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    committed_by: Mapped[str | None] = mapped_column(String(255))
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    submitted_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    review_note: Mapped[str | None] = mapped_column(Text)


class GraphChangeOperationModel(Base):
    __tablename__ = "graph_change_operations"
    __table_args__ = (
        UniqueConstraint(
            "change_set_id",
            "sequence",
            name="uq_graph_change_operations_change_set_sequence",
        ),
    )

    operation_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    change_set_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    op: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    semantic_type: Mapped[str | None] = mapped_column(String(40))
    target_entity_id: Mapped[UUID | None] = mapped_column(GUID)
    client_ref: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float)
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    review_note: Mapped[str | None] = mapped_column(Text)
    acceptance_mode: Mapped[str | None] = mapped_column(String(20))
    accepted_by: Mapped[str | None] = mapped_column(String(255))
    accepted_by_user_id: Mapped[UUID | None] = mapped_column(GUID)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    result_entity_id: Mapped[UUID | None] = mapped_column(GUID)
    error_metadata: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class EntityVersionModel(Base):
    __tablename__ = "entity_versions"
    __table_args__ = (
        UniqueConstraint(
            "entity_type",
            "entity_id",
            "version_number",
            name="uq_entity_versions_entity_number",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    entity_type: Mapped[EntityType] = mapped_column(EnumType(EntityType, length=30), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    committed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )


class GraphDraftBatchSettingsModel(Base):
    __tablename__ = "graph_draft_batch_settings"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "user_id",
            name="uq_graph_draft_batch_settings_project_user",
        ),
    )

    settings_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="CASCADE"),
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cadence_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=24 * 60)
    run_at_local_time: Mapped[str] = mapped_column(String(5), nullable=False, default="18:00")
    timezone_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="America/New_York",
    )
    next_run_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    email_notifications_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    notification_email: Mapped[str | None] = mapped_column(String(320))
    notification_email_confirmed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )
    updated_by: Mapped[str | None] = mapped_column(String(255))


class ReviewEmailOutboxModel(Base):
    __tablename__ = "review_email_outbox"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_review_email_outbox_idempotency_key",
        ),
        CheckConstraint(
            "event_type IN ('review_ready', 'test')",
            name="ck_review_email_outbox_event_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'retryable', 'accepted', 'failed')",
            name="ck_review_email_outbox_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_review_email_outbox_attempt_count",
        ),
        CheckConstraint(
            "TRIM(destination_email) <> ''",
            name="ck_review_email_outbox_destination_email",
        ),
        CheckConstraint(
            "TRIM(idempotency_key) <> ''",
            name="ck_review_email_outbox_idempotency_key",
        ),
        CheckConstraint(
            "(status = 'accepted' AND accepted_at IS NOT NULL) "
            "OR (status <> 'accepted' AND accepted_at IS NULL)",
            name="ck_review_email_outbox_accepted_at",
        ),
    )

    delivery_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    recipient_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    destination_email: Mapped[str] = mapped_column(String(320), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[ReviewEmailDeliveryStatus] = mapped_column(
        EnumType(ReviewEmailDeliveryStatus, length=20),
        nullable=False,
        default=ReviewEmailDeliveryStatus.PENDING,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime,
        nullable=True,
        default=_utc_now,
    )
    claim_token: Mapped[UUID | None] = mapped_column(GUID)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class GraphDraftBatchRunModel(Base):
    __tablename__ = "graph_draft_batch_runs"
    __table_args__ = (UniqueConstraint("batch_key", name="uq_graph_draft_batch_runs_batch_key"),)

    run_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    window_start: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    window_end: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    note_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_note_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    batch_key: Mapped[str] = mapped_column(String(120), nullable=False)
    user_hint: Mapped[str | None] = mapped_column(Text)
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    error_metadata: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    review_assignee: Mapped[str | None] = mapped_column(String(255))
    review_assignee_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class SessionModel(Base):
    __tablename__ = "sessions"

    session_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_type: Mapped[SessionType] = mapped_column(
        EnumType(SessionType, length=30), nullable=False
    )
    status: Mapped[SessionStatus] = mapped_column(
        EnumType(SessionStatus, length=20), default=SessionStatus.ACTIVE
    )
    primary_question_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("questions.question_id"),
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class AcquisitionOutputModel(Base):
    __tablename__ = "acquisition_outputs"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "file_path",
            name="uq_acquisition_outputs_session_path",
        ),
    )

    output_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    session_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class AnalysisModel(Base):
    __tablename__ = "analyses"

    analysis_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    method_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    code_version: Mapped[str] = mapped_column(String(255), nullable=False)
    environment_hash: Mapped[str | None] = mapped_column(String(255))
    external_artifacts: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    executed_by: Mapped[str | None] = mapped_column(String(255))
    executed_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    executed_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    status: Mapped[AnalysisStatus] = mapped_column(
        EnumType(AnalysisStatus, length=20), default="staged"
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class AnalysisDatasetModel(Base):
    __tablename__ = "analysis_datasets"

    analysis_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        primary_key=True,
    )
    dataset_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ClaimModel(Base):
    __tablename__ = "claims"
    __table_args__ = (
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_claims_confidence_range",
        ),
    )

    claim_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[ClaimStatus] = mapped_column(
        EnumType(ClaimStatus, length=20), default="proposed"
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    falsification_criteria: Mapped[str | None] = mapped_column(Text)
    verification_plan: Mapped[str | None] = mapped_column(Text)
    refuting_outcome: Mapped[str | None] = mapped_column(Text)
    external_citations: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class ClaimDatasetModel(Base):
    __tablename__ = "claim_datasets"

    claim_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    dataset_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ClaimAnalysisModel(Base):
    __tablename__ = "claim_analyses"

    claim_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    analysis_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ClaimQuestionModel(Base):
    __tablename__ = "claim_questions"

    claim_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    question_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ClaimEdgeModel(Base):
    __tablename__ = "claim_edges"
    __table_args__ = (
        UniqueConstraint(
            "claim_id",
            "target_claim_id",
            "relation",
            name="uq_claim_edges_claim_target_relation",
        ),
    )

    edge_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    claim_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_claim_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        nullable=False,
    )
    relation: Mapped[ClaimRelation] = mapped_column(
        EnumType(ClaimRelation, length=32), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class ProvenanceLinkModel(Base):
    __tablename__ = "provenance_links"
    __table_args__ = (
        UniqueConstraint(
            "source_entity_id",
            "target_entity_id",
            "relation",
            name="uq_provenance_links_source_target_relation",
        ),
        Index("ix_provenance_links_project_status", "project_id", "status"),
    )

    link_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_entity_type: Mapped[EntityType] = mapped_column(
        EnumType(EntityType, length=40), nullable=False
    )
    source_entity_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    target_entity_type: Mapped[EntityType] = mapped_column(
        EnumType(EntityType, length=40), nullable=False
    )
    target_entity_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    relation: Mapped[ProvenanceLinkRelation] = mapped_column(
        EnumType(ProvenanceLinkRelation, length=32), nullable=False
    )
    basis: Mapped[ProvenanceLinkBasis] = mapped_column(
        EnumType(ProvenanceLinkBasis, length=32), nullable=False
    )
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[ProvenanceLinkStatus] = mapped_column(
        EnumType(ProvenanceLinkStatus, length=20), nullable=False, default="proposed"
    )
    origin: Mapped[ProvenanceLinkOrigin] = mapped_column(
        EnumType(ProvenanceLinkOrigin, length=32), nullable=False, default="system_detected"
    )
    acceptance_mode: Mapped[AcceptanceMode | None] = mapped_column(
        EnumType(AcceptanceMode, length=20)
    )
    accepted_by: Mapped[str | None] = mapped_column(String(255))
    accepted_by_user_id: Mapped[UUID | None] = mapped_column(GUID)
    accepted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(GUID)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class ExplorationNodeModel(Base):
    __tablename__ = "exploration_nodes"

    node_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    node_type: Mapped[ExplorationNodeType] = mapped_column(
        EnumType(ExplorationNodeType, length=32), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    target_entity_type: Mapped[EntityType] = mapped_column(
        EnumType(EntityType, length=40), nullable=False
    )
    target_entity_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    status: Mapped[ExplorationNodeStatus] = mapped_column(
        EnumType(ExplorationNodeStatus, length=20), nullable=False, default="staged"
    )
    choice: Mapped[str | None] = mapped_column(Text)
    alternatives_considered: Mapped[list[str]] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    hypothesis: Mapped[str | None] = mapped_column(Text)
    failure_mode: Mapped[str | None] = mapped_column(Text)
    lesson: Mapped[str | None] = mapped_column(Text)
    tooling_context: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str | None] = mapped_column(Text)
    invalidates_node_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("exploration_nodes.node_id", ondelete="SET NULL"),
    )
    invalidates_claim_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="SET NULL"),
    )
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class ExplorationNodeEdgeModel(Base):
    __tablename__ = "exploration_node_edges"
    __table_args__ = (
        UniqueConstraint(
            "source_node_id",
            "target_node_id",
            "relation",
            name="uq_exploration_node_edges_source_target_relation",
        ),
    )

    edge_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    source_node_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("exploration_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_node_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("exploration_nodes.node_id", ondelete="CASCADE"),
        nullable=False,
    )
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class DataStoreModel(Base):
    __tablename__ = "data_stores"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_data_stores_project_name"),
        UniqueConstraint("group_id", "name", name="uq_data_stores_group_name"),
        CheckConstraint(
            "((project_id IS NOT NULL AND group_id IS NULL) OR "
            "(project_id IS NULL AND group_id IS NOT NULL))",
            name="ck_data_stores_scope_xor",
        ),
        CheckConstraint(
            "((authority_grant_id IS NULL AND authority_grant_fingerprint IS NULL) OR "
            "(authority_grant_id IS NOT NULL AND "
            "authority_grant_fingerprint IS NOT NULL))",
            name="ck_data_stores_authority_binding_pair",
        ),
        CheckConstraint(
            _DATA_STORE_AUTHORITY_GRANT_ID_FORMAT_EXPRESSION,
            name="ck_data_stores_authority_grant_id_format",
        ),
        CheckConstraint(
            _DATA_STORE_AUTHORITY_FINGERPRINT_FORMAT_EXPRESSION,
            name="ck_data_stores_authority_fingerprint_format",
        ),
        Index("ix_data_stores_project_default", "project_id", "is_default"),
        Index("ix_data_stores_group_default", "group_id", "is_default"),
    )

    store_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
    )
    group_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("project_groups.group_id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[StoreKind] = mapped_column(EnumType(StoreKind, length=40), nullable=False)
    capabilities: Mapped[list[object]] = mapped_column(JSON, default=list)
    root: Mapped[str] = mapped_column(String(2000), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(2000))
    credential_ref: Mapped[str | None] = mapped_column(String(255))
    authority_grant_id: Mapped[str | None] = mapped_column(String(128))
    authority_grant_fingerprint: Mapped[str | None] = mapped_column(String(78))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class EvidenceBundleModel(Base):
    __tablename__ = "evidence_bundles"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "created_by",
            "idempotency_key",
            name="uq_evidence_bundles_project_creator_key",
        ),
    )

    bundle_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=_utc_now,
    )


class GoalModel(Base):
    __tablename__ = "goals"

    goal_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
    )
    goal_type: Mapped[GoalType] = mapped_column(EnumType(GoalType, length=40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[GoalStatus] = mapped_column(EnumType(GoalStatus, length=20), default="planned")
    target_date: Mapped[date | None] = mapped_column(Date)
    external_ref: Mapped[str | None] = mapped_column(String(1000))
    attributes: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class GoalLinkModel(Base):
    __tablename__ = "goal_links"
    __table_args__ = (
        UniqueConstraint(
            "goal_id",
            "entity_type",
            "entity_id",
            "relation",
            "slot",
            name="uq_goal_links_goal_entity_relation_slot",
        ),
    )

    link_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    goal_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("goals.goal_id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_type: Mapped[EntityType] = mapped_column(EnumType(EntityType, length=30), nullable=False)
    entity_id: Mapped[UUID] = mapped_column(GUID, nullable=False)
    relation: Mapped[GoalRelation] = mapped_column(
        EnumType(GoalRelation, length=40), nullable=False
    )
    link_status: Mapped[GoalLinkStatus] = mapped_column(
        EnumType(GoalLinkStatus, length=20), default="candidate"
    )
    slot: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class VisualizationModel(Base):
    __tablename__ = "visualizations"

    viz_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    analysis_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        nullable=False,
    )
    viz_type: Mapped[str] = mapped_column(String(40), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    asset_storage_id: Mapped[UUID | None] = mapped_column(GUID)
    asset_filename: Mapped[str | None] = mapped_column(String(255))
    asset_content_type: Mapped[str | None] = mapped_column(String(255))
    asset_size_bytes: Mapped[int | None] = mapped_column(Integer)
    asset_checksum: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    change_set_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("graph_change_sets.change_set_id", ondelete="SET NULL"),
    )
    origin_provider: Mapped[str | None] = mapped_column(String(80))
    origin_model: Mapped[str | None] = mapped_column(String(255))
    origin_prompt_version: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class VisualizationClaimModel(Base):
    __tablename__ = "visualization_claims"

    viz_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("visualizations.viz_id", ondelete="CASCADE"),
        primary_key=True,
    )
    claim_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    user_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class InvitationModel(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
        Index("ix_invitations_email_created", "email", "created_at"),
        Index("ix_invitations_expires_at", "expires_at"),
    )

    invitation_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    consumed_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class PersonalAccessTokenModel(Base):
    __tablename__ = "personal_access_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_personal_access_tokens_token_hash"),
        Index("ix_personal_access_tokens_user_created", "user_id", "created_at"),
        Index("ix_personal_access_tokens_expires_at", "expires_at"),
    )

    token_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scope: Mapped[str] = mapped_column(
        String(40), nullable=False, default="all", server_default="all"
    )
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class SupervisionEdgeModel(Base):
    __tablename__ = "supervision_edges"

    edge_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    supervisor_user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    supervisee_user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class OwnershipReassignmentModel(Base):
    __tablename__ = "ownership_reassignments"

    reassignment_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    from_user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    to_user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, default="")
    record_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class RecordExportEventModel(Base):
    __tablename__ = "record_export_events"

    export_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    group_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("project_groups.group_id", ondelete="SET NULL"),
    )
    project_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    record_counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class UsageEventModel(Base):
    __tablename__ = "usage_events"

    event_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    verb: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column(GUID)
    actor_user_id: Mapped[UUID | None] = mapped_column(GUID)
    actor_role: Mapped[str | None] = mapped_column(String(30))
    principal_type: Mapped[str | None] = mapped_column(String(20))
    surface: Mapped[str | None] = mapped_column(String(20))
    project_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="SET NULL"),
    )
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result_count: Mapped[int | None] = mapped_column(Integer)


class UsageEventRollupModel(Base):
    __tablename__ = "usage_event_rollups"
    __table_args__ = (
        UniqueConstraint(
            "day",
            "verb",
            "resource_type",
            "project_id",
            "actor_role",
            "principal_type",
            "surface",
            "outcome",
            name="uq_usage_event_rollup_bucket",
        ),
    )

    rollup_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    verb: Mapped[str] = mapped_column(String(30), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(40), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="SET NULL"),
    )
    actor_role: Mapped[str | None] = mapped_column(String(30))
    principal_type: Mapped[str | None] = mapped_column(String(20))
    surface: Mapped[str | None] = mapped_column(String(20))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)


class ProjectMembershipModel(Base):
    __tablename__ = "project_memberships"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_memberships_project_user"),
    )

    membership_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    project_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[ProjectMembershipRole] = mapped_column(
        EnumType(ProjectMembershipRole, length=30), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class GroupMembershipModel(Base):
    __tablename__ = "group_memberships"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_memberships_group_user"),
    )

    membership_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    group_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("project_groups.group_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="SET NULL"),
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        default=_utc_now,
        onupdate=_utc_now,
    )


class DeviceTokenModel(Base):
    __tablename__ = "device_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_device_tokens_token_hash"),)

    device_token_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class DeviceEnrollmentModel(Base):
    __tablename__ = "device_enrollments"
    __table_args__ = (
        UniqueConstraint("offer_token_hash", name="uq_device_enrollments_offer_token_hash"),
    )

    enrollment_id: Mapped[UUID] = mapped_column(
        GUID,
        primary_key=True,
        default=uuid4,
    )
    user_id: Mapped[UUID] = mapped_column(
        GUID,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    offer_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=_utc_now)
    consumed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    consumed_device_token_id: Mapped[UUID | None] = mapped_column(
        GUID,
        ForeignKey("device_tokens.device_token_id", ondelete="SET NULL"),
        nullable=True,
    )


Index("ix_questions_project_created_at", QuestionModel.project_id, QuestionModel.created_at)
Index("ix_projects_group_id", ProjectModel.group_id)
Index("ix_project_groups_created_by_user_id", ProjectGroupModel.created_by_user_id)
Index("ix_projects_created_by_user_id", ProjectModel.created_by_user_id)
Index("ix_questions_created_by_user_id", QuestionModel.created_by_user_id)
Index("ix_question_refactors_created_by_user_id", QuestionRefactorModel.created_by_user_id)
Index("ix_datasets_created_by_user_id", DatasetModel.created_by_user_id)
Index("ix_notes_created_by_user_id", NoteModel.created_by_user_id)
Index("ix_graph_change_sets_created_by_user_id", GraphChangeSetModel.created_by_user_id)
Index("ix_graph_change_sets_review_assignee_user_id", GraphChangeSetModel.review_assignee_user_id)
Index("ix_graph_draft_batch_runs_created_by_user_id", GraphDraftBatchRunModel.created_by_user_id)
Index(
    "ix_graph_draft_batch_runs_review_assignee_user_id",
    GraphDraftBatchRunModel.review_assignee_user_id,
)
Index(
    "ix_graph_draft_batch_settings_project_user",
    GraphDraftBatchSettingsModel.project_id,
    GraphDraftBatchSettingsModel.user_id,
)
Index(
    "uq_graph_draft_batch_settings_project_default",
    GraphDraftBatchSettingsModel.project_id,
    unique=True,
    sqlite_where=GraphDraftBatchSettingsModel.user_id.is_(None),
    postgresql_where=GraphDraftBatchSettingsModel.user_id.is_(None),
)
Index(
    "ix_review_email_outbox_claim",
    ReviewEmailOutboxModel.status,
    ReviewEmailOutboxModel.next_attempt_at,
    ReviewEmailOutboxModel.lease_expires_at,
    ReviewEmailOutboxModel.created_at,
)
Index(
    "ix_review_email_outbox_change_set_id",
    ReviewEmailOutboxModel.change_set_id,
)
Index(
    "ix_review_email_outbox_recipient_user_id",
    ReviewEmailOutboxModel.recipient_user_id,
)
Index("ix_sessions_created_by_user_id", SessionModel.created_by_user_id)
Index("ix_analyses_executed_by_user_id", AnalysisModel.executed_by_user_id)
Index("ix_claims_created_by_user_id", ClaimModel.created_by_user_id)
Index("ix_exploration_nodes_created_by_user_id", ExplorationNodeModel.created_by_user_id)
Index("ix_goals_created_by_user_id", GoalModel.created_by_user_id)
Index("ix_goal_links_created_by_user_id", GoalLinkModel.created_by_user_id)
Index("ix_visualizations_created_by_user_id", VisualizationModel.created_by_user_id)
Index("ix_project_memberships_created_by_user_id", ProjectMembershipModel.created_by_user_id)
Index("ix_group_memberships_created_by_user_id", GroupMembershipModel.created_by_user_id)
Index("ix_questions_change_set_id", QuestionModel.change_set_id)
Index("ix_datasets_change_set_id", DatasetModel.change_set_id)
Index("ix_notes_change_set_id", NoteModel.change_set_id)
Index("ix_sessions_change_set_id", SessionModel.change_set_id)
Index("ix_analyses_change_set_id", AnalysisModel.change_set_id)
Index("ix_claims_change_set_id", ClaimModel.change_set_id)
Index("ix_exploration_nodes_change_set_id", ExplorationNodeModel.change_set_id)
Index("ix_goals_change_set_id", GoalModel.change_set_id)
Index("ix_visualizations_change_set_id", VisualizationModel.change_set_id)
Index(
    "ix_questions_superseded_by_question_id",
    QuestionModel.superseded_by_question_id,
)
Index(
    "ix_questions_supersedes_question_id",
    QuestionModel.supersedes_question_id,
)
Index(
    "ix_question_refactors_source_created_at",
    QuestionRefactorModel.source_question_id,
    QuestionRefactorModel.created_at,
)
Index(
    "ix_question_refactors_replacement_created_at",
    QuestionRefactorModel.replacement_question_id,
    QuestionRefactorModel.created_at,
)
Index("ix_datasets_project_created_at", DatasetModel.project_id, DatasetModel.created_at)
Index("ix_notes_project_created_at", NoteModel.project_id, NoteModel.created_at)
Index(
    "ix_graph_change_sets_project_created_at",
    GraphChangeSetModel.project_id,
    GraphChangeSetModel.created_at,
)
Index(
    "ix_graph_change_sets_note_created_at",
    GraphChangeSetModel.source_note_id,
    GraphChangeSetModel.created_at,
)
Index(
    "ix_graph_change_operations_change_set_sequence",
    GraphChangeOperationModel.change_set_id,
    GraphChangeOperationModel.sequence,
)
Index(
    "ix_entity_versions_entity_created_at",
    EntityVersionModel.entity_type,
    EntityVersionModel.entity_id,
    EntityVersionModel.created_at,
)
Index(
    "ix_entity_versions_change_set_id",
    EntityVersionModel.change_set_id,
)
Index(
    "ix_entity_versions_created_by_user_id",
    EntityVersionModel.created_by_user_id,
)
Index(
    "ix_project_memberships_user_project",
    ProjectMembershipModel.user_id,
    ProjectMembershipModel.project_id,
)
Index(
    "ix_project_memberships_project_role",
    ProjectMembershipModel.project_id,
    ProjectMembershipModel.role,
)
Index(
    "ix_group_memberships_user_group",
    GroupMembershipModel.user_id,
    GroupMembershipModel.group_id,
)
Index(
    "ix_group_memberships_group_role",
    GroupMembershipModel.group_id,
    GroupMembershipModel.role,
)
Index(
    "ix_supervision_edges_supervisor_started",
    SupervisionEdgeModel.supervisor_user_id,
    SupervisionEdgeModel.started_at,
)
Index(
    "ix_supervision_edges_supervisee_started",
    SupervisionEdgeModel.supervisee_user_id,
    SupervisionEdgeModel.started_at,
)
Index(
    "ix_ownership_reassignments_from_created",
    OwnershipReassignmentModel.from_user_id,
    OwnershipReassignmentModel.created_at,
)
Index(
    "ix_ownership_reassignments_to_created",
    OwnershipReassignmentModel.to_user_id,
    OwnershipReassignmentModel.created_at,
)
Index(
    "ix_ownership_reassignments_created_by_user_id",
    OwnershipReassignmentModel.created_by_user_id,
)
Index(
    "ix_record_export_events_user_created",
    RecordExportEventModel.user_id,
    RecordExportEventModel.created_at,
)
Index(
    "ix_record_export_events_group_created",
    RecordExportEventModel.group_id,
    RecordExportEventModel.created_at,
)
Index(
    "ix_record_export_events_created_by_user_id",
    RecordExportEventModel.created_by_user_id,
)
Index("ix_usage_events_occurred_at", UsageEventModel.occurred_at)
Index("ix_usage_events_verb", UsageEventModel.verb)
Index("ix_usage_events_resource_type", UsageEventModel.resource_type)
Index("ix_usage_events_project_id", UsageEventModel.project_id)
Index("ix_usage_events_actor_user_id", UsageEventModel.actor_user_id)
Index("ix_usage_event_rollups_day", UsageEventRollupModel.day)
Index(
    "ix_usage_event_rollups_bucket",
    UsageEventRollupModel.verb,
    UsageEventRollupModel.resource_type,
    UsageEventRollupModel.day,
)
Index(
    "uq_supervision_edges_active_pair",
    SupervisionEdgeModel.supervisor_user_id,
    SupervisionEdgeModel.supervisee_user_id,
    unique=True,
    sqlite_where=SupervisionEdgeModel.ended_at.is_(None),
    postgresql_where=SupervisionEdgeModel.ended_at.is_(None),
)
Index(
    "ix_note_targets_entity_lookup",
    NoteTargetModel.entity_type,
    NoteTargetModel.entity_id,
    NoteTargetModel.note_id,
)
Index("ix_sessions_project_started_at", SessionModel.project_id, SessionModel.started_at)
Index("ix_analysis_datasets_dataset_id", AnalysisDatasetModel.dataset_id)
Index("ix_analyses_project_created_at", AnalysisModel.project_id, AnalysisModel.created_at)
Index("ix_claim_datasets_dataset_id", ClaimDatasetModel.dataset_id)
Index("ix_claim_analyses_analysis_id", ClaimAnalysisModel.analysis_id)
Index("ix_claims_project_created_at", ClaimModel.project_id, ClaimModel.created_at)
Index("ix_claim_edges_claim_id", ClaimEdgeModel.claim_id)
Index("ix_claim_edges_target_claim_id", ClaimEdgeModel.target_claim_id)
Index("ix_claim_edges_created_by_user_id", ClaimEdgeModel.created_by_user_id)
Index(
    "ix_exploration_nodes_project_created_at",
    ExplorationNodeModel.project_id,
    ExplorationNodeModel.created_at,
)
Index("ix_exploration_nodes_type", ExplorationNodeModel.node_type)
Index("ix_exploration_nodes_status", ExplorationNodeModel.status)
Index(
    "ix_exploration_nodes_target",
    ExplorationNodeModel.target_entity_type,
    ExplorationNodeModel.target_entity_id,
)
Index("ix_exploration_node_edges_source", ExplorationNodeEdgeModel.source_node_id)
Index("ix_exploration_node_edges_target", ExplorationNodeEdgeModel.target_node_id)
Index("ix_dataset_question_links_question_id", DatasetQuestionLinkModel.question_id)
Index("ix_goals_project_created_at", GoalModel.project_id, GoalModel.created_at)
Index(
    "ix_goal_links_entity_lookup",
    GoalLinkModel.entity_type,
    GoalLinkModel.entity_id,
    GoalLinkModel.goal_id,
)
Index("ix_goal_links_goal_status", GoalLinkModel.goal_id, GoalLinkModel.link_status)
Index(
    "ix_visualizations_analysis_created_at",
    VisualizationModel.analysis_id,
    VisualizationModel.created_at,
)
Index("ix_visualization_claims_claim_id", VisualizationClaimModel.claim_id)
