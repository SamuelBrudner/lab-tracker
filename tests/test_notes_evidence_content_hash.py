"""Notes mirror metadata.evidence_content_hash into an indexed, filterable column.

The metadata JSON stays the source of truth: every write path derives the
column from it, reads expose it as a computed field, and GET /notes filters on
it exactly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.auth import utc_now
from lab_tracker.db_models import NoteModel
from lab_tracker.models import EVIDENCE_CONTENT_HASH_MAX_LENGTH
from lab_tracker.sqlalchemy_repository_parts.repository import SQLAlchemyLabTrackerRepository


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Hash column"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _create_note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    content_hash: str | None,
) -> Any:
    metadata = {"evidence_content_hash": content_hash} if content_hash is not None else {}
    return client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "captured", "metadata": metadata},
        headers=headers,
    )


def _note_id(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    content_hash: str | None,
) -> str:
    response = _create_note(client, headers, project_id, content_hash)
    assert response.status_code == 201
    return response.json()["data"]["note_id"]


def _list_by_hash(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    content_hash: str,
) -> dict[str, Any]:
    response = client.get(
        "/notes",
        params={"project_id": project_id, "evidence_content_hash": content_hash},
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


def _hash_column(client: TestClient, note_id: str) -> str | None:
    with client.app.state.db_session_factory() as session:
        return session.scalar(
            select(NoteModel.evidence_content_hash).where(NoteModel.note_id == note_id)
        )


def test_create_note_rejects_over_long_evidence_content_hash(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)

    too_long = _create_note(
        client, admin_auth_headers, project_id, "a" * (EVIDENCE_CONTENT_HASH_MAX_LENGTH + 1)
    )
    assert too_long.status_code == 422
    assert "evidence_content_hash" in too_long.text

    longest_allowed = "b" * EVIDENCE_CONTENT_HASH_MAX_LENGTH
    accepted = _create_note(client, admin_auth_headers, project_id, longest_allowed)
    assert accepted.status_code == 201
    assert accepted.json()["data"]["evidence_content_hash"] == longest_allowed


def test_note_create_and_metadata_patch_write_through_to_hash_column(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _note_id(client, admin_auth_headers, project_id, "h1")
    assert _hash_column(client, note_id) == "h1"

    listed = _list_by_hash(client, admin_auth_headers, project_id, "h1")
    assert listed["meta"]["total"] == 1
    assert listed["data"][0]["note_id"] == note_id
    assert listed["data"][0]["evidence_content_hash"] == "h1"

    detail = client.get(f"/notes/{note_id}", headers=admin_auth_headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["evidence_content_hash"] == "h1"

    patched = client.patch(f"/notes/{note_id}", json={"metadata": {}}, headers=admin_auth_headers)
    assert patched.status_code == 200
    assert patched.json()["data"]["evidence_content_hash"] is None
    assert _hash_column(client, note_id) is None
    assert _list_by_hash(client, admin_auth_headers, project_id, "h1")["meta"]["total"] == 0


def test_list_notes_filters_by_exact_evidence_content_hash(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    exact = _note_id(client, admin_auth_headers, project_id, "abc")
    _note_id(client, admin_auth_headers, project_id, "ABC")
    _note_id(client, admin_auth_headers, project_id, "abcd")
    _note_id(client, admin_auth_headers, project_id, None)

    listed = _list_by_hash(client, admin_auth_headers, project_id, "abc")

    # A lookup key, not a search: no prefix matching and no case folding.
    assert listed["meta"]["total"] == 1
    assert [note["note_id"] for note in listed["data"]] == [exact]


def test_transcript_claim_paths_preserve_evidence_content_hash_column(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers)
    note_id = _note_id(client, admin_auth_headers, project_id, "h1")
    claim_id = uuid4()

    with client.app.state.db_session_factory() as session:
        notes = SQLAlchemyLabTrackerRepository(session).notes
        claimed_at = utc_now()
        claimed = notes.try_claim_auto_transcription(
            UUID(note_id),
            claim_id=claim_id,
            claimed_at=claimed_at,
            stale_before=claimed_at - timedelta(minutes=5),
        )
        assert claimed is not None
        assert claimed.evidence_content_hash == "h1"
        session.commit()
    assert _hash_column(client, note_id) == "h1"

    with client.app.state.db_session_factory() as session:
        notes = SQLAlchemyLabTrackerRepository(session).notes
        completed = notes.apply_auto_transcription_result(
            UUID(note_id),
            claim_id=claim_id,
            claimed_updated_at=claimed.updated_at,
            text="transcribed",
            metadata_updates={"transcription_provider": "fake"},
            updated_at=utc_now(),
        )
        assert completed is not None
        assert completed.transcribed_text == "transcribed"
        assert completed.evidence_content_hash == "h1"
        session.commit()
    assert _hash_column(client, note_id) == "h1"
    assert _list_by_hash(client, admin_auth_headers, project_id, "h1")["meta"]["total"] == 1
