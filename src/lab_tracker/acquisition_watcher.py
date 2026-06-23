"""File watcher integration for acquisition outputs."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from uuid import UUID

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
from lab_tracker.file_watch import discover_files
from lab_tracker.models import AcquisitionOutput


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
    ) -> None:
        self._api = api
        self._session_id = session_id
        self._watch_paths = [Path(path) for path in watch_paths]
        if not self._watch_paths:
            raise ValueError("watch_paths must not be empty.")
        if failure_backoff_seconds < 0:
            raise ValueError("failure_backoff_seconds must be 0 or greater.")
        self._actor = actor
        self._ignore_hidden = ignore_hidden
        self._failure_backoff_seconds = failure_backoff_seconds
        self._base_path = Path(base_path).resolve() if base_path is not None else None
        self._fingerprints: dict[Path, _FileFingerprint] = {}
        self._retry_after: dict[Path, float] = {}

    def scan(self) -> list[AcquisitionOutput]:
        outputs: list[AcquisitionOutput] = []
        now = time.monotonic()
        for file_path in self._iter_files():
            retry_after = self._retry_after.get(file_path)
            if retry_after is not None and retry_after > now:
                continue
            try:
                current_stat = file_path.stat()
            except (FileNotFoundError, PermissionError):
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
            except Exception:
                self._retry_after[file_path] = now + self._failure_backoff_seconds
                continue
            outputs.append(output)
            self._retry_after.pop(file_path, None)
            self._fingerprints[file_path] = new_fingerprint
        return outputs

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
