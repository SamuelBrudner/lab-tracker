"""Tests for the in-process due-batch scheduler (lab-tracker-1lfm.5)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lab_tracker.app_parts import scheduler as scheduler_module
from lab_tracker.app_parts.runtime import make_lifespan
from lab_tracker.auth import LOCAL_AUTH_USER_ID, Role


class _FakeScope:
    def __init__(self, recorder: dict, *, raise_in_run: bool = False) -> None:
        self._recorder = recorder
        self._raise = raise_in_run
        self.api = SimpleNamespace(run_due_graph_draft_batches=self._run)

    def _run(self, *, draft_client_factory, app_settings, actor):
        self._recorder["actor"] = actor
        self._recorder["draft_client_factory"] = draft_client_factory
        self._recorder["app_settings"] = app_settings
        if self._raise:
            raise RuntimeError("boom")
        self._recorder["ran"] = True

    def __enter__(self) -> _FakeScope:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._recorder.setdefault("exit", []).append(exc_type)
        return False

    def commit(self) -> None:
        self._recorder["committed"] = True


def _tick_runtime(recorder: dict, *, raise_in_run: bool = False, interval: int = 1):
    session = SimpleNamespace(close=lambda: recorder.__setitem__("closed", True))
    scope = _FakeScope(recorder, raise_in_run=raise_in_run)

    def request_scope(repo, *, surface, close):
        recorder["surface"] = surface
        recorder["repo"] = repo
        recorder["close"] = close
        return scope

    return SimpleNamespace(
        settings=SimpleNamespace(scheduler_interval_seconds=interval),
        session_factory=lambda: session,
        lab_tracker_api=SimpleNamespace(request_scope=request_scope),
        graph_draft_client_factory=lambda settings: SimpleNamespace(),
    )


def test_tick_runs_due_batches_with_admin_actor_and_commits(monkeypatch):
    recorder: dict = {}
    monkeypatch.setattr(
        scheduler_module, "SQLAlchemyLabTrackerRepository", lambda session: ("repo", session)
    )
    scheduler_module._run_due_batch_tick(_tick_runtime(recorder))

    assert recorder["ran"] is True
    assert recorder["committed"] is True
    assert recorder["surface"] == "scheduler"
    actor = recorder["actor"]
    assert actor.user_id == LOCAL_AUTH_USER_ID
    assert actor.role == Role.ADMIN


def test_tick_propagates_error_and_does_not_commit(monkeypatch):
    recorder: dict = {}
    monkeypatch.setattr(
        scheduler_module, "SQLAlchemyLabTrackerRepository", lambda session: None
    )
    with pytest.raises(RuntimeError, match="boom"):
        scheduler_module._run_due_batch_tick(_tick_runtime(recorder, raise_in_run=True))

    assert "committed" not in recorder
    # The scope saw the exception (so it rolled back and closed).
    assert recorder["exit"][0] is RuntimeError


def test_scheduler_exits_cleanly_on_cancel(monkeypatch):
    async def fake_threadpool(fn, runtime):
        return None

    monkeypatch.setattr(scheduler_module, "run_in_threadpool", fake_threadpool)
    runtime = _tick_runtime({}, interval=0)

    async def drive():
        task = asyncio.create_task(scheduler_module.run_due_batch_scheduler(runtime))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())


def test_scheduler_isolates_tick_errors_and_keeps_running(monkeypatch):
    calls = {"n": 0}
    survived = asyncio.Event()

    async def fake_threadpool(fn, runtime):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        survived.set()

    monkeypatch.setattr(scheduler_module, "run_in_threadpool", fake_threadpool)
    runtime = _tick_runtime({}, interval=0)

    async def drive():
        task = asyncio.create_task(scheduler_module.run_due_batch_scheduler(runtime))
        # A failing first tick must not kill the loop: wait until a later tick runs.
        await asyncio.wait_for(survived.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    assert calls["n"] >= 2  # survived the first error and kept ticking


def _lifespan_runtime(*, enabled: bool, disposed: list, interval: int = 1):
    return SimpleNamespace(
        settings=SimpleNamespace(
            scheduler_enabled=enabled,
            scheduler_interval_seconds=interval,
        ),
        engine=SimpleNamespace(dispose=lambda: disposed.append("disposed")),
    )


def test_lifespan_starts_no_scheduler_when_disabled(monkeypatch):
    started: list = []

    async def fake_scheduler(runtime):
        started.append(runtime)
        await asyncio.sleep(3600)

    monkeypatch.setattr(scheduler_module, "run_due_batch_scheduler", fake_scheduler)
    disposed: list = []
    lifespan = make_lifespan(_lifespan_runtime(enabled=False, disposed=disposed))

    async def drive():
        async with lifespan(SimpleNamespace()):
            await asyncio.sleep(0)

    asyncio.run(drive())
    assert started == []
    assert disposed == ["disposed"]


def test_lifespan_starts_and_cancels_scheduler_when_enabled(monkeypatch):
    started: list = []
    cancelled = {"flag": False}

    async def fake_scheduler(runtime):
        started.append(runtime)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled["flag"] = True
            raise

    monkeypatch.setattr(scheduler_module, "run_due_batch_scheduler", fake_scheduler)
    disposed: list = []
    lifespan = make_lifespan(_lifespan_runtime(enabled=True, disposed=disposed))

    async def drive():
        async with lifespan(SimpleNamespace()):
            await asyncio.sleep(0)

    asyncio.run(drive())
    assert len(started) == 1
    assert cancelled["flag"] is True
    assert disposed == ["disposed"]
