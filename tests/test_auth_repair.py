from __future__ import annotations

import getpass
import json
import os
import warnings

import httpx
import pytest

from lab_tracker.mcp_api_client import LabTrackerAPIAuthError, lab_tracker_api_error
from lab_tracker_client import auth_repair, cli

BASE_URL = "https://lab.example.test"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("LAB_TRACKER_"):
            monkeypatch.delenv(key)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "profile"))
    path = tmp_path / "profile/config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"base_url": BASE_URL, "default_project_id": "keep-project"}))
    return path


def _transport(*, statuses=None, fail=False):
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/projects"
        assert str(request.url).startswith(BASE_URL)
        if fail:
            raise httpx.ConnectError("network error lpat_never_echo_this", request=request)
        if not request.headers.get("authorization"):
            return httpx.Response(401)
        code = (statuses or {}).get(request.headers.get("authorization"), 200)
        return httpx.Response(
            code,
            json={
                "data": [],
                "meta": {"auth_enabled": True},
                "error": "lpat_never_echo_this",
            },
        )

    return httpx.MockTransport(respond), requests


def _repair(profile, **kwargs):
    return auth_repair.repair(
        profile.parent.parent / "repo",
        home=profile.parent.parent / "home",
        **kwargs,
    )


@pytest.mark.parametrize("old", [None, "lpat_revoked"])
def test_guided_recovery_verifies_before_private_save_and_preserves_bindings(profile, old, capsys):
    if old:
        _set_token(profile, old)
    transport, requests = _transport(statuses={"Bearer lpat_revoked": 401})
    result = _repair(
        profile,
        transport=transport,
        confirm=lambda _: "yes",
        secret_prompt=lambda _: "lpat_replacement",
    )
    assert result["ok"] and result["profile_changed"]
    assert result["status"] == "restart_required"
    assert not result["live_mcp_verified"] and not result["recovery_complete"]
    assert json.loads(profile.read_text()) == {
        "base_url": BASE_URL,
        "access_token": "lpat_replacement",
        "default_project_id": "keep-project",
    }
    if os.name != "nt":
        assert profile.stat().st_mode & 0o777 == 0o600
    assert requests[-1].url.path == "/projects"
    assert result["replacement_verification"]["authenticated_read_ok"]
    assert "lpat_replacement" not in json.dumps(result) + str(capsys.readouterr())
    assert result["agents_url"] == BASE_URL + "/app/agents"


def _set_token(path, token):
    data = json.loads(path.read_text())
    data["access_token"] = token
    path.write_text(json.dumps(data))


def _no_prompt(_):
    pytest.fail("diagnosis should not prompt or rotate")


def test_valid_profile_prefers_host_restart_and_rerun_is_read_only(profile):
    _set_token(profile, "lpat_good")
    before = profile.read_bytes()
    transport, _ = _transport()
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["ok"] and not result["profile_changed"]
    assert "retry the originally blocked" in result["next_step"]
    assert profile.read_bytes() == before


@pytest.mark.parametrize("key", ["LAB_TRACKER_MCP_API_KEY", "LAB_TRACKER_MCP_TOKEN"])
def test_rejected_env_override_does_not_replace_valid_saved_token(profile, monkeypatch, key):
    _set_token(profile, "lpat_good")
    before = profile.read_bytes()
    monkeypatch.setenv(key, "lpat_bad")
    transport, _ = _transport(statuses={"Bearer lpat_bad": 401})
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["status"] == "override_conflict"
    assert result["credential_source"] == key
    assert result["saved_profile_verification"]["authenticated_read_ok"]
    assert profile.read_bytes() == before
    assert "lpat_bad" not in json.dumps(result)


def test_profile_token_is_never_sent_to_different_env_instance(profile, monkeypatch):
    _set_token(profile, "lpat_good")
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "https://other.example.test")
    transport, requests = _transport()
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["status"] == "profile_instance_mismatch"
    assert requests == []


