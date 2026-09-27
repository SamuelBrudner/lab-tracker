"""HTTP integration tests for device-token enrollment + policy (lab-tracker-bbd)."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.app import create_app
from lab_tracker.auth import Role, utc_now
from lab_tracker.routes.device_auth import (
    _ENROLLMENT_QR_BORDER,
    _ENROLLMENT_QR_DARK,
    _ENROLLMENT_QR_ERROR,
    _ENROLLMENT_QR_LIGHT,
    _ENROLLMENT_QR_MODULE_SIZE,
    _build_enrollment_qr_svg,
)


def _device_headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _create_project(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/projects", json={"name": "Devices"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _pair_device(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    *,
    label: str = "iPhone",
) -> tuple[str, str]:
    enrollment = client.post(
        "/auth/devices/enrollment",
        json={},
        headers=admin_auth_headers,
    )
    assert enrollment.status_code == 201, enrollment.text
    payload = enrollment.json()["data"]
    consume = client.post(
        "/auth/devices/consume",
        json={"offer_token": payload["offer_token"], "label": label},
    )
    assert consume.status_code == 201, consume.text
    return consume.json()["data"]["device_token_id"], consume.json()["data"]["secret"]


def test_create_enrollment_returns_offer_with_enrollment_url_and_qr(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    response = client.post(
        "/auth/devices/enrollment",
        json={"ttl_minutes": 5},
        headers=admin_auth_headers,
    )
    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["offer_token"].startswith("lpair_")
    assert payload["enrollment_url"].endswith(f"/app/enroll?offer={payload['offer_token']}")
    # Full URL, not relative — phone needs a host it can reach.
    assert payload["enrollment_url"].startswith("http://") or payload["enrollment_url"].startswith(
        "https://"
    )
    qr_svg = payload["enrollment_qr_svg"]
    assert qr_svg.startswith("<svg") and "</svg>" in qr_svg


def test_enrollment_qr_is_phone_scanner_friendly():
    qr_svg = _build_enrollment_qr_svg(
        "https://lab.example.com/app/enroll?offer=lpair_test"
    )

    assert _ENROLLMENT_QR_DARK == "#000000"
    assert _ENROLLMENT_QR_LIGHT == "#ffffff"
    assert _ENROLLMENT_QR_ERROR == "l"
    assert _ENROLLMENT_QR_MODULE_SIZE >= 7
    assert _ENROLLMENT_QR_BORDER >= 4
    assert 'shape-rendering="crispEdges"' in qr_svg
    assert 'fill="#000000"' in qr_svg
    assert 'fill="#ffffff"' in qr_svg
    assert "<rect" in qr_svg
    assert "stroke=" not in qr_svg
    assert "qrline" not in qr_svg
    assert "#0d8b6f" not in qr_svg


def test_enrollment_url_honors_base_url_override(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    client.app.state.settings.base_url = "https://lab.example.com"
    try:
        response = client.post(
            "/auth/devices/enrollment",
            json={},
            headers=admin_auth_headers,
        )
        payload = response.json()["data"]
        assert payload["enrollment_url"].startswith("https://lab.example.com/app/enroll?offer=")
    finally:
        client.app.state.settings.base_url = ""


def test_consume_enrollment_is_public_and_single_use(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    enrollment = client.post(
        "/auth/devices/enrollment",
        json={},
        headers=admin_auth_headers,
    ).json()["data"]

    consume_first = client.post(
        "/auth/devices/consume",
        json={"offer_token": enrollment["offer_token"], "label": "iPhone"},
    )
    assert consume_first.status_code == 201
    issued = consume_first.json()["data"]
    assert issued["secret"].startswith("ldev_")
    assert issued["label"] == "iPhone"

    consume_again = client.post(
        "/auth/devices/consume",
        json={"offer_token": enrollment["offer_token"], "label": "Other"},
    )
    assert consume_again.status_code == 401


def test_list_and_revoke_device_round_trip(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    device_token_id, _ = _pair_device(client, admin_auth_headers, label="iPad")

    listed = client.get("/auth/devices", headers=admin_auth_headers)
    assert listed.status_code == 200
    devices = listed.json()["data"]
    assert any(d["device_token_id"] == device_token_id for d in devices)

    revoked = client.delete(
        f"/auth/devices/{device_token_id}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["data"]["revoked_at"] is not None


def test_local_auth_disabled_can_list_and_pair_devices(
    monkeypatch,
    migrated_sqlite_database_url: str,
):
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")

    with TestClient(create_app()) as client:
        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["data"]["username"] == "local-tester"

        empty_list = client.get("/auth/devices")
        assert empty_list.status_code == 200, empty_list.text
        assert empty_list.json()["data"] == []
        assert empty_list.json()["meta"]["total"] == 0
        assert empty_list.json()["meta"]["limit"] >= 1

        enrollment = client.post("/auth/devices/enrollment", json={})
        assert enrollment.status_code == 201, enrollment.text
        offer_token = enrollment.json()["data"]["offer_token"]

        consume = client.post(
            "/auth/devices/consume",
            json={"offer_token": offer_token, "label": "Local phone"},
        )
        assert consume.status_code == 201, consume.text
        device_token_id = consume.json()["data"]["device_token_id"]

        listed = client.get("/auth/devices")
        assert listed.status_code == 200, listed.text
        assert [device["device_token_id"] for device in listed.json()["data"]] == [
            device_token_id
        ]


def test_device_token_can_post_captures(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    _, secret = _pair_device(client, admin_auth_headers)

    upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("snap.jpg", b"image-bytes", "image/jpeg")},
        headers=_device_headers(secret),
    )
    assert upload.status_code == 201, upload.text

    quick = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id},
        files={"file": ("snap2.jpg", b"image-2", "image/jpeg")},
        headers=_device_headers(secret),
    )
    assert quick.status_code == 201, quick.text


def test_device_token_can_read_but_not_mutate_other_entities(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    _, secret = _pair_device(client, admin_auth_headers)

    listing = client.get("/projects", headers=_device_headers(secret))
    assert listing.status_code == 200

    forbidden_create = client.post(
        "/projects",
        json={"name": "From device"},
        headers=_device_headers(secret),
    )
    assert forbidden_create.status_code == 403

    forbidden_delete = client.delete(
        f"/projects/{project_id}",
        headers=_device_headers(secret),
    )
    assert forbidden_delete.status_code == 403


def test_device_token_cannot_read_or_mutate_auth_endpoints(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    _, secret = _pair_device(client, admin_auth_headers)

    listing = client.get("/auth/devices", headers=_device_headers(secret))
    assert listing.status_code == 403

    enroll = client.post(
        "/auth/devices/enrollment",
        json={},
        headers=_device_headers(secret),
    )
    assert enroll.status_code == 403


def test_device_token_can_introspect_its_own_session(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    # The PWA needs /auth/me to confirm the device token is live; the
    # policy whitelists this single endpoint inside /auth/*.
    _, secret = _pair_device(client, admin_auth_headers, label="iPhone 14")

    me = client.get("/auth/me", headers=_device_headers(secret))
    assert me.status_code == 200
    payload = me.json()["data"]
    # Same user as the paired admin
    assert payload["role"] == "admin"


def test_revoked_device_token_is_rejected(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    device_token_id, secret = _pair_device(client, admin_auth_headers)

    client.delete(f"/auth/devices/{device_token_id}", headers=admin_auth_headers)

    rejected = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("snap.jpg", b"x", "image/jpeg")},
        headers=_device_headers(secret),
    )
    assert rejected.status_code == 401


def test_unknown_device_token_is_rejected(client: TestClient):
    response = client.get("/projects", headers=_device_headers("ldev_definitely-not-a-real-token"))
    assert response.status_code == 401


def test_consume_rejects_invalid_offer(client: TestClient):
    response = client.post(
        "/auth/devices/consume",
        json={"offer_token": "lpair_does-not-exist", "label": "x"},
    )
    assert response.status_code == 401


@pytest.mark.parametrize("label", ["", " "])
def test_consume_rejects_empty_label(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    label: str,
):
    enrollment = client.post(
        "/auth/devices/enrollment",
        json={},
        headers=admin_auth_headers,
    ).json()["data"]

    response = client.post(
        "/auth/devices/consume",
        json={"offer_token": enrollment["offer_token"], "label": label},
    )
    assert response.status_code in (400, 422)


def _register_and_login(
    client: TestClient,
    *,
    role: Role,
    prefix: str,
) -> tuple[str, dict[str, str]]:
    username = f"{prefix}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username,
        password="secret",
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert login.status_code == 200, login.text
    return str(user.user_id), _device_headers(login.json()["data"]["access_token"])


def test_admin_can_list_and_revoke_another_users_paired_devices(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    device_token_id, secret = _pair_device(client, owner_headers, label="Owner phone")
    _pair_device(client, admin_auth_headers, label="Admin's own")
    # Changing the owner's credentials ends their sessions but not their devices,
    # so an admin needs these routes to cut a departed member's devices off.
    reset = client.patch(
        f"/auth/users/{owner_id}",
        json={"password": "a-new-password"},
        headers=admin_auth_headers,
    )
    assert reset.status_code == 200, reset.text
    assert client.get("/auth/me", headers=_device_headers(secret)).status_code == 200

    listed = client.get(f"/auth/users/{owner_id}/devices", headers=admin_auth_headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [item["device_token_id"] for item in body["data"]] == [device_token_id]
    assert body["data"][0]["label"] == "Owner phone"
    assert body["meta"] == {"limit": 50, "offset": 0, "total": 1}
    assert "secret" not in body["data"][0]

    revoked = client.delete(
        f"/auth/users/{owner_id}/devices/{device_token_id}",
        headers=admin_auth_headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["data"]["revoked_at"] is not None
    assert client.get("/auth/me", headers=_device_headers(secret)).status_code == 401
    again = client.get(f"/auth/users/{owner_id}/devices", headers=admin_auth_headers)
    assert again.json()["data"][0]["revoked_at"] is not None


def test_admin_device_management_rejects_mismatched_and_unknown_targets(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    _owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    other_id, _ = _register_and_login(client, role=Role.VIEWER, prefix="other")
    device_token_id, secret = _pair_device(client, owner_headers)

    mismatched = client.delete(
        f"/auth/users/{other_id}/devices/{device_token_id}",
        headers=admin_auth_headers,
    )
    unknown_device = client.delete(
        f"/auth/users/{other_id}/devices/{uuid4()}",
        headers=admin_auth_headers,
    )
    unknown_user_list = client.get(f"/auth/users/{uuid4()}/devices", headers=admin_auth_headers)
    unknown_user_revoke = client.delete(
        f"/auth/users/{uuid4()}/devices/{device_token_id}",
        headers=admin_auth_headers,
    )

    assert mismatched.status_code == 404
    # A device of another user is indistinguishable from a missing one.
    assert mismatched.json() == unknown_device.json()
    assert unknown_user_list.status_code == 404
    assert unknown_user_revoke.status_code == 404
    assert client.get("/auth/me", headers=_device_headers(secret)).status_code == 200


def test_admin_device_management_requires_an_interactive_admin(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    owner_id, owner_headers = _register_and_login(client, role=Role.EDITOR, prefix="owner")
    device_token_id, secret = _pair_device(client, owner_headers)
    _admin_device_id, admin_device_secret = _pair_device(client, admin_auth_headers)
    admin_pat = client.post(
        "/auth/tokens",
        json={
            "label": "Admin agent",
            "role": "admin",
            "read_only": False,
            "expires_at": (utc_now() + timedelta(days=7)).isoformat(),
        },
        headers=admin_auth_headers,
    )
    assert admin_pat.status_code == 201, admin_pat.text
    pat_headers = _device_headers(admin_pat.json()["data"]["secret"])
    list_path = f"/auth/users/{owner_id}/devices"
    revoke_path = f"/auth/users/{owner_id}/devices/{device_token_id}"

    by_editor = client.get(list_path, headers=owner_headers)
    revoke_by_editor = client.delete(revoke_path, headers=owner_headers)
    by_service = client.get(list_path, headers=pat_headers)
    revoke_by_service = client.delete(revoke_path, headers=pat_headers)
    by_device = client.get(list_path, headers=_device_headers(admin_device_secret))
    revoke_by_device = client.delete(revoke_path, headers=_device_headers(admin_device_secret))

    # A signed-in non-admin is an authorization denial (403), not a credential failure.
    assert by_editor.status_code == 403
    assert by_editor.json()["error"]["message"] == "Admin privileges required."
    assert revoke_by_editor.status_code == 403
    assert by_service.status_code == 403
    assert revoke_by_service.status_code == 403
    assert by_device.status_code == 403
    assert revoke_by_device.status_code == 403
    assert client.get("/auth/me", headers=_device_headers(secret)).status_code == 200


def _note_metadata(client: TestClient, admin_auth_headers: dict[str, str], note_id: str) -> dict:
    response = client.get(f"/notes/{note_id}", headers=admin_auth_headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["metadata"]


def test_device_captures_are_stamped_with_device_identity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    device_token_id, secret = _pair_device(client, admin_auth_headers, label="Bench phone")
    client_metadata = {"capture_hint": "rig 2"}

    upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id, "metadata": json.dumps(client_metadata)},
        files={"file": ("snap.jpg", b"image-bytes", "image/jpeg")},
        headers=_device_headers(secret),
    )
    assert upload.status_code == 201, upload.text
    quick = client.post(
        "/notes/quick-capture",
        data={"project_id": project_id, "metadata": json.dumps(client_metadata)},
        files={"file": ("snap2.jpg", b"image-2", "image/jpeg")},
        headers=_device_headers(secret),
    )
    assert quick.status_code == 201, quick.text
    text = client.post(
        "/notes",
        json={"project_id": project_id, "raw_content": "Fly 12", "metadata": client_metadata},
        headers=_device_headers(secret),
    )
    assert text.status_code == 201, text.text

    for response in (upload, quick, text):
        metadata = _note_metadata(client, admin_auth_headers, response.json()["data"]["note_id"])
        assert metadata["capture_device_token_id"] == device_token_id
        assert metadata["capture_device_label"] == "Bench phone"
        # Server stamping adds to the client's bag; it never replaces it.
        assert metadata["capture_hint"] == "rig 2"


def test_user_captures_carry_no_device_identity(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)

    upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("snap.jpg", b"image-bytes", "image/jpeg")},
        headers=admin_auth_headers,
    )
    assert upload.status_code == 201, upload.text

    metadata = _note_metadata(client, admin_auth_headers, upload.json()["data"]["note_id"])
    assert "capture_device_token_id" not in metadata
    assert "capture_device_label" not in metadata


def test_client_supplied_device_identity_metadata_is_rejected(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    project_id = _create_project(client, admin_auth_headers)
    _, secret = _pair_device(client, admin_auth_headers)

    spoofed_label = client.post(
        "/notes",
        json={
            "project_id": project_id,
            "raw_content": "Fly 12",
            "metadata": {"capture_device_label": "spoof"},
        },
        headers=admin_auth_headers,
    )
    assert spoofed_label.status_code == 422, spoofed_label.text

    spoofed_token = client.post(
        "/notes/upload-file",
        data={
            "project_id": project_id,
            "metadata": json.dumps({"capture_device_token_id": str(uuid4())}),
        },
        files={"file": ("snap.jpg", b"image-bytes", "image/jpeg")},
        headers=_device_headers(secret),
    )
    assert spoofed_token.status_code == 422, spoofed_token.text
