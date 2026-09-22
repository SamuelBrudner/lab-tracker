"""File watcher integration for acquisition outputs."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from types import MappingProxyType
from uuid import UUID

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
from lab_tracker.errors import AuthError, NotFoundError, RateLimitError, ValidationError
from lab_tracker.file_watch import discover_files
from lab_tracker.models import AcquisitionOutput

_logger = logging.getLogger(__name__)

_DEFAULT_MAX_FAILURE_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class RegistrationFailure:
    """The latest unresolved registration failure for one watched file."""

    path: Path
    attempts: int
    last_error: str
    retry_after: float | None
    """``time.monotonic()`` deadline for the next attempt; ``None`` when the
    file was rejected and is retried only after it changes."""


@dataclass(frozen=True)
class _FileFingerprint:
    size_bytes: int
    mtime: float
    checksum: str


def _hash_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_hidden_relative(path: Path, *, relative_to: Path) -> bool:
    try:
        candidate = path.relative_to(relative_to)
    except ValueError:
        candidate = path
    return any(part.startswith(".") for part in candidate.parts)


class AcquisitionOutputWatcher:
    """Polls one or more directories and registers new outputs with the API."""

    def __init__(
        self,
        api: LabTrackerAPI,
        session_id: UUID,
        watch_paths: Iterable[str | Path],
        *,
        actor: AuthContext | None = None,
        base_path: str | Path | None = None,
        ignore_hidden: bool = True,
        failure_backoff_seconds: float = 5.0,
        max_failure_backoff_seconds: float | None = None,
        persistent_failure_threshold: int = 5,
    ) -> None:
        self._api = api
        self._session_id = session_id
        self._watch_paths = [Path(path) for path in watch_paths]
        if not self._watch_paths:
            raise ValueError("watch_paths must not be empty.")
        if failure_backoff_seconds < 0:
            raise ValueError("failure_backoff_seconds must be 0 or greater.")
        if max_failure_backoff_seconds is None:
            # Default cap; never below the base so callers that predate the cap
            # and pass a larger base backoff keep working.
            max_failure_backoff_seconds = max(
                _DEFAULT_MAX_FAILURE_BACKOFF_SECONDS, failure_backoff_seconds
            )
        elif max_failure_backoff_seconds < failure_backoff_seconds:
            raise ValueError(
                "max_failure_backoff_seconds must be at least failure_backoff_seconds."
            )
        if persistent_failure_threshold < 1:
            raise ValueError("persistent_failure_threshold must be 1 or greater.")
        self._actor = actor
        self._ignore_hidden = ignore_hidden
        self._failure_backoff_seconds = failure_backoff_seconds
        self._max_failure_backoff_seconds = max_failure_backoff_seconds
        self._persistent_failure_threshold = persistent_failure_threshold
        self._base_path = Path(base_path).resolve() if base_path is not None else None
        self._fingerprints: dict[Path, _FileFingerprint] = {}
        self._failures: dict[Path, RegistrationFailure] = {}
        self._rejected: dict[Path, _FileFingerprint] = {}
        self._failure_count = 0

    @property
    def failures(self) -> Mapping[Path, RegistrationFailure]:
        """Files whose latest registration attempt failed (read-only view)."""
        return MappingProxyType(self._failures)

    @property
    def failure_count(self) -> int:
        """Total registration failures since the watcher was created."""
        return self._failure_count

    def scan(self) -> list[AcquisitionOutput]:
        """Register new or changed files and return the registered outputs.

        A transient registration failure is logged (WARNING, escalating to ERROR
        once a file has failed ``persistent_failure_threshold`` times in a row)
        and retried with exponential backoff. A ``ValidationError`` rejects that
        file until it changes. ``NotFoundError`` and ``AuthError`` mean the
        session or actor can never register anything, so they are logged and
        re-raised, which also stops ``run()``.
        """
        outputs: list[AcquisitionOutput] = []
        now = time.monotonic()
        seen: set[Path] = set()
        for file_path in self._iter_files():
            seen.add(file_path)
            failure = self._failures.get(file_path)
            if (
                failure is not None
                and failure.retry_after is not None
                and failure.retry_after > now
            ):
                continue
            try:
                current_stat = file_path.stat()
            except (FileNotFoundError, PermissionError):
                continue
            rejected = self._rejected.get(file_path)
            if (
                rejected is not None
                and rejected.size_bytes == current_stat.st_size
                and rejected.mtime == current_stat.st_mtime
            ):
                continue
            fingerprint = self._fingerprints.get(file_path)
            if (
                fingerprint
                and fingerprint.size_bytes == current_stat.st_size
                and fingerprint.mtime == current_stat.st_mtime
            ):
                continue
            new_fingerprint = self._stable_fingerprint(file_path)
            if new_fingerprint is None:
                continue
            if fingerprint and fingerprint.checksum == new_fingerprint.checksum:
                self._fingerprints[file_path] = new_fingerprint
                continue
            try:
                output = self._api.register_acquisition_output(
                    self._session_id,
                    file_path=self._format_path(file_path),
                    checksum=new_fingerprint.checksum,
                    size_bytes=new_fingerprint.size_bytes,
                    actor=self._actor,
                )
            except (NotFoundError, AuthError) as error:
                if isinstance(error, RateLimitError):
                    self._record_transient_failure(file_path, error, now=now)
                    continue
                self._failure_count += 1
                _logger.error(
                    "Acquisition output watcher for session %s cannot register %s: %s; "
                    "stopping because every later registration would fail the same way.",
                    self._session_id,
                    file_path,
                    error,
                )
                raise
            except ValidationError as error:
                self._record_rejection(file_path, new_fingerprint, error)
                continue
            except Exception as error:
                self._record_transient_failure(file_path, error, now=now)
                continue
            outputs.append(output)
            self._failures.pop(file_path, None)
            self._rejected.pop(file_path, None)
            self._fingerprints[file_path] = new_fingerprint
        # Forget failures for files that no longer exist so `failures` only
        # reports outputs that are still waiting to be registered.
        for gone in [path for path in self._failures if path not in seen]:
            del self._failures[gone]
            self._rejected.pop(gone, None)
        return outputs

    def _record_transient_failure(self, path: Path, error: Exception, *, now: float) -> None:
        previous = self._failures.get(path)
        attempts = 1 if previous is None else previous.attempts + 1
        delay = min(
            self._failure_backoff_seconds * (2 ** (attempts - 1)),
            self._max_failure_backoff_seconds,
        )
        self._failures[path] = RegistrationFailure(
            path=path,
            attempts=attempts,
            last_error=f"{type(error).__name__}: {error}",
            retry_after=now + delay,
        )
        self._failure_count += 1
        persistent = attempts >= self._persistent_failure_threshold
        _logger.log(
            logging.ERROR if persistent else logging.WARNING,
            "Acquisition output watcher for session %s failed to register %s "
            "(attempt %d%s): %s; retrying in %.1fs.",
            self._session_id,
            path,
            attempts,
            ", persistent failure" if persistent else "",
            error,
            delay,
            exc_info=error,
        )

    def _record_rejection(
        self, path: Path, fingerprint: _FileFingerprint, error: ValidationError
    ) -> None:
        previous = self._failures.get(path)
        attempts = 1 if previous is None else previous.attempts + 1
        self._failures[path] = RegistrationFailure(
            path=path,
            attempts=attempts,
            last_error=f"{type(error).__name__}: {error}",
            retry_after=None,
        )
        self._rejected[path] = fingerprint
        self._failure_count += 1
        _logger.error(
            "Acquisition output watcher for session %s: registration of %s was rejected: "
            "%s; not retrying until the file changes.",
            self._session_id,
            path,
            error,
        )

    def run(self, *, interval: float = 1.0, stop_event: Event | None = None) -> None:
        if interval <= 0:
            raise ValueError("interval must be greater than 0.")
        while True:
            self.scan()
            if stop_event is None:
                time.sleep(interval)
                continue
            if stop_event.wait(interval):
                return

    def _format_path(self, path: Path) -> str:
        resolved = path.resolve()
        if self._base_path is None:
            return str(resolved)
        try:
            return str(resolved.relative_to(self._base_path))
        except ValueError:
            return str(resolved)

    def _stable_fingerprint(self, path: Path) -> _FileFingerprint | None:
        try:
            before = path.stat()
            checksum = _hash_file(path)
            after = path.stat()
        except (FileNotFoundError, PermissionError):
            return None
        if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
            return None
        return _FileFingerprint(
            size_bytes=after.st_size,
            mtime=after.st_mtime,
            checksum=checksum,
        )

    def _iter_files(self) -> Iterable[Path]:
        for root in self._watch_paths:
            if not root.exists():
                continue
            yield from discover_files(root, ignore_hidden=self._ignore_hidden)
