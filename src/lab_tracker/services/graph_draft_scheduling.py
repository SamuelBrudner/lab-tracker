"""Batch scheduling and background execution for graph drafts."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TypeVar
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.config import Settings
from lab_tracker.errors import AuthError, NotFoundError, ValidationError
from lab_tracker.graph_drafting import GraphDraftClient, GraphDraftClientFactory
from lab_tracker.models import (
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    GraphDraftBatchTrigger,
    Note,
    NoteStatus,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.provider_error_redaction import (
    configured_provider_secrets,
    provider_error_message,
)
from lab_tracker.services import graph_draft_batch_policy as batch_policy
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_batch_reservation import (
    GraphDraftBatchReservationCoordinator,
)
from lab_tracker.services.graph_draft_scheduling_ports import (
    BatchDraftGenerator,
    SchedulingAuthorization,
    SchedulingNotes,
    SchedulingProjects,
    SchedulingProvenanceLinks,
    SchedulingRecords,
    SchedulingRepository,
)
from lab_tracker.services.review_email_service import normalize_review_email
from lab_tracker.services.shared import actor_user_fk, actor_user_id

logger = logging.getLogger(__name__)
SettingValueT = TypeVar("SettingValueT")


def _validated_setting_patch(
    value: PatchValue[SettingValueT | None],
    field_name: str,
) -> PatchValue[SettingValueT]:
    if not is_provided(value):
        return NOT_PROVIDED
    if value is None:
        raise ValidationError(f"{field_name} must not be null.")
    return value


class BatchSchedulingCoordinator(BaseService):
    """Own batch settings, run preparation, workers, and due dispatch."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        records: SchedulingRecords,
        generation: BatchDraftGenerator,
        projects: SchedulingProjects,
        notes: SchedulingNotes,
        authorization: SchedulingAuthorization,
        provenance_links: SchedulingProvenanceLinks | None = None,
        review_email_available: bool = False,
    ) -> None:
        super().__init__(context)
        self.records = records
        self.generation = generation
        self.projects = projects
        self.notes = notes
        self.authorization = authorization
        self.provenance_links = provenance_links
        self.reservations = GraphDraftBatchReservationCoordinator(
            context,
            projects=projects,
            notes=notes,
        )
        self.review_email_available = bool(review_email_available)

    @property
    def scheduling_repository(self) -> SchedulingRepository:
        return self._context.active_repository()

    def get_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        # Per-user settings now include a notification address. Keep another
        # user's address owner-only while preserving ordinary read access to
        # the project-level default and to one's own settings.
        if user_id is not None and (actor is None or user_id != actor.user_id):
            self.authorization.require_owner(project_id, actor=actor)
        else:
            self.authorization.require_read(project_id, actor=actor)
        # Global-read authorization can succeed without consulting the project
        # repository. Resolve the target only after authorization so missing
        # projects return the canonical 404 without becoming an existence
        # oracle for unauthorized callers.
        self.projects.get_project(project_id)
        settings = self.scheduling_repository.get_graph_draft_batch_settings_by_project(
            project_id,
            user_id=user_id,
        )
        if settings is not None:
            settings.review_email_available = self.review_email_available
            return settings
        default = self.scheduling_repository.get_graph_draft_batch_settings_by_project(project_id)
        settings = batch_policy.default_batch_settings(
            project_id=project_id,
            user_id=user_id,
            actor=actor,
            inherit_from=default,
        )
        settings.review_email_available = self.review_email_available
        return settings

    def update_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        enabled: PatchValue[bool | None] = NOT_PROVIDED,
        cadence_minutes: PatchValue[int | None] = NOT_PROVIDED,
        run_at_local_time: PatchValue[str | None] = NOT_PROVIDED,
        timezone_name: PatchValue[str | None] = NOT_PROVIDED,
        user_id: PatchValue[UUID | None] = NOT_PROVIDED,
        email_notifications_enabled: PatchValue[bool | None] = NOT_PROVIDED,
        notification_email: PatchValue[str | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        enabled = _validated_setting_patch(enabled, "enabled")
        cadence_minutes = _validated_setting_patch(cadence_minutes, "cadence_minutes")
        run_at_local_time = _validated_setting_patch(
            run_at_local_time,
            "run_at_local_time",
        )
        timezone_name = _validated_setting_patch(timezone_name, "timezone_name")
        user_id = _validated_setting_patch(user_id, "user_id")
        email_notifications_enabled = _validated_setting_patch(
            email_notifications_enabled,
            "email_notifications_enabled",
        )
        resolved_user_id = user_id if is_provided(user_id) else None
        # Contributors may schedule their own project's daily batch -- the
        # project-level default (user_id is None) and their own per-user
        # settings (user_id == actor). Editing *another* user's per-user
        # settings still requires owner.
        editing_other_user = resolved_user_id is not None and (
            actor is None or resolved_user_id != actor.user_id
        )
        if editing_other_user:
            self.authorization.require_owner(project_id, actor=actor)
        else:
            self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        settings = self.scheduling_repository.get_graph_draft_batch_settings_by_project(
            project_id,
            user_id=resolved_user_id,
        )
        if settings is None:
            default = self.scheduling_repository.get_graph_draft_batch_settings_by_project(
                project_id
            )
            settings = batch_policy.default_batch_settings(
                project_id=project_id,
                user_id=resolved_user_id,
                actor=actor,
                inherit_from=default,
            )
        settings.review_email_available = self.review_email_available
        before = settings.model_copy(deep=True)
        if is_provided(enabled):
            settings.enabled = enabled
        if is_provided(cadence_minutes):
            if cadence_minutes < 60:
                raise ValidationError("cadence_minutes must be at least 60.")
            settings.cadence_minutes = cadence_minutes
        if is_provided(run_at_local_time):
            batch_policy.validate_run_at_local_time(run_at_local_time)
            settings.run_at_local_time = run_at_local_time
        if is_provided(timezone_name):
            batch_policy.zoneinfo_for(timezone_name)
            settings.timezone_name = timezone_name
        if is_provided(notification_email):
            cleaned_email = (
                normalize_review_email(notification_email)
                if notification_email is not None and notification_email.strip()
                else None
            )
            if cleaned_email != settings.notification_email:
                settings.notification_email = cleaned_email
                settings.notification_email_confirmed_at = (
                    utc_now() if cleaned_email is not None else None
                )
        if is_provided(email_notifications_enabled):
            if email_notifications_enabled and not self.review_email_available:
                raise ValidationError(
                    "Review email delivery is not enabled on this Lab Tracker host."
                )
            settings.email_notifications_enabled = email_notifications_enabled
        if settings.email_notifications_enabled:
            if settings.user_id is None:
                raise ValidationError("Email alerts require per-user batch settings with user_id.")
            if not settings.notification_email or settings.notification_email_confirmed_at is None:
                raise ValidationError(
                    "notification_email is required before email alerts can be enabled."
                )
        scheduling_changed = any(
            (
                settings.enabled != before.enabled,
                settings.cadence_minutes != before.cadence_minutes,
                settings.run_at_local_time != before.run_at_local_time,
                settings.timezone_name != before.timezone_name,
            )
        )
        notification_changed = any(
            (
                settings.email_notifications_enabled != before.email_notifications_enabled,
                settings.notification_email != before.notification_email,
                settings.notification_email_confirmed_at != before.notification_email_confirmed_at,
            )
        )
        if not scheduling_changed and not notification_changed:
            return settings
        if scheduling_changed:
            settings.next_run_at = (
                batch_policy.next_run_at(
                    cadence_minutes=settings.cadence_minutes,
                    run_at_local_time=settings.run_at_local_time,
                    timezone_name=settings.timezone_name,
                )
                if settings.enabled
                else None
            )
        settings.updated_at = utc_now()
        settings.updated_by = actor_user_id(actor)
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_settings.save(settings)
        return settings

    def run_graph_draft_batch_for_project(
        self,
        project_id: UUID,
        *,
        draft_client: GraphDraftClient,
        since: datetime | None = None,
        until: datetime | None = None,
        trigger: GraphDraftBatchTrigger = GraphDraftBatchTrigger.MANUAL,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
    ) -> GraphDraftBatchRun:
        self.authorization.require_contributor(project_id, actor=actor)
        reservation_requested_at = batch_policy.as_utc(utc_now())
        reviewer = self.reservations.resolve_batch_reviewer(
            trigger=trigger,
            actor=actor,
            review_assignee=review_assignee,
            review_assignee_user_id=review_assignee_user_id,
        )
        run, notes, created = self.reservations.reserve_graph_draft_batch_run(
            project_id,
            since=since,
            until=until,
            trigger=trigger,
            user_hint=user_hint,
            actor=actor,
            reviewer=reviewer,
            initial_status=GraphDraftBatchRunStatus.RUNNING,
            reservation_requested_at=reservation_requested_at,
        )
        if not created:
            return run
        # Independent, best-effort deterministic stage: propose content-hash
        # provenance links for human review. A failure here must never flip the
        # LLM batch to FAILED or block drafting.
        if self.provenance_links is not None:
            try:
                self.provenance_links.propose_links_from_content_hash(project_id, actor=actor)
            except Exception:
                logger.exception("provenance-link detector failed for project %s", project_id)
        if not notes:
            run.status = GraphDraftBatchRunStatus.SKIPPED
            run.summary = "No staged notes landed in this batch window."
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            with self.unit_of_work():
                self.scheduling_repository.graph_draft_batch_runs.save(run)
            return run
        try:
            change_set = self.generation.create_batch_graph_draft(
                notes,
                draft_client=draft_client,
                user_hint=user_hint,
                actor=actor,
                window=(run.window_start, run.window_end),
                batch_key=run.batch_key,
                review_assignee=run.review_assignee,
                review_assignee_user_id=run.review_assignee_user_id,
            )
        except Exception as exc:
            run.status = GraphDraftBatchRunStatus.FAILED
            run.summary = "Batch draft failed before a change set could be stored."
            run.error_metadata = {
                "category": "runner_error",
                "message": provider_error_message(exc),
            }
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            with self.unit_of_work():
                self.scheduling_repository.graph_draft_batch_runs.save(run)
            return run
        run.change_set_id = change_set.change_set_id
        run.summary = change_set.summary
        run.error_metadata = dict(change_set.error_metadata)
        run.status = (
            GraphDraftBatchRunStatus.READY
            if change_set.status == GraphChangeSetStatus.READY
            else GraphDraftBatchRunStatus.FAILED
        )
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_runs.save(run)
        return run

    def enqueue_graph_draft_batch_for_project(
        self,
        project_id: UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        trigger: GraphDraftBatchTrigger = GraphDraftBatchTrigger.MANUAL,
        user_hint: str | None = None,
        actor: AuthContext | None = None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
    ) -> GraphDraftBatchRun:
        self.authorization.require_contributor(project_id, actor=actor)
        reservation_requested_at = batch_policy.as_utc(utc_now())
        reviewer = self.reservations.resolve_batch_reviewer(
            trigger=trigger,
            actor=actor,
            review_assignee=review_assignee,
            review_assignee_user_id=review_assignee_user_id,
        )
        run, _notes, _created = self.reservations.reserve_graph_draft_batch_run(
            project_id,
            since=since,
            until=until,
            trigger=trigger,
            user_hint=user_hint,
            actor=actor,
            reviewer=reviewer,
            initial_status=GraphDraftBatchRunStatus.PENDING,
            reservation_requested_at=reservation_requested_at,
        )
        return run

    def process_next_graph_draft_batch_run(
        self,
        *,
        draft_client_factory: GraphDraftClientFactory,
        app_settings: Settings,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchRun | None:
        claimed = self.claim_next_graph_draft_batch_run()
        if claimed is None:
            return None
        draft_client: GraphDraftClient | None = None
        try:
            draft_client = draft_client_factory(app_settings)
            return self.execute_graph_draft_batch_run(
                claimed.run_id,
                draft_client=draft_client,
                actor=actor,
            )
        except Exception as exc:
            return self._fail_batch_run(
                claimed,
                summary="Queued batch draft failed before a change set could be stored.",
                category="worker_error",
                error=exc,
                secrets=configured_provider_secrets(app_settings),
            )
        finally:
            if draft_client is not None:
                close = getattr(draft_client, "close", None)
                if callable(close):
                    close()

    def claim_next_graph_draft_batch_run(self) -> GraphDraftBatchRun | None:
        with self.unit_of_work():
            return self.scheduling_repository.claim_next_pending_graph_draft_batch_run(
                claimed_at=utc_now(),
            )

    def execute_graph_draft_batch_run(
        self,
        run_id: UUID,
        *,
        draft_client: GraphDraftClient,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchRun:
        run = self.records.get_graph_draft_batch_run(run_id)
        if run.status == GraphDraftBatchRunStatus.PENDING:
            with self.unit_of_work():
                claimed = self.scheduling_repository.graph_draft_batch_runs.get(run_id)
                if claimed is None:
                    raise NotFoundError("Graph draft batch run does not exist.")
                claimed.status = GraphDraftBatchRunStatus.RUNNING
                claimed.started_at = utc_now()
                claimed.updated_at = claimed.started_at
                self.scheduling_repository.graph_draft_batch_runs.save(claimed)
                run = claimed
        if run.status != GraphDraftBatchRunStatus.RUNNING:
            return run
        notes = [self.notes.get_note(note_id) for note_id in run.source_note_ids]
        if not notes:
            run.status = GraphDraftBatchRunStatus.SKIPPED
            run.summary = "No staged notes landed in this batch window."
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            with self.unit_of_work():
                self.scheduling_repository.graph_draft_batch_runs.save(run)
            return run
        try:
            change_set = self.generation.create_batch_graph_draft(
                notes,
                draft_client=draft_client,
                user_hint=run.user_hint,
                actor=actor,
                window=(run.window_start, run.window_end),
                batch_key=run.batch_key,
                review_assignee=run.review_assignee,
                review_assignee_user_id=run.review_assignee_user_id,
            )
        except Exception as exc:
            return self._fail_batch_run(
                run,
                summary="Queued batch draft failed before a change set could be stored.",
                category="runner_error",
                error=exc,
            )
        run.change_set_id = change_set.change_set_id
        run.summary = change_set.summary
        run.error_metadata = dict(change_set.error_metadata)
        run.status = (
            GraphDraftBatchRunStatus.READY
            if change_set.status == GraphChangeSetStatus.READY
            else GraphDraftBatchRunStatus.FAILED
        )
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_runs.save(run)
        return run

    def get_graph_draft_batch_run(self, run_id: UUID) -> GraphDraftBatchRun:
        return self.records.get_graph_draft_batch_run(run_id)

    def list_graph_draft_batch_runs(
        self,
        *,
        project_id: UUID | None = None,
        status: GraphDraftBatchRunStatus | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self.records.list_graph_draft_batch_runs(
            project_id=project_id,
            status=status,
        )

    def _fail_batch_run(
        self,
        run: GraphDraftBatchRun,
        *,
        summary: str,
        category: str,
        error: Exception,
        secrets: tuple[str, ...] = (),
    ) -> GraphDraftBatchRun:
        run.status = GraphDraftBatchRunStatus.FAILED
        run.summary = summary
        run.error_metadata = {
            "category": category,
            "message": provider_error_message(error, secrets=secrets),
        }
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_runs.save(run)
        return run

    def run_due_graph_draft_batches(
        self,
        *,
        draft_client_factory: GraphDraftClientFactory,
        app_settings: Settings,
        actor: AuthContext | None = None,
        now: datetime | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self._dispatch_due_graph_draft_batches(
            draft_client_factory=draft_client_factory,
            app_settings=app_settings,
            actor=actor,
            now=now,
            enqueue=False,
        )

    def enqueue_due_graph_draft_batches(
        self,
        *,
        actor: AuthContext | None = None,
        now: datetime | None = None,
    ) -> list[GraphDraftBatchRun]:
        return self._dispatch_due_graph_draft_batches(
            draft_client_factory=None,
            app_settings=None,
            actor=actor,
            now=now,
            enqueue=True,
        )

    def _dispatch_due_graph_draft_batches(
        self,
        *,
        draft_client_factory: GraphDraftClientFactory | None,
        app_settings: Settings | None,
        actor: AuthContext | None,
        now: datetime | None,
        enqueue: bool,
    ) -> list[GraphDraftBatchRun]:
        if not self.authorization.has_global_admin(actor):
            raise AuthError("Only admins can run scheduled batch drafts.")
        current_time = batch_policy.as_utc(now or utc_now())
        due_settings = self.scheduling_repository.list_due_graph_draft_batch_settings(current_time)
        runs: list[GraphDraftBatchRun] = []
        for batch_settings in due_settings:
            if batch_settings.next_run_at is None:
                continue
            claimed_next_run_at = batch_policy.next_run_at(
                cadence_minutes=batch_settings.cadence_minutes,
                run_at_local_time=batch_settings.run_at_local_time,
                timezone_name=batch_settings.timezone_name,
                now=current_time,
            )
            with self.unit_of_work():
                claimed_settings = self.scheduling_repository.claim_due_graph_draft_batch_settings(
                    batch_settings.settings_id,
                    observed_next_run_at=batch_settings.next_run_at,
                    next_run_at=claimed_next_run_at,
                    updated_at=utc_now(),
                    updated_by=actor_user_id(actor),
                )
            if claimed_settings is None:
                continue
            batch_settings = claimed_settings
            try:
                self.projects.get_project(batch_settings.project_id)
            except NotFoundError:
                batch_settings.enabled = False
                batch_settings.next_run_at = None
                batch_settings.updated_at = utc_now()
                batch_settings.updated_by = actor_user_id(actor)
                with self.unit_of_work():
                    self.scheduling_repository.graph_draft_batch_settings.save(batch_settings)
                continue
            reviewers = self._scheduled_reviewers_for_settings(
                batch_settings,
                until=current_time,
            )
            for reviewer in reviewers:
                draft_client: GraphDraftClient | None = None
                try:
                    if enqueue:
                        run = self.enqueue_graph_draft_batch_for_project(
                            batch_settings.project_id,
                            until=current_time,
                            trigger=GraphDraftBatchTrigger.SCHEDULED,
                            actor=actor,
                            review_assignee=reviewer.reviewer,
                            review_assignee_user_id=reviewer.reviewer_user_id,
                        )
                    else:
                        if draft_client_factory is None or app_settings is None:
                            raise RuntimeError(
                                "Scheduled graph drafting requires a client factory "
                                "and application settings."
                            )
                        draft_client = draft_client_factory(app_settings)
                        run = self.run_graph_draft_batch_for_project(
                            batch_settings.project_id,
                            draft_client=draft_client,
                            until=current_time,
                            trigger=GraphDraftBatchTrigger.SCHEDULED,
                            actor=actor,
                            review_assignee=reviewer.reviewer,
                            review_assignee_user_id=reviewer.reviewer_user_id,
                        )
                except Exception as exc:
                    run = self._record_failed_scheduled_batch_run(
                        batch_settings.project_id,
                        window_end=current_time,
                        error=exc,
                        actor=actor,
                        review_assignee=reviewer.reviewer,
                        review_assignee_user_id=reviewer.reviewer_user_id,
                        secrets=configured_provider_secrets(app_settings),
                    )
                finally:
                    if draft_client is not None:
                        close = getattr(draft_client, "close", None)
                        if callable(close):
                            close()
                runs.append(run)
        return runs

    def _scheduled_reviewers_for_settings(
        self,
        settings: GraphDraftBatchSettings,
        *,
        until: datetime,
    ) -> list[batch_policy.BatchReviewer]:
        latest_by_reviewer: dict[
            tuple[str | None, UUID | None],
            GraphDraftBatchRun | None,
        ] = {}
        drafted_note_ids_by_reviewer: dict[tuple[str | None, UUID | None], set[UUID]] = {}

        def note_is_new_for_reviewer(note: Note, reviewer: batch_policy.BatchReviewer) -> bool:
            key = (reviewer.reviewer, reviewer.reviewer_user_id)
            if key not in latest_by_reviewer:
                latest = self.scheduling_repository.latest_successful_graph_draft_batch_run(
                    settings.project_id,
                    review_assignee_user_id=reviewer.reviewer_user_id,
                    review_assignee=reviewer.reviewer,
                )
                latest_by_reviewer[key] = latest
                drafted_note_ids_by_reviewer[key] = (
                    self.scheduling_repository.successful_graph_draft_batch_source_note_ids_at_window_end(
                        settings.project_id,
                        latest.window_end,
                        review_assignee_user_id=reviewer.reviewer_user_id,
                        review_assignee=reviewer.reviewer,
                    )
                    if latest is not None
                    else set()
                )
            latest = latest_by_reviewer[key]
            window_start = batch_policy.as_utc(
                latest.window_end if latest is not None else datetime(1970, 1, 1)
            )
            note_created_at = batch_policy.as_utc(note.created_at)
            if note_created_at > batch_policy.as_utc(until) or note_created_at < window_start:
                return False
            return not (
                latest is not None
                and note_created_at == window_start
                and note.note_id in drafted_note_ids_by_reviewer[key]
            )

        staged_notes = [
            note
            for note in self.notes.list_notes(project_id=settings.project_id)
            if note.status == NoteStatus.STAGED
        ]
        if settings.user_id is not None:
            reviewer = batch_policy.BatchReviewer(
                reviewer=str(settings.user_id),
                reviewer_user_id=settings.user_id,
            )
            if any(
                batch_policy.note_matches_reviewer(note, reviewer)
                and note_is_new_for_reviewer(note, reviewer)
                for note in staged_notes
            ):
                return [reviewer]
            return []
        explicit_user_ids = {
            row.user_id
            for row in self.scheduling_repository.list_graph_draft_batch_settings_for_project(
                settings.project_id
            )
            if row.user_id is not None
        }
        reviewers: dict[tuple[str | None, UUID | None], batch_policy.BatchReviewer] = {}
        for note in staged_notes:
            if note.created_by_user_id is not None and note.created_by_user_id in explicit_user_ids:
                continue
            reviewer = batch_policy.reviewer_for_note(note)
            if reviewer.reviewer is None and reviewer.reviewer_user_id is None:
                continue
            if not note_is_new_for_reviewer(note, reviewer):
                continue
            reviewers[(reviewer.reviewer, reviewer.reviewer_user_id)] = reviewer
        return [
            reviewers[key]
            for key in sorted(
                reviewers,
                key=lambda item: (str(item[1] or ""), str(item[0] or "")),
            )
        ]

    def _record_failed_scheduled_batch_run(
        self,
        project_id: UUID,
        *,
        window_end: datetime,
        error: Exception,
        actor: AuthContext | None,
        review_assignee: str | None = None,
        review_assignee_user_id: UUID | None = None,
        secrets: tuple[str, ...] = (),
    ) -> GraphDraftBatchRun:
        latest_success = self.scheduling_repository.latest_successful_graph_draft_batch_run(
            project_id,
            review_assignee_user_id=review_assignee_user_id,
            review_assignee=review_assignee,
        )
        window_start = batch_policy.as_utc(
            latest_success.window_end if latest_success is not None else datetime(1970, 1, 1)
        )
        finished_at = utc_now()
        run = GraphDraftBatchRun(
            run_id=uuid4(),
            project_id=project_id,
            trigger=GraphDraftBatchTrigger.SCHEDULED,
            status=GraphDraftBatchRunStatus.FAILED,
            window_start=window_start,
            window_end=window_end,
            note_count=0,
            batch_key=batch_policy.make_batch_key(
                project_id=project_id,
                since=window_start,
                until=window_end,
                note_ids=[],
                review_assignee=review_assignee,
                review_assignee_user_id=review_assignee_user_id,
            ),
            summary="Scheduled batch draft failed before a project run could complete.",
            error_metadata={
                "category": "scheduler_error",
                "message": provider_error_message(error, secrets=secrets),
            },
            finished_at=finished_at,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.scheduling_repository),
            review_assignee=review_assignee,
            review_assignee_user_id=review_assignee_user_id,
        )
        run.updated_at = finished_at
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_runs.save(run)
        return run
