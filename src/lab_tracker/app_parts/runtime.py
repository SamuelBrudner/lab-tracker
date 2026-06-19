"""Runtime dependency and lifespan setup for the FastAPI app."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import (
    AuthService,
    DeviceAuthService,
    InvitationTokenService,
    TokenService,
    ensure_local_auth_user,
)
from lab_tracker.config import Settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.file_storage import LocalFileStorageBackend
from lab_tracker.graph_drafting import make_graph_draft_client
from lab_tracker.logging import configure_logging
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.rate_limit import InMemoryRateLimiter

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppRuntime:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    auth_enabled: bool
    auth_service: AuthService
    device_auth_service: DeviceAuthService
    invitation_token_service: InvitationTokenService
    token_service: TokenService
    file_storage_backend: LocalFileStorageBackend
    raw_note_storage: LocalNoteStorage
    lab_tracker_api: LabTrackerAPI
    graph_draft_client_factory: Callable[..., Any]
    auth_rate_limiter: InMemoryRateLimiter


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
    return AppRuntime(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        auth_enabled=auth_enabled,
        auth_service=auth_service,
        device_auth_service=device_auth_service,
        invitation_token_service=invitation_token_service,
        token_service=token_service,
        file_storage_backend=file_storage_backend,
        raw_note_storage=raw_note_storage,
        lab_tracker_api=lab_tracker_api,
        graph_draft_client_factory=make_graph_draft_client,
        auth_rate_limiter=auth_rate_limiter,
    )


def make_lifespan(engine: Engine):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    return lifespan


def configure_app_state(app: FastAPI, runtime: AppRuntime) -> None:
    app.state.db_engine = runtime.engine
    app.state.db_session_factory = runtime.session_factory
    app.state.auth_service = runtime.auth_service
    app.state.device_auth_service = runtime.device_auth_service
    app.state.invitation_token_service = runtime.invitation_token_service
    app.state.auth_enabled = runtime.auth_enabled
    app.state.settings = runtime.settings
    app.state.token_service = runtime.token_service
    app.state.file_storage_backend = runtime.file_storage_backend
    app.state.raw_note_storage = runtime.raw_note_storage
    app.state.lab_tracker_api = runtime.lab_tracker_api
    app.state.graph_draft_client_factory = runtime.graph_draft_client_factory
    app.state.auth_rate_limiter = runtime.auth_rate_limiter


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
