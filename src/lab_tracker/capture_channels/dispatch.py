"""Run the capture pollers (email, bookings, store scans) independently.

Every trigger -- the optional in-process ticker, ``POST /integrations/run-due``,
and ``lab-tracker integrations poll`` -- calls :func:`run_due_pollers`. Each
poller is skipped when unconfigured (no network call), runs at most once per
``LAB_TRACKER_INTEGRATIONS_POLL_MIN_INTERVAL_SECONDS`` across all triggers, is
bounded by its own caps and deadlines, and is isolated: an exception in one
poller (or in one feed, mailbox message, or scan inside it) is recorded with a
static detail and never stops the others, nor the graph-draft batch dispatch,
which runs on its own path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.auth import AuthContext
from lab_tracker.bounded_subprocess import ProcessExecutor
from lab_tracker.capture_channels.bookings import (
    CalendarFetcher,
    CalendarFetchError,
    policy_calendar_fetcher,
    sync_calendar,
)
from lab_tracker.capture_channels.common import UserDirectory
from lab_tracker.capture_channels.email_capture import (
    MAX_STORED_ATTACHMENT_BYTES,
    EmailCaptureConfig,
    EmailCaptureError,
    ImapFactory,
    MessageOutcome,
    ParsedEmail,
    default_imap_factory,
    poll_mailbox,
    stage_email,
)
from lab_tracker.capture_channels.poll_state import PollState, PollStateUnavailable
from lab_tracker.capture_channels.settings import (
    StoreScan,
    parse_booking_calendars,
    parse_store_scans,
)
from lab_tracker.capture_channels.store_scan import (
    LOCAL_STORE_SCAN_UNSUPPORTED_MESSAGE,
    STORE_SCAN_UNAUTHORIZED_MESSAGE,
    LocalStoreScanAccess,
    RcloneStoreAdapter,
    StoreAdapter,
    StoreScanError,
    adapter_supports_listing,
    run_store_scan,
)
from lab_tracker.models import StoreCapability, StoreKind, utc_now
from lab_tracker.outbound_http import OutboundHttpClient, OutboundHttpPolicy
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_definition import is_rclone_store_kind
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.store_authority_use import (
    DetachedStoreAuthorityBinding,
    StoreAuthoritySnapshotProvider,
    StoreAuthorityUseProof,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)

_logger = logging.getLogger(__name__)

EMAIL_POLLER: Final = "email"
BOOKINGS_POLLER: Final = "bookings"
STORE_SCANS_POLLER: Final = "store_scans"
POLLER_NAMES: Final = (EMAIL_POLLER, BOOKINGS_POLLER, STORE_SCANS_POLLER)
# Wall-clock budget per poller run: once spent, the poller stops between items
# (messages, feeds, scans, files) and leaves the rest for its next run, so one
# slow poller cannot starve the others in the same trigger.
POLLER_BUDGET_SECONDS: Final = 240.0
PollerStatus = Literal["ran", "failed", "skipped_interval", "not_configured", "busy"]


class PollerReport(BaseModel):
    """What one poller did on this trigger."""

    poller: str
    status: PollerStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    next_eligible_at: datetime | None = None
    counts: dict[str, int] = {}
    errors: list[str] = []


class PollRunReport(BaseModel):
    """The outcome of one trigger across every poller."""

    trigger: str
    pollers: list[PollerReport]


@dataclass(frozen=True)
class CaptureRuntime:
    """Everything a poller needs, composed once from the app or the CLI."""

    settings: Any
    session_factory: sessionmaker[Session]
    api_for_session: Callable[[Session], Any]
    users: UserDirectory
    system_actor: AuthContext
    outbound_http_policy: OutboundHttpPolicy
    outbound_http_client: OutboundHttpClient
    rclone_remote_policy: RcloneRemotePolicy
    process_executor: ProcessExecutor
    local_store_access: LocalStoreScanAccess
    # The fixed startup registry snapshot store scans revalidate against; the
    # same provider resolution and health use, never a re-read of configuration.
    store_authority_snapshot_provider: StoreAuthoritySnapshotProvider = field(repr=False)
    state: PollState
    imap_factory: ImapFactory = default_imap_factory
    calendar_fetcher: CalendarFetcher | None = None
    clock: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    poller_budget_seconds: float = POLLER_BUDGET_SECONDS


def email_configured(settings: Any) -> bool:
    return bool(str(settings.email_capture_address or "").strip())


def bookings_configured(settings: Any) -> bool:
    return bool(str(settings.booking_calendars or "").strip())


def store_scans_configured(settings: Any) -> bool:
    return bool(str(settings.store_scans or "").strip())


def any_poller_configured(settings: Any) -> bool:
    return (
        email_configured(settings)
        or bookings_configured(settings)
        or store_scans_configured(settings)
    )


def run_due_pollers(
    runtime: CaptureRuntime,
    *,
    trigger: str,
    only: Collection[str] | None = None,
    force: bool = False,
) -> PollRunReport:
    """Run every configured, due poller once; never raises for a poller's failure."""

    pollers: list[tuple[str, Callable[[Any], bool], _PollerRun]] = [
        (EMAIL_POLLER, email_configured, _run_email),
        (BOOKINGS_POLLER, bookings_configured, _run_bookings),
        (STORE_SCANS_POLLER, store_scans_configured, _run_store_scans),
    ]
    min_interval = timedelta(seconds=float(runtime.settings.integrations_poll_min_interval_seconds))
    reports: list[PollerReport] = []
    for name, configured, run in pollers:
        if only is not None and name not in only:
            continue
        if not configured(runtime.settings):
            reports.append(PollerReport(poller=name, status="not_configured"))
            continue
        started_at = runtime.clock()
        if not force:
            try:
                next_eligible = runtime.state.acquire_run(
                    name, now=started_at, min_interval=min_interval
                )
            except PollStateUnavailable as exc:
                reports.append(PollerReport(poller=name, status="busy", errors=[str(exc)]))
                continue
            if next_eligible is not None:
                reports.append(
                    PollerReport(
                        poller=name, status="skipped_interval", next_eligible_at=next_eligible
                    )
                )
                continue
        reports.append(_run_one(name, run, runtime, started_at=started_at))
    return PollRunReport(trigger=trigger, pollers=reports)


