from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from starlette.requests import Request

from lab_tracker.app import create_app
from lab_tracker.auth import Role
from lab_tracker.db import Base
from lab_tracker.routes.auth import _bootstrap_token_for_status


def _bootstrap_database(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "auth-routes.db"
    database_url = f"sqlite+pysqlite:///{db_path}"
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", database_url)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    monkeypatch.setenv("LAB_TRACKER_NOTE_STORAGE_PATH", str(tmp_path / "note-storage"))
    monkeypatch.setenv("LAB_TRACKER_AUTH_SECRET_KEY", "test-secret")
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "true")

    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    engine.dispose()


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_admin(client: TestClient, *, username: str = "root", password: str = "secret") -> None:
    client.app.state.auth_service.register_user(
        username=username,
        password=password,
        role=Role.ADMIN,
    )


def _login(client: TestClient, username: str, password: str) -> str:
    login_response = client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )
    assert login_response.status_code == 200
    return login_response.json()["data"]["access_token"]


def test_register_login_and_me_round_trip(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201
        register_payload = register_response.json()["data"]
        assert register_payload["token_type"] == "bearer"
        token = register_payload["access_token"]

        me_response = client.get("/auth/me", headers=_auth_headers(token))
        assert me_response.status_code == 200
        me_payload = me_response.json()["data"]
        assert me_payload["username"] == "sam"
        assert me_payload["role"] == "viewer"

        login_response = client.post(
            "/auth/login",
            json={"username": "sam", "password": "secret"},
        )
        assert login_response.status_code == 200
        login_payload = login_response.json()["data"]
        assert login_payload["user"]["user_id"] == me_payload["user_id"]


def test_login_rejects_invalid_credentials(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201

        login_response = client.post(
            "/auth/login",
            json={"username": "sam", "password": "wrong"},
        )
        assert login_response.status_code == 401
        payload = login_response.json()
        assert payload["error"]["code"] == "auth_error"


def test_register_and_password_reset_reject_short_passwords(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "short"},
        )
        assert register_response.status_code == 422
        assert "at least 6 characters" in register_response.json()["error"]["message"]

        _seed_admin(client)
        admin_token = _login(client, "root", "secret")
        users_response = client.get("/auth/users", headers=_auth_headers(admin_token))
        root_id = users_response.json()["data"][0]["user_id"]
        reset_response = client.patch(
            f"/auth/users/{root_id}",
            json={"password": "tiny"},
            headers=_auth_headers(admin_token),
        )
        assert reset_response.status_code == 422
        assert "at least 6 characters" in reset_response.json()["error"]["message"]


def test_protected_routes_require_authorization(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        response = client.post("/projects", json={"name": "No Auth"})
    assert response.status_code == 401
    payload = response.json()
    assert payload["error"]["code"] == "auth_error"


def test_local_auth_disabled_allows_write_without_authorization(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_AUTH_ENABLED", "false")
    with TestClient(create_app()) as client:
        me_response = client.get("/auth/me")
        assert me_response.status_code == 200
        me_payload = me_response.json()
        assert me_payload["data"]["username"] == "local-tester"
        assert me_payload["data"]["role"] == "admin"
        assert me_payload["meta"]["auth_enabled"] is False

        create_response = client.post("/projects", json={"name": "No Login"})
        assert create_response.status_code == 201
        assert create_response.json()["data"]["created_by"] == me_payload["data"]["user_id"]


def test_protected_routes_accept_valid_authorization(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        _seed_admin(client, username="sam", password="secret")
        token = _login(client, "sam", "secret")

        create_response = client.post(
            "/projects",
            json={"name": "With Auth"},
            headers=_auth_headers(token),
        )
        assert create_response.status_code == 201
        project_id = create_response.json()["data"]["project_id"]

        list_response = client.get("/projects", headers=_auth_headers(token))
        assert list_response.status_code == 200
        project_ids = [item["project_id"] for item in list_response.json()["data"]]
        assert project_id in project_ids


def test_register_non_viewer_requires_admin_token(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        no_auth_response = client.post(
            "/auth/register",
            json={"username": "editor-1", "password": "secret", "role": "editor"},
        )
        assert no_auth_response.status_code == 401
        assert no_auth_response.json()["error"]["code"] == "auth_error"

        viewer_register_response = client.post(
            "/auth/register",
            json={"username": "viewer-1", "password": "secret"},
        )
        assert viewer_register_response.status_code == 201
        viewer_token = viewer_register_response.json()["data"]["access_token"]

        viewer_auth_response = client.post(
            "/auth/register",
            json={"username": "editor-2", "password": "secret", "role": "editor"},
            headers=_auth_headers(viewer_token),
        )
        assert viewer_auth_response.status_code == 401
        assert viewer_auth_response.json()["error"]["code"] == "auth_error"

        _seed_admin(client)
        admin_token = _login(client, "root", "secret")
        admin_auth_response = client.post(
            "/auth/register",
            json={"username": "editor-3", "password": "secret", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert admin_auth_response.status_code == 201
        assert admin_auth_response.json()["data"]["user"]["role"] == "editor"


def test_bootstrap_admin_allows_first_admin_registration(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN", "bootstrap-secret")
    # Local-mode disclosure is gated on the real connection peer, so present a
    # loopback peer; the peer-based gating itself is unit-tested below.
    with TestClient(
        create_app(),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    ) as client:
        status_response = client.get("/auth/bootstrap-status")
        assert status_response.status_code == 200
        status_payload = status_response.json()["data"]
        assert status_payload == {
            "bootstrap_admin_configured": True,
            "bootstrap_token": "bootstrap-secret",
            "bootstrap_token_warning": None,
            "first_admin_available": True,
            "has_users": False,
        }

        bootstrap_response = client.post(
            "/auth/register",
            json={
                "username": "root",
                "password": "secret",
                "role": "admin",
                "bootstrap_token": status_payload["bootstrap_token"],
            },
        )
        assert bootstrap_response.status_code == 201
        payload = bootstrap_response.json()["data"]
        assert payload["user"]["role"] == "admin"

        used_status_response = client.get("/auth/bootstrap-status")
        assert used_status_response.status_code == 200
        used_status_payload = used_status_response.json()["data"]
        assert used_status_payload["first_admin_available"] is False
        assert used_status_payload["has_users"] is True
        assert used_status_payload["bootstrap_token"] is None
        assert used_status_payload["bootstrap_token_warning"] is None

        admin_token = payload["access_token"]
        editor_response = client.post(
            "/auth/register",
            json={"username": "editor-1", "password": "secret", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert editor_response.status_code == 201
        assert editor_response.json()["data"]["user"]["role"] == "editor"

        repeat_bootstrap = client.post(
            "/auth/register",
            json={
                "username": "root-2",
                "password": "secret",
                "role": "admin",
                "bootstrap_token": "bootstrap-secret",
            },
        )
        assert repeat_bootstrap.status_code == 401
        assert repeat_bootstrap.json()["error"]["code"] == "auth_error"


def _bootstrap_status_request(app, *, client, host="lab.example.org") -> Request:
    """Build a minimal request with a chosen connection peer and Host header.

    The TestClient transport cannot set a non-loopback peer, so the peer-based
    trust boundary is exercised directly against the helper.
    """
    return Request(
        {
            "type": "http",
            "app": app,
            "client": client,
            "headers": [(b"host", host.encode())],
        }
    )


def test_bootstrap_token_disclosed_to_local_peer(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN", "bootstrap-secret")
    app = create_app()
    request = _bootstrap_status_request(app, client=("127.0.0.1", 50000))
    token, warning = _bootstrap_token_for_status(
        request, bootstrap_token="bootstrap-secret", has_users=False
    )
    assert token == "bootstrap-secret"
    assert warning is None


def test_bootstrap_token_hidden_from_remote_peer_despite_spoofed_host(monkeypatch, tmp_path):
    """A remote peer cannot read the token by spoofing a loopback Host header."""
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN", "bootstrap-secret")
    app = create_app()
    request = _bootstrap_status_request(
        app, client=("8.8.8.8", 50000), host="127.0.0.1"
    )
    token, warning = _bootstrap_token_for_status(
        request, bootstrap_token="bootstrap-secret", has_users=False
    )
    assert token is None
    assert "local, LAN, or VPN" in warning


def test_bootstrap_token_hidden_when_peer_unknown(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN", "bootstrap-secret")
    app = create_app()
    request = _bootstrap_status_request(app, client=None)
    token, _warning = _bootstrap_token_for_status(
        request, bootstrap_token="bootstrap-secret", has_users=False
    )
    assert token is None


def test_bootstrap_status_can_opt_into_public_first_run_disclosure(
    monkeypatch,
    tmp_path,
):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN", "bootstrap-secret")
    monkeypatch.setenv("LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE", "first_run")
    with TestClient(create_app(), base_url="https://lab.example.org") as client:
        status_response = client.get("/auth/bootstrap-status")
        assert status_response.status_code == 200
        status_payload = status_response.json()["data"]
        assert status_payload["bootstrap_token"] == "bootstrap-secret"
        assert status_payload["bootstrap_token_warning"] is None


def test_admin_can_manage_users(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        _seed_admin(client)
        admin_token = _login(client, "root", "secret")

        viewer_response = client.post(
            "/auth/register",
            json={"username": "viewer-1", "password": "old-secret"},
        )
        assert viewer_response.status_code == 201
        viewer = viewer_response.json()["data"]["user"]
        viewer_token = viewer_response.json()["data"]["access_token"]

        denied_response = client.get("/auth/users", headers=_auth_headers(viewer_token))
        assert denied_response.status_code == 401

        users_response = client.get("/auth/users", headers=_auth_headers(admin_token))
        assert users_response.status_code == 200
        usernames = [item["username"] for item in users_response.json()["data"]]
        assert usernames == ["root", "viewer-1"]

        update_response = client.patch(
            f"/auth/users/{viewer['user_id']}",
            json={"role": "editor", "password": "new-secret"},
            headers=_auth_headers(admin_token),
        )
        assert update_response.status_code == 200
        assert update_response.json()["data"]["role"] == "editor"

        old_login_response = client.post(
            "/auth/login",
            json={"username": "viewer-1", "password": "old-secret"},
        )
        assert old_login_response.status_code == 401

        new_login_response = client.post(
            "/auth/login",
            json={"username": "viewer-1", "password": "new-secret"},
        )
        assert new_login_response.status_code == 200
        assert new_login_response.json()["data"]["user"]["role"] == "editor"

        root_id = next(
            item["user_id"]
            for item in users_response.json()["data"]
            if item["username"] == "root"
        )
        demote_last_admin_response = client.patch(
            f"/auth/users/{root_id}",
            json={"role": "viewer"},
            headers=_auth_headers(admin_token),
        )
        assert demote_last_admin_response.status_code == 422
        assert "At least one admin" in demote_last_admin_response.json()["error"]["message"]


def test_admin_can_invite_user_by_email(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_PUBLIC_BASE_URL", "https://lab.example.org")
    with TestClient(create_app()) as client:
        _seed_admin(client)
        admin_token = _login(client, "root", "secret")

        denied_response = client.post(
            "/auth/invitations",
            json={"email": "member@example.org", "role": "editor"},
        )
        assert denied_response.status_code == 401

        invite_response = client.post(
            "/auth/invitations",
            json={"email": "Member@Example.Org", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert invite_response.status_code == 201
        invitation = invite_response.json()["data"]
        invitation_id = invitation["invitation_id"]
        assert invitation["email"] == "member@example.org"
        assert invitation["role"] == "editor"
        assert invitation["status"] == "pending"
        assert invitation["warning"] is None
        assert invitation["invite_url"].startswith("https://lab.example.org/app?invite=")
        assert "mailto:member%40example.org" in invitation["mailto_url"]

        invitations_response = client.get(
            "/auth/invitations",
            headers=_auth_headers(admin_token),
        )
        assert invitations_response.status_code == 200
        listed = invitations_response.json()["data"]
        assert [item["invitation_id"] for item in listed] == [invitation_id]
        assert listed[0]["status"] == "pending"

        invite_token = invitation["invite_url"].split("invite=", 1)[1].split("&", 1)[0]
        register_response = client.post(
            "/auth/register",
            json={
                "invite_token": invite_token,
                "password": "invite-secret",
                "username": "MEMBER@EXAMPLE.ORG",
            },
        )
        assert register_response.status_code == 201
        payload = register_response.json()["data"]
        assert payload["user"]["username"] == "member@example.org"
        assert payload["user"]["role"] == "editor"

        replay_response = client.post(
            "/auth/register",
            json={
                "invite_token": invite_token,
                "password": "other-secret",
                "username": "member@example.org",
            },
        )
        assert replay_response.status_code == 401
        assert "Invitation" in replay_response.json()["error"]["message"]

        login_response = client.post(
            "/auth/login",
            json={"username": "Member@Example.Org", "password": "invite-secret"},
        )
        assert login_response.status_code == 200

        consumed_response = client.get(
            "/auth/invitations",
            headers=_auth_headers(admin_token),
        )
        assert consumed_response.json()["data"][0]["status"] == "consumed"


def test_invitation_defaults_to_editor_and_warns_for_local_base_url(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app(), base_url="http://127.0.0.1:8000") as client:
        _seed_admin(client)
        admin_token = _login(client, "root", "secret")

        invite_response = client.post(
            "/auth/invitations",
            json={"email": "member@example.org"},
            headers=_auth_headers(admin_token),
        )

        assert invite_response.status_code == 201
        invitation = invite_response.json()["data"]
        assert invitation["role"] == "editor"
        assert invitation["invite_url"].startswith("http://127.0.0.1:8000/app?invite=")
        assert "LAB_TRACKER_PUBLIC_BASE_URL" in invitation["warning"]


def test_admin_can_revoke_invitation(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        _seed_admin(client)
        admin_token = _login(client, "root", "secret")
        invite_response = client.post(
            "/auth/invitations",
            json={"email": "member@example.org", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert invite_response.status_code == 201
        invitation = invite_response.json()["data"]
        invite_token = invitation["invite_url"].split("invite=", 1)[1].split("&", 1)[0]

        revoked_response = client.delete(
            f"/auth/invitations/{invitation['invitation_id']}",
            headers=_auth_headers(admin_token),
        )
        assert revoked_response.status_code == 200
        assert revoked_response.json()["data"]["status"] == "revoked"

        register_response = client.post(
            "/auth/register",
            json={
                "invite_token": invite_token,
                "password": "invite-secret",
                "username": "member@example.org",
            },
        )
        assert register_response.status_code == 401
        assert "revoked" in register_response.json()["error"]["message"]


def test_invitation_token_must_match_registration_email(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    with TestClient(create_app()) as client:
        _seed_admin(client)
        admin_token = _login(client, "root", "secret")
        invite_response = client.post(
            "/auth/invitations",
            json={"email": "member@example.org", "role": "editor"},
            headers=_auth_headers(admin_token),
        )
        assert invite_response.status_code == 201
        invite_token = invite_response.json()["data"]["invite_url"].split("invite=", 1)[1].split(
            "&", 1
        )[0]

        register_response = client.post(
            "/auth/register",
            json={
                "invite_token": invite_token,
                "password": "invite-secret",
                "username": "other@example.org",
            },
        )
        assert register_response.status_code == 401
        assert "Invitation token" in register_response.json()["error"]["message"]


def test_refresh_issues_fresh_token_ttl(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)

    import lab_tracker.auth as auth_module

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    current_time = {"value": now}

    def fake_utc_now() -> datetime:
        return current_time["value"]

    monkeypatch.setattr(auth_module, "utc_now", fake_utc_now)

    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201
        token = register_response.json()["data"]["access_token"]
        raw_expires = register_response.json()["data"]["expires_at"]
        expires_at = datetime.fromisoformat(raw_expires.replace("Z", "+00:00"))

        current_time["value"] = now + timedelta(hours=1)
        refresh_response = client.post("/auth/refresh", headers=_auth_headers(token))
        assert refresh_response.status_code == 200
        refresh_payload = refresh_response.json()["data"]
        raw_refreshed = refresh_payload["expires_at"]
        refreshed_expires_at = datetime.fromisoformat(raw_refreshed.replace("Z", "+00:00"))

        assert refreshed_expires_at > expires_at


def test_refresh_rejects_expired_token(monkeypatch, tmp_path):
    _bootstrap_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES", "1")

    import lab_tracker.auth as auth_module

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    current_time = {"value": now}

    def fake_utc_now() -> datetime:
        return current_time["value"]

    monkeypatch.setattr(auth_module, "utc_now", fake_utc_now)

    with TestClient(create_app()) as client:
        register_response = client.post(
            "/auth/register",
            json={"username": "sam", "password": "secret"},
        )
        assert register_response.status_code == 201
        token = register_response.json()["data"]["access_token"]

        current_time["value"] = now + timedelta(minutes=2)
        refresh_response = client.post("/auth/refresh", headers=_auth_headers(token))
        assert refresh_response.status_code == 401
        assert refresh_response.json()["error"]["code"] == "auth_error"
