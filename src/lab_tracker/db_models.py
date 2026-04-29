"""SQLAlchemy models for lab tracker."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectModel(Base):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class QuestionModel(Base):
    __tablename__ = "questions"

    question_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[str] = mapped_column(String(40), nullable=False)
    hypothesis: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="staged")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class QuestionParentModel(Base):
    __tablename__ = "question_parents"

    question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )


class DatasetModel(Base):
    __tablename__ = "datasets"

    dataset_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    commit_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    primary_question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("questions.question_id"),
        nullable=False,
    )
    manifest_files: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    manifest_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_nwb_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_bids_metadata: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    manifest_note_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    manifest_source_session_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="staged")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class DatasetQuestionLinkModel(Base):
    __tablename__ = "dataset_question_links"

    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )
    question_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("questions.question_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome_status: Mapped[str] = mapped_column(String(20), default="unknown")


class DatasetFileModel(Base):
    __tablename__ = "dataset_files"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "path",
            name="uq_dataset_files_dataset_path",
        ),
    )

    file_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        nullable=False,
    )
    storage_id: Mapped[str] = mapped_column(String(36), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class NoteModel(Base):
    __tablename__ = "notes"

    note_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    raw_storage_id: Mapped[str | None] = mapped_column(String(36))
    raw_filename: Mapped[str | None] = mapped_column(String(255))
    raw_content_type: Mapped[str | None] = mapped_column(String(255))
    raw_size_bytes: Mapped[int | None] = mapped_column(Integer)
    raw_checksum: Mapped[str | None] = mapped_column(String(64))
    transcribed_text: Mapped[str | None] = mapped_column(Text)
    note_metadata: Mapped[dict[str, str]] = mapped_column("metadata", JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="staged")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class NoteTargetModel(Base):
    __tablename__ = "note_targets"

    note_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)


class GraphChangeSetModel(Base):
    __tablename__ = "graph_change_sets"

    change_set_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_note_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_checksum: Mapped[str | None] = mapped_column(String(64))
    source_content_type: Mapped[str | None] = mapped_column(String(255))
    source_filename: Mapped[str | None] = mapped_column(String(255))
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="drafting")
    commit_message: Mapped[str | None] = mapped_column(Text)
    error_metadata: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_by: Mapped[str | None] = mapped_column(String(255))


class GraphChangeOperationModel(Base):
    __tablename__ = "graph_change_operations"
    __table_args__ = (
        UniqueConstraint(
            "change_set_id",
            "sequence",
            name="uq_graph_change_operations_change_set_sequence",
        ),
    )

    operation_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    change_set_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("graph_change_sets.change_set_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    op: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_entity_id: Mapped[str | None] = mapped_column(String(36))
    client_ref: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float | None] = mapped_column(Float)
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    result_entity_id: Mapped[str | None] = mapped_column(String(36))
    error_metadata: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class SessionModel(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    primary_question_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("questions.question_id"),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
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

    output_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class AnalysisModel(Base):
    __tablename__ = "analyses"

    analysis_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    method_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    code_version: Mapped[str] = mapped_column(String(255), nullable=False)
    environment_hash: Mapped[str | None] = mapped_column(String(255))
    executed_by: Mapped[str | None] = mapped_column(String(255))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    status: Mapped[str] = mapped_column(String(20), default="staged")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class AnalysisDatasetModel(Base):
    __tablename__ = "analysis_datasets"

    analysis_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        primary_key=True,
    )
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ClaimModel(Base):
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class ClaimDatasetModel(Base):
    __tablename__ = "claim_datasets"

    claim_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        primary_key=True,
    )


class ClaimAnalysisModel(Base):
    __tablename__ = "claim_analyses"

    claim_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )
    analysis_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        primary_key=True,
    )


class VisualizationModel(Base):
    __tablename__ = "visualizations"

    viz_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    analysis_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("analyses.analysis_id", ondelete="CASCADE"),
        nullable=False,
    )
    viz_type: Mapped[str] = mapped_column(String(40), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class VisualizationClaimModel(Base):
    __tablename__ = "visualization_claims"

    viz_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("visualizations.viz_id", ondelete="CASCADE"),
        primary_key=True,
    )
    claim_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("claims.claim_id", ondelete="CASCADE"),
        primary_key=True,
    )


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    user_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


Index("ix_questions_project_created_at", QuestionModel.project_id, QuestionModel.created_at)
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
Index("ix_dataset_question_links_question_id", DatasetQuestionLinkModel.question_id)
Index(
    "ix_visualizations_analysis_created_at",
    VisualizationModel.analysis_id,
    VisualizationModel.created_at,
)
Index("ix_visualization_claims_claim_id", VisualizationClaimModel.claim_id)
