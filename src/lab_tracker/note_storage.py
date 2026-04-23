"""Raw note storage backends."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import NoteRawAsset


class LocalNoteStorage:
    """Store raw note bytes on local disk."""

    def __init__(self, base_path: str | Path) -> None:
        self._base_path = Path(base_path)

    def store(self, content: bytes, *, filename: str, content_type: str) -> NoteRawAsset:
        self._validate_store_inputs(filename=filename, content_type=content_type)
        if not content:
            raise ValidationError("raw_content must not be empty.")
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

    def store_stream(
        self,
        stream: BinaryIO,
        *,
        filename: str,
        content_type: str,
        chunk_size: int = 1024 * 1024,
    ) -> NoteRawAsset:
        self._validate_store_inputs(filename=filename, content_type=content_type)
        storage_id = uuid4()
        path = self._path_for(storage_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        checksum = hashlib.sha256()
        size_bytes = 0
        with path.open("wb") as handle:
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                checksum.update(chunk)
                size_bytes += len(chunk)
                handle.write(chunk)

        if size_bytes == 0:
            path.unlink(missing_ok=True)
            raise ValidationError("raw_content must not be empty.")

        return NoteRawAsset(
            storage_id=storage_id,
            filename=filename.strip(),
            content_type=content_type.strip(),
            size_bytes=size_bytes,
            checksum=checksum.hexdigest(),
        )

    def read(self, storage_id: UUID) -> bytes:
        path = self._path_for(storage_id)
        if not path.exists():
            raise NotFoundError("Raw note content not found.")
        return path.read_bytes()

    def delete(self, storage_id: UUID) -> None:
        path = self._path_for(storage_id)
        if not path.exists():
            raise NotFoundError("Raw note content not found.")
        path.unlink()

    def _validate_store_inputs(self, *, filename: str, content_type: str) -> None:
        if not filename or not filename.strip():
            raise ValidationError("filename must not be empty.")
        if not content_type or not content_type.strip():
            raise ValidationError("content_type must not be empty.")

    def _write_bytes(self, storage_id: UUID, content: bytes) -> None:
        path = self._path_for(storage_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _path_for(self, storage_id: UUID) -> Path:
        return self._base_path / storage_id.hex
