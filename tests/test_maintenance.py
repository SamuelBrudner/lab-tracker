"""Maintenance ordering, consent, install identity, and aggregate failure handling."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lab_tracker_client import cli
from lab_tracker_client import maintenance as m

OLD = "a" * 40
NEW = "b" * 40
PROJECT = "78926e8b-f704-417a-9e51-e5b2d2710950"


def _args(*argv):
    parser = argparse.ArgumentParser()
    m.add_parser_options(parser)
    return parser.parse_args(argv)


def _doctor(clean=True, *, importable=True):
    return {
        "command": "doctor",
        "package_version": "0.1.0",
        "targets": [
            {"name": name, "present": clean, "in_sync": clean, "drifted": not clean}
            for name in sorted(m.CONVENTIONS)
        ],
        "lt_mcp": {"importable": importable},
        "server": {"reachable": False},
        "warnings": ["Could not reach server; release comparison skipped."],
    }


def _result(argv, payload, code=0):
    return subprocess.CompletedProcess(argv, code, stdout=json.dumps(payload), stderr="")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tool = tools / "lab-tracker"
    bin_dir = tool / ("Scripts" if m.os.name == "nt" else "bin")
    bin_dir.mkdir(parents=True)
    lt = bin_dir / ("lt.exe" if m.os.name == "nt" else "lt")
    lt.touch()
    (bin_dir / ("python.exe" if m.os.name == "nt" else "python3")).touch()
    (tool / "uv-receipt.toml").touch()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "lt_ids.json").write_text(json.dumps({"project_id": PROJECT}))
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(tmp_path / "skills"))
    monkeypatch.setattr(m.shutil, "which", lambda name: str(lt) if name == "lt" else name)
    return tools, lt, repo


class Runner:
    def __init__(self, tools, *, revision=OLD, changed=True):
        self.tools = tools
        self.revision = revision
        self.changed = changed
        self.applied = False
        self.calls = []
        self.fail_install = False
        self.wrong_revision = False
        self.bad_preview = False
        self.verification_fails = False
        self.url = m.UPSTREAM
        self.metadata_extra = {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert kwargs["timeout"] > 0
        assert kwargs["cwd"].is_dir()
        assert "PYTHONPATH" not in kwargs["env"]
        if "-c" in argv:
            return _result(
                argv,
                {
                    "url": self.url,
                    "vcs_info": {"vcs": "git", "commit_id": self.revision},
                    **self.metadata_extra,
                },
            )
        if argv[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(argv, 0, stdout=NEW + "\trefs/heads/main\n")
        if argv[:3] == ["uv", "tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, stdout=str(self.tools) + "\n")
        if argv[:3] == ["uv", "tool", "install"]:
            if not self.wrong_revision:
                self.revision = NEW
            return subprocess.CompletedProcess(argv, int(self.fail_install), stdout="", stderr="")
        if argv[1] == "doctor":
            clean = (self.applied or not self.changed) and not self.verification_fails
            return _result(argv, _doctor(clean), int(not clean))
        if argv[1] == "update":
            if self.bad_preview:
                return subprocess.CompletedProcess(argv, 1, stdout="lpat_secret", stderr="secret")
            payload = {
                "created": [],
                "overwritten": [],
                "warnings": [],
                "offers": [],
                "diffs": {},
            }
            if self.changed and not self.applied:
                payload.update(overwritten=["AGENTS.md"], diffs={"AGENTS.md": "lpat_secret"})
            if "--dry-run" not in argv:
                self.applied = True
            return _result(argv, payload)
        pytest.fail(f"Unexpected invocation: {argv}")


def _use_runner(monkeypatch, machine, **kwargs):
    runner = Runner(machine[0], **kwargs)
    monkeypatch.setattr(m.subprocess, "run", runner)
    return runner


def test_apply_upgrades_verifies_then_uses_fresh_doctor_and_previews_before_writes(
    machine,
    monkeypatch,
):
    runner = _use_runner(monkeypatch, machine)
    _, lt, repo = machine
    monkeypatch.setenv("PYTHONPATH", "/stale/checkout")
    report = m.maintain(_args("--repo", str(repo), "--upgrade-client", "--yes"))
    assert report["ok"] is True
    assert report["client_update"]["status"] == "updated"
    assert report["repos"][0]["status"] == "updated"
    install_index = next(
        i for i, call in enumerate(runner.calls) if call[:3] == ["uv", "tool", "install"]
    )
    assert runner.calls[install_index][5] == f"git+{m.UPSTREAM}@{NEW}"
    assert "-c" in runner.calls[install_index + 1]  # independently verifies PEP 610 after install
    assert runner.calls[install_index + 2] == [str(lt), "doctor", "--target", str(repo)]
    assert runner.calls[install_index + 3][-1] == "--dry-run"
    assert runner.calls[install_index + 4][-1] == "--yes"
    assert runner.calls[install_index + 5][1] == "doctor"
    assert "lpat_secret" not in json.dumps(report)


def test_default_previews_have_no_package_or_repo_writes(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    report = m.maintain(_args("--repo", str(machine[2]), "--upgrade-client"))
    assert report["dry_run"] is True
    assert report["client_update"]["status"] == "would-update"
    assert report["repos"][0]["status"] == "would-update"
    assert report["ok"] is False
    assert not any(call[:3] == ["uv", "tool", "install"] for call in runner.calls)
    assert all("--dry-run" in call for call in runner.calls if call[1] == "update")


def test_matching_commit_skips_install_even_when_version_string_is_unchanged(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine, revision=NEW, changed=False)
    report = m.maintain(_args("--repo", str(machine[2]), "--upgrade-client", "--yes"))
    assert report["ok"] is True
    assert report["client_update"]["status"] == "current"
    assert not any(call[:2] == ["uv", "tool"] for call in runner.calls)
    assert not runner.applied


@pytest.mark.parametrize("failure", ["fail_install", "wrong_revision"])
def test_failed_or_mismatched_upgrade_stops_before_touching_repos(machine, monkeypatch, failure):
    runner = _use_runner(monkeypatch, machine)
    setattr(runner, failure, True)
    report = m.maintain(_args("--repo", str(machine[2]), "--upgrade-client", "--yes"))
    assert report["ok"] is False
    assert report["errors"]
    assert report["repos"] == []
    assert not any(call[1] in {"doctor", "update"} for call in runner.calls)


@pytest.mark.parametrize(
    "extra",
    [
        {"dir_info": {"editable": True}},
        {"vcs_info": {}},
        {"vcs_info": "bad"},
    ],
)
def test_untrusted_or_editable_install_is_not_replaced(machine, monkeypatch, extra):
    runner = _use_runner(monkeypatch, machine)
    runner.metadata_extra = extra
    report = m.maintain(_args("--repo", str(machine[2]), "--upgrade-client", "--yes"))
    assert report["errors"]
    assert not any(call[:2] in (["git", "ls-remote"], ["uv", "tool"]) for call in runner.calls)


def test_other_source_is_refused_without_printing_its_url(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    runner.url = "https://username:secret@other.example/repo"
    report = m.maintain(_args("--repo", str(machine[2]), "--upgrade-client", "--yes"))
    assert report["errors"]
    assert "secret" not in json.dumps(report)


def test_uv_tool_directory_must_match_selected_install(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    runner.tools = machine[0].parent / "other-tools"
    report = m.maintain(_args("--repo", str(machine[2]), "--upgrade-client", "--yes"))
    assert report["errors"]
    assert not any(call[:3] == ["uv", "tool", "install"] for call in runner.calls)


def test_explicit_revision_does_not_query_moving_main(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    report = m.maintain(
        _args(
            "--repo",
            str(machine[2]),
            "--upgrade-client",
            "--revision",
            NEW,
            "--yes",
        )
    )
    assert report["ok"] is True
    assert not any(call[0] == "git" for call in runner.calls)


def test_all_and_explicit_repos_are_deduplicated_and_missing_repo_does_not_stop_sweep(
    machine,
    monkeypatch,
    tmp_path,
):
    runner = _use_runner(monkeypatch, machine, changed=False)
    config = tmp_path / "config"
    config.mkdir()
    missing = tmp_path / "missing"
    repo = machine[2]
    registry = config / "applied-repos.json"
    content = json.dumps({"version": 1, "repos": [{"root": str(missing)}, {"root": str(repo)}]})
    registry.write_text(content)
    report = m.maintain(_args("--all", "--repo", str(missing), "--repo", str(repo), "--yes"))
    assert len(report["repos"]) == 2
    assert report["repos"][0]["status"] == "error"
    assert report["repos"][1]["status"] == "current"
    assert report["ok"] is False
    assert not missing.exists()
    assert registry.read_text() == content  # never prunes entries implicitly
    assert not any(str(missing) in call for call in runner.calls)


@pytest.mark.parametrize("content", ["not-json", "[]", '{"version":1,"repos":[{"root":""}]}'])
def test_invalid_registry_fails_before_any_installs_or_updates(
    machine, monkeypatch, tmp_path, content
):
    runner = _use_runner(monkeypatch, machine)
    config = tmp_path / "config"
    config.mkdir()
    (config / "applied-repos.json").write_text(content)
    report = m.maintain(_args("--all", "--yes", "--upgrade-client"))
    assert report["errors"]
    assert runner.calls == []


def test_one_failed_preview_does_not_apply_that_repo_or_prevent_other_checks(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    runner.bad_preview = True
    report = m.maintain(_args("--repo", str(machine[2]), "--yes"))
    assert report["repos"][0]["status"] == "error"
    assert not runner.applied
    assert "lpat_secret" not in json.dumps(report)


def test_verification_failure_is_not_reported_as_fixed(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    runner.verification_fails = True
    report = m.maintain(_args("--repo", str(machine[2]), "--yes"))
    assert report["repos"][0]["status"] == "verification_failed"
    assert report["ok"] is False


def test_missing_binding_stays_actionable_without_guessing_or_creating_a_project(
    machine, monkeypatch
):
    runner = _use_runner(monkeypatch, machine, changed=False)
    ids = machine[2] / "lt_ids.json"
    ids.write_text('{"project_id":""}')
    report = m.maintain(_args("--repo", str(machine[2]), "--yes"))
    assert report["repos"][0]["binding"] == "needs_project_binding"
    assert report["ok"] is False
    assert ids.read_text() == '{"project_id":""}'
    assert all("bind" not in call and "create" not in call for call in runner.calls)


def test_skills_refresh_is_separately_requested_and_previewed_first(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    report = m.maintain(_args("--repo", str(machine[2]), "--refresh-skills", "--yes"))
    assert report["ok"] is True
    skill_calls = [call for call in runner.calls if "--skills-only" in call]
    assert skill_calls[0][-1] == "--dry-run"
    assert skill_calls[1][-1] == "--skills-only"
    assert "applied" in report["skills"]


def test_real_cli_repairs_a_repo_and_second_run_has_no_writes(tmp_path, monkeypatch):
    console = Path(sys.executable).parent / ("lt.exe" if m.os.name == "nt" else "lt")
    if not console.is_file():
        pytest.skip("The test interpreter has no installed lt console script.")
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("LAB_TRACKER_ACCESS_TOKEN", raising=False)
    repo = tmp_path / "real-repo"
    repo.mkdir()
    ids = repo / "lt_ids.json"
    ids.write_text(json.dumps({"project_id": PROJECT}))
    instructions = "# Project instructions\n\nPreserve the user's own instructions.\n"
    (repo / "AGENTS.md").write_text(instructions)
    args = _args("--repo", str(repo), "--lt-path", str(console), "--yes")
    first = m.maintain(args)
    assert first["ok"] is True, first
    assert first["repos"][0]["status"] == "updated"
    assert (repo / "AGENTS.md").read_text().startswith(instructions)
    assert json.loads(ids.read_text()) == {"project_id": PROJECT}
    snapshots = {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    second = m.maintain(args)
    assert second["ok"] is True, second
    assert second["repos"][0]["status"] == "current"
    assert "applied" not in second["repos"][0]
    assert snapshots == {path: path.read_bytes() for path in repo.rglob("*") if path.is_file()}


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "doctor", "targets": [], "lt_mcp": []},
        {"command": "doctor", "targets": ["broken"]},
        {"command": "doctor", "targets": [], "server": "broken"},
    ],
)
def test_malformed_doctor_reports_are_structured_failures(machine, monkeypatch, payload):
    monkeypatch.setattr(m.subprocess, "run", lambda argv, **k: _result(argv, payload))
    report = m.maintain(_args("--repo", str(machine[2]), "--yes"))
    assert report["repos"][0]["status"] == "error"
    assert report["ok"] is False


def test_cli_returns_json_and_nonzero_for_remaining_work(machine, monkeypatch, capsys):
    _use_runner(monkeypatch, machine)
    with pytest.raises(SystemExit) as error:
        cli.main(["maintain", "--repo", str(machine[2])])
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["command"] == "maintain"
    assert report["repos"][0]["status"] == "would-update"


def test_no_selection_and_bad_revision_return_machine_readable_errors(machine, monkeypatch):
    runner = _use_runner(monkeypatch, machine)
    for argv in [
        [],
        ["--repo", str(machine[2]), "--revision", NEW],
        ["--repo", str(machine[2]), "--revision", "main", "--upgrade-client"],
    ]:
        report = m.maintain(_args(*argv))
        assert report["ok"] is False
        assert report["errors"]
    assert runner.calls == []


def test_subprocess_timeout_is_bounded_and_does_not_print_child_output(machine, monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="secret")

    monkeypatch.setattr(m.subprocess, "run", timeout)
    report = m.maintain(_args("--repo", str(machine[2]), "--yes"))
    assert report["repos"][0]["status"] == "error"
    assert "timed out" in report["repos"][0]["error"]
    assert "secret" not in json.dumps(report)


def test_stdout_without_json_is_a_failure(machine, monkeypatch):
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(
            argv,
            0,
            stdout="secret",
            stderr="",
        ),
    )
    report = m.maintain(_args("--repo", str(machine[2]), "--yes"))
    assert report["repos"][0]["status"] == "error"
    assert "secret" not in json.dumps(report)


def test_init_preview_includes_both_managed_block_additions(tmp_path):
    from lab_tracker.cli import init_consumer_repo

    repo = tmp_path / "new-repo"
    preview = init_consumer_repo(repo, yes=True, dry_run=True)
    diff = preview.diffs[repo / "AGENTS.md"]
    assert "+<!-- BEGIN LAB TRACKER MCP ACTIVATION -->" in diff
    assert "+<!-- BEGIN LAB TRACKER AGENTS CODE CONVENTIONS -->" in diff
    assert not repo.exists()


def test_checkout_bootstrap_needs_no_installed_python_packages(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/maintain_lab_tracker.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--upgrade-client" in result.stdout


def test_preview_file_list_omits_cancelled_intermediate_updates():
    summary = m._update_summary(
        {
            "created": [],
            "overwritten": ["AGENTS.md", "CLAUDE.md", "AGENTS.md"],
            "diffs": {"CLAUDE.md": "a real net change"},
        },
        preview=True,
    )
    assert summary["overwritten"] == ["CLAUDE.md"]
