"""In-process scheduler that drives due graph-draft batches.

Disabled by default (``Settings.scheduler_enabled``). When enabled, the app
lifespan launches :func:`run_due_batch_scheduler` as a background task. Each tick
runs the existing run-due batch logic in a worker thread against a fresh request
scope, reusing the same ``claim_due`` idempotency that guards the HTTP
``/batches/run-due`` endpoint, so running this alongside manual runs (or several
worker processes) never double-fires a project.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from lab_tracker.app_parts.middleware import local_auth_context
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

if TYPE_CHECKING:
    from lab_tracker.app_parts.runtime import AppRuntime

_logger = logging.getLogger(__name__)


def _run_due_batch_tick(runtime: AppRuntime) -> None:
    """Run one due-batch pass in a fresh, self-closing request scope.

    Mirrors the HTTP db-session middleware: a new session per tick (closed on
    scope exit) and an explicit commit so the ``claim_due`` advance of
    ``next_run_at`` persists. The scheduler actor is the local admin so the
    admin-gated run-due path is authorized.
    """
    session = runtime.session_factory()
    repository = SQLAlchemyLabTrackerRepository(session)
    with runtime.lab_tracker_api.request_scope(
        repository,
        surface="scheduler",
        close=session.close,
    ) as scope:
        scope.api.run_due_graph_draft_batches(
            draft_client_factory=runtime.graph_draft_client_factory,
            app_settings=runtime.settings,
            actor=local_auth_context(),
        )
        scope.commit()


async def run_due_batch_scheduler(runtime: AppRuntime) -> None:
    """Periodically run due graph-draft batches until cancelled.

    Sleeps first, then awaits a single threadpool tick before sleeping again, so
    a slow LLM batch cannot overlap the next tick within this process. A failing
    tick is logged and the loop continues; cancellation exits cleanly.
    """
    interval = runtime.settings.scheduler_interval_seconds
    _logger.info("Graph-draft batch scheduler started (interval=%ss).", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await run_in_threadpool(_run_due_batch_tick, runtime)
            except Exception:  # noqa: BLE001 - one bad tick must not kill the loop
                _logger.exception("Scheduled graph-draft batch tick failed.")
    except asyncio.CancelledError:
        _logger.info("Graph-draft batch scheduler stopping.")
        raise
