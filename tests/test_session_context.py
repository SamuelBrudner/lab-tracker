"""Session link codes, path matching, and the per-checkout active session."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from lab_tracker.models import encode_session_link_code as server_encode
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LabTracker, LTAPIError, LTValidationError
from lab_tracker_client.session_context import (
    _reset_session_hints_for_tests,
    active_session_path,
    clear_active_session,
    decode_session_link_code,
    encode_session_link_code,
    find_session_link_code,
    read_active_session,
    session_id_from_reference,
    session_target,
    set_active_session,
    strict_session_id,
)

SESSION_ID = "3d4f6a1e-9c2b-4a8e-8f01-2b3c4d5e6f70"
PROJECT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class SessionServer:
    """Answers GET /sessions/{id} for the sessions it knows."""

    def __init__(self, sessions: dict[str, str]) -> None:
        self.sessions = sessions
        self.requested: list[str] = []

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.requested.append(session_id)
        if session_id not in self.sessions:
            raise LTAPIError("Session does not exist.")
        return {"session_id": session_id, "project_id": self.sessions[session_id]}


def _cli_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    class _FromEnv:
        @staticmethod
        def from_env(**_kwargs: object) -> LabTracker:
            return LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(lt_cli, "LabTracker", _FromEnv)


def _session_handler(sessions: dict[str, str]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.rsplit("/", 1)[-1]
        if request.method == "GET" and session_id in sessions:
            return httpx.Response(
                200,
                json={"data": {"session_id": session_id, "project_id": sessions[session_id]}},
            )
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": "Session does not exist."}}
        )

    return handler


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_SESSION_CONTEXT", raising=False)
    monkeypatch.chdir(tmp_path)
    _reset_session_hints_for_tests()
    return tmp_path


def test_link_codes_round_trip_and_match_the_server_encoding() -> None:
    code = encode_session_link_code(SESSION_ID)
    assert code == server_encode(uuid.UUID(SESSION_ID))
    assert len(code) == 26
    assert decode_session_link_code(code) == SESSION_ID
    assert decode_session_link_code(f"LT-{code.lower()}") == SESSION_ID
    assert decode_session_link_code(code[:13] + "-" + code[13:]) == SESSION_ID


def test_decode_rejects_wrong_length_and_alphabet() -> None:
    with pytest.raises(LTValidationError, match="26"):
        decode_session_link_code("ABC")
    with pytest.raises(LTValidationError, match="[Ii]nvalid link_code characters"):
        decode_session_link_code("1" * 26)


def test_session_reference_accepts_uuid_or_link_code_and_passes_other_ids_through() -> None:
    code = encode_session_link_code(SESSION_ID)
    assert session_id_from_reference(SESSION_ID.upper()) == SESSION_ID
    assert session_id_from_reference(code) == SESSION_ID
    assert session_id_from_reference(f"LT-{code}") == SESSION_ID
    assert session_id_from_reference("session-1") == "session-1"
    assert session_id_from_reference("   ") is None
    assert strict_session_id(code) == SESSION_ID
    with pytest.raises(LTValidationError):
        strict_session_id("session-1")


def test_find_session_link_code_in_paths_but_not_in_look_alikes() -> None:
    code = encode_session_link_code(SESSION_ID)
    assert find_session_link_code(f"rig2/session001_LT-{code}/trace.png") == (code, SESSION_ID)
    assert find_session_link_code(f"rig2/LT-{code.lower()}/sweep.nwb") == (code, SESSION_ID)
    # Embedded in a longer alphanumeric run, or the wrong alphabet: no match.
    assert find_session_link_code(f"x{code}y/a.png") is None
    assert find_session_link_code(f"xLT-{code}/a.png") is None
    assert find_session_link_code("2025_12_10_Rig2_session001.nwb") is None


def test_find_session_link_code_needs_the_lt_prefix_in_a_path() -> None:
    """Any 26 base32 letters decode to 16 bytes, so only the explicit prefix
    marks a folder or file name as naming a session."""

    code = encode_session_link_code(SESSION_ID)
    assert find_session_link_code(f"{code}/sweep.nwb") is None
    assert find_session_link_code(f"{code.lower()}/sweep.nwb") is None
    assert find_session_link_code("a" * 26) is None
    assert find_session_link_code("supplementaryinformationaq/x.png") is None
    assert find_session_link_code("results/ThermalConductivitySamples/plot.png") is None


def test_find_session_link_code_needs_the_canonical_encoding() -> None:
    """A prefixed code whose trailing pad bits are not zero is a look-alike
    (or a typo): the server never prints it, so it names no session."""

    # "Z" leaves non-zero pad bits; the server would print "...XYY" instead.
    assert find_session_link_code("LT-ABCDEFGHIJKLMNOPQRSTUVWXYZ/x.png") is None
    assert find_session_link_code(f"LT-{'A' * 25}B/x.png") is None
    canonical = "A" * 26
    assert encode_session_link_code(decode_session_link_code(canonical)) == canonical
    assert find_session_link_code(f"LT-{canonical}/x.png") == (
        canonical,
        "00000000-0000-0000-0000-000000000000",
    )


def test_decode_rejects_a_link_code_the_server_would_never_print() -> None:
    with pytest.raises(LTValidationError, match="typo"):
        decode_session_link_code("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def test_active_session_is_written_read_expired_and_cleared(checkout: Path) -> None:
    code = encode_session_link_code(SESSION_ID)
    server = SessionServer({SESSION_ID: PROJECT_A})
    result = set_active_session(code, client=server, hours=2)
    path = active_session_path()
    assert result["action"] == "set"
    assert Path(result["path"]) == path
    assert path.is_file()
    assert server.requested == [SESSION_ID]
    active = read_active_session()
    assert active is not None
    assert active["session_id"] == SESSION_ID
    assert active["link_code"] == code
    # The project is the session's own, looked up on the server.
    assert active["project_id"] == PROJECT_A
    assert active["source"] == "checkout"

    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    path.write_text(json.dumps(stale), encoding="utf-8")
    assert read_active_session() is None

    assert clear_active_session()["action"] == "cleared"
    assert not path.exists()
    assert clear_active_session()["action"] == "absent"


def test_env_session_overrides_the_checkout_and_bad_values_yield_nothing(
    checkout: Path, monkeypatch
) -> None:
    set_active_session(SESSION_ID, client=SessionServer({SESSION_ID: PROJECT_A}))
    other = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
    monkeypatch.setenv("LAB_TRACKER_SESSION_ID", encode_session_link_code(other))
    active = read_active_session()
    assert active is not None
    assert active["session_id"] == other
    assert active["source"] == "env"
    monkeypatch.setenv("LAB_TRACKER_SESSION_ID", "not-a-session")
    assert read_active_session() is None


def test_set_active_session_requires_a_real_session_reference(checkout: Path) -> None:
    server = SessionServer({SESSION_ID: PROJECT_A})
    with pytest.raises(LTValidationError):
        set_active_session("session-1", client=server)
    with pytest.raises(LTValidationError, match="--hours"):
        set_active_session(SESSION_ID, client=server, hours=0)


def test_set_active_session_fails_loudly_for_an_unknown_session(checkout: Path) -> None:
    with pytest.raises(LTAPIError, match="Session does not exist"):
        set_active_session(SESSION_ID, client=SessionServer({}))
    assert not active_session_path().exists()


def test_set_active_session_rejects_a_project_the_session_is_not_in(checkout: Path) -> None:
    with pytest.raises(LTValidationError, match=PROJECT_A):
        set_active_session(
            SESSION_ID, client=SessionServer({SESSION_ID: PROJECT_A}), project_id=PROJECT_B
        )
    assert not active_session_path().exists()


def test_session_target_only_in_the_project_the_session_was_verified_for(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_active_session(SESSION_ID, client=SessionServer({SESSION_ID: PROJECT_A}))
    active = read_active_session()
    assert session_target(active, PROJECT_A) == SESSION_ID
    assert session_target(active, PROJECT_B) is None
    assert session_target(active, None) is None
    assert session_target(None, PROJECT_A) is None
    assert capsys.readouterr().err == ""


def test_an_env_session_is_a_per_shell_choice_and_always_targets(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LAB_TRACKER_SESSION_ID", SESSION_ID)
    assert session_target(read_active_session(), PROJECT_B) == SESSION_ID


def test_a_context_recorded_without_its_project_is_unverified_and_hints_once(
    checkout: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = active_session_path()
    path.parent.mkdir(parents=True)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    path.write_text(
        json.dumps({"version": 1, "session_id": SESSION_ID, "expires_at": expires}),
        encoding="utf-8",
    )
    active = read_active_session()
    assert active is not None and active["project_id"] is None

    assert session_target(active, PROJECT_A) is None
    assert session_target(active, PROJECT_A) is None

    err = capsys.readouterr().err
    assert err.count("recorded without its project") == 1
    assert f"lt session use {encode_session_link_code(SESSION_ID)}" in err


def test_cli_session_verbs(
    checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_client(monkeypatch, _session_handler({SESSION_ID: PROJECT_A}))
    code = encode_session_link_code(SESSION_ID)
    lt_cli.main(["session", "use", f"LT-{code}", "--hours", "1", "--project", PROJECT_A])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "session-use"
    assert payload["session_id"] == SESSION_ID
    assert payload["link_code"] == code
    assert payload["project_id"] == PROJECT_A

    lt_cli.main(["session", "status"])
    status = json.loads(capsys.readouterr().out)
    assert status["active"] is True
    assert status["session_id"] == SESSION_ID

    lt_cli.main(["session", "clear"])
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["action"] == "cleared"
    lt_cli.main(["session", "status"])
    assert json.loads(capsys.readouterr().out)["active"] is False


def test_cli_session_use_fails_loudly_for_an_unknown_session(
    checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_client(monkeypatch, _session_handler({}))
    with pytest.raises(SystemExit) as exited:
        lt_cli.main(["session", "use", SESSION_ID])
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "error:" in err and "Session does not exist." in err
    assert not active_session_path().exists()


def test_cli_session_use_fails_loudly_when_the_server_is_unreachable(
    checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _cli_client(monkeypatch, offline)
    with pytest.raises(SystemExit) as exited:
        lt_cli.main(["session", "use", encode_session_link_code(SESSION_ID)])
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert f"Session {SESSION_ID} could not be verified" in err
    assert "The active session was not changed." in err
    assert not active_session_path().exists()


def test_cli_session_use_dry_run_writes_nothing(
    checkout: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cli_client(monkeypatch, _session_handler({SESSION_ID: PROJECT_A}))
    lt_cli.main(["session", "use", SESSION_ID, "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "would-set"
    assert not active_session_path().exists()
