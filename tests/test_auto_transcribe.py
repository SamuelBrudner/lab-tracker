"""Opt-in background transcription of uploaded audio captures."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import select

from lab_tracker.api import LabTrackerAPI
from lab_tracker.app_parts.middleware import system_auth_context
from lab_tracker.db_models import UsageEventModel
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository


class FakeTranscriptionClient:
    provider = "fake"
    model = "fake-gpt"
    transcription_model = "fake-transcribe"

    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.transcription_calls: list[dict[str, Any]] = []
        self.closed = False

    def transcribe_audio(
        self,
        *,
        audio_bytes: bytes,
        filename: str,
        content_type: str,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        self.transcription_calls.append(
            {
                "audio_bytes": audio_bytes,
                "content_type": content_type,
                "filename": filename,
                "prompt": prompt,
            }
        )
        if self.error:
            raise GraphDraftingError(self.error)
        return {"text": "Fly 12 tracked better after pulse onset."}

    def close(self) -> None:
        self.closed = True


class RacingTranscriptionClient(FakeTranscriptionClient):
    """Commit a human note edit in another session during the provider call."""

    def __init__(
        self,
        app: Any,
        project_id: str,
        *,
        transcribed_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._app = app
        self._project_id = project_id
        self._transcribed_text = transcribed_text
        self._metadata = metadata

    def transcribe_audio(self, **kwargs: Any) -> dict[str, Any]:
        with self._app.state.db_session_factory() as session:
            api = LabTrackerAPI(
                raw_storage=self._app.state.raw_note_storage,
                repository=SQLAlchemyLabTrackerRepository(session),
                settings=self._app.state.settings,
                surface="http",
            )
            note = api.list_notes(project_id=UUID(self._project_id))[0]
            update: dict[str, Any] = {}
            if self._transcribed_text is not None:
                update["transcribed_text"] = self._transcribed_text
            if self._metadata is not None:
                update["metadata"] = self._metadata
            api.update_note(
                note.note_id,
                actor=system_auth_context(),
                **update,
            )
        return super().transcribe_audio(**kwargs)


def _project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Auto transcribe"}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _get_note(client: TestClient, headers: dict[str, str], note_id: str) -> dict[str, Any]:
    response = client.get(f"/notes/{note_id}", headers=headers)
    assert response.status_code == 200
    return response.json()["data"]


def _enable_auto_transcription(client: TestClient) -> None:
    client.app.state.settings.auto_transcribe_voice_captures = True


def test_uploaded_audio_note_is_transcribed_in_the_background(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client

    response = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps(
                {
                    "capture_hint": "Rig 2 Fly 12",
                    "transcript_status": "pending",
                }
            ),
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    # The response body is rendered before the post-response task runs.
    assert response.json()["data"]["transcribed_text"] is None
    note = _get_note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert note["transcribed_text"] == "Fly 12 tracked better after pulse onset."
    assert note["metadata"]["transcript_status"] == "ready"
    assert note["metadata"]["transcript_model"] == "fake-transcribe"
    assert fake_client.transcription_calls[0]["audio_bytes"] == b"fake-audio-bytes"
    assert fake_client.transcription_calls[0]["content_type"] == "audio/webm"
    assert fake_client.transcription_calls[0]["prompt"] == "Rig 2 Fly 12"
    assert fake_client.closed is True


def test_quick_capture_audio_is_transcribed_without_capture_metadata(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client

    response = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        files={"file": ("memo.m4a", b"quick-audio-bytes", "audio/mp4")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    note = _get_note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert note["transcribed_text"] == "Fly 12 tracked better after pulse onset."
    assert note["metadata"]["transcript_status"] == "ready"
    assert fake_client.transcription_calls[0]["prompt"] is None
    assert fake_client.closed is True


def test_failed_auto_transcription_is_fail_soft_and_manual_path_still_works(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    project_id = _project(client, admin_auth_headers)
    failing_client = FakeTranscriptionClient(error="provider unavailable")
    client.app.state.graph_draft_client_factory = lambda _settings: failing_client

    response = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps({"transcript_status": "pending"}),
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    note_id = response.json()["data"]["note_id"]
    note = _get_note(client, admin_auth_headers, note_id)
    assert note["transcribed_text"] is None
    assert note["metadata"]["transcript_status"] == "pending"
    assert failing_client.transcription_calls != []
    assert failing_client.closed is True

    working_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: working_client
    manual = client.post(
        f"/notes/{note_id}/transcript",
        json={"prompt": "Rig 2 Fly 12"},
        headers=admin_auth_headers,
    )

    assert manual.status_code == 200
    assert manual.json()["data"]["transcribed_text"] == (
        "Fly 12 tracked better after pulse onset."
    )
    assert manual.json()["data"]["metadata"]["transcript_status"] == "ready"


def test_auto_transcription_never_overwrites_human_edit_made_mid_flight(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    project_id = _project(client, admin_auth_headers)
    racing_client = RacingTranscriptionClient(
        client.app,
        project_id,
        transcribed_text="Human transcript wins.",
    )
    client.app.state.graph_draft_client_factory = lambda _settings: racing_client

    response = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps({"transcript_status": "pending"}),
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    note = _get_note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert racing_client.transcription_calls != []
    assert note["transcribed_text"] == "Human transcript wins."
    assert note["metadata"]["transcript_status"] == "pending"


def test_manual_transcription_preserves_metadata_edit_made_mid_flight(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    note_id = response.json()["data"]["note_id"]

    racing_client = RacingTranscriptionClient(
        client.app,
        project_id,
        metadata={"bench_flag": "checked"},
    )
    client.app.state.graph_draft_client_factory = lambda _settings: racing_client

    manual = client.post(
        f"/notes/{note_id}/transcript",
        json={},
        headers=admin_auth_headers,
    )

    assert manual.status_code == 200
    data = manual.json()["data"]
    assert data["transcribed_text"] == "Fly 12 tracked better after pulse onset."
    assert data["metadata"]["transcript_status"] == "ready"
    assert data["metadata"]["bench_flag"] == "checked"


def test_default_disabled_setting_skips_paid_provider_call(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client
    client.app.state.settings.auto_transcribe_voice_captures = False

    response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert response.status_code == 201
    note = _get_note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert note["transcribed_text"] is None
    assert fake_client.transcription_calls == []


def test_non_audio_and_pretranscribed_uploads_do_not_call_provider(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client

    image = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("notebook.jpg", b"fake-image-bytes", "image/jpeg")},
        headers=admin_auth_headers,
    )
    audio = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "transcribed_text": "Already transcribed locally.",
        },
        files={"file": ("voice.webm", b"fake-audio-bytes", "audio/webm")},
        headers=admin_auth_headers,
    )

    assert image.status_code == 201
    assert audio.status_code == 201
    assert fake_client.transcription_calls == []


def test_idempotent_audio_replay_does_not_schedule_a_second_paid_call(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client
    form = {
        "client_capture_id": "stable-mobile-capture-id",
        "project_id": project_id,
        "metadata": json.dumps(
            {
                "capture_hint": "Rig 2 Fly 12",
                "transcript_status": "pending",
            }
        ),
    }
    files = {"file": ("voice.webm", b"same-audio-bytes", "audio/webm")}

    created = client.post(
        "/notes/upload-file",
        data=form,
        files=files,
        headers=admin_auth_headers,
    )
    replayed = client.post(
        "/notes/upload-file",
        data=form,
        files=files,
        headers=admin_auth_headers,
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.json()["data"]["note_id"] == created.json()["data"]["note_id"]
    assert replayed.json()["data"]["transcribed_text"] == (
        "Fly 12 tracked better after pulse onset."
    )
    assert len(fake_client.transcription_calls) == 1


def test_auto_transcription_records_content_free_usage_event(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _enable_auto_transcription(client)
    client.app.state.settings.usage_events = True
    project_id = _project(client, admin_auth_headers)
    fake_client = FakeTranscriptionClient()
    client.app.state.graph_draft_client_factory = lambda _settings: fake_client

    response = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("voice.webm", b"private-audio", "audio/webm")},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201

    with client.app.state.db_session_factory() as session:
        events = list(
            session.scalars(
                select(UsageEventModel).where(
                    UsageEventModel.verb == "transcribe",
                    UsageEventModel.resource_id == response.json()["data"]["note_id"],
                )
            )
        )

    assert len(events) == 1
    assert events[0].project_id == UUID(project_id)
    assert events[0].outcome == "ok"
    assert events[0].surface == "http"
