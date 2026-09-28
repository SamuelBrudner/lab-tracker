from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lab_tracker_client import cli as lt_cli
from lab_tracker_client.hpc import (
    epilog_finish,
    find_submit_manifest,
    finish_event,
    init_config,
    run_submit_command,
    slurm_exit_code,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EPILOG_SCRIPT = _REPO_ROOT / "scripts" / "slurm-task-epilog.sh"
_SLURM_ENV = (
    "SLURM_JOB_ID",
    "SLURM_ARRAY_JOB_ID",
    "SLURM_ARRAY_TASK_ID",
    "SLURM_SUBMIT_DIR",
    "SLURM_JOB_WORK_DIR",
    "SLURM_CLUSTER_NAME",
    "SLURM_JOB_EXIT_CODE",
    "SLURM_JOB_EXIT_CODE2",
    "SLURM_SCRIPT_CONTEXT",
    "SLURM_STEP_ID",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "LAB_TRACKER_HPC_CONFIG",
        "LAB_TRACKER_HPC_OUTBOX",
        "LAB_TRACKER_HPC_RUN_ID",
        "LAB_TRACKER_HPC_EPILOG_ENABLED",
        *_SLURM_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _submit(tmp_path: Path, *, job_output: str = "Submitted batch job 4242", **kwargs):
    checkout = tmp_path / "analysis"
    checkout.mkdir()
    config = init_config(
        project_id="project-1",
        cluster="bouchet",
        config_path=checkout / ".lab-tracker" / "hpc.json",
    )
    submit_dir = checkout / "jobs"
    submit_dir.mkdir()
    result = run_submit_command(
        config,
        [sys.executable, "-c", f"print({job_output!r})"],
        cwd=submit_dir,
        **kwargs,
    )
    return config, submit_dir, result


def _enter_job(monkeypatch, submit_dir: Path, job_id: str = "4242", **extra: str) -> None:
    """Simulate a job started with --export=NONE: only Slurm's own variables."""

    monkeypatch.setenv("SLURM_JOB_ID", job_id)
    monkeypatch.setenv("SLURM_SUBMIT_DIR", str(submit_dir))
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def _finish_events(outbox: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(outbox.glob("*.finish.*.json"))]


def test_submit_writes_the_run_manifest_into_the_submit_directory(tmp_path) -> None:
    config, submit_dir, result = _submit(tmp_path, question_id="question-1", tags=["pilot"])

    manifest_path = submit_dir / ".lab-tracker" / "hpc-runs" / "job-4242.json"
    assert result["run_manifest"] == str(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    assert manifest["run_id"] == result["run_id"]
    assert manifest["job_id"] == "4242"
    assert manifest["config"] == str(config.config_path)
    assert manifest["outbox"] == str(config.outbox_path())
    assert manifest["question_id"] == "question-1"
    assert manifest["tags"] == ["pilot"]
    assert manifest["submit_dir"] == str(submit_dir)
    assert "project_id" not in manifest  # the config's project, not an override


def test_failed_submission_writes_no_manifest(tmp_path) -> None:
    checkout = tmp_path / "analysis"
    checkout.mkdir()
    config = init_config(
        project_id="p", cluster="c", config_path=checkout / ".lab-tracker" / "hpc.json"
    )

    result = run_submit_command(
        config, [sys.executable, "-c", "import sys; sys.exit(1)"], cwd=checkout
    )

    assert "run_manifest" not in result
    assert not (checkout / ".lab-tracker" / "hpc-runs").exists()


def test_export_none_job_finds_its_run_and_config_through_the_manifest(
    tmp_path, monkeypatch, capsys
) -> None:
    config, submit_dir, result = _submit(tmp_path)
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _enter_job(monkeypatch, submit_dir)

    lt_cli.main(["hpc", "begin"])
    begin = json.loads(capsys.readouterr().out)
    lt_cli.main(["hpc", "finish", "--exit-code", "0"])
    finish = json.loads(capsys.readouterr().out)

    assert begin["run_id"] == finish["run_id"] == result["run_id"]
    assert begin["outbox"] == str(config.outbox_path())
    [event] = _finish_events(config.outbox_path())
    assert event["scheduler"]["job_id"] == "4242"
    assert event["scheduler"]["state"] == "completed"


def test_epilog_finishes_the_run_like_hpc_finish(tmp_path, monkeypatch, capsys) -> None:
    config, submit_dir, result = _submit(tmp_path, question_id="question-1", tags=["pilot"])
    (submit_dir / "slurm-4242.out").write_text("epoch 1\nloss 0.1\ndone\n", encoding="utf-8")
    _enter_job(monkeypatch, submit_dir, SLURM_SCRIPT_CONTEXT="epilog_task")

    lt_cli.main(["hpc", "epilog"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["command"] == "hpc-epilog"
    assert payload["action"] == "finished"
    assert payload["run_id"] == result["run_id"]
    [event] = _finish_events(config.outbox_path())
    assert event["run_id"] == result["run_id"]
    assert event["question_id"] == "question-1"
    assert event["tags"] == ["pilot"]
    scheduler = event["scheduler"]
    assert scheduler["job_id"] == "4242"
    assert scheduler["state"] == "ended"
    assert "exit_code" not in scheduler
    assert scheduler["finish_source"] == "epilog"
    assert scheduler["epilog_context"] == "epilog_task"
    assert "loss 0.1" in event["log_excerpt"]
    assert event["cwd"] == str(submit_dir)


def test_epilog_is_idempotent_when_the_job_already_finished_itself(tmp_path, monkeypatch) -> None:
    config, submit_dir, result = _submit(tmp_path)
    _enter_job(monkeypatch, submit_dir)
    monkeypatch.setenv("LAB_TRACKER_HPC_RUN_ID", result["run_id"])
    finish_event(config, exit_code=0)
    monkeypatch.delenv("LAB_TRACKER_HPC_RUN_ID")

    first = epilog_finish()
    second = epilog_finish()

    assert first["action"] == second["action"] == "already_finished"
    [event] = _finish_events(config.outbox_path())
    assert event["scheduler"]["exit_code"] == 0


def test_repeated_epilogs_write_one_finish_event(tmp_path, monkeypatch) -> None:
    config, submit_dir, _result = _submit(tmp_path)
    _enter_job(monkeypatch, submit_dir)

    assert epilog_finish()["action"] == "finished"
    assert epilog_finish()["action"] == "already_finished"
    assert len(_finish_events(config.outbox_path())) == 1


def test_epilog_records_array_tasks_separately(tmp_path, monkeypatch) -> None:
    config, submit_dir, result = _submit(tmp_path)
    for task, job in (("3", "4245"), ("4", "4246"), ("3", "4245")):
        _enter_job(
            monkeypatch, submit_dir, job, SLURM_ARRAY_JOB_ID="4242", SLURM_ARRAY_TASK_ID=task
        )
        epilog_finish()

    events = _finish_events(config.outbox_path())
    assert sorted(event["scheduler"]["array_task_id"] for event in events) == ["3", "4"]
    assert {event["run_id"] for event in events} == {result["run_id"]}


def test_epilog_reads_the_exit_code_privileged_epilogs_provide(tmp_path, monkeypatch) -> None:
    config, submit_dir, _result = _submit(tmp_path)
    _enter_job(monkeypatch, submit_dir, SLURM_JOB_EXIT_CODE2="3:0")

    payload = epilog_finish()

    assert payload["exit_code"] == 3
    [event] = _finish_events(config.outbox_path())
    assert event["scheduler"]["state"] == "failed"
    assert slurm_exit_code({"SLURM_JOB_EXIT_CODE2": "0:9"}) == 137
    assert slurm_exit_code({"SLURM_JOB_EXIT_CODE": str(2 << 8)}) == 2
    assert slurm_exit_code({"SLURM_JOB_EXIT_CODE": "15"}) == 143
    assert slurm_exit_code({}) is None


def test_epilog_is_a_quiet_noop_without_a_job_manifest_or_when_disabled(
    tmp_path, monkeypatch
) -> None:
    assert epilog_finish()["action"] == "not_in_job"
    _enter_job(monkeypatch, tmp_path, "999")
    assert epilog_finish()["action"] == "no_run"
    assert find_submit_manifest() is None
    config, submit_dir, _result = _submit(tmp_path)
    _enter_job(monkeypatch, submit_dir)
    monkeypatch.setenv("LAB_TRACKER_HPC_EPILOG_ENABLED", "0")
    assert epilog_finish()["action"] == "disabled"
    assert _finish_events(config.outbox_path()) == []


def test_epilog_fail_silent_swallows_a_broken_config(tmp_path, monkeypatch, capsys) -> None:
    config, submit_dir, _result = _submit(tmp_path)
    assert config.config_path is not None
    config.config_path.write_text("{not json", encoding="utf-8")
    _enter_job(monkeypatch, submit_dir)

    with pytest.raises(SystemExit) as raised:
        lt_cli.main(["hpc", "epilog"])
    assert "lt hpc epilog:" in str(raised.value.code)
    capsys.readouterr()

    lt_cli.main(["hpc", "epilog", "--fail-silent"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def _run_epilog_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SLURM_", "LAB_TRACKER_HPC_", "LAB_TRACKER_LT"))
    }
    return subprocess.run(
        ["sh", str(_EPILOG_SCRIPT)],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_task_epilog_script_runs_lt_hpc_epilog_for_the_batch_step_only(tmp_path) -> None:
    config, submit_dir, result = _submit(tmp_path)
    recorder = tmp_path / "fake-lt"
    calls = tmp_path / "calls.txt"
    recorder.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nexit 3\n', encoding="utf-8")
    recorder.chmod(0o755)
    job = {
        "SLURM_JOB_ID": "4242",
        "SLURM_SUBMIT_DIR": str(submit_dir),
        "LAB_TRACKER_LT": str(recorder),
    }

    assert _run_epilog_script({**job, "SLURM_STEP_ID": "0"}).returncode == 0
    assert not calls.exists()
    assert _run_epilog_script({**job, "SLURM_JOB_ID": "777"}).returncode == 0
    assert not calls.exists()
    assert _run_epilog_script({**job, "LAB_TRACKER_HPC_EPILOG_ENABLED": "0"}).returncode == 0
    assert not calls.exists()

    completed = _run_epilog_script(job)

    assert completed.returncode == 0
    assert calls.read_text().strip() == "hpc epilog --fail-silent"
    assert completed.stdout == ""


def test_task_epilog_script_end_to_end_with_the_recorded_lt(tmp_path) -> None:
    config, submit_dir, result = _submit(tmp_path)
    manifest = json.loads((submit_dir / ".lab-tracker" / "hpc-runs" / "job-4242.json").read_text())
    if not manifest.get("lt_command"):
        pytest.skip("no lt executable next to this interpreter")

    completed = _run_epilog_script(
        {"SLURM_JOB_ID": "4242", "SLURM_SUBMIT_DIR": str(submit_dir), "SLURM_STEP_ID": "batch"}
    )

    assert completed.returncode == 0, completed.stderr
    [event] = _finish_events(config.outbox_path())
    assert event["run_id"] == result["run_id"]
    assert event["scheduler"]["finish_source"] == "epilog"


def test_finish_log_excerpts_are_credential_scrubbed(tmp_path, monkeypatch) -> None:
    config, submit_dir, result = _submit(tmp_path)
    log = submit_dir / "job.log"
    log.write_text("connecting postgres://lab:hunter2@db/x\npassword=hunter2\nok\n")
    monkeypatch.setenv("LAB_TRACKER_HPC_RUN_ID", result["run_id"])

    event, _path = finish_event(config, exit_code=0, logs=[log])

    assert "hunter2" not in event["log_excerpt"]
    assert "ok" in event["log_excerpt"]
