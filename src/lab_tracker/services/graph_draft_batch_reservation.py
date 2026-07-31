"""Atomic reviewer-note reservation for graph-draft batches."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchTrigger,
    Note,
    utc_now,
)
from lab_tracker.services import graph_draft_batch_policy as batch_policy
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_scheduling_ports import (
    SchedulingNotes,
    SchedulingProjects,
    SchedulingRepository,
)
from lab_tracker.services.shared import actor_user_fk, actor_user_id


class GraphDraftBatchReservationCoordinator(BaseService):
    """Reserve a disjoint reviewer-note set before provider work."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: SchedulingProjects,
        notes: SchedulingNotes,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.notes = notes

    @property
    def scheduling_repository(self) -> SchedulingRepository:
        return self._context.active_repository()

    def resolve_batch_reviewer(
        self,
        *,
        trigger: GraphDraftBatchTrigger,
        actor: AuthContext | None,
        review_assignee: str | None,
        review_assignee_user_id: UUID | None,
    ) -> batch_policy.BatchReviewer:
        if (
            trigger == GraphDraftBatchTrigger.MANUAL
            and review_assignee is None
            and review_assignee_user_id is None
        ):
            # The ordinary Run now action is a personal review operation. Keep
            # the string identifier for auth-disabled/legacy installs, and use
            # the FK whenever this request represents a persisted user.
            review_assignee = actor_user_id(actor)
            review_assignee_user_id = actor_user_fk(
                actor,
                self.scheduling_repository,
            )
        return batch_policy.BatchReviewer(
            reviewer=review_assignee,
            reviewer_user_id=review_assignee_user_id,
        )

    def reserve_graph_draft_batch_run(
        self,
        project_id: UUID,
        *,
        since: datetime | None,
        until: datetime | None,
        trigger: GraphDraftBatchTrigger,
        user_hint: str | None,
        actor: AuthContext | None,
        reviewer: batch_policy.BatchReviewer,
        initial_status: GraphDraftBatchRunStatus,
        reservation_requested_at: datetime,
    ) -> tuple[GraphDraftBatchRun, list[Note], bool]:
        """Atomically reserve a disjoint reviewer-note set."""

        with self.application_transaction():
            # Global administrators can pass the route-level contributor
            # shortcut, so preserve the service's canonical missing-project
            # 404 before locks or lazy settings writes touch foreign keys.
            self.projects.get_project(project_id)
            repository = self.scheduling_repository
            # Reviewer locks are deliberately disjoint, but all reviewers may
            # lazily create the same project-default settings row. Serialize
            # that shared initialization before entering reviewer scope.
            repository.lock_graph_draft_batch_settings(project_id)
            self._ensure_graph_draft_batch_settings_row(project_id, actor=actor)
            repository.lock_graph_draft_batch_reviewer(
                project_id,
                review_assignee_user_id=reviewer.reviewer_user_id,
                review_assignee=reviewer.reviewer,
            )
            active_runs = repository.active_graph_draft_batch_runs(
                project_id,
                review_assignee_user_id=reviewer.reviewer_user_id,
                review_assignee=reviewer.reviewer,
            )
            latest_run = repository.latest_graph_draft_batch_run(
                project_id,
                review_assignee_user_id=reviewer.reviewer_user_id,
                review_assignee=reviewer.reviewer,
            )
            active_note_ids = {
                note_id for active_run in active_runs for note_id in active_run.source_note_ids
            }
            run, notes, eligible_note_ids = self._prepare_graph_draft_batch_run(
                project_id,
                since=since,
                until=until,
                trigger=trigger,
                user_hint=user_hint,
                actor=actor,
                reviewer=reviewer,
                initial_status=initial_status,
                active_note_ids=active_note_ids,
                previous_run=latest_run,
            )
            # A redundant request whose eligible notes are already reserved
            # rejoins the active run that owns those notes. Multiple disjoint
            # runs can coexist, so "newest active" is not sufficient here.
            if not notes and active_runs:
                matching_active_run = self._matching_active_batch_run(
                    active_runs,
                    eligible_note_ids=eligible_note_ids,
                    requested_run=run,
                )
                if matching_active_run is not None:
                    return matching_active_run, [], False
            if (
                not notes
                and latest_run is not None
                and latest_run.status
                in {
                    GraphDraftBatchRunStatus.READY,
                    GraphDraftBatchRunStatus.SKIPPED,
                }
                and latest_run.finished_at is not None
                and batch_policy.as_utc(latest_run.finished_at) >= reservation_requested_at
            ):
                # This request began while the winning inline run still held
                # the reviewer lock; return that result after the lock wait.
                return latest_run, [], False
            # Before stable reservation keys, explicit-window batches used a
            # key that included the exact window bounds. Honor such successful
            # rows so a replay across an upgrade does not redraft the notes.
            legacy_batch_key = batch_policy.make_batch_key(
                project_id=project_id,
                since=run.window_start,
                until=run.window_end,
                note_ids=run.source_note_ids,
                review_assignee=reviewer.reviewer,
                review_assignee_user_id=reviewer.reviewer_user_id,
            )
            legacy_existing = repository.get_graph_draft_batch_run_by_key(legacy_batch_key)
            if (
                legacy_existing is not None
                and legacy_existing.status != GraphDraftBatchRunStatus.FAILED
            ):
                return legacy_existing, [], False

            existing = repository.get_graph_draft_batch_run_by_key(run.batch_key)
            while (
                notes
                and existing is not None
                and existing.status == GraphDraftBatchRunStatus.FAILED
            ):
                # Failed attempts form a deterministic retry chain. This does
                # not depend on which unrelated reviewer run happened most
                # recently, and each concurrent retry observes the same next
                # generation.
                run.batch_key = batch_policy.make_reserved_batch_key(
                    project_id=project_id,
                    note_ids=run.source_note_ids,
                    review_assignee=reviewer.reviewer,
                    review_assignee_user_id=reviewer.reviewer_user_id,
                    generation_run_id=existing.run_id,
                )
                existing = repository.get_graph_draft_batch_run_by_key(run.batch_key)
            if existing is not None:
                return existing, [], False
            try:
                with self.recoverable_unit_of_work() as writable_repository:
                    writable_repository.graph_draft_batch_runs.save(run)
            except IntegrityError:
                # SQLite's coarse write fence normally serializes this path;
                # the unique key remains a final cross-request race backstop.
                existing = repository.get_graph_draft_batch_run_by_key(run.batch_key)
                if existing is None:
                    raise
                return existing, [], False
            return run, notes, True

    def _prepare_graph_draft_batch_run(
        self,
        project_id: UUID,
        *,
        since: datetime | None,
        until: datetime | None,
        trigger: GraphDraftBatchTrigger,
        user_hint: str | None,
        actor: AuthContext | None,
        reviewer: batch_policy.BatchReviewer,
        initial_status: GraphDraftBatchRunStatus,
        active_note_ids: set[UUID],
        previous_run: GraphDraftBatchRun | None,
    ) -> tuple[GraphDraftBatchRun, list[Note], set[UUID]]:
        reviewer_filter = (
            reviewer
            if reviewer.reviewer is not None or reviewer.reviewer_user_id is not None
            else None
        )
        now = batch_policy.as_utc(utc_now())
        requested_window_end = batch_policy.as_utc(until) if until is not None else now
        window_end = min(requested_window_end, now)
        latest_success = self.scheduling_repository.latest_successful_graph_draft_batch_run(
            project_id,
            review_assignee_user_id=(
                reviewer_filter.reviewer_user_id if reviewer_filter is not None else None
            ),
            review_assignee=(reviewer_filter.reviewer if reviewer_filter is not None else None),
        )
        window_start = batch_policy.as_utc(
            since
            or (latest_success.window_end if latest_success is not None else datetime(1970, 1, 1))
        )
        if since is not None and window_start >= window_end:
            raise ValidationError("Batch window since must be before until.")
        continuing_auto_window = since is None and latest_success is not None
        already_drafted_note_ids = (
            self.scheduling_repository.successful_graph_draft_batch_source_note_ids_at_window_end(
                project_id,
                window_start,
                review_assignee_user_id=reviewer.reviewer_user_id,
                review_assignee=reviewer.reviewer,
            )
            if continuing_auto_window
            else set()
        )
        eligible_notes = batch_policy.staged_notes_in_window(
            self.notes.list_notes(project_id=project_id),
            since=window_start,
            until=window_end,
            include_start=continuing_auto_window,
            exclude_note_ids=already_drafted_note_ids,
            reviewer=reviewer_filter,
        )
        eligible_note_ids = {note.note_id for note in eligible_notes}
        notes = [note for note in eligible_notes if note.note_id not in active_note_ids]
        notes, window_end = batch_policy.limit_notes_to_draft(
            notes,
            window_end=window_end,
        )
        note_ids = [note.note_id for note in notes]
        run = GraphDraftBatchRun(
            run_id=uuid4(),
            project_id=project_id,
            trigger=trigger,
            status=initial_status,
            window_start=window_start,
            window_end=window_end,
            note_count=len(notes),
            source_note_ids=note_ids,
            batch_key=batch_policy.make_reserved_batch_key(
                project_id=project_id,
                note_ids=note_ids,
                review_assignee=reviewer.reviewer,
                review_assignee_user_id=reviewer.reviewer_user_id,
                generation_run_id=(
                    previous_run.run_id if previous_run is not None and not note_ids else None
                ),
            ),
            user_hint=user_hint.strip() if user_hint else None,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.scheduling_repository),
            review_assignee=reviewer.reviewer,
            review_assignee_user_id=reviewer.reviewer_user_id,
        )
        return run, notes, eligible_note_ids

    @staticmethod
    def _matching_active_batch_run(
        active_runs: list[GraphDraftBatchRun],
        *,
        eligible_note_ids: set[UUID],
        requested_run: GraphDraftBatchRun,
    ) -> GraphDraftBatchRun | None:
        """Return the active reservation that best owns this request's notes."""

        if eligible_note_ids:
            candidates: list[tuple[bool, int, int, GraphDraftBatchRun]] = []
            for index, active_run in enumerate(active_runs):
                source_note_ids = set(active_run.source_note_ids)
                overlap = eligible_note_ids & source_note_ids
                if not overlap:
                    continue
                candidates.append(
                    (
                        eligible_note_ids.issubset(source_note_ids),
                        len(overlap),
                        -index,
                        active_run,
                    )
                )
            if candidates:
                return max(candidates, key=lambda candidate: candidate[:3])[3]
            return None

        # Empty reservations have no note ownership to compare. Rejoin one
        # only when its explicit cursor is exactly the same.
        return next(
            (
                active_run
                for active_run in active_runs
                if not active_run.source_note_ids
                and active_run.window_start == requested_run.window_start
                and active_run.window_end == requested_run.window_end
            ),
            None,
        )

    def _ensure_graph_draft_batch_settings_row(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
        user_id: UUID | None = None,
    ) -> None:
        if (
            self.scheduling_repository.get_graph_draft_batch_settings_by_project(
                project_id,
                user_id=user_id,
            )
            is not None
        ):
            return
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_settings.save(
                batch_policy.default_batch_settings(
                    project_id=project_id,
                    user_id=user_id,
                    actor=actor,
                )
            )
