"""Compose the capture pollers from a running app, and the optional ticker."""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from fastapi import FastAPI

from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.capture_channels.dispatch import (
    CaptureRuntime,
    any_poller_configured,
    run_due_pollers,
)
from lab_tracker.capture_channels.poll_state import PollState

_logger = logging.getLogger(__name__)

MAX_TICK_SECONDS: Final = 60.0


def capture_runtime_from_app(app: FastAPI) -> CaptureRuntime:
    """The app's poller runtime (tests may pre-seed ``app.state.capture_runtime``)."""

    cached = getattr(app.state, "capture_runtime", None)
    if isinstance(cached, CaptureRuntime):
        return cached
    state = app.state
    runtime = CaptureRuntime(
        settings=state.settings,
        session_factory=state.db_session_factory,
        api_for_session=lambda session: state.session_api_factory(session, surface="background"),
        users=state.auth_service,
        system_actor=system_auth_context(),
        outbound_http_policy=state.outbound_http_policy,
        outbound_http_client=state.outbound_http_client,
        rclone_remote_policy=state.rclone_remote_policy,
        process_executor=state.process_executor,
        local_filesystem_operations=state.local_filesystem_operations,
        state=PollState.from_settings(state.settings),
    )
    app.state.capture_runtime = runtime
    return runtime


def start_capture_poller_tasks(app: FastAPI) -> list[asyncio.Task[None]]:
    """Start the in-process ticker when the operator enabled it and a poller is configured."""

    settings = getattr(app.state, "settings", None)
    if (
        settings is None
        or not getattr(settings, "integrations_poller_enabled", False)
        or not any_poller_configured(settings)
    ):
        return []
    return [asyncio.create_task(_capture_poller_loop(app))]


async def _capture_poller_loop(app: FastAPI) -> None:
    tick = min(MAX_TICK_SECONDS, float(app.state.settings.integrations_poll_min_interval_seconds))
    while True:
        try:
            await asyncio.to_thread(
                run_due_pollers, capture_runtime_from_app(app), trigger="ticker"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Capture poller tick failed.")
        await asyncio.sleep(tick)
