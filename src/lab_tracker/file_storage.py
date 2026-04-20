"""Pluggable file storage backends.

This module provides a minimal interface for storing opaque file bytes and a local
filesystem implementation. It is intended to be reused by higher-level domain
wrappers (e.g., note raw assets, dataset files).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from lab_tracker.errors import NotFoundError, ValidationError


LAB_TRACKER_FILE_STORAGE_PATH_ENV = "LAB_TRACKER_FILE_STORAGE_PATH"


class FileStorageBackend(ABC):
    def store_stream(
        self,
        chunks: Iterable[bytes],
        *,
        filename: str,
        content_type: str,
    ) -> StoredFileMetadata:
        """Persist a stream of byte chunks and return stored-file metadata."""

        payload = bytearray()
        for chunk in chunks:
            if chunk:
                payload.extend(chunk)
        storage_id = self.store(bytes(payload), filename=filename, content_type=content_type)
        return StoredFileMetadata(
            storage_id=storage_id,
            filename=(filename or "").strip(),
            content_type=(content_type or "").strip(),
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            created_at=datetime.now(timezone.utc),
        )

    @abstractmethod
    def store(self, file_bytes: bytes, *, filename: str, content_type: str) -> UUID:
        """Persist bytes and return a storage id."""

    @abstractmethod
    def retrieve(self, storage_id: UUID) -> bytes:
        """Load bytes for a previously stored file."""

    @abstractmethod
    def delete(self, storage_id: UUID) -> None:
        """Remove a stored file and its metadata."""

    @abstractmethod
    def exists(self, storage_id: UUID) -> bool:
        """Return True when the storage id is present."""

    def iter_chunks(
        self,
        storage_id: UUID,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> Iterable[bytes]:
        """Yield file content in chunks for streaming responses.

        Backends should override this to avoid loading large files into memory.
        The default implementation falls back to `retrieve()`.
        """

        yield self.retrieve(storage_id)


@dataclass(frozen=True)
class StoredFileMetadata:
    storage_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime

    def to_json_dict(self) -> dict[str, object]:
        return {
            "storage_id": str(self.storage_id),
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat(),
        }


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file in the same directory then atomically replace.
    with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, path)


class LocalFileStorageBackend(FileStorageBackend):
    """Local filesystem storage with UUID sharding and checksum sidecar metadata."""

    def __init__(self, base_path: str | Path | None = None) -> None:
        configured = (
            base_path
            or os.environ.get(LAB_TRACKER_FILE_STORAGE_PATH_ENV)
            or "./file_storage"
        )
        self._base_path = Path(configured).expanduser()

    @property
    def base_path(self) -> Path:
        return self._base_path

    def store(self, file_bytes: bytes, *, filename: str, content_type: str) -> UUID:
        metadata = self.store_stream([file_bytes], filename=filename, content_type=content_type)
        return metadata.storage_id

    def store_stream(
        self,
        chunks: Iterable[bytes],
        *,
        filename: str,
        content_type: str,
    ) -> StoredFileMetadata:
        cleaned_filename = (filename or "").strip()
        cleaned_content_type = (content_type or "").strip()
        if not cleaned_filename:
            raise ValidationError("filename must not be empty.")
        if not cleaned_content_type:
            raise ValidationError("content_type must not be empty.")

        storage_id = uuid4()
        hasher = hashlib.sha256()
        size_bytes = 0
        data_path = self._data_path(storage_id)
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=data_path.parent) as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                hasher.update(chunk)
                size_bytes += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_name = handle.name
        os.replace(tmp_name, data_path)
        checksum = hasher.hexdigest()
        metadata = StoredFileMetadata(
            storage_id=storage_id,
            filename=cleaned_filename,
            content_type=cleaned_content_type,
            size_bytes=size_bytes,
            sha256=checksum,
            created_at=datetime.now(timezone.utc),
        )
        _atomic_write_bytes(
            self._meta_path(storage_id),
            json.dumps(metadata.to_json_dict(), indent=2, sort_keys=True).encode("utf-8"),
        )
        return metadata

    def retrieve(self, storage_id: UUID) -> bytes:
        path = self._data_path(storage_id)
        if not path.exists():
            raise NotFoundError("Stored file not found.")
        return path.read_bytes()

    def delete(self, storage_id: UUID) -> None:
        path = self._data_path(storage_id)
        meta_path = self._meta_path(storage_id)
        if not path.exists():
            raise NotFoundError("Stored file not found.")
        path.unlink()
        if meta_path.exists():
            meta_path.unlink()

    def exists(self, storage_id: UUID) -> bool:
        return self._data_path(storage_id).exists()

    def iter_chunks(
        self,
        storage_id: UUID,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> Iterable[bytes]:
        path = self._data_path(storage_id)
        if not path.exists():
            raise NotFoundError("Stored file not found.")

        def _generate() -> Iterator[bytes]:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

        return _generate()

    def _shard_dir(self, storage_id: UUID) -> Path:
        hex_id = storage_id.hex
        shard1 = hex_id[:2]
        shard2 = hex_id[2:4]
        return self._base_path / shard1 / shard2

    def _data_path(self, storage_id: UUID) -> Path:
        return self._shard_dir(storage_id) / f"{storage_id.hex}.bin"

    def _meta_path(self, storage_id: UUID) -> Path:
        return self._shard_dir(storage_id) / f"{storage_id.hex}.json"
