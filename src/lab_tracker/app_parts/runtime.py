"""Runtime dependency and lifespan setup for the FastAPI app."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.auth import (
    AuthService,
    DeviceAuthService,
    InvitationTokenService,
    PersonalAccessTokenService,
    TokenService,
    ensure_local_auth_user,
)
from lab_tracker.backup import database_lock_path
from lab_tracker.config import Settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.file_storage import LocalFileStorageBackend
from lab_tracker.graph_drafting import make_graph_draft_client
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.process_lock import ProcessLock
from lab_tracker.rate_limit import InMemoryRateLimiter
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppRuntime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    auth_enabled: bool
    auth_service: AuthService
    device_auth_service: DeviceAuthService
    personal_access_token_service: PersonalAccessTokenService
    invitation_token_service: InvitationTokenService
    token_service: TokenService
    file_storage_backend: LocalFileStorageBackend
    raw_note_storage: LocalNoteStorage
    lab_tracker_api: LabTrackerAPI
    graph_draft_client_factory: Callable[..., Any]
    auth_rate_limiter: InMemoryRateLimiter
    pat_rate_limiter: InMemoryRateLimiter


def build_app_runtime(settings: Settings) -> AppRuntime:
    configure_logging(settings.log_level)
    engine = get_engine(settings)
    session_factory = get_session_factory(engine=engine)
    auth_enabled = settings.is_auth_enabled()
    _log_startup_config_summary(settings, engine=engine, auth_enabled=auth_enabled)
    if not auth_enabled:
        try:
            ensure_local_auth_user(session_factory)
        except SQLAlchemyError as exc:
            _logger.warning("Local auth user bootstrap skipped: %s", exc)

    auth_service = AuthService(session_factory=session_factory)
    device_auth_service = DeviceAuthService(session_factory=session_factory)
    personal_access_token_service = PersonalAccessTokenService(
        session_factory=session_factory
    )
    token_service = TokenService(
        settings.auth_secret_key,
        ttl_minutes=settings.auth_token_ttl_minutes,
    )
    invitation_token_service = InvitationTokenService(
        settings.auth_secret_key,
        ttl_hours=settings.auth_invite_ttl_hours,
        session_factory=session_factory,
    )
    file_storage_backend = LocalFileStorageBackend(
        settings.file_storage_path,
        max_bytes=settings.max_upload_bytes,
    )
    raw_note_storage = LocalNoteStorage(
        settings.note_storage_path,
        max_bytes=settings.max_upload_bytes,
    )
    lab_tracker_api = LabTrackerAPI(
        raw_storage=raw_note_storage,
        settings=settings,
    )
    auth_rate_limiter = InMemoryRateLimiter(
        max_attempts=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    pat_rate_limiter = InMemoryRateLimiter(
        max_attempts=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    return AppRuntime(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        auth_enabled=auth_enabled,
        auth_service=auth_service,
        device_auth_service=device_auth_service,
        personal_access_token_service=personal_access_token_service,
        invitation_token_service=invitation_token_service,
        token_service=token_service,
        file_storage_backend=file_storage_backend,
        raw_note_storage=raw_note_storage,
        lab_tracker_api=lab_tracker_api,
        graph_draft_client_factory=make_graph_draft_client,
        auth_rate_limiter=auth_rate_limiter,
        pat_rate_limiter=pat_rate_limiter,
    )


def make_lifespan(engine: Engine):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock = _acquire_database_lock(engine)
        scheduler_task = _start_daily_review_scheduler(app)
        try:
            yield
        finally:
            if scheduler_task is not None:
                scheduler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scheduler_task
            if lock is not None:
                lock.release()
            engine.dispose()

    return lifespan


def _start_daily_review_scheduler(app: FastAPI) -> asyncio.Task[None] | None:
    """Start the opt-in in-process daily-review scheduler when enabled.

    Off by default: only when ``LAB_TRACKER_DAILY_REVIEW_IN_PROCESS_SCHEDULER``
    is set does the process run its own clock for run-due drafting, replacing
    the external cron/Scheduled Task. The external trigger keeps working either
    way, so this is a reversible convenience layer, not a new surface.
    """

    settings = getattr(app.state, "settings", None)
    if settings is None or not getattr(
        settings, "daily_review_in_process_scheduler", False
    ):
        return None
    poll_seconds = max(1, getattr(settings, "daily_review_poll_seconds", 900))
    _logger.info(
        "Starting in-process daily-review scheduler (polling every %ds).",
        poll_seconds,
    )
    return asyncio.create_task(_daily_review_scheduler_loop(app, poll_seconds))


def _daily_review_tick(app: FastAPI) -> list[Any]:
    """Run one scheduled drafting pass as the non-interactive system actor.

    Mirrors ``POST /batches/run-due`` but originates inside the process. The
    system actor can DRAFT (admin) yet is structurally barred from accepting or
    committing (``ProjectAuthorizationPolicy.require_interactive``), so this only
    ever produces READY proposals for human review -- never a commit. Runs
    synchronously; callers offload it off the event loop.
    """

    session = app.state.db_session_factory()
    try:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = app.state.lab_tracker_api.for_request(repository)
        draft_client_factory = getattr(
            app.state, "graph_draft_client_factory", make_graph_draft_client
        )
        runs = api.run_due_graph_draft_batches(
            draft_client_factory=draft_client_factory,
            app_settings=app.state.settings,
            actor=system_auth_context(),
        )
        session.commit()
        return runs
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _run_daily_review_tick_safely(app: FastAPI) -> None:
    """Run one tick, logging and swallowing any failure.

    A transient model or database error must never escape to kill the scheduler
    loop and silently stop reviews; the next tick simply tries again.
    """

    try:
        runs = _daily_review_tick(app)
    except Exception:
        _logger.exception("In-process daily-review scheduler tick failed.")
        return
    _logger.info(
        "In-process daily-review scheduler tick completed (%d run(s)).",
        len(runs),
    )


async def _daily_review_scheduler_loop(app: FastAPI, poll_seconds: int) -> None:
    """Poll for due daily-review batches on a fixed interval until cancelled.

    The blocking drafting pass runs off the event loop so it never stalls
    request handling, and cancellation (on shutdown) propagates out of the
    sleep to end the loop cleanly.
    """

    while True:
        await asyncio.sleep(poll_seconds)
        await run_in_threadpool(_run_daily_review_tick_safely, app)


def _acquire_database_lock(engine: Engine) -> ProcessLock | None:
    """Hold the advisory database lock for the server's lifetime.

    This lets the offline ``restore`` command detect a live server (which holds
    this lock) and refuse to clobber a database that is in use. Best-effort and
    SQLite-only: Postgres/in-memory databases get no lock, and a failure to
    acquire never blocks startup — it only means restore cannot rely on the lock
    signal for this process.
    """

    try:
        lock_path = database_lock_path(str(engine.url))
    except Exception:  # pragma: no cover - defensive; never block startup
        return None
    if lock_path is None:
        return None
    lock = ProcessLock(lock_path)
    if lock.acquire():
        return lock
    _logger.warning(
        "Could not acquire the database lock at %s; another Lab Tracker process "
        "may be using this database.",
        lock_path,
    )
    return None


def configure_app_state(app: FastAPI, runtime: AppRuntime) -> None:
    app.state.db_engine = runtime.engine
    app.state.db_session_factory = runtime.session_factory
    app.state.auth_service = runtime.auth_service
    app.state.device_auth_service = runtime.device_auth_service
    app.state.personal_access_token_service = runtime.personal_access_token_service
    app.state.invitation_token_service = runtime.invitation_token_service
    app.state.auth_enabled = runtime.auth_enabled
    app.state.settings = runtime.settings
    app.state.token_service = runtime.token_service
    app.state.file_storage_backend = runtime.file_storage_backend
    app.state.raw_note_storage = runtime.raw_note_storage
    app.state.lab_tracker_api = runtime.lab_tracker_api
    app.state.graph_draft_client_factory = runtime.graph_draft_client_factory
    app.state.auth_rate_limiter = runtime.auth_rate_limiter
    app.state.pat_rate_limiter = runtime.pat_rate_limiter


def _log_startup_config_summary(
    settings: Settings,
    *,
    engine: Engine,
    auth_enabled: bool,
) -> None:
    _logger.info(
        "Lab Tracker startup: environment=%s database_backend=%s auth_enabled=%s",
        settings.environment,
        engine.dialect.name,
        auth_enabled,
    )
