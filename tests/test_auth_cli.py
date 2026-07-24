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
