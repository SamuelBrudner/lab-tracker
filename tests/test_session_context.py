"""Session link codes, path matching, and the per-checkout active session."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lab_tracker.models import encode_session_link_code as server_encode
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LTValidationError
from lab_tracker_client.session_context import (
    active_session_path,
    clear_active_session,
    decode_session_link_code,
    encode_session_link_code,
    find_session_link_code,
    read_active_session,
    session_id_from_reference,
    set_active_session,
    strict_session_id,
)

SESSION_ID = "3d4f6a1e-9c2b-4a8e-8f01-2b3c4d5e6f70"


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_SESSION_CONTEXT", raising=False)
    monkeypatch.chdir(tmp_path)
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
    with pytest.raises(LTValidationError, match="invalid characters"):
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
    assert find_session_link_code(f"{code.lower()}/sweep.nwb") == (code, SESSION_ID)
    # Embedded in a longer alphanumeric run, or the wrong alphabet: no match.
    assert find_session_link_code(f"x{code}y/a.png") is None
    assert find_session_link_code("2025_12_10_Rig2_session001.nwb") is None
    assert find_session_link_code("a" * 26) is None or find_session_link_code("a" * 26)[1]


def test_active_session_is_written_read_expired_and_cleared(checkout: Path) -> None:
    code = encode_session_link_code(SESSION_ID)
    result = set_active_session(code, project_id="project-1", hours=2)
    path = active_session_path()
    assert result["action"] == "set"
    assert Path(result["path"]) == path
    assert path.is_file()
    active = read_active_session()
    assert active is not None
    assert active["session_id"] == SESSION_ID
    assert active["link_code"] == code
    assert active["project_id"] == "project-1"
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
    set_active_session(SESSION_ID)
    other = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
    monkeypatch.setenv("LAB_TRACKER_SESSION_ID", encode_session_link_code(other))
    active = read_active_session()
    assert active is not None
    assert active["session_id"] == other
    assert active["source"] == "env"
    monkeypatch.setenv("LAB_TRACKER_SESSION_ID", "not-a-session")
    assert read_active_session() is None


def test_set_active_session_requires_a_real_session_reference(checkout: Path) -> None:
    with pytest.raises(LTValidationError):
        set_active_session("session-1")
    with pytest.raises(LTValidationError, match="--hours"):
        set_active_session(SESSION_ID, hours=0)


def test_cli_session_verbs(checkout: Path, capsys) -> None:
    code = encode_session_link_code(SESSION_ID)
    lt_cli.main(["session", "use", f"LT-{code}", "--hours", "1", "--project", "p-1"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "session-use"
    assert payload["session_id"] == SESSION_ID
    assert payload["link_code"] == code

    lt_cli.main(["session", "status"])
    status = json.loads(capsys.readouterr().out)
    assert status["active"] is True
    assert status["session_id"] == SESSION_ID

    lt_cli.main(["session", "clear"])
    cleared = json.loads(capsys.readouterr().out)
    assert cleared["action"] == "cleared"
    lt_cli.main(["session", "status"])
    assert json.loads(capsys.readouterr().out)["active"] is False


def test_cli_session_use_dry_run_writes_nothing(checkout: Path, capsys) -> None:
    lt_cli.main(["session", "use", SESSION_ID, "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "would-set"
    assert not active_session_path().exists()
