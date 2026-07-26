"""Runtime dependency and lifespan setup for the FastAPI app."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.artifact_resolution import (
    LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV,
    ResolverRegistry,
    check_store_health,
    outbound_http_policy_from_config,
    registry_from_env,
)
from lab_tracker.artifact_resolution_admission import ArtifactResolutionAdmission
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
from lab_tracker.git_remote_policy import GitRemotePolicy
from lab_tracker.graph_drafting import GraphDraftClientFactory, make_graph_draft_client
from lab_tracker.logging import configure_logging
from lab_tracker.models import ReviewEmailDelivery
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.outbound_http import OutboundHttpPolicy
from lab_tracker.process_lock import ProcessLock
from lab_tracker.rate_limit import InMemoryRateLimiter
from lab_tracker.review_email_transport import (
    ReviewEmailDeliveryError,
    ReviewEmailProvider,
    ReviewReadyEmail,
    SMTPReviewEmailProvider,
    SMTPSettings,
    SMTPTLSMode,
)
from lab_tracker.review_links import sign_review_link
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.store_health import (
    CachedStoreHealthProbe,
    StoreHealth,
    StoreProbeTarget,
)
from lab_tracker.store_health_admission import StoreHealthAdmission

_logger = logging.getLogger(__name__)


class _OwnedGitHealthWorkdir:
    """Own a private empty directory with silent GC fallback cleanup."""

    def __init__(self) -> None:
        path = Path(mkdtemp(prefix="lab-tracker-git-health-"))
        try:
            os.chmod(path, 0o700)
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise
        self.path = path
        self._finalizer = weakref.finalize(
            self,
            shutil.rmtree,
            path,
            ignore_errors=True,
        )

    def cleanup(self) -> None:
        """Remove the directory at most once."""

        self._finalizer()


@dataclass(frozen=True)
class _LegacyStoreHealthProbe:
    """Adapt the legacy leaf probes to the detached control-plane target."""

    git_remote_policy: GitRemotePolicy
    git_health_workdir: Path

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        return check_store_health(
            target,
            git_remote_policy=self.git_remote_policy,
            git_health_cwd=self.git_health_workdir,
        )


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
    graph_draft_client_factory: GraphDraftClientFactory
    review_email_provider: ReviewEmailProvider | None
    auth_rate_limiter: InMemoryRateLimiter
    pat_rate_limiter: InMemoryRateLimiter
    outbound_http_policy: OutboundHttpPolicy
    git_remote_policy: GitRemotePolicy
    resolver_registry: ResolverRegistry
    artifact_resolution_admission: ArtifactResolutionAdmission
    store_health_admission: StoreHealthAdmission
    git_health_workdir: Path
    store_health_checker: CachedStoreHealthProbe
    _git_health_workdir_owner: _OwnedGitHealthWorkdir = field(
        repr=False,
        compare=False,
    )

    def cleanup_git_health_workdir(self) -> None:
        """Remove the app-owned non-repository Git health working directory."""

        self._git_health_workdir_owner.cleanup()


def build_app_runtime(settings: Settings) -> AppRuntime:
    configure_logging(settings.log_level)
    outbound_http_policy = outbound_http_policy_from_config(
        allowed_authorities=settings.resolver_http_allowed_authorities,
        allowed_networks=settings.resolver_http_allowed_networks,
    )
    git_remote_policy = GitRemotePolicy.from_config(
        settings.git_allowed_remotes,
        variable=LAB_TRACKER_GIT_ALLOWED_REMOTES_ENV,
    )
    git_health_workdir_owner = _OwnedGitHealthWorkdir()
    try:
        resolver_registry = registry_from_env(
            http_policy=outbound_http_policy,
            git_remote_policy=git_remote_policy,
            http_deadline_seconds=settings.resolver_http_deadline_seconds,
            subprocess_deadline_seconds=settings.resolver_subprocess_deadline_seconds,
        )
        return _build_app_runtime(
            settings,
            outbound_http_policy=outbound_http_policy,
            git_remote_policy=git_remote_policy,
            resolver_registry=resolver_registry,
            git_health_workdir_owner=git_health_workdir_owner,
        )
    except BaseException:
        git_health_workdir_owner.cleanup()
        raise


def _build_app_runtime(
    settings: Settings,
    *,
    outbound_http_policy: OutboundHttpPolicy,
    git_remote_policy: GitRemotePolicy,
    resolver_registry: ResolverRegistry,
    git_health_workdir_owner: _OwnedGitHealthWorkdir,
) -> AppRuntime:
    git_health_workdir = git_health_workdir_owner.path
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
    artifact_resolution_admission = ArtifactResolutionAdmission(
        global_in_flight_limit=settings.artifact_resolution_global_in_flight_limit,
        per_actor_in_flight_limit=settings.artifact_resolution_per_actor_in_flight_limit,
    )
    store_health_admission = StoreHealthAdmission(
        global_in_flight_limit=settings.store_health_global_in_flight_limit,
        per_actor_in_flight_limit=settings.store_health_per_actor_in_flight_limit,
    )
    store_health_checker = CachedStoreHealthProbe(
        _LegacyStoreHealthProbe(
            git_remote_policy=git_remote_policy,
            git_health_workdir=git_health_workdir,
        ),
        max_entries=settings.store_health_cache_max_entries,
        ttl_seconds=settings.store_health_cache_ttl_seconds,
        singleflight_wait_seconds=settings.store_health_singleflight_wait_seconds,
    )
    review_email_provider = _build_review_email_provider(settings)
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
        review_email_provider=review_email_provider,
        auth_rate_limiter=auth_rate_limiter,
        pat_rate_limiter=pat_rate_limiter,
        outbound_http_policy=outbound_http_policy,
        git_remote_policy=git_remote_policy,
        resolver_registry=resolver_registry,
        artifact_resolution_admission=artifact_resolution_admission,
        store_health_admission=store_health_admission,
        git_health_workdir=git_health_workdir,
        store_health_checker=store_health_checker,
        _git_health_workdir_owner=git_health_workdir_owner,
    )


def make_lifespan(
    runtime: AppRuntime,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock: ProcessLock | None = None
        background_tasks: list[asyncio.Task[None]] = []
        try:
            lock = _acquire_database_lock(runtime.engine)
            background_tasks = [
                *_start_graph_draft_background_tasks(app),
                *_start_review_email_background_tasks(app),
            ]
            yield
        finally:
            try:
                try:
                    await _stop_background_tasks(background_tasks)
                finally:
                    if lock is not None:
                        lock.release()
            finally:
                try:
                    runtime.engine.dispose()
                finally:
                    runtime.cleanup_git_health_workdir()

    return lifespan


def _start_graph_draft_background_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    settings = getattr(app.state, "settings", None)
    if settings is None or not _graph_draft_background_enabled(settings):
        return []
    tasks = [asyncio.create_task(_graph_draft_worker_loop(app))]
    if settings.graph_draft_scheduler_enabled:
        tasks.append(asyncio.create_task(_graph_draft_scheduler_loop(app)))
    return tasks


def _start_review_email_background_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    settings = getattr(app.state, "settings", None)
    provider = getattr(app.state, "review_email_provider", None)
    if (
        settings is None
        or not settings.review_email_enabled
        or settings.review_email_transport != "smtp"
        or provider is None
    ):
        return []
    return [asyncio.create_task(_review_email_worker_loop(app))]


async def _stop_background_tasks(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _graph_draft_background_enabled(settings: Settings) -> bool:
    return bool(settings.graph_draft_background_enabled or settings.graph_draft_scheduler_enabled)


async def _graph_draft_worker_loop(app: FastAPI) -> None:
    settings = app.state.settings
    poll_seconds = settings.graph_draft_worker_poll_seconds
    while True:
        try:
            processed = await asyncio.to_thread(_process_one_graph_draft_batch_run, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Graph draft background worker tick failed.")
            processed = False
        if not processed:
            await asyncio.sleep(poll_seconds)


async def _graph_draft_scheduler_loop(app: FastAPI) -> None:
    settings = app.state.settings
    interval_seconds = settings.graph_draft_scheduler_interval_seconds
    while True:
        try:
            await asyncio.to_thread(_enqueue_due_graph_draft_batches, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Graph draft scheduler tick failed.")
        await asyncio.sleep(interval_seconds)


async def _review_email_worker_loop(app: FastAPI) -> None:
    settings = app.state.settings
    while True:
        try:
            processed = await asyncio.to_thread(_process_one_review_email, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Review email worker tick failed.")
            processed = False
        if not processed:
            await asyncio.sleep(settings.review_email_worker_poll_seconds)


def _process_one_graph_draft_batch_run(app: FastAPI) -> bool:
    with app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=app.state.settings,
            surface="background",
        )
        run = api.process_next_graph_draft_batch_run(
            draft_client_factory=app.state.graph_draft_client_factory,
            app_settings=app.state.settings,
            actor=system_auth_context(),
        )
        return run is not None


def _enqueue_due_graph_draft_batches(app: FastAPI) -> None:
    with app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=app.state.settings,
            surface="background",
        )
        api.enqueue_due_graph_draft_batches(actor=system_auth_context())


def _process_one_review_email(app: FastAPI) -> bool:
    provider = app.state.review_email_provider
    if provider is None:
        return False
    with app.state.db_session_factory() as session:
        api = LabTrackerAPI(
            raw_storage=app.state.raw_note_storage,
            repository=SQLAlchemyLabTrackerRepository(session),
            settings=app.state.settings,
            surface="background",
        )
        delivery = api.review_emails.claim_next(
            lease_seconds=app.state.settings.review_email_claim_lease_seconds
        )
        if delivery is None:
            return False
        claim_token = delivery.claim_token
        if claim_token is None:
            raise RuntimeError("Claimed review email has no lease token.")
        try:
            review_url = _review_email_url(app.state.settings, delivery)
            result = provider.send_review_ready(
                ReviewReadyEmail(
                    recipient_email=delivery.destination_email,
                    review_url=review_url,
                    idempotency_key=delivery.idempotency_key,
                    event_type=delivery.event_type,
                )
            )
        except ReviewEmailDeliveryError as exc:
            api.review_emails.mark_failed(
                delivery.delivery_id,
                claim_token=claim_token,
                error_message=str(exc),
                retryable=exc.retryable,
            )
        except Exception:
            _logger.exception("Review email message preparation failed.")
            api.review_emails.mark_failed(
                delivery.delivery_id,
                claim_token=claim_token,
                error_message="Review email message preparation failed.",
                retryable=False,
            )
        else:
            api.review_emails.mark_accepted(
                delivery.delivery_id,
                claim_token=claim_token,
                provider_message_id=result.message_id,
            )
        return True


def _review_email_url(
    settings: Settings,
    delivery: ReviewEmailDelivery,
) -> str:
    base_url = settings.public_base_url.rstrip("/")
    if delivery.event_type == "test":
        return f"{base_url}/app/"
    if (
        delivery.change_set_id is None
        or delivery.recipient_user_id is None
    ):
        raise ValueError("Review-ready delivery is missing its review binding.")
    token = sign_review_link(
        settings.auth_secret_key,
        delivery.change_set_id,
        recipient_user_id=delivery.recipient_user_id,
        delivery_id=delivery.delivery_id,
        ttl_minutes=settings.review_email_link_ttl_minutes,
    )
    return f"{base_url}/r/{token}"


def _build_review_email_provider(settings: Settings) -> ReviewEmailProvider | None:
    if not settings.review_email_enabled or settings.review_email_transport != "smtp":
        return None
    return SMTPReviewEmailProvider(
        SMTPSettings(
            host=settings.review_email_smtp_host,
            port=settings.review_email_smtp_port,
            sender_email=settings.review_email_smtp_from_address,
            username=settings.review_email_smtp_username or None,
            password=settings.review_email_smtp_password or None,
            tls_mode=SMTPTLSMode(settings.review_email_smtp_tls_mode),
            timeout_seconds=settings.review_email_smtp_timeout_seconds,
        )
    )


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
    app.state.review_email_provider = runtime.review_email_provider
    app.state.auth_rate_limiter = runtime.auth_rate_limiter
    app.state.pat_rate_limiter = runtime.pat_rate_limiter
    app.state.outbound_http_policy = runtime.outbound_http_policy
    app.state.git_remote_policy = runtime.git_remote_policy
    app.state.resolver_registry = runtime.resolver_registry
    app.state.artifact_resolution_admission = runtime.artifact_resolution_admission
    app.state.store_health_admission = runtime.store_health_admission
    app.state.git_health_workdir = runtime.git_health_workdir
    app.state.store_health_checker = runtime.store_health_checker
    app.state.cleanup_git_health_workdir = runtime.cleanup_git_health_workdir


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
