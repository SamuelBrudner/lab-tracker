"""Daily graph review service mixin."""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.graph_drafting import ANALYSIS_PROMPT_VERSION, IMAGE_PROMPT_VERSION
from lab_tracker.models import (
    DailyGraphReview,
    DailyGraphReviewStatus,
    GraphChangeSet,
    Note,
    NoteStatus,
    utc_now,
)
from lab_tracker.services.graph_draft_service import _is_text_asset, _text_checksum
from lab_tracker.services.shared import WRITE_ROLES, _actor_user_id


class DailyGraphReviewServiceMixin:
    def create_daily_graph_review(
        self,
        project_id: UUID,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        draft_client: object,
        include_brief: bool = True,
        actor: AuthContext | None = None,
    ) -> DailyGraphReview:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        resolved_window_end = _as_utc(window_end or utc_now())
        resolved_window_start = _as_utc(
            window_start
            or self._latest_daily_graph_review_window_end(project_id)
            or _start_of_utc_day(resolved_window_end)
        )
        if resolved_window_start >= resolved_window_end:
            raise ValidationError("window_start must be before window_end.")

        existing = self._find_daily_graph_review_for_window(
            project_id=project_id,
            window_start=resolved_window_start,
            window_end=resolved_window_end,
        )
        if existing is not None:
            if include_brief and not existing.review_brief:
                change_sets = self._daily_review_change_sets(existing)
                source_notes = self._source_notes_for_change_sets(
                    project_id=existing.project_id,
                    change_sets=change_sets,
                )
                self._apply_daily_review_brief(
                    existing,
                    change_sets=change_sets,
                    source_notes=source_notes,
                    draft_client=draft_client,
                )
                existing.updated_at = utc_now()
                self._save_daily_graph_review(existing)
            return existing

        review = DailyGraphReview(
            review_id=uuid4(),
            project_id=project_id,
            window_start=resolved_window_start,
            window_end=resolved_window_end,
            status=DailyGraphReviewStatus.DRAFTING,
            created_by=_actor_user_id(actor),
        )
        self._save_daily_graph_review(review)

        notes = self._daily_review_source_notes(
            project_id=project_id,
            window_start=resolved_window_start,
            window_end=resolved_window_end,
        )
        change_sets: list[GraphChangeSet] = []
        errors: list[dict[str, str]] = []
        for note in notes:
            try:
                change_set = self._daily_review_change_set_for_note(
                    note,
                    draft_client=draft_client,
                    actor=actor,
                )
            except ValidationError as exc:
                errors.append({"note_id": str(note.note_id), "message": str(exc)})
                continue
            if change_set is not None:
                change_sets.append(change_set)

        review.change_set_ids = [change_set.change_set_id for change_set in change_sets]
        review.status = DailyGraphReviewStatus.READY
        review.summary = _daily_review_summary(notes=notes, change_sets=change_sets)
        if errors:
            review.error_metadata = {"source_errors": errors}
        if include_brief:
            self._apply_daily_review_brief(
                review,
                change_sets=change_sets,
                source_notes=notes,
                draft_client=draft_client,
            )
        review.updated_at = utc_now()
        self._save_daily_graph_review(review)
        return review

    def get_daily_graph_review(self, review_id: UUID) -> DailyGraphReview:
        return self._get_from_repository_or_store(
            attribute_name="daily_graph_reviews",
            entity_id=review_id,
            label="Daily graph review",
            loader=lambda repository: repository.daily_graph_reviews.get(review_id),
        )

    def list_daily_graph_reviews(
        self,
        *,
        project_id: UUID | None = None,
        status: DailyGraphReviewStatus | None = None,
    ) -> list[DailyGraphReview]:
        reviews = self._query_from_repository(
            attribute_name="daily_graph_reviews",
            loader=lambda repository: repository.query_daily_graph_reviews(
                project_id=project_id,
                status=status.value if status is not None else None,
                limit=None,
                offset=0,
            ),
            entity_id_getter=lambda review: review.review_id,
        )
        if reviews is not None:
            return reviews
        values = list(self._store.daily_graph_reviews.values())
        if project_id is not None:
            values = [item for item in values if item.project_id == project_id]
        if status is not None:
            values = [item for item in values if item.status == status]
        return sorted(values, key=lambda item: (item.window_end, item.review_id), reverse=True)

    def mark_daily_graph_review_reviewed(
        self,
        review_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> DailyGraphReview:
        require_role(actor, WRITE_ROLES)
        review = self.get_daily_graph_review(review_id)
        if review.status == DailyGraphReviewStatus.FAILED:
            raise ValidationError("Failed daily graph reviews cannot be marked reviewed.")
        review.status = DailyGraphReviewStatus.REVIEWED
        review.reviewed_at = utc_now()
        review.reviewed_by = _actor_user_id(actor)
        review.updated_at = review.reviewed_at
        self._save_daily_graph_review(review)
        return review

    def _save_daily_graph_review(self, review: DailyGraphReview) -> None:
        self._remember_entity("daily_graph_reviews", review.review_id, review)
        self._run_repository_write(lambda repository: repository.daily_graph_reviews.save(review))

    def _daily_review_source_notes(
        self,
        *,
        project_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> list[Note]:
        notes = [
            note
            for note in self.list_notes(project_id=project_id)
            if note.status != NoteStatus.ARCHIVED
            and window_start < _as_utc(note.created_at) <= window_end
        ]
        return sorted(notes, key=lambda note: (note.created_at, note.note_id))

    def _daily_review_change_set_for_note(
        self,
        note: Note,
        *,
        draft_client: object,
        actor: AuthContext | None,
    ) -> GraphChangeSet | None:
        candidate = self._daily_review_draft_candidate(note)
        if candidate is None:
            return None
        prompt_version, source_checksum = candidate
        existing = self._existing_graph_change_set_for_source(
            note=note,
            prompt_version=prompt_version,
            source_checksum=source_checksum,
        )
        if existing is not None:
            return existing
        if prompt_version == IMAGE_PROMPT_VERSION:
            return self.create_graph_draft_from_note(
                note.note_id,
                draft_client=draft_client,
                actor=actor,
            )
        return self.create_analysis_graph_draft_from_note(
            note.note_id,
            draft_client=draft_client,
            actor=actor,
        )

    def _daily_review_draft_candidate(self, note: Note) -> tuple[str, str] | None:
        if note.raw_asset is not None and note.raw_asset.content_type.lower().startswith("image/"):
            return IMAGE_PROMPT_VERSION, note.raw_asset.checksum
        if not _note_has_analysis_evidence(note):
            return None
        evidence_text = self._analysis_evidence_from_note(note)
        return ANALYSIS_PROMPT_VERSION, _text_checksum(evidence_text)

    def _existing_graph_change_set_for_source(
        self,
        *,
        note: Note,
        prompt_version: str,
        source_checksum: str,
    ) -> GraphChangeSet | None:
        for change_set in self.list_graph_change_sets(source_note_id=note.note_id):
            if (
                change_set.prompt_version == prompt_version
                and change_set.source_checksum == source_checksum
            ):
                return change_set
        return None

    def _daily_review_change_sets(self, review: DailyGraphReview) -> list[GraphChangeSet]:
        change_sets_by_id = {
            change_set.change_set_id: change_set
            for change_set in self.list_graph_change_sets(project_id=review.project_id)
        }
        return [
            change_sets_by_id[change_set_id]
            for change_set_id in review.change_set_ids
            if change_set_id in change_sets_by_id
        ]

    def _source_notes_for_change_sets(
        self,
        *,
        project_id: UUID,
        change_sets: list[GraphChangeSet],
    ) -> list[Note]:
        notes_by_id = {note.note_id: note for note in self.list_notes(project_id=project_id)}
        return [
            notes_by_id[change_set.source_note_id]
            for change_set in change_sets
            if change_set.source_note_id in notes_by_id
        ]

    def _apply_daily_review_brief(
        self,
        review: DailyGraphReview,
        *,
        change_sets: list[GraphChangeSet],
        source_notes: list[Note],
        draft_client: object,
    ) -> None:
        try:
            draft_review_report = getattr(draft_client, "draft_review_report")
            if not callable(draft_review_report):
                raise ValidationError("Graph draft client cannot create review briefs.")
            report = draft_review_report(
                review_context=_daily_review_brief_context(
                    review=review,
                    change_sets=change_sets,
                    source_notes=source_notes,
                )
            )
            review.review_brief = _normalize_review_brief(report)
            error_metadata = dict(review.error_metadata)
            error_metadata.pop("review_brief_error", None)
            review.error_metadata = error_metadata
        except Exception as exc:
            review.review_brief = {}
            error_metadata = dict(review.error_metadata)
            error_metadata["review_brief_error"] = str(exc)
            review.error_metadata = error_metadata

    def _find_daily_graph_review_for_window(
        self,
        *,
        project_id: UUID,
        window_start: datetime,
        window_end: datetime,
    ) -> DailyGraphReview | None:
        for review in self.list_daily_graph_reviews(project_id=project_id):
            if (
                _as_utc(review.window_start) == window_start
                and _as_utc(review.window_end) == window_end
            ):
                return review
        return None

    def _latest_daily_graph_review_window_end(self, project_id: UUID) -> datetime | None:
        reviews = [
            review
            for review in self.list_daily_graph_reviews(project_id=project_id)
            if review.status in {DailyGraphReviewStatus.READY, DailyGraphReviewStatus.REVIEWED}
        ]
        if not reviews:
            return None
        return max(_as_utc(review.window_end) for review in reviews)


def _note_has_analysis_evidence(note: Note) -> bool:
    if note.raw_content.strip():
        return True
    if note.transcribed_text and note.transcribed_text.strip():
        return True
    return note.raw_asset is not None and _is_text_asset(note.raw_asset.content_type)


def _daily_review_summary(*, notes: list[Note], change_sets: list[GraphChangeSet]) -> str:
    ready_count = sum(1 for change_set in change_sets if change_set.status.value == "ready")
    failed_count = sum(1 for change_set in change_sets if change_set.status.value == "failed")
    if not notes:
        return "No new graph-review evidence was found in this window."
    return (
        f"Reviewed {len(notes)} evidence note"
        f"{'' if len(notes) == 1 else 's'} and linked {len(change_sets)} graph draft"
        f"{'' if len(change_sets) == 1 else 's'}"
        f" ({ready_count} ready, {failed_count} failed)."
    )


def _daily_review_brief_context(
    *,
    review: DailyGraphReview,
    change_sets: list[GraphChangeSet],
    source_notes: list[Note],
) -> dict[str, Any]:
    notes_by_id = {note.note_id: note for note in source_notes}
    return {
        "review_id": str(review.review_id),
        "generated_at": utc_now().isoformat(),
        "window_start": _as_utc(review.window_start).isoformat(),
        "window_end": _as_utc(review.window_end).isoformat(),
        "summary": review.summary or "",
        "draft_count": len(change_sets),
        "drafts": [
            _change_set_brief_context(
                change_set,
                source_note=notes_by_id.get(change_set.source_note_id),
            )
            for change_set in change_sets
        ],
    }


def _change_set_brief_context(
    change_set: GraphChangeSet,
    *,
    source_note: Note | None,
) -> dict[str, Any]:
    return {
        "change_set_id": str(change_set.change_set_id),
        "status": change_set.status.value,
        "prompt_version": change_set.prompt_version,
        "source_note_id": str(change_set.source_note_id),
        "source_filename": change_set.source_filename,
        "source_content_type": change_set.source_content_type,
        "source_summary": _source_note_summary(source_note),
        "source_metadata": dict(source_note.metadata) if source_note is not None else {},
        "operations": [
            {
                "op": operation.op.value,
                "entity_type": operation.entity_type.value,
                "status": operation.status.value,
                "rationale": operation.rationale,
                "confidence": operation.confidence,
                "payload": operation.payload,
                "source_refs": operation.source_refs,
            }
            for operation in change_set.operations
        ],
    }


def _source_note_summary(note: Note | None) -> str:
    if note is None:
        return ""
    parts = [note.raw_content.strip()]
    if note.transcribed_text and note.transcribed_text.strip():
        parts.append(f"Transcript:\n{note.transcribed_text.strip()}")
    if note.raw_asset is not None:
        parts.append(
            "\n".join(
                (
                    "Raw asset:",
                    f"- filename: {note.raw_asset.filename}",
                    f"- content_type: {note.raw_asset.content_type}",
                    f"- checksum: {note.raw_asset.checksum}",
                )
            )
        )
    return "\n\n".join(part for part in parts if part)[:4000]


def _normalize_review_brief(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValidationError("Review brief model returned a non-object response.")
    draft_summaries: list[dict[str, Any]] = []
    for item in report.get("draft_summaries") or []:
        if not isinstance(item, dict):
            continue
        draft_summaries.append(
            {
                "change_set_id": str(item.get("change_set_id") or ""),
                "headline": str(item.get("headline") or "Graph draft"),
                "interpretation": str(item.get("interpretation") or ""),
                "recommended_action": str(item.get("recommended_action") or ""),
                "risks": _string_list(item.get("risks")),
                "questions_for_reviewer": _string_list(item.get("questions_for_reviewer")),
            }
        )
    return {
        "title": str(report.get("title") or "Daily Graph Review"),
        "executive_summary": str(report.get("executive_summary") or ""),
        "review_priorities": _string_list(report.get("review_priorities")),
        "draft_summaries": draft_summaries,
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _start_of_utc_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=timezone.utc)
