"""Tests for the guided-setup verbs: profile, status, init delegation, bind."""

from __future__ import annotations

import json
import sys

import pytest

from lab_tracker_client import LTRecord
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LabTracker, load_connection_profile


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
