"""Batch scheduling and background execution for graph drafts."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.config import Settings
from lab_tracker.errors import NotFoundError, PermissionDeniedError
from lab_tracker.graph_drafting import GraphDraftClient, GraphDraftClientFactory
from lab_tracker.models import (
    DelegatedCurationPolicy,
    ExternalContextPolicy,
    GraphChangeSetStatus,
    GraphDraftBatchRun,
    GraphDraftBatchRunStatus,
    GraphDraftBatchSettings,
    GraphDraftBatchTrigger,
    Note,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue
from lab_tracker.provider_error_redaction import (
    configured_provider_secrets,
    provider_error_message,
)
from lab_tracker.services import graph_draft_batch_policy as batch_policy
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_batch_reservation import (
    GraphDraftBatchReservationCoordinator,
)
from lab_tracker.services.graph_draft_batch_settings import BatchSettingsCoordinator
from lab_tracker.services.graph_draft_delegation import DelegatedCurationCoordinator
from lab_tracker.services.graph_draft_generation import (
    configured_provider_generation_lease_seconds,
    provider_generation_lease_seconds,
)
from lab_tracker.services.graph_draft_scheduling_ports import (
    BatchDraftGenerator,
    SchedulingAuthorization,
    SchedulingProvenanceLinks,
    SchedulingRecords,
    SchedulingRepository,
)
from lab_tracker.services.provenance_detection_stage import propose_deterministic_links
from lab_tracker.services.shared import actor_user_fk, actor_user_id

logger = logging.getLogger(__name__)
class BatchSchedulingCoordinator(BaseService):
    """Own batch settings, run preparation, workers, and due dispatch."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        records: SchedulingRecords,
        generation: BatchDraftGenerator,
        settings: BatchSettingsCoordinator,
        reservations: GraphDraftBatchReservationCoordinator,
        authorization: SchedulingAuthorization,
        provenance_links: SchedulingProvenanceLinks | None = None,
        delegation: DelegatedCurationCoordinator | None = None,
    ) -> None:
        super().__init__(context)
        self.records = records
        self.generation = generation
        self.settings = settings
        self.reservations = reservations
        self.authorization = authorization
        self.provenance_links = provenance_links
        self.delegation = delegation

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
        return self.settings.get_graph_draft_batch_settings(
            project_id, user_id=user_id, actor=actor
        )

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
        external_context_policy: PatchValue[ExternalContextPolicy | None] = NOT_PROVIDED,
        external_provider_acknowledged: PatchValue[bool | None] = NOT_PROVIDED,
        delegated_curation: PatchValue[DelegatedCurationPolicy | None] = NOT_PROVIDED,
        delegated_curation_acknowledged: PatchValue[bool | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        return self.settings.update_graph_draft_batch_settings(
            project_id,
            enabled=enabled,
            cadence_minutes=cadence_minutes,
            run_at_local_time=run_at_local_time,
            timezone_name=timezone_name,
            user_id=user_id,
            email_notifications_enabled=email_notifications_enabled,
            notification_email=notification_email,
            external_context_policy=external_context_policy,
            external_provider_acknowledged=external_provider_acknowledged,
            delegated_curation=delegated_curation,
            delegated_curation_acknowledged=delegated_curation_acknowledged,
            actor=actor,
        )

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
            initial_status=GraphDraftBatchRunStatus.PENDING,
            reservation_requested_at=reservation_requested_at,
        )
        if not created:
            if run.status in {
                GraphDraftBatchRunStatus.PENDING,
                GraphDraftBatchRunStatus.RUNNING,
            }:
                return self.execute_graph_draft_batch_run(
                    run.run_id,
                    draft_client=draft_client,
                    actor=actor,
                )
            return run
        return self.execute_graph_draft_batch_run(
            run.run_id,
            draft_client=draft_client,
            actor=actor,
        )

    def _propose_deterministic_links(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> None:
        """Best-effort deterministic stage run once per claimed batch execution.

        Proposes content-hash, exact-id, and time-window provenance links for
        human review; each detector's failure is logged and swallowed, never
        failing the batch.
        """

        if self.provenance_links is None:
            return
        propose_deterministic_links(self.provenance_links, project_id, actor=actor)

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
        draft_client: GraphDraftClient | None = None
        claimed = self.claim_next_graph_draft_batch_run(
            lease_seconds=configured_provider_generation_lease_seconds(
                app_settings
            )
        )
        if claimed is None:
            return None
        try:
            draft_client = draft_client_factory(app_settings)
            return self.execute_graph_draft_batch_run(
                claimed.run_id,
                draft_client=draft_client,
                claim_token=claimed.claim_token,
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

    def claim_next_graph_draft_batch_run(
        self,
        *,
        lease_seconds: int,
    ) -> GraphDraftBatchRun | None:
        claimed_at = utc_now()
        lease_until = claimed_at + timedelta(seconds=max(1, lease_seconds))
        with self.unit_of_work():
            claimed = self.scheduling_repository.claim_next_pending_graph_draft_batch_run(
                claimed_at=claimed_at,
                lease_until=lease_until,
                claim_token=uuid4(),
            )
        if claimed is not None:
            self._commit_request_claim_checkpoint()
        return claimed

    def execute_graph_draft_batch_run(
        self,
        run_id: UUID,
        *,
        draft_client: GraphDraftClient,
        claim_token: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchRun:
        run = self.records.get_graph_draft_batch_run(run_id)
        if claim_token is None:
            claimed_at = utc_now()
            lease_until = claimed_at + timedelta(
                seconds=provider_generation_lease_seconds(draft_client)
            )
            with self.unit_of_work():
                claimed = self.scheduling_repository.claim_graph_draft_batch_run(
                    run_id,
                    claimed_at=claimed_at,
                    lease_until=lease_until,
                    claim_token=uuid4(),
                )
            if claimed is None:
                return self.records.get_graph_draft_batch_run(run_id)
            self._commit_request_claim_checkpoint()
            run = claimed
            claim_token = run.claim_token
        if run.status != GraphDraftBatchRunStatus.RUNNING:
            return run
        if claim_token is None or run.claim_token != claim_token:
            return self.records.get_graph_draft_batch_run(run_id)
        # Every claimed execution — synchronous run-now, the queued worker and
        # due dispatch — runs the detectors exactly once, before drafting.
        self._propose_deterministic_links(run.project_id, actor=actor)

        def renew_run(_attempt: int) -> bool:
            renewed_at = utc_now()
            lease_until = renewed_at + timedelta(
                seconds=provider_generation_lease_seconds(draft_client)
            )
            with self.unit_of_work():
                renewed = self.scheduling_repository.renew_graph_draft_batch_run_claim(
                    run_id,
                    claim_token,
                    renewed_at=renewed_at,
                    lease_until=lease_until,
                )
            if renewed is None:
                return False
            self._commit_request_claim_checkpoint()
            return True

        notes = self._batch_source_notes(run.source_note_ids)
        if not notes:
            run.status = GraphDraftBatchRunStatus.SKIPPED
            run.summary = "No staged notes landed in this batch window."
            run.finished_at = utc_now()
            run.updated_at = run.finished_at
            return self._finish_batch_run(run, claim_token)
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
                before_attempt=renew_run,
            )
        except Exception as exc:
            return self._fail_batch_run(
                run,
                summary="Queued batch draft failed before a change set could be stored.",
                category="runner_error",
                error=exc,
            )
        if self.delegation is not None:
            # Under a delegated-curation grant the pass accepts what the grant
            # admits and commits when nothing is left for a person; the run
            # reports the draft as ready either way.
            change_set = self.delegation.apply_delegated_curation(change_set)
        run.change_set_id = change_set.change_set_id
        run.summary = change_set.summary
        run.error_metadata = dict(change_set.error_metadata)
        run.status = (
            GraphDraftBatchRunStatus.READY
            if change_set.status
            in {GraphChangeSetStatus.READY, GraphChangeSetStatus.COMMITTED}
            else GraphDraftBatchRunStatus.FAILED
        )
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        return self._finish_batch_run(run, claim_token)

    def _batch_source_notes(self, note_ids: list[UUID]) -> list[Note]:
        """Load a run's frozen source notes in one query, in reservation order."""

        if not note_ids:
            return []
        loaded, _ = self.scheduling_repository.query_notes(
            note_ids=set(note_ids),
            limit=None,
            offset=0,
        )
        by_id = {note.note_id: note for note in loaded}
        if any(note_id not in by_id for note_id in note_ids):
            raise NotFoundError("Note does not exist.")
        return [by_id[note_id] for note_id in note_ids]

    def get_graph_draft_batch_run(self, run_id: UUID) -> GraphDraftBatchRun:
        return self.records.get_graph_draft_batch_run(run_id)

    def query_graph_draft_batch_runs(
        self,
        query: batch_policy.BatchRunQuery,
    ) -> tuple[list[GraphDraftBatchRun], int]:
        return self.records.query_graph_draft_batch_runs(query)

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
        if run.claim_token is None:
            return run
        return self._finish_batch_run(run, run.claim_token)

    def _finish_batch_run(
        self,
        run: GraphDraftBatchRun,
        claim_token: UUID,
    ) -> GraphDraftBatchRun:
        finished_at = run.finished_at or utc_now()
        run.finished_at = finished_at
        run.updated_at = finished_at
        with self.unit_of_work():
            completed = self.scheduling_repository.finish_graph_draft_batch_run_claim(
                run,
                claim_token,
                finished_at=finished_at,
            )
        return completed or self.records.get_graph_draft_batch_run(run.run_id)

    def _commit_request_claim_checkpoint(self) -> None:
        """Publish a batch ownership fence before provider work begins."""

        if self._context.is_request_managed():
            self._context.active_repository().commit()

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
            raise PermissionDeniedError("Only admins can run scheduled batch drafts.")
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
                self.reservations.projects.get_project(batch_settings.project_id)
            except NotFoundError:
                batch_settings.enabled = False
                batch_settings.next_run_at = None
                batch_settings.updated_at = utc_now()
                batch_settings.updated_by = actor_user_id(actor)
                with self.unit_of_work():
                    self.scheduling_repository.graph_draft_batch_settings.save(batch_settings)
                continue
            reviewers = self.reservations.scheduled_reviewers_for_settings(
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
