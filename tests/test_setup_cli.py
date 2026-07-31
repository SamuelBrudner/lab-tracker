"""Tests for the guided-setup verbs: profile, status, init delegation, bind."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import lab_tracker_client.setup as setup_helpers
from lab_tracker_client import LTRecord
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LabTracker, LTValidationError, load_connection_profile

SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _clear_connection_env(monkeypatch) -> None:
    for name in (
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_MCP_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_MCP_USERNAME",
        "LAB_TRACKER_MCP_PASSWORD",
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_HPC_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    home = tmp_path / "lt-home"
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(home))
    _clear_connection_env(monkeypatch)
    return home


def test_from_env_prefers_env_over_profile(config_home, monkeypatch) -> None:
    config_home.mkdir(parents=True)
    (config_home / "config.json").write_text(
        json.dumps(
            {
                "base_url": "http://profile:9000",
                "access_token": "profile-token",
                "default_project_id": "profile-project",
            }
        ),
        encoding="utf-8",
    )

    client = LabTracker.from_env()
    try:
        assert client.base_url == "http://profile:9000"
        assert client.access_token == "profile-token"
        assert client.default_project_id == "profile-project"
    finally:
        client.close()

    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://env:8000")
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "env-token")
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "env-project")
    client = LabTracker.from_env()
    try:
        assert client.base_url == "http://env:8000"
        assert client.access_token == "env-token"
        assert client.default_project_id == "env-project"
    finally:
        client.close()


def test_from_env_ignores_profile_token_when_env_supplies_credentials(
    config_home, monkeypatch
) -> None:
    config_home.mkdir(parents=True)
    (config_home / "config.json").write_text(
        json.dumps({"base_url": "http://profile:9000", "access_token": "profile-token"}),
        encoding="utf-8",
    )

    # Env username/password means the env credentials must drive auth.
    monkeypatch.setenv("LAB_TRACKER_USERNAME", "alice")
    monkeypatch.setenv("LAB_TRACKER_PASSWORD", "pw")
    client = LabTracker.from_env()
    try:
        assert client.access_token is None
        assert client.username == "alice"
    finally:
        client.close()

    # An env base URL pointing at a DIFFERENT server must not receive the
    # profile token saved for another server.
    monkeypatch.delenv("LAB_TRACKER_USERNAME")
    monkeypatch.delenv("LAB_TRACKER_PASSWORD")
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://other:8000")
    client = LabTracker.from_env()
    try:
        assert client.access_token is None
    finally:
        client.close()

    # Same server (modulo trailing slash) keeps the profile token usable.
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://profile:9000/")
    client = LabTracker.from_env()
    try:
        assert client.access_token == "profile-token"
    finally:
        client.close()


def test_load_connection_profile_is_fail_soft(config_home) -> None:
    assert load_connection_profile() == {}

    config_home.mkdir(parents=True)
    (config_home / "config.json").write_text("{not json", encoding="utf-8")
    assert load_connection_profile() == {}

    client = LabTracker.from_env()
    try:
        assert client.base_url == "http://127.0.0.1:8000"
    finally:
        client.close()

    (config_home / "config.json").write_text(
        json.dumps(
            {
                "base_url": "not-a-url",
                "access_token": "wrong-server-token",
            }
        ),
        encoding="utf-8",
    )
    client = LabTracker.from_env()
    try:
        assert client.base_url == "http://127.0.0.1:8000"
        assert client.access_token is None
    finally:
        client.close()


def test_setup_connect_requires_consent(config_home, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["setup", "connect", "--base-url", "http://x:1"])
    assert "--yes" in str(excinfo.value)
    assert not (config_home / "config.json").exists()


def test_setup_connect_dry_run_writes_nothing_and_redacts_token(
    config_home, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("LAB_TRACKER_ACCESS_TOKEN", "secret-token")
    lt_cli.main(
        [
            "setup",
            "connect",
            "--base-url",
            "http://lab:8000",
            "--save-token",
            "--dry-run",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "setup-connect"
    assert payload["dry_run"] is True
    assert payload["has_token"] is True
    assert "secret-token" not in json.dumps(payload)
    assert not (config_home / "config.json").exists()


def test_setup_connect_persists_token_only_with_save_token(config_home, capsys) -> None:
    lt_cli.main(
        [
            "setup",
            "connect",
            "--base-url",
            "http://lab:8000",
            "--project",
            "project-1",
            "--yes",
        ]
    )
    capsys.readouterr()
    profile = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert profile == {"base_url": "http://lab:8000", "default_project_id": "project-1"}

    with pytest.raises(SystemExit):
        lt_cli.main(["setup", "connect", "--token", "tok", "--yes"])

    lt_cli.main(["setup", "connect", "--save-token", "--token", "tok", "--yes"])
    capsys.readouterr()
    profile = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert profile["access_token"] == "tok"
    assert profile["base_url"] == "http://lab:8000"

    lt_cli.main(["setup", "connect", "--uninstall", "--yes"])
    capsys.readouterr()
    assert not (config_home / "config.json").exists()


def test_setup_connect_windows_acl_failure_never_writes_token_or_replaces_profile(
    config_home, monkeypatch
) -> None:
    config_home.mkdir(parents=True)
    profile_path = config_home / "config.json"
    original = b'{\n  "base_url": "https://existing.example"\n}\n'
    profile_path.write_bytes(original)
    observed_temp_bytes: list[bytes] = []

    monkeypatch.setattr(setup_helpers.sys, "platform", "win32")

    def reject_empty_temp(path):
        observed_temp_bytes.append(path.read_bytes())
        return False

    monkeypatch.setattr(
        setup_helpers,
        "_harden_profile_permissions",
        reject_empty_temp,
    )

    with pytest.raises(SystemExit, match="no changes were saved"):
        lt_cli.main(
            [
                "setup",
                "connect",
                "--save-token",
                "--token",
                "new-secret-token",
                "--yes",
            ]
        )

    assert observed_temp_bytes == [b""]
    assert profile_path.read_bytes() == original
    assert not list(config_home.glob(".config.json.*.tmp"))
    assert all(
        b"new-secret-token" not in candidate.read_bytes()
        for candidate in config_home.iterdir()
        if candidate.is_file()
    )


def test_setup_connect_windows_acl_recheck_failure_cleans_temp_and_preserves_profile(
    config_home, monkeypatch
) -> None:
    config_home.mkdir(parents=True)
    profile_path = config_home / "config.json"
    original = b'{\n  "base_url": "https://existing.example"\n}\n'
    profile_path.write_bytes(original)
    observed_temp_bytes: list[bytes] = []

    monkeypatch.setattr(setup_helpers.sys, "platform", "win32")

    def fail_recheck(path):
        observed_temp_bytes.append(path.read_bytes())
        return len(observed_temp_bytes) == 1

    monkeypatch.setattr(
        setup_helpers,
        "_harden_profile_permissions",
        fail_recheck,
    )

    with pytest.raises(SystemExit, match="no changes were saved"):
        lt_cli.main(
            [
                "setup",
                "connect",
                "--save-token",
                "--token",
                "new-secret-token",
                "--yes",
            ]
        )

    assert observed_temp_bytes[0] == b""
    assert b"new-secret-token" in observed_temp_bytes[1]
    assert profile_path.read_bytes() == original
    assert not list(config_home.glob(".config.json.*.tmp"))


def test_setup_connect_interrupt_after_token_write_cleans_temp_and_preserves_profile(
    config_home, monkeypatch
) -> None:
    config_home.mkdir(parents=True)
    profile_path = config_home / "config.json"
    original = b'{\n  "base_url": "https://existing.example"\n}\n'
    profile_path.write_bytes(original)

    monkeypatch.setattr(setup_helpers.sys, "platform", "win32")
    monkeypatch.setattr(
        setup_helpers,
        "_harden_profile_permissions",
        lambda _path: True,
    )

    def interrupt_after_flush(_fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_helpers.os, "fsync", interrupt_after_flush)

    with pytest.raises(KeyboardInterrupt):
        lt_cli.main(
            [
                "setup",
                "connect",
                "--save-token",
                "--token",
                "new-secret-token",
                "--yes",
            ]
        )

    assert profile_path.read_bytes() == original
    assert not list(config_home.glob(".config.json.*.tmp"))


def test_setup_connect_never_leaks_a_previously_stored_token(config_home, capsys) -> None:
    lt_cli.main(["setup", "connect", "--save-token", "--token", "stored-secret", "--yes"])
    capsys.readouterr()

    # A later run (dry-run or applying) diffs against the token-bearing
    # profile; the stored secret must not surface on the '-' side.
    lt_cli.main(["setup", "connect", "--project", "p-2", "--dry-run"])
    dry_payload = json.loads(capsys.readouterr().out)
    assert "stored-secret" not in json.dumps(dry_payload)

    lt_cli.main(["setup", "connect", "--project", "p-2", "--yes"])
    apply_payload = json.loads(capsys.readouterr().out)
    assert "stored-secret" not in json.dumps(apply_payload)
    profile = json.loads((config_home / "config.json").read_text(encoding="utf-8"))
    assert profile["access_token"] == "stored-secret"


def test_installed_source_revision_reads_pep610_commit_id(monkeypatch) -> None:
    direct_url = json.dumps(
        {
            "url": "https://github.com/SamuelBrudner/lab-tracker.git",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": SOURCE_REVISION,
                "commit_id": SOURCE_REVISION.upper(),
            },
        }
    )

    def distribution(name):
        assert name == "lab-tracker"
        return SimpleNamespace(read_text=lambda _filename: direct_url)

    monkeypatch.setattr(setup_helpers.importlib.metadata, "distribution", distribution)

    assert setup_helpers.installed_source_revision() == SOURCE_REVISION


def test_setup_verify_client_compares_the_pep610_git_revision(
    config_home, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        setup_helpers,
        "installed_source_revision",
        lambda: SOURCE_REVISION,
    )

    lt_cli.main(
        [
            "setup",
            "verify-client",
            "--expected-revision",
            SOURCE_REVISION,
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "command": "setup-verify-client",
        "expected_revision": SOURCE_REVISION,
        "installed_revision": SOURCE_REVISION,
        "compatible": True,
        "ok": True,
    }


def test_setup_verify_client_fails_closed_for_missing_or_mismatched_revision(
    config_home, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(setup_helpers, "installed_source_revision", lambda: None)

    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(
            [
                "setup",
                "verify-client",
                "--expected-revision",
                SOURCE_REVISION,
            ]
        )
    assert excinfo.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["compatible"] is False
    assert payload["ok"] is False
    assert "no immutable Git revision metadata" in payload["error"]

    mismatched_revision = "f" * 40
    monkeypatch.setattr(
        setup_helpers,
        "installed_source_revision",
        lambda: mismatched_revision,
    )
    mismatch = setup_helpers.verify_client_revision(SOURCE_REVISION)
    assert mismatch["installed_revision"] == mismatched_revision
    assert mismatch["compatible"] is False
    assert mismatch["ok"] is False
    assert "does not match" in mismatch["error"]

    with pytest.raises(LTValidationError, match="full 40-character Git source revision"):
        setup_helpers.verify_client_revision("main")


def test_setup_verify_mcp_prefers_the_current_client_environment(
    tmp_path, monkeypatch
) -> None:
    scripts_dir = tmp_path / "client-environment" / "bin"
    scripts_dir.mkdir(parents=True)
    python = scripts_dir / "python"
    companion = scripts_dir / "lt-mcp"
    python.touch()
    companion.touch()
    monkeypatch.setattr(setup_helpers.sys, "executable", str(python))
    monkeypatch.setattr(
        setup_helpers.shutil,
        "which",
        lambda _command: "/other-environment/lt-mcp",
    )

    assert setup_helpers._resolve_mcp_executable("lt-mcp") == str(companion)
    assert (
        setup_helpers._resolve_mcp_executable("custom-lt-mcp")
        == "/other-environment/lt-mcp"
    )


def test_setup_verify_mcp_refuses_to_launch_a_mismatched_client(
    config_home, monkeypatch
) -> None:
    monkeypatch.setattr(
        setup_helpers,
        "installed_source_revision",
        lambda: "f" * 40,
    )

    def unexpected_launch(_command):
        raise AssertionError("MCP executable resolution must not run for a mismatched client")

    monkeypatch.setattr(setup_helpers, "_resolve_mcp_executable", unexpected_launch)

    payload = setup_helpers.verify_mcp_launch(expected_revision=SOURCE_REVISION)

    assert payload["ok"] is False
    assert payload["launched"] is False
    assert payload["client_compatibility"]["compatible"] is False
    assert "Refusing to launch MCP" in payload["error"]


def test_setup_verify_mcp_launches_stdio_health_and_authenticated_project_read(
    config_home, monkeypatch
) -> None:
    observed = {"calls": []}

    def fake_run(args, **kwargs):
        requests = [
            json.loads(line)
            for line in kwargs["input"].splitlines()
            if '"id":' in line
        ]
        tool_name = requests[-1]["params"]["name"]
        observed["calls"].append(
            {
                "args": args,
                "env": kwargs["env"],
                "input": kwargs["input"],
                "tool_name": tool_name,
            }
        )
        structured_content = (
            {"status": "ok"}
            if tool_name == "lab_tracker_health"
            else {
                "data": [],
                "meta": {"limit": 1, "offset": 0, "total": 0},
            }
        )
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {
                                "name": "lab-tracker-mcp",
                                "version": "test",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "isError": False,
                            "structuredContent": structured_content,
                        },
                    }
                ),
            ]
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(setup_helpers, "installed_source_revision", lambda: SOURCE_REVISION)
    monkeypatch.setattr(
        setup_helpers,
        "_resolve_mcp_executable",
        lambda _command: "/tools/lt-mcp",
    )
    monkeypatch.setattr(setup_helpers.subprocess, "run", fake_run)

    payload = setup_helpers.verify_mcp_launch(expected_revision=SOURCE_REVISION)

    assert payload["ok"] is True
    assert payload["initialized"] is True
    assert payload["health_ok"] is True
    assert payload["authenticated_read_ok"] is True
    assert len(observed["calls"]) == 2
    assert all(call["args"] == ["/tools/lt-mcp"] for call in observed["calls"])
    assert all(
        call["env"]["LAB_TRACKER_MCP_TRANSPORT"] == "stdio"
        for call in observed["calls"]
    )
    assert [call["tool_name"] for call in observed["calls"]] == [
        "lab_tracker_health",
        "lab_tracker_list_projects",
    ]


def test_setup_verify_mcp_reports_auth_failure_and_redacts_tokens(
    config_home, monkeypatch
) -> None:
    monkeypatch.setattr(setup_helpers, "installed_source_revision", lambda: SOURCE_REVISION)
    monkeypatch.setattr(
        setup_helpers,
        "_resolve_mcp_executable",
        lambda _command: "/tools/lt-mcp",
    )

    def fake_run(_args, **kwargs):
        requests = [
            json.loads(line)
            for line in kwargs["input"].splitlines()
            if '"id":' in line
        ]
        tool_name = requests[-1]["params"]["name"]
        structured_content = (
            {"status": "ok"}
            if tool_name == "lab_tracker_health"
            else {
                "error": {
                    "code": "lab_tracker_api_error",
                    "message": "Authentication required.",
                }
            }
        )
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "result": {
                                "serverInfo": {"name": "lab-tracker-mcp"}
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {
                                "isError": False,
                                "structuredContent": structured_content,
                            },
                        }
                    ),
                ]
            ),
            stderr=(
                "startup failed with lpat_do-not-print "
                "linv_invite-secret ldev_device-secret lpair_pair-secret"
            ),
        )

    monkeypatch.setattr(setup_helpers.subprocess, "run", fake_run)

    payload = setup_helpers.verify_mcp_launch(expected_revision=SOURCE_REVISION)

    assert payload["ok"] is False
    assert payload["health_ok"] is True
    assert payload["authenticated_read_ok"] is False
    assert "authenticated project read failed" in payload["error"]
    assert "lpat_do-not-print" not in payload["diagnostic"]
    assert "linv_invite-secret" not in payload["diagnostic"]
    assert "ldev_device-secret" not in payload["diagnostic"]
    assert "lpair_pair-secret" not in payload["diagnostic"]


def test_setup_status_is_read_only_and_reports_repo_state(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    (repo / "lt_ids.json").write_text(
        json.dumps({"project_id": "", "project_name": "demo"}), encoding="utf-8"
    )
    before = sorted(path.name for path in repo.iterdir())

    lt_cli.main(["setup", "status", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "setup-status"
    assert payload["server"]["base_url"] == "http://127.0.0.1:8000"
    assert payload["server"]["source"] == "default"
    assert payload["server"]["reachable"] in {True, False}
    assert payload["profile"]["present"] is False
    assert payload["repo"]["scaffold"]["lt_ids.json"] is True
    assert payload["repo"]["scaffold"][".mcp.json"] is False
    assert payload["repo"]["lt_ids"]["project_id_bound"] is False
    assert payload["watch"]["config_present"] is False
    assert payload["hpc"]["config_present"] is False
    assert sorted(path.name for path in repo.iterdir()) == before


def test_setup_status_suggests_persisting_default_base_url(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(setup_helpers, "probe_health", lambda _url: True)

    lt_cli.main(["setup", "status", "--target", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["server"]["source"] == "default"
    assert any("setup connect --base-url" in item for item in payload["suggestions"])


def test_setup_status_watch_guidance_is_optional_when_commit_hook_active(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    (tmp_path / "artifacts").mkdir()
    monkeypatch.setattr(
        setup_helpers,
        "_hooks_status",
        lambda _root: {
            "git_repo": True,
            "post_commit_present": True,
            "managed_block_present": True,
            "lt_path_exists": True,
        },
    )

    lt_cli.main(["setup", "status", "--target", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["watch"]["candidate_roots"][0]["name"] == "artifacts"
    watch_suggestions = [
        item for item in payload["suggestions"] if "watch add" in item
    ]
    assert watch_suggestions
    assert "Commit snapshots are active" in watch_suggestions[0]
    assert "skip watch setup" in watch_suggestions[0]
    assert "artifacts" in watch_suggestions[0]


def test_setup_status_fail_silent_never_raises(config_home, tmp_path, capsys) -> None:
    lt_cli.main(["setup", "status", "--target", str(tmp_path / "missing"), "--fail-silent"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "setup-status"


def test_setup_init_delegates_to_consumer_scaffold(config_home, tmp_path, capsys) -> None:
    repo = tmp_path / "consumer"
    lt_cli.main(["setup", "init", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "setup-init"
    assert (repo / ".mcp.json").exists()
    assert (repo / "lt_ids.json").exists()
    assert any("lt project bind" in offer for offer in payload["offers"])

    claude = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "lt setup status" in claude
    agents = (repo / "AGENTS.lt.md").read_text(encoding="utf-8")
    assert "lt setup status" in agents


def test_setup_init_uses_saved_profile_base_url(config_home, tmp_path, capsys) -> None:
    config_home.mkdir(parents=True)
    (config_home / "config.json").write_text(
        json.dumps({"base_url": "https://lt.example.test"}), encoding="utf-8"
    )
    repo = tmp_path / "consumer-profile"

    lt_cli.main(["setup", "init", "--target", str(repo)])
    payload = json.loads(capsys.readouterr().out)

    mcp_config = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    server_env = mcp_config["mcpServers"]["lab-tracker"]["env"]
    assert server_env["LAB_TRACKER_BASE_URL"] == "https://lt.example.test"
    assert payload["mcp_base_url"] == "https://lt.example.test"
    assert payload["mcp_base_url_source"] == "profile"


def test_update_preserves_saved_profile_base_url(config_home, tmp_path, capsys) -> None:
    config_home.mkdir(parents=True)
    (config_home / "config.json").write_text(
        json.dumps({"base_url": "https://lt.example.test/app"}),
        encoding="utf-8",
    )
    repo = tmp_path / "consumer-profile-update"

    lt_cli.main(["setup", "init", "--target", str(repo)])
    capsys.readouterr()
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lab-tracker": {"command": "old"}}}),
        encoding="utf-8",
    )

    lt_cli.main(["update", "--target", str(repo)])
    capsys.readouterr()

    mcp_config = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
    server_env = mcp_config["mcpServers"]["lab-tracker"]["env"]
    assert server_env["LAB_TRACKER_BASE_URL"] == "https://lt.example.test"


def test_setup_init_dry_run_writes_nothing(config_home, tmp_path, capsys) -> None:
    repo = tmp_path / "consumer-dry"
    lt_cli.main(["setup", "init", "--target", str(repo), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "setup-init"
    assert payload["diffs"]
    assert not repo.exists()


class _FakeBindClient:
    def __init__(self, projects: list[dict[str, str]]) -> None:
        self.projects = projects
        self.created: list[str] = []

    def list_projects(self) -> list[LTRecord]:
        return [LTRecord(project) for project in self.projects]

    def upsert_project(self, *, name: str, description: str = "", status=None) -> LTRecord:
        self.created.append(name)
        record = {"project_id": "created-1", "name": name}
        self.projects.append(record)
        return LTRecord(record)

    def close(self) -> None:
        pass


def _fake_bind_client(monkeypatch, projects: list[dict[str, str]]) -> _FakeBindClient:
    fake = _FakeBindClient(projects)
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )
    return fake


class _AuthFailBindClient(_FakeBindClient):
    def list_projects(self) -> list[LTRecord]:
        raise RuntimeError("401: set LAB_TRACKER_USERNAME/PASSWORD")


def test_project_bind_requires_consent(config_home, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_bind_client(monkeypatch, [{"project_id": "p-1", "name": "Demo"}])
    with pytest.raises(SystemExit) as excinfo:
        lt_cli.main(["project", "bind", "--name", "Demo"])
    assert "--yes" in str(excinfo.value)
    assert not (tmp_path / "lt_ids.json").exists()


def test_project_bind_dry_run_and_write(config_home, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_bind_client(monkeypatch, [{"project_id": "p-1", "name": "Demo"}])

    lt_cli.main(["project", "bind", "--name", "Demo", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "would-bind"
    assert payload["project_id"] == "p-1"
    assert "+" in payload["diff"]
    assert not (tmp_path / "lt_ids.json").exists()

    lt_cli.main(["project", "bind", "--name", "Demo", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "bound"
    ids_payload = json.loads((tmp_path / "lt_ids.json").read_text(encoding="utf-8"))
    assert ids_payload["project_id"] == "p-1"
    assert ids_payload["project_name"] == "Demo"


def test_project_bind_preserves_existing_ids_keys(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lt_ids.json").write_text(
        json.dumps({"project_id": "", "question_id": "q-9"}), encoding="utf-8"
    )
    _fake_bind_client(monkeypatch, [{"project_id": "p-1", "name": "Demo"}])

    lt_cli.main(["project", "bind", "--project-id", "p-1", "--yes"])
    capsys.readouterr()
    ids_payload = json.loads((tmp_path / "lt_ids.json").read_text(encoding="utf-8"))
    assert ids_payload["project_id"] == "p-1"
    assert ids_payload["question_id"] == "q-9"


def test_project_bind_by_id_writes_with_auth_warning_when_lookup_fails(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = _AuthFailBindClient([])
    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: fake),  # noqa: ARG005
    )

    lt_cli.main(["project", "bind", "--project-id", "p-raw", "--dry-run"])
    dry_payload = json.loads(capsys.readouterr().out)
    assert dry_payload["action"] == "would-bind"
    assert dry_payload["project_id"] == "p-raw"
    assert "LAB_TRACKER_ACCESS_TOKEN" in dry_payload["warnings"][0]
    assert "setup connect --save-token" in dry_payload["warnings"][0]
    assert not (tmp_path / "lt_ids.json").exists()

    lt_cli.main(["project", "bind", "--project-id", "p-raw", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "bound"
    ids_payload = json.loads((tmp_path / "lt_ids.json").read_text(encoding="utf-8"))
    assert ids_payload == {"project_id": "p-raw"}


def test_project_bind_fails_loudly_on_ambiguous_name(
    config_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_bind_client(
        monkeypatch,
        [
            {"project_id": "p-1", "name": "Demo"},
            {"project_id": "p-2", "name": "Demo"},
        ],
    )
    with pytest.raises(Exception, match="Multiple projects"):
        lt_cli.main(["project", "bind", "--name", "Demo", "--yes"])
    assert not (tmp_path / "lt_ids.json").exists()


def test_project_bind_create_requires_flag(config_home, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    fake = _fake_bind_client(monkeypatch, [])

    with pytest.raises(Exception, match="--create"):
        lt_cli.main(["project", "bind", "--name", "Fresh", "--yes"])
    assert fake.created == []

    lt_cli.main(["project", "bind", "--name", "Fresh", "--create", "--yes"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["created_project"] is True
    assert fake.created == ["Fresh"]


def test_project_bind_create_dry_run_does_not_create(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    fake = _fake_bind_client(monkeypatch, [])

    lt_cli.main(["project", "bind", "--name", "Fresh", "--create", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "would-bind"
    assert payload["created_project"] is True
    assert fake.created == []
    assert not (tmp_path / "lt_ids.json").exists()


def test_project_bind_rejects_malformed_ids_file(
    config_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "lt_ids.json").write_text("{broken", encoding="utf-8")
    _fake_bind_client(monkeypatch, [{"project_id": "p-1", "name": "Demo"}])

    with pytest.raises(Exception, match="not valid JSON"):
        lt_cli.main(["project", "bind", "--project-id", "p-1", "--yes"])
    assert (tmp_path / "lt_ids.json").read_text(encoding="utf-8") == "{broken"


def test_watch_add_list_remove_round_trip(config_home, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    config_path = tmp_path / ".lab-tracker" / "watch.json"

    lt_cli.main(
        [
            "watch",
            "add",
            str(results),
            "--include",
            "*.png",
            "--config",
            str(config_path),
        ]
    )
    add_payload = json.loads(capsys.readouterr().out)
    assert add_payload["command"] == "watch-add"
    assert add_payload["action"] == "added"
    assert add_payload["created_config"] is True

    lt_cli.main(
        [
            "watch",
            "add",
            str(results),
            "--include",
            "*.png",
            "--config",
            str(config_path),
        ]
    )
    repeat_payload = json.loads(capsys.readouterr().out)
    assert repeat_payload["action"] == "unchanged"
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert len(stored["watches"]) == 1

    lt_cli.main(["watch", "list", "--config", str(config_path)])
    list_payload = json.loads(capsys.readouterr().out)
    assert list_payload["command"] == "watch-list"
    assert len(list_payload["watches"]) == 1

    lt_cli.main(["watch", "remove", str(results), "--config", str(config_path)])
    remove_payload = json.loads(capsys.readouterr().out)
    assert remove_payload["command"] == "watch-remove"
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["watches"] == []


def test_watch_add_preserves_existing_entries_and_project(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    lt_cli.main(
        ["watch", "init", "--project", "project-1", "--config", str(config_path)]
    )
    capsys.readouterr()

    lt_cli.main(["watch", "add", "inbox", "--config", str(config_path)])
    capsys.readouterr()
    lt_cli.main(
        [
            "watch",
            "add",
            "outputs",
            "--sink",
            "acquisition-output",
            "--session",
            "s-1",
            "--config",
            str(config_path),
        ]
    )
    capsys.readouterr()

    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["project_id"] == "project-1"
    assert [watch["root"] for watch in stored["watches"]] == ["inbox", "outputs"]
    assert stored["watches"][1]["session_id"] == "s-1"


def test_watch_add_acquisition_output_requires_session(
    config_home, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Exception, match="session"):
        lt_cli.main(["watch", "add", "outputs", "--sink", "acquisition-output"])


def test_watch_add_dry_run_writes_nothing(config_home, tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    lt_cli.main(["watch", "add", "inbox", "--config", str(config_path), "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "added"
    assert payload["dry_run"] is True
    assert not config_path.exists()


def test_watch_add_refuses_malformed_config(config_home, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(Exception, match="not valid JSON"):
        lt_cli.main(["watch", "add", "inbox", "--config", str(config_path)])
    assert config_path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths are case-insensitive.")
def test_watch_remove_matches_case_insensitively_for_deleted_folders(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    missing_root = str(tmp_path / "NoSuchDir" / "Results")

    lt_cli.main(["watch", "add", missing_root, "--config", str(config_path)])
    capsys.readouterr()
    lt_cli.main(["watch", "remove", missing_root.lower(), "--config", str(config_path)])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "watch-remove"
    assert json.loads(config_path.read_text(encoding="utf-8"))["watches"] == []


def test_setup_status_tolerates_malformed_watch_config(
    config_home, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "watch.json"
    config_path.parent.mkdir(parents=True)
    # A non-integer version makes load_config raise ValueError, not
    # LTValidationError; status must stay a read-only inventory regardless.
    config_path.write_text(
        json.dumps({"version": "not-a-number", "outbox": "o", "watches": []}),
        encoding="utf-8",
    )

    lt_cli.main(["setup", "status", "--target", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "setup-status"
    assert payload["watch"]["config_present"] is None
    assert payload["watch"]["detail"]


def test_watch_scan_and_sync_accept_fail_silent(config_home, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # No watch config exists: without --fail-silent these raise, with it they
    # return silently so hook/scheduler invocations can never block.
    lt_cli.main(["watch", "scan", "--fail-silent"])
    lt_cli.main(["watch", "sync", "--fail-silent"])
