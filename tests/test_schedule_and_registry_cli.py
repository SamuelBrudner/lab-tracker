"""Tests for OS-scheduler enrollment, the applied-repos registry, doctor --all,
and the schedulable `lt watch run` composition."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lab_tracker.cli import init_consumer_repo
from lab_tracker_client import LTRecord
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.registry import list_repos, record_repo, registry_path
from lab_tracker_client.schedule import (
    install_schedule,
    task_name,
    uninstall_schedule,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(tmp_path / "skills-home"))
    for name in (
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


class _FakeRunner:
    """Captures scheduler invocations; never touches the real scheduler."""

    def __init__(self, crontab_text: str = "") -> None:
        self.calls: list[list[str]] = []
        self.crontab_text = crontab_text

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["crontab", "-l"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.crontab_text, stderr="")
        if argv[:2] == ["crontab", "-"]:
            self.crontab_text = kwargs.get("input", "")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _watch_config(tmp_path: Path) -> Path:
    config = tmp_path / "repo" / ".lab-tracker" / "watch.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps({"version": 1, "outbox": ".lab-tracker/outbox/watch", "watches": []}),
        encoding="utf-8",
    )
    return config


def test_schedule_windows_uses_schtasks_with_deterministic_name(home) -> None:
    config = _watch_config(home)
    runner = _FakeRunner()
    payload = install_schedule(
        config_path=config,
        interval_minutes=10,
        lt_path="C:/venv/Scripts/lt.exe",
        platform="win32",
        runner=runner,
    )
    assert payload["scheduler"] == "schtasks"
    assert payload["task_name"] == task_name(config.resolve())
    argv = runner.calls[0]
    assert argv[:3] == ["schtasks", "/Create", "/F"]
    assert argv[argv.index("/MO") + 1] == "10"
    command = argv[argv.index("/TR") + 1]
    assert "watch run" in command
    assert "--fail-silent" in command
    assert str(config.resolve()) in command

    removal = uninstall_schedule(config_path=config, platform="win32", runner=runner)
    assert removal["action"] == "removed"
    assert runner.calls[-1][:3] == ["schtasks", "/Delete", "/F"]


def test_schedule_cron_upserts_managed_line(home) -> None:
    config = _watch_config(home)
    runner = _FakeRunner(crontab_text="0 1 * * * /usr/bin/backup\n")
    payload = install_schedule(
        config_path=config,
        interval_minutes=5,
        lt_path="/venv/bin/lt",
        platform="linux",
        runner=runner,
    )
    assert payload["scheduler"] == "crontab"
    assert "*/5 * * * *" in payload["cron_line"]
    assert "# LAB-TRACKER-WATCH" in payload["cron_line"]
    assert "/usr/bin/backup" in runner.crontab_text
    assert payload["cron_line"] in runner.crontab_text

    # Re-install replaces the managed line instead of stacking duplicates.
    install_schedule(
        config_path=config,
        interval_minutes=7,
        lt_path="/venv/bin/lt",
        platform="linux",
        runner=runner,
    )
    assert runner.crontab_text.count("# LAB-TRACKER-WATCH") == 1
    assert "*/7 * * * *" in runner.crontab_text

    uninstall_schedule(config_path=config, platform="linux", runner=runner)
    assert "# LAB-TRACKER-WATCH" not in runner.crontab_text
    assert "/usr/bin/backup" in runner.crontab_text


def test_schedule_refuses_to_clobber_unreadable_crontab(home) -> None:
    config = _watch_config(home)

    class _Failing(_FakeRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append(list(argv))
            if argv[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="permission denied"
                )
            return super().__call__(argv, **kwargs)

    runner = _Failing()
    # An AMBIGUOUS crontab read failure must refuse rather than risk
    # replacing the user's real crontab with only our line.
    with pytest.raises(Exception, match="refusing"):
        install_schedule(
            config_path=config, lt_path="/venv/bin/lt", platform="linux", runner=runner
        )
    assert ["crontab", "-"] not in [call[:2] for call in runner.calls]

    # A definite "no crontab for user" is a legitimate empty start.
    class _Empty(_FakeRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append(list(argv))
            if argv[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="no crontab for snb6"
                )
            return super().__call__(argv, **kwargs)

    empty_runner = _Empty()
    payload = install_schedule(
        config_path=config, lt_path="/venv/bin/lt", platform="linux", runner=empty_runner
    )
    assert payload["cron_line"] in empty_runner.crontab_text


def test_schedule_requires_existing_config_and_sane_interval(home) -> None:
    runner = _FakeRunner()
    with pytest.raises(Exception, match="lt watch add"):
        install_schedule(
            config_path=home / "missing" / "watch.json",
            platform="linux",
            runner=runner,
        )
    config = _watch_config(home)
    with pytest.raises(Exception, match="interval"):
        install_schedule(
            config_path=config, interval_minutes=0, platform="linux", runner=runner
        )
    assert runner.calls == []


def test_schedule_cli_requires_consent_and_dry_run_touches_nothing(
    home, monkeypatch, capsys
) -> None:
    config = _watch_config(home)
    monkeypatch.chdir(config.parent.parent)

    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "schedule"])

    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    lt_cli.main(["setup", "schedule", "--dry-run", "--lt-path", "C:/x/lt.exe"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "setup-schedule"
    assert payload["action"] == "would-install"
    assert called == []
    assert list_repos() == []


def test_schedule_rejects_cron_incompatible_intervals(home) -> None:
    config = _watch_config(home)
    runner = _FakeRunner()
    with pytest.raises(Exception, match="59 or less"):
        install_schedule(
            config_path=config, interval_minutes=90, platform="linux", runner=runner
        )
    with pytest.raises(Exception, match="1439"):
        install_schedule(
            config_path=config, interval_minutes=2000, platform="win32", runner=runner
        )
    assert runner.calls == []


def test_schedule_cron_preserves_user_lines_verbatim(home) -> None:
    config = _watch_config(home)
    # Leading blank line and trailing blank line belong to the user.
    runner = _FakeRunner(crontab_text="\n0 1 * * * /usr/bin/backup\n\n")
    install_schedule(
        config_path=config, lt_path="/venv/bin/lt", platform="linux", runner=runner
    )
    lines = runner.crontab_text.splitlines()
    assert lines[0] == ""
    assert lines[1] == "0 1 * * * /usr/bin/backup"
    assert lines[2] == ""
    assert "# LAB-TRACKER-WATCH" in lines[3]


def test_schedule_uninstall_raises_on_denied_delete(home) -> None:
    config = _watch_config(home)

    class _Denied(_FakeRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append(list(argv))
            if argv[:2] == ["schtasks", "/Delete"]:
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="ERROR: Access is denied."
                )
            return super().__call__(argv, **kwargs)

    with pytest.raises(Exception, match="Delete failed"):
        uninstall_schedule(config_path=config, platform="win32", runner=_Denied())

    class _NotFound(_FakeRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append(list(argv))
            if argv[:2] == ["schtasks", "/Delete"]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="ERROR: The system cannot find the file specified.",
                )
            return super().__call__(argv, **kwargs)

    payload = uninstall_schedule(config_path=config, platform="win32", runner=_NotFound())
    assert payload["action"] == "absent"


def test_registry_root_honest_for_custom_config_locations(home) -> None:
    from lab_tracker_client.registry import repo_root_for_config

    canonical = home / "repo" / ".lab-tracker" / "watch.json"
    assert repo_root_for_config(canonical) == (home / "repo").resolve()
    custom = home / "configs" / "watch.json"
    assert repo_root_for_config(custom) == (home / "configs").resolve()


def test_registry_round_trip_and_fail_soft(home) -> None:
    record_repo(home / "repo-a", "watch-add")
    record_repo(home / "repo-a", "hooks-install")
    record_repo(home / "repo-b", "init")

    repos = list_repos()
    assert len(repos) == 2
    entry = next(item for item in repos if item["root"].endswith("repo-a"))
    assert entry["actions"] == ["watch-add", "hooks-install"]

    registry_path().write_text("{broken", encoding="utf-8")
    assert list_repos() == []
    record_repo(home / "repo-c", "init")
    assert [Path(item["root"]).name for item in list_repos()] == ["repo-c"]


def test_init_and_watch_add_record_repos(home, monkeypatch, capsys) -> None:
    repo = home / "consumer"
    init_consumer_repo(repo)
    roots = [item["root"] for item in list_repos()]
    assert str(repo.resolve()) in roots

    monkeypatch.chdir(repo)
    config_path = repo / ".lab-tracker" / "watch.json"
    lt_cli.main(["watch", "add", "results", "--config", str(config_path)])
    capsys.readouterr()
    entry = next(item for item in list_repos() if item["root"] == str(repo.resolve()))
    assert "watch-add" in entry["actions"]

    # Dry-run init must not record.
    before = len(list_repos())
    init_consumer_repo(home / "dry-repo", dry_run=True)
    assert len(list_repos()) == before


def test_doctor_all_sweeps_registry(home, monkeypatch, capsys) -> None:
    clean = home / "clean-repo"
    init_consumer_repo(clean, yes=True)
    drifted = home / "drifted-repo"
    init_consumer_repo(drifted, yes=True)
    claude = drifted / "CLAUDE.md"
    claude.write_text(
        claude.read_text(encoding="utf-8").replace("first-line", "first-line (edited)"),
        encoding="utf-8",
    )
    missing = home / "gone-repo"
    record_repo(missing, "init")

    with pytest.raises(SystemExit):
        lt_cli.main(["doctor", "--all"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "doctor-all"
    by_root = {Path(item["root"]).name: item for item in payload["repos"]}
    assert by_root["clean-repo"]["drifted"] == []
    assert by_root["drifted-repo"]["drifted"] == ["CLAUDE.md"]
    assert "lt update" in by_root["drifted-repo"]["suggestion"]
    assert by_root["gone-repo"]["missing"] is True

    # --fail-silent swallows the drift exit code for hooks/schedulers.
    lt_cli.main(["doctor", "--all", "--fail-silent"])

    # --prune-missing drops the stale entry (explicitly consented via the
    # flag); remaining drift still exits nonzero.
    with pytest.raises(SystemExit):
        lt_cli.main(["doctor", "--all", "--prune-missing"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["pruned"] == [str(missing.resolve())]
    assert all(not item.get("missing") for item in payload["repos"])
    roots = [item["root"] for item in list_repos()]
    assert str(missing.resolve()) not in roots


def test_update_all_refreshes_enrolled_repos_and_agent_skills(home, capsys) -> None:
    first = home / "first-repo"
    second = home / "second-repo"
    init_consumer_repo(first)
    init_consumer_repo(second)

    lt_cli.main(["update", "--all", "--yes", "--install-skills", "--dry-run"])
    preview = json.loads(capsys.readouterr().out)
    assert preview["command"] == "update-all"
    assert preview["dry_run"] is True
    assert len(preview["repos"]) == 2
    assert "BEGIN LAB TRACKER CODE CONVENTIONS" not in (first / "GEMINI.md").read_text(
        encoding="utf-8"
    )

    lt_cli.main(["update", "--all", "--yes", "--install-skills"])
    applied = json.loads(capsys.readouterr().out)
    assert applied["command"] == "update-all"
    for repo in (first, second):
        assert "BEGIN LAB TRACKER CODE CONVENTIONS" in (repo / "CLAUDE.md").read_text(
            encoding="utf-8"
        )
        assert "BEGIN LAB TRACKER AGENTS CODE CONVENTIONS" in (
            repo / "AGENTS.md"
        ).read_text(encoding="utf-8")
        assert "BEGIN LAB TRACKER CODE CONVENTIONS" in (repo / "GEMINI.md").read_text(
            encoding="utf-8"
        )
        assert (repo / ".cursor" / "rules" / "lab-tracker.mdc").exists()

    skills_home = home / "skills-home"
    assert "save_artifact" in (
        skills_home / "lab-tracker" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert (skills_home / "lab-tracker-setup" / "SKILL.md").exists()

    lt_cli.main(["doctor", "--all"])
    doctor = json.loads(capsys.readouterr().out)
    assert all(not repo["drifted"] for repo in doctor["repos"])


def test_watch_run_composes_scan_and_sync(home, monkeypatch, capsys) -> None:
    repo = home / "run-repo"
    inbox = repo / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "note.md").write_text("captured", encoding="utf-8")
    config_path = repo / ".lab-tracker" / "watch.json"
    monkeypatch.chdir(repo)
    lt_cli.main(
        [
            "watch",
            "add",
            str(inbox),
            "--include",
            "*.md",
            "--project",
            "p-1",
            "--config",
            str(config_path),
        ]
    )
    capsys.readouterr()

    class FakeClient:
        def build_evidence_note_index(self, *, project_id):
            return {}

        def import_evidence_file(self, **kwargs):
            from lab_tracker_client.client import EvidenceImportResult

            return EvidenceImportResult(
                action="imported",
                path=str(kwargs.get("file_path", "")),
                source_external_id=str(kwargs.get("source_external_id", "")),
                source_uri=str(kwargs.get("source_uri", "")),
                content_hash="0" * 64,
                metadata={},
                note=LTRecord({"note_id": "n-1"}),
            )

        def close(self):
            pass

    monkeypatch.setattr(
        lt_cli.LabTracker,
        "from_env",
        classmethod(lambda cls: FakeClient()),  # noqa: ARG005
    )
    lt_cli.main(
        [
            "watch",
            "run",
            "--config",
            str(config_path),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "watch-run"
    assert payload["errors"] == []
    assert payload["scan"]["configured"] is True
    assert payload["sync"]["results"]