Expired = Callable[[], bool]


@dataclass
class _Outcome:
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add(self, **counts: int) -> None:
        for key, value in counts.items():
            self.counts[key] = self.counts.get(key, 0) + int(value)


_PollerRun = Callable[[CaptureRuntime, Expired], _Outcome]


def _run_one(
    name: str,
    run: _PollerRun,
    runtime: CaptureRuntime,
    *,
    started_at: datetime,
) -> PollerReport:
    status: PollerStatus = "ran"
    deadline = runtime.monotonic() + runtime.poller_budget_seconds

    def expired() -> bool:
        return runtime.monotonic() >= deadline

    try:
        outcome = run(runtime, expired)
    except Exception as exc:  # isolation: one poller never stops the others
        _logger.warning("Capture poller %s failed with %s.", name, type(exc).__name__)
        outcome = _Outcome(errors=[_static_detail(exc)])
        status = "failed"
    finished_at = runtime.clock()
    runtime.state.finish_run(name, now=finished_at, status=status)
    return PollerReport(
        poller=name,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        counts=outcome.counts,
        errors=outcome.errors,
    )


def _static_detail(exc: Exception) -> str:
    if isinstance(exc, (EmailCaptureError, CalendarFetchError, StoreScanError)):
        return str(exc)
    return f"{type(exc).__name__} while polling; see the server log."


# --------------------------------------------------------------------------- email


