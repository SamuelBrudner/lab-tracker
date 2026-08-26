import hashlib
from io import BytesIO
from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import EntityRef, EntityType
from lab_tracker.note_storage import LocalNoteStorage


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_upload_note_raw_persists_and_downloads(tmp_path):
    api = repository_backed_api(raw_storage=LocalNoteStorage(tmp_path))
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    target = EntityRef(entity_type=EntityType.PROJECT, entity_id=project.project_id)
    metadata = {"source": "mobile", "capture_id": "img-001"}
    content = b"binary-note"

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=content,
        filename="note.jpg",
        content_type="image/jpeg",
        targets=[target],
        metadata=metadata,
        actor=actor,
    )

    assert note.raw_asset is not None
    assert note.raw_asset.filename == "note.jpg"
    assert note.raw_asset.content_type == "image/jpeg"
    assert note.raw_asset.size_bytes == len(content)
    assert note.raw_content == ""
    assert note.targets == [target]
    assert note.metadata == metadata

    asset, downloaded = api.download_note_raw(note.note_id)

    assert downloaded == content
    assert asset.storage_id == note.raw_asset.storage_id
    assert asset.checksum == hashlib.sha256(content).hexdigest()


def test_upload_note_raw_preserves_manual_transcript(tmp_path):
    api = repository_backed_api(raw_storage=LocalNoteStorage(tmp_path))
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"\xff\xd8\xff\x00\x00binary-image",
        filename="note.jpg",
        content_type="image/jpeg",
        transcribed_text="manual transcript",
        actor=actor,
    )

    assert note.transcribed_text == "manual transcript"


def test_uploaded_text_asset_has_bounded_utf8_projection(tmp_path):
    api = repository_backed_api(raw_storage=LocalNoteStorage(tmp_path))
    actor = _actor()
    project = api.create_project("Text evidence", actor=actor)
    content = "αβγ\n## Diff\ndiff --git a/a.py b/a.py\n".encode()

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=content,
        filename="commit.md",
        content_type="text/markdown; charset=utf-8",
        actor=actor,
    )

    assert note.raw_asset is not None
    assert note.raw_asset.is_text is True
    asset, excerpt = api.read_note_raw_text(note.note_id, max_chars=3)
    assert asset == note.raw_asset
    assert excerpt.text == "αβγ"
    assert excerpt.included_bytes == len("αβγ".encode())
    assert excerpt.omitted_bytes == len(content) - excerpt.included_bytes
    assert excerpt.truncated is True


def test_uploaded_text_projection_rejects_binary_and_invalid_utf8(tmp_path):
    api = repository_backed_api(raw_storage=LocalNoteStorage(tmp_path))
    actor = _actor()
    project = api.create_project("Text validation", actor=actor)
    binary_note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"image",
        filename="image.jpg",
        content_type="image/jpeg",
        actor=actor,
    )
    invalid_note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"valid prefix\xff",
        filename="invalid.txt",
        content_type="text/plain",
        actor=actor,
    )

    with pytest.raises(ValidationError, match="not a supported text type"):
        api.read_note_raw_text(binary_note.note_id, max_chars=100)
    with pytest.raises(ValidationError, match="valid UTF-8"):
        api.read_note_raw_text(invalid_note.note_id, max_chars=100)


def test_upload_note_raw_rolls_back_raw_asset_when_note_creation_fails(tmp_path):
    api = repository_backed_api(raw_storage=LocalNoteStorage(tmp_path))
    actor = _actor()

    with pytest.raises(NotFoundError, match="Project does not exist."):
        api.upload_note_raw(
            project_id=uuid4(),
            content=b"binary-note",
            filename="note.jpg",
            content_type="image/jpeg",
            actor=actor,
        )

    assert list(tmp_path.iterdir()) == []


def test_delete_note_removes_stored_raw_asset(tmp_path):
    api = repository_backed_api(raw_storage=LocalNoteStorage(tmp_path))
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"binary-note",
        filename="note.jpg",
        content_type="image/jpeg",
        actor=actor,
    )
    assert note.raw_asset is not None
    raw_path = tmp_path / note.raw_asset.storage_id.hex
    assert raw_path.exists()

    api.delete_note(note.note_id, actor=actor)

    assert not raw_path.exists()


def test_local_note_storage_rejects_oversized_stream_and_cleans_partial_file(tmp_path):
    storage = LocalNoteStorage(tmp_path, max_bytes=5)

    with pytest.raises(ValidationError, match="configured limit"):
        storage.store_stream(
            BytesIO(b"123456"),
            filename="too-large.jpg",
            content_type="image/jpeg",
            chunk_size=3,
        )

    assert list(tmp_path.iterdir()) == []


def test_local_note_storage_cleans_temp_file_when_atomic_replace_fails(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = LocalNoteStorage(tmp_path)

    def failing_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("lab_tracker.note_storage.os.replace", failing_replace)

    with pytest.raises(OSError, match="replace failed"):
        storage.store(
            b"raw-capture",
            filename="capture.txt",
            content_type="text/plain",
        )

    assert list(tmp_path.iterdir()) == []
