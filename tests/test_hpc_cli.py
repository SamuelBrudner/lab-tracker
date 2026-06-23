from __future__ import annotations

import json
import sys

from lab_tracker_client import cli as lt_cli


def _clear_hpc_env(monkeypatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_HPC_CONFIG", raising=False)
    monkeypatch.delenv("LAB_TRACKER_HPC_OUTBOX", raising=False)
    monkeypatch.delenv("LAB_TRACKER_HPC_RUN_ID", raising=False)


def test_hpc_cli_submit_runs_command_and_writes_outbox(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "hpc.json"
    lt_cli.main(
        [
            "hpc",
            "init",
            "--project",
            "project-1",
            "--cluster",
            "bouchet",
            "--config",
            str(config_path),
        ]
    )
    capsys.readouterr()

    lt_cli.main(
        [
            "hpc",
            "submit",
            "--config",
            str(config_path),
            "--",
            sys.executable,
            "-c",
            "print('Submitted batch job 12345')",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    event_files = list((tmp_path / ".lab-tracker" / "outbox" / "hpc").glob("*.json"))
    event = json.loads(event_files[0].read_text(encoding="utf-8"))
    assert payload["command"] == "hpc-submit"
    assert payload["job_id"] == "12345"
    assert payload["exit_code"] == 0
    assert len(event_files) == 1
    assert event["event_type"] == "submit"
    assert event["scheduler"]["job_id"] == "12345"


def test_hpc_cli_begin_finish_status(tmp_path, monkeypatch, capsys) -> None:
    _clear_hpc_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / ".lab-tracker" / "hpc.json"
    lt_cli.main(
        [
            "hpc",
            "init",
            "--project",
            "project-1",
            "--cluster",
            "generic",
            "--config",
            str(config_path),
        ]
    )
    capsys.readouterr()

    lt_cli.main(["hpc", "begin", "--config", str(config_path), "--run", "run-1"])
    begin_payload = json.loads(capsys.readouterr().out)
    lt_cli.main(
        [
            "hpc",
            "finish",
            "--config",
            str(config_path),
            "--run",
            "run-1",
            "--exit-code",
            "0",
            "--metric",
            "accuracy=0.9",
        ]
    )
    finish_payload = json.loads(capsys.readouterr().out)
    lt_cli.main(["hpc", "status", "--config", str(config_path)])
    status_payload = json.loads(capsys.readouterr().out)

    assert begin_payload["event_type"] == "begin"
    assert finish_payload["event_type"] == "finish"
    assert status_payload["pending"] == 2
    assert status_payload["total"] == 2