@pytest.mark.parametrize("code,status", [(403, "insufficient_access"), (503, "unavailable")])
def test_permission_and_service_errors_do_not_trigger_rotation(profile, code, status):
    _set_token(profile, "lpat_test")
    transport, _ = _transport(statuses={"Bearer lpat_test": code})
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["status"] == status
    assert not result["ok"]
    assert "lpat_never_echo_this" not in json.dumps(result)


def test_transport_failure_is_context_specific_and_redacted(profile):
    _set_token(profile, "lpat_test")
    transport, _ = _transport(fail=True)
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["status"] == "connectivity_error"
    assert "sandbox" in result["next_step"]
    assert "lpat_never_echo_this" not in json.dumps(result)


@pytest.mark.parametrize("answer,token", [("no", "unused"), ("yes", "")])
def test_cancel_preserves_profile(profile, answer, token):
    before = profile.read_bytes()
    result = _repair(profile, confirm=lambda _: answer, secret_prompt=lambda _: token)
    assert result["status"] == "cancelled"
    assert profile.read_bytes() == before


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_interrupt_preserves_profile(profile, exc):
    def stop(_):
        raise exc()

    before = profile.read_bytes()
    result = _repair(profile, confirm=lambda _: "yes", secret_prompt=stop)
    assert result["status"] == "cancelled"
    assert profile.read_bytes() == before


def test_echoing_getpass_fallback_is_refused(profile):
    def unsafe(_):
        warnings.warn("echo would be enabled", getpass.GetPassWarning, stacklevel=2)
        pytest.fail("must fail before reading a token")

    before = profile.read_bytes()
    result = _repair(profile, confirm=lambda _: "yes", secret_prompt=unsafe)
    assert result["status"] == "cancelled"
    assert profile.read_bytes() == before


def test_rejected_replacement_is_not_saved(profile):
    transport, _ = _transport(statuses={"Bearer lpat_bad": 401})
    before = profile.read_bytes()
    result = _repair(
        profile, transport=transport, confirm=lambda _: "yes", secret_prompt=lambda _: "lpat_bad"
    )
    assert result["status"] == "rejected"
    assert profile.read_bytes() == before


def test_diagnose_never_prompts(profile):
    result = _repair(profile, diagnose=True, confirm=_no_prompt)
    assert result["status"] == "missing"
    assert not result["profile_changed"]


def test_malformed_profile_is_never_overwritten(profile):
    profile.write_text('{"access_token":')
    before = profile.read_bytes()
    result = _repair(profile, confirm=_no_prompt)
    assert result["status"] == "invalid_configuration"
    assert profile.read_bytes() == before


def test_concurrent_profile_change_is_not_clobbered(profile):
    transport, _ = _transport()

    def enter_token(_):
        _set_token(profile, "lpat_other_session")
        return "lpat_new"

    result = _repair(
        profile, transport=transport, confirm=lambda _: "yes", secret_prompt=enter_token
    )
    assert result["status"] == "profile_changed_during_repair"
    assert json.loads(profile.read_text())["access_token"] == "lpat_other_session"


def test_selected_host_override_is_diagnosed_without_mutating_settings(profile):
    home = profile.parent.parent / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.lab_tracker]\ncommand = "lt-mcp"\n'
        "[mcp_servers.lab_tracker.env]\n"
        f'LAB_TRACKER_CONFIG_DIR = "{profile.parent.as_posix()}"\n'
        f'LAB_TRACKER_BASE_URL = "{BASE_URL}"\n'
        'LAB_TRACKER_MCP_API_KEY = "lpat_stale_host"\n'
    )
    _set_token(profile, "lpat_good")
    before = config.read_bytes()
    transport, _ = _transport(statuses={"Bearer lpat_stale_host": 401})
    result = _repair(profile, host="codex", transport=transport, confirm=_no_prompt)
    assert result["status"] == "override_conflict"
    assert "codex" in result["restart"]
    assert "lpat_stale_host" not in json.dumps(result)
    assert config.read_bytes() == before


