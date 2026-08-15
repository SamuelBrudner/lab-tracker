"""Structural ports and value objects for graph-draft generation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.models import (
    GraphChangeOperation,
    GraphChangeSet,
    GraphDraftMode,
    Note,
    NoteRawAsset,
)


class GenerationRecords(Protocol):
    def save_graph_change_set(self, change_set: GraphChangeSet) -> None: ...

    def list_graph_change_sets(
        self,
        *,
        draft_mode: GraphDraftMode | None = None,
        batch_key: str | None = None,
    ) -> list[GraphChangeSet]: ...

    def get_graph_change_set(self, change_set_id: UUID) -> GraphChangeSet: ...

    def claim_graph_change_set_generation(
        self,
        candidate: GraphChangeSet,
        *,
        claimed_at: datetime,
        lease_until: datetime,
        claim_token: UUID,
    ) -> tuple[GraphChangeSet, bool]: ...

    def renew_graph_change_set_generation(
        self,
        change_set_id: UUID,
        claim_token: UUID,
        *,
        renewed_at: datetime,
        lease_until: datetime,
    ) -> GraphChangeSet | None: ...

    def complete_graph_change_set_generation(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        completed_at: datetime,
    ) -> GraphChangeSet | None: ...

    def fail_graph_change_set_generation(
        self,
        change_set: GraphChangeSet,
        claim_token: UUID,
        *,
        failed_at: datetime,
    ) -> GraphChangeSet | None: ...


class GenerationNotes(Protocol):
    def get_note(self, note_id: UUID) -> Note: ...

    def download_note_raw(self, note_id: UUID) -> tuple[NoteRawAsset, bytes]: ...


class GenerationAuthorization(Protocol):
    def require_contributor(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None: ...


class GenerationContextBuilder(Protocol):
    def prepare_note_sources_for_graph_draft(
        self,
        note_id: UUID,
        *,
        mode: GraphDraftMode,
        source_note_ids: list[UUID] | None = None,
    ) -> dict[str, Any]: ...

    def build_graph_context_packet(
        self,
        note: Note,
        *,
        source_notes: list[Note],
        user_hint: str | None,
        actor: AuthContext | None,
    ) -> dict[str, Any]: ...

    def image_only_context_packet(
        self,
        note: Note,
        *,
        source_notes: list[Note],
        user_hint: str | None,
    ) -> dict[str, Any]: ...

    def build_batch_graph_context(
        self,
        notes: list[Note],
        *,
        window: tuple[datetime, datetime] | None,
        actor: AuthContext | None,
        batch_note_limit: int,
    ) -> dict[str, Any]: ...


class GenerationPatchValidator(Protocol):
    def validate_top_level(self, graph_patch: dict[str, Any]) -> None: ...

    def operations_from_graph_patch(
        self,
        change_set: GraphChangeSet,
        graph_patch: dict[str, Any],
    ) -> list[GraphChangeOperation]: ...


class ReviewEmailEnqueuer(Protocol):
    def enqueue_ready_review(
        self,
        change_set: GraphChangeSet,
    ) -> object | None: ...


class DraftFromNoteCallable(Protocol):
    def __call__(
        self,
        *,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: str,
        source_artifacts: list[dict[str, Any]],
        image_bytes: bytes | None,
        image_content_type: str | None,
        extra_images: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


class DraftFromImageCallable(Protocol):
    def __call__(
        self,
        *,
        image_bytes: bytes,
        content_type: str,
        graph_context: dict[str, Any],
        user_hint: str | None,
        draft_mode: str,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class GeneratedDraftProposal:
    """A validated, non-persisted proposal returned to another lifecycle owner."""

    context_packet: dict[str, Any]
    operations: list[GraphChangeOperation]
    summary: str
    uncertain_fields: list[str]
    clarification_requests: list[str]


@dataclass(frozen=True)
class GenerationClaim:
    """One durable provider-generation ownership decision."""

    change_set: GraphChangeSet
    claim_token: UUID
    acquired: bool
