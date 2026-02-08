"""File watcher integration for acquisition outputs."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Iterable
from uuid import UUID

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext
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


def _is_hidden(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts)


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
    ) -> None:
        self._api = api
        self._session_id = session_id
        self._watch_paths = [Path(path) for path in watch_paths]
        if not self._watch_paths:
            raise ValueError("watch_paths must not be empty.")
        self._actor = actor
        self._ignore_hidden = ignore_hidden
        self._base_path = Path(base_path).resolve() if base_path is not None else None
        self._fingerprints: dict[Path, _FileFingerprint] = {}

    def scan(self) -> list[AcquisitionOutput]:
        outputs: list[AcquisitionOutput] = []
        for file_path in self._iter_files():
            try:
                stat = file_path.stat()
            except (FileNotFoundError, PermissionError):
                continue
            fingerprint = self._fingerprints.get(file_path)
            if (
                fingerprint
                and fingerprint.size_bytes == stat.st_size
                and fingerprint.mtime == stat.st_mtime
            ):
                continue
            try:
                checksum = _hash_file(file_path)
            except (FileNotFoundError, PermissionError):
                continue
            if fingerprint and fingerprint.checksum == checksum:
                self._fingerprints[file_path] = _FileFingerprint(
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                    checksum=checksum,
                )
                continue
            output = self._api.register_acquisition_output(
                self._session_id,
                file_path=self._format_path(file_path),
                checksum=checksum,
                size_bytes=stat.st_size,
                actor=self._actor,
            )
            outputs.append(output)
            self._fingerprints[file_path] = _FileFingerprint(
                size_bytes=stat.st_size,
                mtime=stat.st_mtime,
                checksum=checksum,
            )
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

    def _iter_files(self) -> Iterable[Path]:
        for root in self._watch_paths:
            if not root.exists():
                continue
            if root.is_file():
                if self._ignore_hidden and _is_hidden(root):
                    continue
                yield root.resolve()
                continue
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                if self._ignore_hidden and _is_hidden(candidate):
                    continue
                yield candidate.resolve()
