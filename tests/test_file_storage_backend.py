import hashlib
import json

import pytest

from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.file_storage import (
    LAB_TRACKER_FILE_STORAGE_PATH_ENV,
    LocalFileStorageBackend,
)


def test_local_file_storage_backend_store_retrieve_delete(tmp_path):
    backend = LocalFileStorageBackend(tmp_path)
    content = b"hello-world"

    storage_id = backend.store(content, filename="greeting.txt", content_type="text/plain")

    assert backend.exists(storage_id) is True
    assert backend.retrieve(storage_id) == content

    shard1 = storage_id.hex[:2]
    shard2 = storage_id.hex[2:4]
    data_path = tmp_path / shard1 / shard2 / f"{storage_id.hex}.bin"
    meta_path = tmp_path / shard1 / shard2 / f"{storage_id.hex}.json"

    assert data_path.exists()
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text())
    assert meta["storage_id"] == str(storage_id)
    assert meta["filename"] == "greeting.txt"
    assert meta["content_type"] == "text/plain"
    assert meta["size_bytes"] == len(content)
    assert meta["sha256"] == hashlib.sha256(content).hexdigest()
    assert "created_at" in meta

    backend.delete(storage_id)

    assert backend.exists(storage_id) is False
    assert data_path.exists() is False
    assert meta_path.exists() is False
    with pytest.raises(NotFoundError):
        backend.retrieve(storage_id)
    with pytest.raises(NotFoundError):
        backend.delete(storage_id)


def test_local_file_storage_backend_store_stream(tmp_path):
    backend = LocalFileStorageBackend(tmp_path)
    chunks = [b"hello", b"-", b"world"]

    metadata = backend.store_stream(
        chunks,
        filename="stream.txt",
        content_type="text/plain",
    )

    assert backend.exists(metadata.storage_id) is True
    assert backend.retrieve(metadata.storage_id) == b"hello-world"
    assert metadata.size_bytes == len(b"hello-world")
    assert metadata.sha256 == hashlib.sha256(b"hello-world").hexdigest()


def test_local_file_storage_backend_uses_env_var_default(tmp_path, monkeypatch):
    monkeypatch.setenv(LAB_TRACKER_FILE_STORAGE_PATH_ENV, str(tmp_path))
    backend = LocalFileStorageBackend()
    assert backend.base_path == tmp_path
    storage_id = backend.store(
        b"env-test",
        filename="a.bin",
        content_type="application/octet-stream",
    )
    assert backend.exists(storage_id) is True


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("", "text/plain"),
        ("  ", "text/plain"),
        ("file.txt", ""),
        ("file.txt", "  "),
    ],
)
def test_local_file_storage_backend_validates_required_fields(tmp_path, filename, content_type):
    backend = LocalFileStorageBackend(tmp_path)
    with pytest.raises(ValidationError):
        backend.store(b"x", filename=filename, content_type=content_type)
