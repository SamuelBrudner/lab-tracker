"""Raw-body voice capture for hands-free phone shortcuts (bench capture).

``POST /notes/voice-capture`` takes the recorded audio as the whole request
body and everything else as query parameters, so an iOS Shortcut, Tasker, or
HTTP Shortcuts action can send it without building a multipart form. It lands
a staged note exactly like the phone capture routes, under the same auth.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from conftest import _register_test_user
from fastapi.testclient import TestClient

from lab_tracker.auth import utc_now

VOICE_CAPTURE_PATH = "/notes/voice-capture"
AUDIO = b"\x00\x00\x00\x18ftypM4A voice memo bytes"
_CHUNK_BYTES = 16 * 1024


def _headers(token: str, content_type: str | None = "audio/mp4") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def _bearer(auth_headers: dict[str, str]) -> str:
    return auth_headers["Authorization"].removeprefix("Bearer ")


def _create_project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Bench"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _start_session(client: TestClient, headers: dict[str, str], project_id: str) -> str:
    response = client.post(
        "/sessions",
        json={"project_id": project_id, "session_type": "operational"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["session_id"]


def _close_session(client: TestClient, headers: dict[str, str], session_id: str) -> None:
    response = client.patch(
        f"/sessions/{session_id}",
        json={"status": "closed", "ended_at": utc_now().isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def _pair_device(client: TestClient, headers: dict[str, str], label: str = "Shortcut") -> str:
    offer = client.post("/auth/devices/enrollment", json={}, headers=headers)
    assert offer.status_code == 201, offer.text
    consume = client.post(
        "/auth/devices/consume",
        json={"offer_token": offer.json()["data"]["offer_token"], "label": label},
    )
    assert consume.status_code == 201, consume.text
    return consume.json()["data"]["secret"]


def _mint_token(
    client: TestClient,
    headers: dict[str, str],
    *,
    scope: str,
    read_only: bool = False,
    role: str = "editor",
) -> str:
    response = client.post(
        "/auth/tokens",
        json={
            "label": f"{scope} shortcut",
            "role": role,
            "read_only": read_only,
            "scope": scope,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["secret"]


def _post_voice(
    client: TestClient,
    token: str,
    *,
    params: dict[str, str],
    body: bytes = AUDIO,
    content_type: str | None = "audio/mp4",
):
    return client.post(
        VOICE_CAPTURE_PATH,
        params=params,
        content=body,
        headers=_headers(token, content_type),
    )


def _note(client: TestClient, headers: dict[str, str], note_id: str) -> dict[str, Any]:
    response = client.get(f"/notes/{note_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _session_targets(note: dict[str, Any]) -> list[str]:
    return [target["entity_id"] for target in note["targets"] if target["entity_type"] == "session"]


def test_device_shortcut_lands_a_staged_voice_note_in_the_latest_active_session(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    _start_session(client, admin_auth_headers, project_id)
    latest_session_id = _start_session(client, admin_auth_headers, project_id)
    secret = _pair_device(client, admin_auth_headers, label="iPhone shortcut")

    response = _post_voice(
        client,
        secret,
        params={"project_id": project_id, "session_id": "latest", "hint": "rig 2"},
    )

    assert response.status_code == 201, response.text
    note = _note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert note["status"] == "staged"
    assert note["raw_asset"]["content_type"] == "audio/mp4"
    assert note["raw_asset"]["size_bytes"] == len(AUDIO)
    assert _session_targets(note) == [latest_session_id]
    metadata = note["metadata"]
    assert metadata["capture_channel"] == "shortcut"
    assert metadata["capture_source"] == "mobile_capture"
    assert metadata["capture_kind"] == "voice"
    assert metadata["transcript_status"] == "pending"
    assert metadata["capture_hint"] == "rig 2"
    assert metadata["capture_session_resolution"] == "latest_active"
    # No client clock was sent; the note's own created_at is the capture time.
    assert "captured_at" not in metadata
    # Stamped by the server exactly like the phone capture routes.
    assert metadata["capture_device_label"] == "iPhone shortcut"
    assert metadata["source_file_name"].endswith(".m4a")


def test_latest_skips_closed_sessions_and_other_peoples_sessions(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    own_session_id = _start_session(client, admin_auth_headers, project_id)
    closed_session_id = _start_session(client, admin_auth_headers, project_id)
    _close_session(client, admin_auth_headers, closed_session_id)
    colleague = _register_test_user(client, username_prefix="colleague")
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": colleague.user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    _start_session(client, colleague.headers, project_id)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id, "session_id": "latest"},
    )

    assert response.status_code == 201, response.text
    note = _note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert _session_targets(note) == [own_session_id]


def test_latest_without_an_active_session_lands_untargeted_and_says_so(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id, "session_id": "latest"},
    )

    assert response.status_code == 201, response.text
    note = _note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert note["targets"] == []
    assert note["metadata"]["capture_session_resolution"] == "none_active"


def test_explicit_session_and_no_session(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    session_id = _start_session(client, admin_auth_headers, project_id)
    token = _bearer(admin_auth_headers)

    explicit = _post_voice(
        client, token, params={"project_id": project_id, "session_id": session_id}
    )
    bare = _post_voice(client, token, params={"project_id": project_id})

    assert explicit.status_code == 201, explicit.text
    explicit_note = _note(client, admin_auth_headers, explicit.json()["data"]["note_id"])
    assert _session_targets(explicit_note) == [session_id]
    assert explicit_note["metadata"]["capture_session_resolution"] == "explicit"
    assert bare.status_code == 201, bare.text
    bare_note = _note(client, admin_auth_headers, bare.json()["data"]["note_id"])
    assert bare_note["targets"] == []
    assert "capture_session_resolution" not in bare_note["metadata"]


def test_session_from_another_project_is_refused(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    other_project_id = _create_project(client, admin_auth_headers)
    other_session_id = _start_session(client, admin_auth_headers, other_project_id)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id, "session_id": other_session_id},
    )

    assert 400 <= response.status_code < 500, response.text
    assert response.status_code != 201


@pytest.mark.parametrize("session_id", ["yesterday", "12345"])
def test_malformed_session_reference_is_rejected(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    session_id: str,
):
    project_id = _create_project(client, admin_auth_headers)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id, "session_id": session_id},
    )

    assert response.status_code == 422, response.text
    assert "latest" in response.json()["error"]["message"]


def test_non_audio_bodies_are_refused(client: TestClient, admin_auth_headers: dict[str, str]):
    project_id = _create_project(client, admin_auth_headers)
    token = _bearer(admin_auth_headers)

    image = _post_voice(client, token, params={"project_id": project_id}, content_type="image/png")
    unnamed = _post_voice(
        client,
        token,
        params={"project_id": project_id},
        content_type="application/octet-stream",
    )

    assert image.status_code == 422, image.text
    assert "audio" in image.json()["error"]["message"]
    assert unnamed.status_code == 422, unnamed.text


def test_octet_stream_with_an_audio_filename_is_accepted(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id, "filename": "../Recording 12.m4a"},
        content_type="application/octet-stream",
    )

    assert response.status_code == 201, response.text
    note = _note(client, admin_auth_headers, response.json()["data"]["note_id"])
    assert note["raw_asset"]["content_type"] == "audio/mp4"
    assert note["raw_asset"]["filename"] == "Recording 12.m4a"


def test_empty_body_is_refused(client: TestClient, admin_auth_headers: dict[str, str]):
    project_id = _create_project(client, admin_auth_headers)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id},
        body=b"",
    )

    assert response.status_code == 422, response.text
    assert "empty" in response.json()["error"]["message"]


def test_client_capture_id_replays_return_the_same_note(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    token = _bearer(admin_auth_headers)
    params = {
        "project_id": project_id,
        "client_capture_id": "shortcut-2026-09-28T10:00:00",
        "captured_at": "2026-09-28T12:00:00+02:00",
    }

    first = _post_voice(client, token, params=params)
    replay = _post_voice(client, token, params=params)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["note_id"] == first.json()["data"]["note_id"]
    note = _note(client, admin_auth_headers, first.json()["data"]["note_id"])
    # The phone's clock, normalized to UTC like the capture page's.
    assert note["metadata"]["captured_at"] == "2026-09-28T10:00:00+00:00"


def test_malformed_captured_at_is_rejected(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)

    response = _post_voice(
        client,
        _bearer(admin_auth_headers),
        params={"project_id": project_id, "captured_at": "Sep 28, 2026 at 10:00"},
    )

    assert response.status_code == 422, response.text


def test_auth_matches_phone_capture(client: TestClient, admin_auth_headers: dict[str, str]):
    project_id = _create_project(client, admin_auth_headers)
    params = {"project_id": project_id}
    # A personal token acts with its own role, so its user needs membership.
    admin_user_id = client.get("/auth/me", headers=admin_auth_headers).json()["data"]["user_id"]
    membership = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": admin_user_id, "role": "contributor"},
        headers=admin_auth_headers,
    )
    assert membership.status_code == 201, membership.text
    stage_token = _mint_token(client, admin_auth_headers, scope="stage_evidence")
    read_only_token = _mint_token(
        client, admin_auth_headers, scope="all", read_only=True, role="viewer"
    )
    outsider = _register_test_user(client, username_prefix="outsider")

    unauthenticated = client.post(
        VOICE_CAPTURE_PATH,
        params=params,
        content=AUDIO,
        headers={"Content-Type": "audio/mp4"},
    )
    staged = _post_voice(client, stage_token, params=params)
    read_only = _post_voice(client, read_only_token, params=params)
    not_a_member = _post_voice(client, _bearer(outsider.headers), params=params)

    assert unauthenticated.status_code == 401
    assert staged.status_code == 201, staged.text
    assert read_only.status_code == 403
    assert not_a_member.status_code in {403, 404}


def test_declared_oversized_body_is_refused(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    client.app.state.settings.max_upload_bytes = len(AUDIO) - 1

    response = _post_voice(client, _bearer(admin_auth_headers), params={"project_id": project_id})

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "payload_too_large"


def _stream_raw(
    client: TestClient,
    *,
    query: str,
    headers: dict[str, str],
    chunks: Iterator[bytes],
) -> tuple[int, int]:
    """POST ``chunks`` without a Content-Length; return (status, bytes pulled)."""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": VOICE_CAPTURE_PATH,
        "raw_path": VOICE_CAPTURE_PATH.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [
            (b"host", b"testserver"),
            *[(key.lower().encode(), value.encode()) for key, value in headers.items()],
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "extensions": {},
        "state": client.app_state.copy(),
    }
    pulled = 0
    body_done = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal pulled, body_done
        if body_done:
            return {"type": "http.disconnect"}
        chunk = next(chunks, None)
        if chunk is None:
            body_done = True
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    async def run() -> None:
        await client.app(scope, receive, send)

    assert client.portal is not None
    client.portal.call(run)
    start = next(message for message in messages if message["type"] == "http.response.start")
    return start["status"], pulled


def test_chunked_oversized_body_stops_reading_at_the_limit(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
):
    project_id = _create_project(client, admin_auth_headers)
    max_bytes = 64 * 1024
    client.app.state.settings.max_upload_bytes = max_bytes
    writes: list[str] = []
    storage = client.app.state.raw_note_storage
    original_store_stream = storage.store_stream

    def spy_store_stream(*args: Any, **kwargs: Any) -> Any:
        writes.append("store_stream")
        return original_store_stream(*args, **kwargs)

    monkeypatch.setattr(storage, "store_stream", spy_store_stream)

    def chunks() -> Iterator[bytes]:
        for _ in range(3 * 1024 * 1024 // _CHUNK_BYTES):
            yield b"x" * _CHUNK_BYTES

    status, pulled = _stream_raw(
        client,
        query=f"project_id={project_id}",
        headers=_headers(_bearer(admin_auth_headers)),
        chunks=chunks(),
    )

    assert status == 413
    assert pulled <= max_bytes + _CHUNK_BYTES
    assert writes == []


def test_denied_capture_never_reads_the_body(
    client: TestClient, admin_auth_headers: dict[str, str]
):
    project_id = _create_project(client, admin_auth_headers)
    outsider = _register_test_user(client, username_prefix="outsider")

    def chunks() -> Iterator[bytes]:
        for _ in range(64):
            yield b"x" * _CHUNK_BYTES

    status, pulled = _stream_raw(
        client,
        query=f"project_id={project_id}",
        headers=_headers(_bearer(outsider.headers)),
        chunks=chunks(),
    )

    assert status in {403, 404}
    assert pulled == 0
