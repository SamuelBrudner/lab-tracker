import hashlib
from uuid import uuid4

import pytest

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import NotFoundError
from lab_tracker.models import EntityRef, EntityType
from lab_tracker.note_storage import LocalNoteStorage
from lab_tracker.services.extraction_backends import RegexQuestionExtractionBackend
from lab_tracker.services.ocr_backends import OCRBackend, OCRResult


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


class _FakeOCRBackend(OCRBackend):
    backend_name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self, image_bytes: bytes, content_type: str | None) -> OCRResult:
        return OCRResult(text=self._text, confidence=99.0, regions=[])


class _ExplodingOCRBackend(OCRBackend):
    backend_name = "explode"

    def extract_text(self, image_bytes: bytes, content_type: str | None) -> OCRResult:
        raise AssertionError("OCR backend should not be called when transcribed_text is provided.")


class _FailingOCRBackend(OCRBackend):
    backend_name = "fails"

    def extract_text(self, image_bytes: bytes, content_type: str | None) -> OCRResult:
        raise RuntimeError("simulated OCR failure")


def test_upload_note_raw_populates_transcribed_text_from_ocr_and_extracts_questions(tmp_path):
    transcript = "Q: Does this work?\nQuestion: What is the next step"
    api = LabTrackerAPI.in_memory(
        raw_storage=LocalNoteStorage(tmp_path),
        ocr_backend=_FakeOCRBackend(transcript),
        question_extraction_backend=RegexQuestionExtractionBackend(),
    )
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"\xFF\xD8\xFF\x00\x00binary-image",
        filename="note.jpg",
        content_type="image/jpeg",
        actor=actor,
    )

    assert note.transcribed_text == transcript
    questions = api.extract_questions_from_note(note.note_id, actor=actor)
    assert {question.text for question in questions} == {
        "Does this work?",
        "What is the next step",
    }


def test_upload_note_raw_skips_ocr_when_transcribed_text_provided(tmp_path):
    api = LabTrackerAPI.in_memory(
        raw_storage=LocalNoteStorage(tmp_path),
        ocr_backend=_ExplodingOCRBackend(),
    )
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"\xFF\xD8\xFF\x00\x00binary-image",
        filename="note.jpg",
        content_type="image/jpeg",
        transcribed_text="manual transcript",
        actor=actor,
    )

    assert note.transcribed_text == "manual transcript"


def test_upload_note_raw_gracefully_handles_ocr_failure(tmp_path):
    api = LabTrackerAPI.in_memory(
        raw_storage=LocalNoteStorage(tmp_path),
        ocr_backend=_FailingOCRBackend(),
    )
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)

    note = api.upload_note_raw(
        project_id=project.project_id,
        content=b"\xFF\xD8\xFF\x00\x00binary-image",
        filename="note.jpg",
        content_type="image/jpeg",
        actor=actor,
    )

    assert note.raw_asset is not None
    assert note.transcribed_text is None


def test_upload_note_raw_rolls_back_raw_asset_when_note_creation_fails(tmp_path):
    api = LabTrackerAPI.in_memory(raw_storage=LocalNoteStorage(tmp_path))
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
    api = LabTrackerAPI.in_memory(raw_storage=LocalNoteStorage(tmp_path))
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
