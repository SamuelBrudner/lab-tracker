"""SQLAlchemy models for lab tracker."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    review_policy: Mapped[str] = mapped_column(String(20), default="none")
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
    created_from: Mapped[str] = mapped_column(String(40), default="manual")
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


class DatasetReviewModel(Base):
    __tablename__ = "dataset_reviews"

    review_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    dataset_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("datasets.dataset_id", ondelete="CASCADE"),
        nullable=False,
    )
    reviewer_user_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    comments: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    transcribed_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="staged")
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class NoteExtractedEntityModel(Base):
    __tablename__ = "note_extracted_entities"

    extracted_entity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    provenance: Mapped[str] = mapped_column(String(255), nullable=False)


class NoteTagSuggestionModel(Base):
    __tablename__ = "note_tag_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "note_id",
            "entity_label",
            "vocabulary",
            "term_id",
            name="uq_note_tag_suggestion",
        ),
    )

    suggestion_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    note_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_label: Mapped[str] = mapped_column(String(255), nullable=False)
    vocabulary: Mapped[str] = mapped_column(String(40), nullable=False)
    term_id: Mapped[str] = mapped_column(String(255), nullable=False)
    term_label: Mapped[str] = mapped_column(String(255), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    provenance: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="staged")
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NoteTargetModel(Base):
    __tablename__ = "note_targets"

    note_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("notes.note_id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_type: Mapped[str] = mapped_column(String(30), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)


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
