"""HTTP integration tests for device-token enrollment + policy (lab-tracker-bbd)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from lab_tracker.app import create_app
from lab_tracker.auth import Role
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


def _register_user(
    client: TestClient,
    *,
    role: Role = Role.ADMIN,
    username_prefix: str = "user",
) -> tuple[str, dict[str, str]]:
    username = f"{username_prefix}-{uuid4().hex[:8]}"
    password = "secret"
    user = client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=role,
    )
    login = client.post("/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    token = login.json()["data"]["access_token"]
    return str(user.user_id), {"Authorization": f"Bearer {token}"}


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


def test_enrollment_url_honors_public_base_url_override(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    client.app.state.settings.public_base_url = "https://lab.example.com"
    try:
        response = client.post(
            "/auth/devices/enrollment",
            json={},
            headers=admin_auth_headers,
        )
        payload = response.json()["data"]
        assert payload["enrollment_url"].startswith("https://lab.example.com/app/enroll?offer=")
    finally:
        client.app.state.settings.public_base_url = ""


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
    assert issued["kind"] == "mobile"

    consume_again = client.post(
        "/auth/devices/consume",
        json={"offer_token": enrollment["offer_token"], "label": "Other"},
    )
    assert consume_again.status_code == 401


def test_qr_paired_device_inherits_enrollment_creator_user_id(client: TestClient):
    owner_user_id, owner_headers = _register_user(client, username_prefix="device-owner")
    other_user_id, other_headers = _register_user(client, username_prefix="other-user")
    assert owner_user_id != other_user_id
    project_id = _create_project(client, owner_headers)

    enrollment = client.post(
        "/auth/devices/enrollment",
        json={},
        headers=owner_headers,
    )
    assert enrollment.status_code == 201, enrollment.text
    offer_token = enrollment.json()["data"]["offer_token"]

    consume = client.post(
        "/auth/devices/consume",
        json={"offer_token": offer_token, "label": "Owner phone"},
        headers=other_headers,
    )
    assert consume.status_code == 201, consume.text
    issued = consume.json()["data"]
    device_token_id = issued["device_token_id"]
    device_secret = issued["secret"]

    me = client.get("/auth/me", headers=_device_headers(device_secret))
    assert me.status_code == 200, me.text
    assert me.json()["data"]["user_id"] == owner_user_id
    assert me.json()["meta"]["device_token"]["kind"] == "mobile"

    owner_devices = client.get("/auth/devices", headers=owner_headers)
    assert owner_devices.status_code == 200, owner_devices.text
    assert any(
        device["device_token_id"] == device_token_id
        and device["kind"] == "mobile"
        for device in owner_devices.json()["data"]
    )

    other_devices = client.get("/auth/devices", headers=other_headers)
    assert other_devices.status_code == 200, other_devices.text
    assert all(
        device["device_token_id"] != device_token_id
        for device in other_devices.json()["data"]
    )

    upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("owner-snap.jpg", b"image-bytes", "image/jpeg")},
        headers=_device_headers(device_secret),
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["data"]["created_by_user_id"] == owner_user_id


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
    assert quick.status_code == 202, quick.text


def test_registered_computer_token_is_shown_once_and_captures_as_owner(
    client: TestClient,
):
    owner_user_id, owner_headers = _register_user(client, username_prefix="computer-owner")
    project_id = _create_project(client, owner_headers)

    issued = client.post(
        "/auth/devices",
        json={"label": "Rig desktop", "kind": "computer"},
        headers=owner_headers,
    )
    assert issued.status_code == 201, issued.text
    payload = issued.json()["data"]
    assert payload["secret"].startswith("ldev_")
    assert payload["kind"] == "computer"
    assert payload["label"] == "Rig desktop"

    listed = client.get("/auth/devices", headers=owner_headers)
    assert listed.status_code == 200, listed.text
    listed_device = next(
        item
        for item in listed.json()["data"]
        if item["device_token_id"] == payload["device_token_id"]
    )
    assert listed_device["kind"] == "computer"
    assert "secret" not in listed_device

    me = client.get("/auth/me", headers=_device_headers(payload["secret"]))
    assert me.status_code == 200, me.text
    assert me.json()["data"]["user_id"] == owner_user_id
    assert me.json()["meta"]["device_token"] == {
        "device_token_id": payload["device_token_id"],
        "label": "Rig desktop",
        "kind": "computer",
    }

    upload = client.post(
        "/notes/upload-file",
        data={"project_id": project_id},
        files={"file": ("rig-log.txt", b"rig observation", "text/plain")},
        headers=_device_headers(payload["secret"]),
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["data"]["created_by_user_id"] == owner_user_id

    forbidden_project = client.post(
        "/projects",
        json={"name": "Not from registered computer"},
        headers=_device_headers(payload["secret"]),
    )
    assert forbidden_project.status_code == 403

    forbidden_graph = client.post(
        f"/notes/{upload.json()['data']['note_id']}/analysis-graph-drafts",
        headers=_device_headers(payload["secret"]),
    )
    assert forbidden_graph.status_code == 403


def test_register_computer_rejects_mobile_kind_and_device_callers(
    client: TestClient,
    admin_auth_headers: dict[str, str],
):
    invalid_kind = client.post(
        "/auth/devices",
        json={"label": "Phone through wrong route", "kind": "mobile"},
        headers=admin_auth_headers,
    )
    assert invalid_kind.status_code == 422

    _, secret = _pair_device(client, admin_auth_headers)
    from_device = client.post(
        "/auth/devices",
        json={"label": "Nested desktop", "kind": "computer"},
        headers=_device_headers(secret),
    )
    assert from_device.status_code == 403


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
