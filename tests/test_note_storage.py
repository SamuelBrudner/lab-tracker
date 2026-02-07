import hashlib
from uuid import uuid4

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.models import EntityRef, EntityType
from lab_tracker.note_storage import LocalNoteStorage


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def test_upload_note_raw_persists_and_downloads(tmp_path):
    api = LabTrackerAPI.in_memory(raw_storage=LocalNoteStorage(tmp_path))
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