def _run_email(runtime: CaptureRuntime, expired: Expired) -> _Outcome:
    config = EmailCaptureConfig.from_settings(runtime.settings)
    if config is None:
        return _Outcome()
    max_attachment_bytes = min(MAX_STORED_ATTACHMENT_BYTES, int(runtime.settings.max_upload_bytes))
    outcome = _Outcome()
    with runtime.session_factory() as session:
        api = runtime.api_for_session(session)

        def stage(parsed: ParsedEmail) -> MessageOutcome:
            try:
                return stage_email(
                    parsed,
                    config=config,
                    users=runtime.users,
                    api=api,
                    max_attachment_bytes=max_attachment_bytes,
                )
            except Exception as exc:  # leave the message unseen and retry next poll
                _logger.warning("Email capture could not stage a message (%s).", type(exc).__name__)
                session.rollback()
                return MessageOutcome("failed", "storage_error")

        result = poll_mailbox(
            config=config, imap_factory=runtime.imap_factory, stage=stage, expired=expired
        )
    outcome.add(
        examined=result.examined,
        stored=result.stored,
        duplicate=result.duplicate,
        rejected=result.rejected,
        failed=result.failed,
        notes_created=result.notes_created,
        left_for_next_poll=result.left_for_next_poll,
    )
    outcome.add(
        **{f"rejected_{reason}": count for reason, count in result.rejection_reasons.items()}
    )
    return outcome


# --------------------------------------------------------------------------- bookings


def _run_bookings(runtime: CaptureRuntime, expired: Expired) -> _Outcome:
    calendars = parse_booking_calendars(
        runtime.settings.booking_calendars or "", variable="LAB_TRACKER_BOOKING_CALENDARS"
    )
    fetch = runtime.calendar_fetcher or policy_calendar_fetcher(
        runtime.outbound_http_policy, runtime.outbound_http_client
    )
    outcome = _Outcome()
    now = runtime.clock()
    for position, calendar in enumerate(calendars):
        if expired():
            outcome.add(feeds_left_for_next_poll=len(calendars) - position)
            break
        try:
            with runtime.session_factory() as session:
                result = sync_calendar(
                    calendar,
                    fetch=fetch,
                    api=runtime.api_for_session(session),
                    actor=runtime.system_actor,
                    now=now,
                )
        except Exception as exc:  # one failing feed never stops the next
            _logger.warning(
                "Booking feed for instrument %r failed with %s.",
                calendar.instrument,
                type(exc).__name__,
            )
            outcome.add(feeds_failed=1)
            outcome.errors.append(f"{calendar.instrument}: {_static_detail(exc)}")
            continue
        outcome.add(
            feeds_synced=1,
            bookings_in_window=result.in_window,
            created=result.created,
            updated=result.updated,
            unchanged=result.unchanged,
            cancelled_not_captured=result.cancelled_not_captured,
            left_reviewed=result.left_reviewed,
            skipped_events=result.skipped_events,
        )
    return outcome


# --------------------------------------------------------------------------- store scans


def _run_store_scans(runtime: CaptureRuntime, expired: Expired) -> _Outcome:
    scans = parse_store_scans(
        runtime.settings.store_scans or "", variable="LAB_TRACKER_STORE_SCANS"
    )
    outcome = _Outcome()
    now = runtime.clock()
    for position, scan in enumerate(scans):
        if expired():
            outcome.add(scans_left_for_next_poll=len(scans) - position)
            break
        label = f"{scan.store}/{scan.prefix.path if scan.prefix else ''}"
        try:
            with runtime.session_factory() as session:
                binding = _detach_scan_binding(session, scan)
                authority = _authorize_store_scan(runtime, scan, binding)
                adapter = _store_adapter(runtime, authority)
                result = run_store_scan(
                    scan,
                    authority=authority,
                    adapter=adapter,
                    api=runtime.api_for_session(session),
                    actor=runtime.system_actor,
                    baselines=runtime.state,
                    now=now,
                    hash_max_bytes=int(runtime.settings.store_scan_hash_max_bytes),
                    expired=expired,
                )
        except Exception as exc:  # one failing scan never stops the next
            _logger.warning(
                "Store scan %s failed with %s.", _scan_log_label(scan), type(exc).__name__
            )
            outcome.add(scans_failed=1)
            outcome.errors.append(f"{label}: {_static_detail(exc)}")
            continue
        outcome.add(
            scans_run=1,
            listed=result.listed,
            matched=result.matched,
            baseline_recorded=result.baseline_recorded,
            created=result.created,
            already_captured=result.already_captured,
            settling=result.settling,
            deferred=result.deferred,
            hashed=result.hashed,
            hash_pending=result.hash_pending,
            truncated_listings=int(result.truncated_listing),
        )
    return outcome


