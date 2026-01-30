"""Raw note storage backends."""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import NoteRawAsset


class LocalNoteStorage:
    """Store raw note bytes on local disk."""

    def __init__(self, base_path: str | Path) -> None:
        self._base_path = Path(base_path)

    def store(self, content: bytes, *, filename: str, content_type: str) -> NoteRawAsset:
        if not content:
            raise ValidationError("raw_content must not be empty.")
        if not filename or not filename.strip():
            raise ValidationError("filename must not be empty.")
        if not content_type or not content_type.strip():
            raise ValidationError("content_type must not be empty.")
        storage_id = uuid4()
        checksum = hashlib.sha256(content).hexdigest()
        asset = NoteRawAsset(
            storage_id=storage_id,
            filename=filename.strip(),
            content_type=content_type.strip(),
            size_bytes=len(content),
            checksum=checksum,
        )
        self._write_bytes(storage_id, content)
        return asset

    def read(self, storage_id: UUID) -> bytes:
        path = self._path_for(storage_id)
        if not path.exists():
            raise NotFoundError("Raw note content not found.")
        return path.read_bytes()

    def _write_bytes(self, storage_id: UUID, content: bytes) -> None:
        path = self._path_for(storage_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _path_for(self, storage_id: UUID) -> Path:
        return self._base_path / storage_id.hex