def test_selected_host_profile_only_is_saved_in_intended_location(profile):
    home = profile.parent.parent / "home"
    host_profile = home / ".lab-tracker/config.json"
    host_profile.parent.mkdir(parents=True)
    host_profile.write_bytes(profile.read_bytes())
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.lab_tracker]\ncommand = "lt-mcp"\n')
    before = profile.read_bytes()
    transport, _ = _transport()
    result = _repair(
        profile,
        host="codex",
        transport=transport,
        confirm=lambda _: "yes",
        secret_prompt=lambda _: "lpat_new",
    )
    assert result["profile_changed"]
    assert profile.read_bytes() == before
    assert json.loads(host_profile.read_text())["access_token"] == "lpat_new"


def test_unauthenticated_health_cannot_establish_success(profile):
    _set_token(profile, "lpat_test")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={
                "data": {"user_id": "local"},
                "meta": {"auth_enabled": False},
            },
        )
    )
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["status"] == "authentication_unverified"
    assert not result["ok"]


def test_cli_diagnosis_failure_has_nonzero_exit(profile, capsys, monkeypatch):
    monkeypatch.setattr(auth_repair.Path, "home", lambda: profile.parent.parent / "home")
    with pytest.raises(SystemExit) as exc:
        cli.main(["auth", "repair", "--diagnose", "--target", str(profile.parent)])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "missing"


def test_structured_auth_error_points_to_supported_command():
    error = lab_tracker_api_error(
        "lab_tracker_get_decision_context",
        LabTrackerAPIAuthError(
            "Invalid token",
            status_code=401,
        ),
    )
    assert error["next_action"]["command"] == "lt auth repair"
    assert "lt auth repair" in error["next_action"]["reason"]


def test_default_diagnosis_identifies_host_overrides_before_rotation(profile):
    home = profile.parent.parent / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[mcp_servers.lab_tracker]\ncommand = "lt-mcp"\n'
        "[mcp_servers.lab_tracker.env]\n"
        f'LAB_TRACKER_BASE_URL = "{BASE_URL}"\n'
        'LAB_TRACKER_MCP_API_KEY = "lpat_host_secret"\n'
    )
    result = _repair(profile, confirm=_no_prompt)
    assert result["status"] == "select_host"
    registration = result["registrations"][0]
    assert registration["base_url"] == BASE_URL
    assert registration["credential_source"] == "LAB_TRACKER_MCP_API_KEY"
    assert "lpat_host_secret" not in json.dumps(result)


def test_profile_without_url_does_not_send_token_to_env_override(profile, monkeypatch):
    profile.write_text(json.dumps({"access_token": "lpat_implicit_localhost"}))
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", BASE_URL)
    transport, requests = _transport()
    result = _repair(profile, transport=transport, confirm=_no_prompt)
    assert result["status"] == "profile_instance_mismatch"
    assert requests == []


@pytest.mark.parametrize("scope", ["all", "stage_evidence", "batch_run_due"])
def test_verification_with_real_lpat_policy(client, admin_auth_headers, scope):
    from datetime import timedelta

    from lab_tracker.auth import utc_now

    issued = client.post(
        "/auth/tokens",
        headers=admin_auth_headers,
        json={
            "label": "Repair integration test",
            "role": "admin",
            "scope": scope,
            "read_only": scope != "stage_evidence",
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
    )
    assert issued.status_code == 201
    secret = issued.json()["data"]["secret"]
    paths = []

    def request_real_api(request):
        paths.append((request.method, request.url.path))
        return client.get(str(request.url), headers=dict(request.headers))

    transport = httpx.MockTransport(request_real_api)
    result = auth_repair.verify_credential(
        str(client.base_url).rstrip("/"), secret, transport=transport
    )
    assert result["status"] == ("insufficient_access" if scope == "batch_run_due" else "valid")
    assert paths == [("GET", "/projects"), ("GET", "/projects")]
    assert secret not in json.dumps(result)