def _detach_scan_binding(session: Session, scan: StoreScan) -> DetachedStoreAuthorityBinding | None:
    """Select the scan's store, detach its grant binding, then release the read scope.

    The name resolves like ``store://`` resolution (own store first, then the
    group's) and never falls through to a group store once a project store of
    that name exists. A kind no adapter can list is reported as such before
    detaching: the kind is not secret, and kinds the binding rejects (such as
    ``object_table`` and ``database``) would otherwise surface only as the
    opaque authorization denial. Past this point the released row is never
    consulted.
    """

    store = SQLAlchemyLabTrackerRepository(session).data_stores.get_by_name(
        scan.project_id, scan.store
    )
    unlistable = (
        store.kind
        if store is not None
        and type(store.kind) is StoreKind
        and not adapter_supports_listing(store.kind)
        else None
    )
    binding = (
        detach_store_authority_binding(store) if store is not None and unlistable is None else None
    )
    session.rollback()
    if store is None:
        raise StoreScanError("No data store with that name is registered for the project.")
    if unlistable is not None:
        raise StoreScanError(f"Listing is not supported for {unlistable.value} stores.")
    return binding


def _authorize_store_scan(
    runtime: CaptureRuntime,
    scan: StoreScan,
    binding: DetachedStoreAuthorityBinding | None,
) -> StoreAuthorityUseProof:
    """Capture one snapshot and revalidate the detached binding, or deny opaquely.

    Every check here is pure: a legacy, corrupt, renamed, capability-short,
    revoked, changed, or scope-mismatched binding fails before any adapter,
    filesystem, credential, or subprocess work.
    """

    if binding is None or binding.definition.name != scan.store:
        raise StoreScanError(STORE_SCAN_UNAUTHORIZED_MESSAGE)
    kind = binding.definition.kind
    if not adapter_supports_listing(kind):
        raise StoreScanError(f"Listing is not supported for {kind.value} stores.")
    if StoreCapability.LIST not in binding.capabilities:
        raise StoreScanError(STORE_SCAN_UNAUTHORIZED_MESSAGE)
    proof = revalidate_store_authority_binding(runtime.store_authority_snapshot_provider(), binding)
    if proof is None:
        raise StoreScanError(STORE_SCAN_UNAUTHORIZED_MESSAGE)
    return proof


def _store_adapter(runtime: CaptureRuntime, authority: StoreAuthorityUseProof) -> StoreAdapter:
    deadline = float(runtime.settings.resolver_subprocess_deadline_seconds)
    kind = authority.definition.kind
    if kind is StoreKind.LOCAL_FS:
        # Local enumeration and hashing stay disabled until the revalidated
        # grant root is retained inside the filesystem helper (.63.5): the local
        # adapter checks only the global resolver roots, so an in-grant alias
        # could list and hash another project's files. Once it is retained,
        # build ``runtime.local_store_access``'s adapter from ``authority`` here.
        raise StoreScanError(LOCAL_STORE_SCAN_UNSUPPORTED_MESSAGE)
    if is_rclone_store_kind(kind):
        return RcloneStoreAdapter(
            authority=authority,
            policy=runtime.rclone_remote_policy,
            executor=runtime.process_executor,
            deadline_seconds=deadline,
        )
    raise StoreScanError(f"Listing is not supported for {kind.value} stores.")


def _scan_log_label(scan: StoreScan) -> str:
    return f"{scan.project_id}:{scan.store}"
