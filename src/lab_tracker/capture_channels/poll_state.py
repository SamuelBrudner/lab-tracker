"""Durable, cross-process poll state: per-poller rate limits and scan baselines.

One small JSON file shared by every trigger (the in-process ticker, ``POST
/integrations/run-due``, and ``lab-tracker integrations poll``) so a poller runs
at most once per ``LAB_TRACKER_INTEGRATIONS_POLL_MIN_INTERVAL_SECONDS`` no
matter who asks, and a store scan remembers the baseline it recorded on its
first run. Writes are atomic (temp file + replace, mode ``0600``) under an
in-process lock and a non-blocking cross-process sidecar lock; a caller that
cannot take the lock reports the poller as busy rather than running unthrottled.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from lab_tracker.process_lock import ProcessLock

_logger = logging.getLogger(__name__)

STATE_VERSION: Final = 1
DEFAULT_STATE_FILENAME: Final = ".integrations-poll-state.json"
_LOCK_ATTEMPTS: Final = 20
_LOCK_RETRY_SECONDS: Final = 0.05
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class PollStateUnavailable(RuntimeError):
    """The state file is locked by another process or cannot be written."""


def default_state_path(*, note_storage_path: str) -> Path:
    """Where the state lives when ``LAB_TRACKER_INTEGRATIONS_STATE_PATH`` is unset."""

    return Path(note_storage_path).expanduser() / DEFAULT_STATE_FILENAME


class PollState:
    """File-backed poll state; every public method is atomic across processes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        with _PROCESS_LOCKS_GUARD:
            self._thread_lock = _PROCESS_LOCKS.setdefault(str(self.path), threading.Lock())

    @classmethod
    def from_settings(cls, settings: Any) -> PollState:
        configured = str(getattr(settings, "integrations_state_path", "") or "").strip()
        if configured:
            return cls(configured)
        return cls(default_state_path(note_storage_path=str(settings.note_storage_path)))

    def acquire_run(
        self, poller: str, *, now: datetime, min_interval: timedelta
    ) -> datetime | None:
        """Claim a run of ``poller``; return ``None`` or the next eligible time if too soon."""

        with self._locked() as state:
            pollers = state.setdefault("pollers", {})
            entry = pollers.setdefault(poller, {})
            last = _parse_time(entry.get("last_started_at"))
            if last is not None and now - last < min_interval:
                return last + min_interval
            entry["last_started_at"] = now.isoformat()
            self._write(state)
            return None

    def finish_run(self, poller: str, *, now: datetime, status: str) -> None:
        """Record how a claimed run ended (best-effort; never raises)."""

        try:
            with self._locked() as state:
                entry = state.setdefault("pollers", {}).setdefault(poller, {})
                entry["last_finished_at"] = now.isoformat()
                entry["last_status"] = status
                self._write(state)
        except PollStateUnavailable:
            _logger.warning("Could not record the %s poller's outcome.", poller)

    def last_runs(self) -> dict[str, dict[str, str]]:
        """A snapshot of every poller's last start, finish, and status."""

        with self._locked() as state:
            pollers = state.get("pollers", {})
            return {name: dict(entry) for name, entry in pollers.items() if isinstance(entry, dict)}

    def store_scan_baseline(self, scan_key: str) -> frozenset[str] | None:
        with self._locked() as state:
            entry = state.get("store_scan_baselines", {}).get(scan_key)
            if not isinstance(entry, dict) or not isinstance(entry.get("keys"), list):
                return None
            return frozenset(str(key) for key in entry["keys"])

    def set_store_scan_baseline(self, scan_key: str, keys: frozenset[str]) -> None:
        with self._locked() as state:
            state.setdefault("store_scan_baselines", {})[scan_key] = {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "keys": sorted(keys),
            }
            self._write(state)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with self._thread_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise PollStateUnavailable("The poll state directory cannot be created.") from exc
            lock = ProcessLock(self.path.with_name(self.path.name + ".lock"))
            if not _acquire_briefly(lock):
                raise PollStateUnavailable("Another process holds the poll state lock.")
            try:
                yield self._read()
            finally:
                lock.release()

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": STATE_VERSION}
        except (OSError, ValueError) as exc:
            _logger.warning(
                "Poll state at %s is unreadable (%s); starting afresh.",
                self.path,
                type(exc).__name__,
            )
            return {"version": STATE_VERSION}
        if not isinstance(payload, dict):
            return {"version": STATE_VERSION}
        return payload

    def _write(self, state: dict[str, Any]) -> None:
        state["version"] = STATE_VERSION
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=self.path.name, suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, sort_keys=True)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise PollStateUnavailable("The poll state file cannot be written.") from exc


def _acquire_briefly(lock: ProcessLock) -> bool:
    """Take the sidecar lock, waiting at most about a second (it guards a tiny write)."""

    for attempt in range(_LOCK_ATTEMPTS):
        try:
            if lock.acquire():
                return True
        except OSError as exc:
            raise PollStateUnavailable("The poll state lock file cannot be opened.") from exc
        if attempt + 1 < _LOCK_ATTEMPTS:
            time.sleep(_LOCK_RETRY_SECONDS)
    return False


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
