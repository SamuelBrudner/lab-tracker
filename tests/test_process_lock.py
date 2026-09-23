from __future__ import annotations

from pathlib import Path

import pytest

from lab_tracker.process_lock import ProcessLock, ProcessLockUnavailableError


def test_context_manager_holds_the_lock_for_the_body(tmp_path: Path) -> None:
    lock_path = tmp_path / "db.lock"

    with ProcessLock(lock_path) as lock:
        assert lock.held is True
        assert ProcessLock(lock_path).acquire() is False

    assert lock.held is False
    assert ProcessLock(lock_path).acquire() is True


def test_context_manager_refuses_to_run_the_body_unlocked(tmp_path: Path) -> None:
    lock_path = tmp_path / "db.lock"
    holder = ProcessLock(lock_path)
    assert holder.acquire() is True
    body_ran = False

    try:
        with pytest.raises(ProcessLockUnavailableError), ProcessLock(lock_path):
            body_ran = True
    finally:
        holder.release()

    assert body_ran is False
