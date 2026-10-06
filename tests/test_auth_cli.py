from __future__ import annotations

import json
from pathlib import Path

import pytest

from lab_tracker_client import auth as auth_helpers
from lab_tracker_client import cli as lt_cli


@pytest.fixture(autouse=True)
def _clear_appdata(monkeypatch):
    # Keep the Windows desktop-config candidate out of the enumeration so tests
    # are deterministic on every platform.
    monkeypatch.delenv("APPDATA", raising=False)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _mcp_servers(env: dict) -> dict:
    return {"mcpServers": {"lab-tracker": {"command": "lt-mcp", "env": env}}}


def test_auth_doctor_passes_lpat_and_flags_username_password(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _write_json(
        repo / ".mcp.json",
        _mcp_servers(
            {
                "LAB_TRACKER_MCP_BASE_URL": "https://lab.example.test",
                "LAB_TRACKER_MCP_API_KEY": "lpat_good",
            }
        ),
    )
    _write_json(
        home / "Library/Application Support/Claude/claude_desktop_config.json",
        _mcp_servers(
            {
                "LAB_TRACKER_MCP_BASE_URL": "https://lab.example.test",
                "LAB_TRACKER_MCP_USERNAME": "home-test-admin",
                "LAB_TRACKER_MCP_PASSWORD": "stale",
            }
        ),
    )

    payload = auth_helpers.auth_doctor(repo, home=home)

    by_surface = {r["surface"]: r for r in payload["registrations"]}
    assert by_surface["repo:.mcp.json"]["auth_mode"] == "api_key"
    assert "warning" not in by_surface["repo:.mcp.json"]
    desktop = by_surface["claude-desktop"]
    assert desktop["auth_mode"] == "username_password"
    assert desktop["base_url"] == "https://lab.example.test"
    assert "migrate" in desktop["warning"].lower()
    assert payload["deprecated_count"] == 1
    # The Cmd-Q relaunch reminder fires because a desktop registration was found.
    assert any("reopen" in note.lower() for note in payload["notes"])


def test_auth_doctor_reports_canonical_base_url_before_legacy(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write_json(
        repo / ".mcp.json",
        _mcp_servers(
            {
                "LAB_TRACKER_BASE_URL": "https://canonical.example.test",
                "LAB_TRACKER_MCP_BASE_URL": "https://legacy.example.test",
                "LAB_TRACKER_MCP_API_KEY": "lpat_good",
            }
        ),
    )

    payload = auth_helpers.auth_doctor(repo, home=tmp_path / "home")

    assert payload["registrations"][0]["base_url"] == "https://canonical.example.test"


def test_auth_doctor_reads_codex_toml_registration(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_config = home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        """
[mcp_servers.lab_tracker]
command = "lt-mcp"

[mcp_servers.lab_tracker.env]
LAB_TRACKER_BASE_URL = "https://canonical.example.test"
LAB_TRACKER_MCP_API_KEY = "lpat_good"
""".lstrip(),
        encoding="utf-8",
    )

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    registration = next(
        item for item in payload["registrations"] if item["surface"] == "codex"
    )
    assert registration["base_url"] == "https://canonical.example.test"
    assert registration["auth_mode"] == "api_key"


def test_auth_doctor_reads_claude_code_user_and_project_scopes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_json(
        home / ".claude.json",
        {
            "mcpServers": {
                "lab-tracker": {"command": "lt-mcp", "env": {"LAB_TRACKER_MCP_API_KEY": "lpat_u"}}
            },
            "projects": {
                "/work/proj-a": {
                    "mcpServers": {
                        "lab-tracker": {
                            "env": {
                                "LAB_TRACKER_MCP_USERNAME": "u",
                                "LAB_TRACKER_MCP_PASSWORD": "p",
                            }
                        }
                    }
                }
            },
        },
    )

    payload = auth_helpers.auth_doctor(tmp_path / "empty", home=home)

    modes = {(r["surface"], r.get("scope")): r["auth_mode"] for r in payload["registrations"]}
    assert modes[("claude-code", None)] == "api_key"
    assert modes[("claude-code", "/work/proj-a")] == "username_password"
    assert payload["deprecated_count"] == 1


def test_auth_doctor_handles_vscode_servers_schema_with_leftover_keys(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _write_json(
        repo / ".vscode/mcp.json",
        {
            "servers": {
                "lab-tracker": {
                    "command": "lt-mcp",
                    "env": {
                        "LAB_TRACKER_MCP_BASE_URL": "http://127.0.0.1:8000",
                        "LAB_TRACKER_MCP_API_KEY": "${input:lt-token}",
                        "LAB_TRACKER_MCP_USERNAME": "${input:lt-username}",
                        "LAB_TRACKER_MCP_PASSWORD": "${input:lt-password}",
                    },
                }
            }
        },
    )

    payload = auth_helpers.auth_doctor(repo, home=home)

    reg = next(r for r in payload["registrations"] if r["surface"] == "repo:.vscode/mcp.json")
    # api_key (even a ${input} placeholder) wins precedence, so the effective mode
    # is api_key — but the leftover deprecated keys still earn a cleanup warning.
    assert reg["auth_mode"] == "api_key"
    assert "remove" in reg["warning"].lower()
    assert payload["deprecated_count"] == 0  # api_key wins → not a broken login
    assert payload["warning_count"] == 1


def test_auth_doctor_keeps_scanning_legacy_visual_studio_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    _write_json(
        repo / "mcp.visualstudio.json",
        {
            "servers": {
                "lab-tracker": {
                    "command": "lt-mcp",
                    "env": {"LAB_TRACKER_MCP_API_KEY": "lpat_legacy"},
                }
            }
        },
    )

    payload = auth_helpers.auth_doctor(repo, home=home)

    reg = next(
        registration
        for registration in payload["registrations"]
        if registration["surface"] == "repo:mcp.visualstudio.json"
    )
    assert reg["auth_mode"] == "api_key"


def test_auth_doctor_is_fail_soft_on_malformed_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / ".mcp.json").write_text("{ not valid json", encoding="utf-8")

    payload = auth_helpers.auth_doctor(repo, home=home)  # must not raise

    assert payload["registrations"] == []
    assert payload["deprecated_count"] == 0


def test_auth_doctor_cli_exits_nonzero_on_deprecated_and_fail_silent_suppresses(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    _write_json(
        repo / ".mcp.json",
        _mcp_servers(
            {
                "LAB_TRACKER_MCP_BASE_URL": "https://lab.example.test",
                "LAB_TRACKER_MCP_USERNAME": "home-test-admin",
                "LAB_TRACKER_MCP_PASSWORD": "stale",
            }
        ),
    )
    monkeypatch.setattr(auth_helpers.Path, "home", classmethod(lambda cls: home))

    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["auth", "doctor", "--target", str(repo)])
    assert excinfo.value.code == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["deprecated_count"] == 1
    assert "WARN" in captured.err  # human-readable report on stderr

    # --fail-silent turns the audit into a quiet no-op exit for hook/scheduler use.
    lt_cli.main(["auth", "doctor", "--target", str(repo), "--fail-silent"])
    quiet = capsys.readouterr()
    assert quiet.err == ""


# --- Claude Desktop command facts (GH #218 option 2) -------------------------------

_DESKTOP_CONFIG = Path("Library/Application Support/Claude/claude_desktop_config.json")
_RELAUNCH_MARKER = "reopen"


def _write_desktop_config(home: Path, payload: dict) -> Path:
    path = home / _DESKTOP_CONFIG
    _write_json(path, payload)
    return path


def _desktop_entry(command: object) -> dict:
    return {"mcpServers": {"lab-tracker": {"command": command}}}


def _desktop_registration(payload: dict) -> dict:
    return next(r for r in payload["registrations"] if r["surface"] == "claude-desktop")


def _fake_executable(directory: Path, name: str = "lt-mcp") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / name
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _command_notes(payload: dict) -> list[str]:
    return [note for note in payload["notes"] if _RELAUNCH_MARKER not in note.lower()]


@pytest.mark.parametrize("command", ["lt-mcp", "bin/lt-mcp"])
def test_auth_doctor_reports_facts_and_a_neutral_note_for_a_non_absolute_desktop_command(
    tmp_path: Path, monkeypatch, command: str
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"
    _write_desktop_config(home, _desktop_entry(command))

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    desktop = _desktop_registration(payload)
    assert desktop["command"] == command
    assert desktop["command_is_absolute"] is False
    assert desktop["command_exists"] is False
    assert "warning" not in desktop
    assert payload["warning_count"] == 0
    (note,) = _command_notes(payload)
    assert repr(command) in note
    assert "absolute path" in note
    # A precaution, not a prediction: nothing here claims the app fails on it.
    assert "fail" not in note.lower()
    assert "silent" not in note.lower()
    assert _RELAUNCH_MARKER in "".join(payload["notes"]).lower()


def test_auth_doctor_bare_desktop_command_reports_whether_it_resolves_on_this_path(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir = tmp_path / "bin"
    _fake_executable(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir))
    home = tmp_path / "home"
    _write_desktop_config(home, _desktop_entry("lt-mcp"))

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    desktop = _desktop_registration(payload)
    assert desktop["command_is_absolute"] is False
    assert desktop["command_exists"] is True
    assert "warning" not in desktop
    # Resolving here says nothing about the app's own search path, so the note stays.
    assert len(_command_notes(payload)) == 1


def test_auth_doctor_is_quiet_about_an_absolute_desktop_command_that_exists(
    tmp_path: Path,
) -> None:
    executable = _fake_executable(tmp_path / "bin")
    home = tmp_path / "home"
    _write_desktop_config(home, _desktop_entry(str(executable)))

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    desktop = _desktop_registration(payload)
    assert desktop["command"] == str(executable)
    assert desktop["command_is_absolute"] is True
    assert desktop["command_exists"] is True
    assert "warning" not in desktop
    assert payload["warning_count"] == 0
    assert _command_notes(payload) == []


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_auth_doctor_warns_when_an_absolute_desktop_command_is_not_a_file(
    tmp_path: Path, kind: str
) -> None:
    target = tmp_path / "gone" / "lt-mcp"
    if kind == "directory":
        target.mkdir(parents=True)
    home = tmp_path / "home"
    _write_desktop_config(home, _desktop_entry(str(target)))

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    desktop = _desktop_registration(payload)
    assert desktop["command_is_absolute"] is True
    assert desktop["command_exists"] is False
    assert str(target) in desktop["warning"]
    assert "not an existing file" in desktop["warning"]
    assert payload["warning_count"] == 1
    assert payload["deprecated_count"] == 0
    assert _command_notes(payload) == []


def test_auth_doctor_keeps_the_deprecated_login_warning_beside_a_missing_command_warning(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "gone" / "lt-mcp"
    home = tmp_path / "home"
    _write_desktop_config(
        home,
        {
            "mcpServers": {
                "lab-tracker": {
                    "command": str(missing),
                    "env": {
                        "LAB_TRACKER_MCP_USERNAME": "user",
                        "LAB_TRACKER_MCP_PASSWORD": "stale",
                    },
                }
            }
        },
    )

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    desktop = _desktop_registration(payload)
    assert "migrate" in desktop["warning"].lower()
    assert str(missing) in desktop["warning"]
    assert payload["deprecated_count"] == 1
    assert payload["warning_count"] == 1  # one registration, however many problems


def test_auth_doctor_leaves_non_desktop_registrations_without_command_facts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    missing = tmp_path / "gone" / "lt-mcp"
    _write_json(repo / ".mcp.json", {"mcpServers": {"lab-tracker": {"command": str(missing)}}})

    payload = auth_helpers.auth_doctor(repo, home=tmp_path / "home")

    (registration,) = payload["registrations"]
    assert registration["surface"] == "repo:.mcp.json"
    assert not {"command", "command_is_absolute", "command_exists"} & registration.keys()
    assert "warning" not in registration
    assert payload["warning_count"] == 0
    assert payload["notes"] == []


def test_auth_doctor_says_nothing_about_desktop_when_there_is_no_desktop_config(
    tmp_path: Path,
) -> None:
    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=tmp_path / "home")

    assert payload["registrations"] == []
    assert payload["warning_count"] == 0
    assert payload["notes"] == []
    assert auth_helpers.render_report(payload) == (
        "lab-tracker auth: no MCP registrations found across known surfaces."
    )


def test_auth_doctor_ignores_a_malformed_desktop_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / _DESKTOP_CONFIG
    config.parent.mkdir(parents=True)
    config.write_text("{ not valid json", encoding="utf-8")

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)  # must not raise

    assert payload["registrations"] == []
    assert payload["warning_count"] == 0
    assert payload["notes"] == []
    assert config.read_text(encoding="utf-8") == "{ not valid json"


def test_auth_doctor_ignores_a_desktop_config_with_only_other_servers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_desktop_config(
        home,
        {"mcpServers": {"filesystem": {"command": str(tmp_path / "gone" / "npx"), "args": []}}},
    )

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    assert payload["registrations"] == []
    assert payload["warning_count"] == 0
    assert payload["notes"] == []


@pytest.mark.parametrize("command", [None, "", "   ", ["lt-mcp"], 7])
def test_auth_doctor_reports_no_command_facts_when_the_desktop_entry_has_no_command(
    tmp_path: Path, command: object
) -> None:
    home = tmp_path / "home"
    _write_desktop_config(home, _desktop_entry(command))

    payload = auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    desktop = _desktop_registration(payload)
    assert not {"command", "command_is_absolute", "command_exists"} & desktop.keys()
    assert "warning" not in desktop
    assert _command_notes(payload) == []


def test_auth_doctor_never_writes_the_desktop_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = _write_desktop_config(home, _desktop_entry(str(tmp_path / "gone" / "lt-mcp")))
    before = (config.read_bytes(), config.stat().st_mtime_ns)

    auth_helpers.auth_doctor(tmp_path / "repo", home=home)

    assert (config.read_bytes(), config.stat().st_mtime_ns) == before
    assert sorted(path.name for path in config.parent.iterdir()) == [config.name]


def test_auth_doctor_report_and_exit_status_for_a_missing_desktop_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    home = tmp_path / "home"
    missing = tmp_path / "gone" / "lt-mcp"
    _write_desktop_config(home, _desktop_entry(str(missing)))
    monkeypatch.setattr(auth_helpers.Path, "home", classmethod(lambda cls: home))

    # The check is advisory: a warning is printed but the exit status stays zero.
    lt_cli.main(["auth", "doctor", "--target", str(tmp_path / "repo")])

    captured = capsys.readouterr()
    assert json.loads(captured.out)["warning_count"] == 1
    assert "WARN: " in captured.err
    assert str(missing) in captured.err


def test_auth_doctor_report_prints_the_neutral_note_for_a_bare_desktop_command(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _write_desktop_config(home, _desktop_entry("lt-mcp"))

    report = auth_helpers.render_report(auth_helpers.auth_doctor(tmp_path / "repo", home=home))

    assert "WARN" not in report
    assert "note: " in report
    assert "absolute path" in report
