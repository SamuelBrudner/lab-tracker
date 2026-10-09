"""Maintenance retains failures, contains probes, and suppresses duplicate proposals."""

import asyncio
import hashlib
import json
import os
import plistlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from lab_tracker.ai_model_audit import audit_ai_models
from lab_tracker.bounded_subprocess import ProcessDeadline, ProcessResult
from lab_tracker.config import Settings
from lab_tracker.maintenance.cli import scheduler_artifact
from lab_tracker.maintenance.config import Deployment, MaintenanceConfig, load_config
from lab_tracker.maintenance.coordinator import run_once, status
from lab_tracker.maintenance.evaluation import EvaluationGates, compare_samples, evaluate_pair
from lab_tracker.maintenance.probes import (
    BACKUP_FILES,
    CHANGELOG_URL,
    DEPRECATIONS_URL,
    DeploymentProbes,
    ProbeError,
    check_backup,
    fetch_bytes,
    parse_deprecations,
)

NOW = datetime(2026, 10, 8, 22, tzinfo=timezone.utc)
REVISION = "a" * 40
RETIREMENTS = """
# Deprecations
| Shutdown date | Model / system | Recommended replacement |
| --- | --- | --- |
| Feb 26, 2027 | ~old-transcribe~ | ~new-transcribe~ |
""".replace("~", chr(96))


def config(tmp_path):
    return MaintenanceConfig(
        state_dir=tmp_path / "state",
        check_availability=False,
        deployments=[
            Deployment(
                name="primary",
                container="app-1",
                base_url="https://example.com",
                expected_revision=REVISION,
                expected_graph_model="gpt-6.1-sol",
                backup_root=tmp_path / "backups",
            )
        ],
    )


class Probes:
    def __init__(self, inventory):
        self.config = inventory
        self.failures = set()
        self.changelog = "Model: gpt-6.1-sol\nNew model: gpt-6.1-sol"
        self.retirements = RETIREMENTS
        self.health_revision = REVISION
        self.models = audit_ai_models(
            Settings(_env_file=None, environment="local"), today=NOW.date()
        ).as_dict()

    def inspect(self, deployment):
        if "container" in self.failures:
            raise ProbeError("private container detail")
        return {"running": True, "health": "healthy", "revision": REVISION, "image": "sha256:abc"}

    def http(self, url, limit=65536):
        if url.endswith("/health"):
            if "health" in self.failures:
                raise ProbeError("private HTTP detail")
            return json.dumps(
                {"status": "ok", "app": {"source_revision": self.health_revision}}
            ).encode()
        if url == DEPRECATIONS_URL + ".md":
            if "retirements" in self.failures:
                raise ProbeError("source unavailable")
            return self.retirements.encode()
        if url == CHANGELOG_URL + ".md":
            return self.changelog.encode()
        raise AssertionError(f"Unexpected URL {url}")

    def audit(self, deployment):
        if "models" in self.failures:
            raise ProbeError("private credential detail")
        return self.models

    def backup(self, deployment, now):
        if "backup" in self.failures:
            raise ProbeError("private backup detail")
        return {
            "checkpoint": "backup",
            "checkpoint_at": NOW.isoformat(),
            "manifest": {},
            "stale": False,
            "restore_verified": True,
        }


def test_incidents_deduplicate_and_recover_durably(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    healthy = run_once(inventory, probes=probes, now=NOW)
    assert not healthy["attention_needed"]
    assert healthy["proposal"] is None
    probes.failures.add("health")
    failed = run_once(inventory, probes=probes, now=NOW, force=True)
    assert failed["changed"] == ["primary:health"]
    packet = Path(failed["proposal"])
    assert packet.exists()
    assert packet.stat().st_mode & 0o777 == 0o600
    assert "private HTTP detail" not in packet.read_text()
    repeated = run_once(inventory, probes=probes, now=NOW + timedelta(hours=1))
    assert repeated["changed"] == []
    assert repeated["proposal"] == failed["proposal"]
    probes.failures.clear()
    recovered = run_once(inventory, probes=probes, now=NOW + timedelta(hours=2))
    assert recovered["resolved"] == ["primary:health"]
    assert not recovered["attention_needed"]
    assert status(inventory)["resolved"] == recovered["resolved"]
    assert len(list((inventory.state_dir / "proposals").glob("*.json"))) == 2


def test_not_due_and_changed_inventory(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    run_once(inventory, probes=probes, now=NOW)
    skipped = run_once(inventory, probes=probes, now=NOW + timedelta(seconds=1))
    assert skipped["status"] == "not_due"
    inventory.deployments[0].expected_revision = "b" * 40
    changed = run_once(inventory, probes=probes, now=NOW + timedelta(seconds=2))
    assert changed["status"] == "checked"
    assert "primary:health_identity" in changed["findings"]
    assert "primary:container_state" in changed["findings"]


def test_retirement_is_actionable_and_source_failure_is_not_recovery(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    probes.retirements = RETIREMENTS.replace("old-transcribe", "gpt-4o-mini-transcribe")
    report = run_once(inventory, probes=probes, now=NOW)
    key = "primary:retirement:LAB_TRACKER_OPENAI_TRANSCRIPTION_MODEL"
    assert report["findings"][key]["evidence"]["shutdown_on"] == "2027-02-26"
    probes.failures.add("retirements")
    unknown = run_once(inventory, probes=probes, now=NOW, force=True)
    assert key in unknown["findings"]
    assert key not in unknown["resolved"]
    assert "operator:openai_retirements" in unknown["findings"]
    probes.failures = {"models"}
    unknown = run_once(inventory, probes=probes, now=NOW, force=True)
    assert key in unknown["findings"] and key not in unknown["resolved"]


def test_source_change_requires_a_later_catalog_review(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    run_once(inventory, probes=probes, now=NOW)
    probes.changelog += "\nNew model: gpt-6.2-sol"
    changed = run_once(inventory, probes=probes, now=NOW, force=True)
    key = "operator:openai_model_releases"
    assert key in changed["findings"]
    repeated = run_once(inventory, probes=probes, now=NOW, force=True)
    assert key in repeated["findings"] and repeated["changed"] == []
    for model in probes.models["models"]:
        if model["active"]:
            model["reviewed_on"] = "2026-10-09"
    reviewed = run_once(inventory, probes=probes, now=NOW, force=True)
    assert key in reviewed["resolved"]


def test_failed_model_probe_retains_known_drift(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    probes.models = audit_ai_models(
        Settings(_env_file=None, environment="local", openai_model="gpt-4o-mini"),
        today=NOW.date(),
    ).as_dict()
    report = run_once(inventory, probes=probes, now=NOW)
    assert "primary:model_pin" in report["findings"]
    probes.failures.add("models")
    unknown = run_once(inventory, probes=probes, now=NOW, force=True)
    assert "primary:model_pin" in unknown["findings"] and unknown["resolved"] == []
    packet = json.loads(Path(report["proposal"]).with_suffix(".json").read_text())
    change = packet["proposed_runtime_changes"][0]
    assert change["before"] == "gpt-4o-mini" and change["after"] == "gpt-6.1-sol"
    assert not change["apply_automatically"] and not packet["rollout_ready"]


def test_overlapping_runs_skip_without_duplicate_proposals(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    started, finish = Event(), Event()
    original = probes.inspect

    def block(deployment):
        started.set()
        assert finish.wait(5)
        return original(deployment)

    probes.inspect = block
    worker = Thread(target=lambda: run_once(inventory, probes=probes, now=NOW))
    worker.start()
    try:
        assert started.wait(5)
        assert run_once(inventory, probes=Probes(inventory), now=NOW)["status"] == "busy"
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()
    assert status(inventory)["status"] == "checked"


def make_backup(directory):
    directory.mkdir(parents=True)
    hashes = {}
    for name in BACKUP_FILES:
        path = directory / name
        path.write_bytes(f"synthetic {name}".encode())
        os.utime(path, (NOW.timestamp(), NOW.timestamp()))
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(hashes))
    os.utime(manifest, (NOW.timestamp(), NOW.timestamp()))
    return hashes


def test_backup_integrity_age_and_matching_restore_proof(tmp_path):
    deployment = config(tmp_path).deployments[0]
    hashes = make_backup(deployment.backup_root / "checkpoint")
    proof = tmp_path / "restore.json"
    proof.write_text(
        json.dumps(
            {
                "all_restored_rows_preserved": True,
                "backup_manifest": hashes,
                "scratch_resources_cleaned": True,
            }
        )
    )
    deployment.restore_evidence = proof
    result = check_backup(deployment, NOW, ProcessDeadline.after(10))
    assert result["restore_verified"] and not result["stale"]
    assert check_backup(deployment, NOW + timedelta(days=2), ProcessDeadline.after(10))["stale"]
    (deployment.backup_root / "checkpoint" / "postgres.dump").write_bytes(b"corrupt")
    with pytest.raises(ProbeError):
        check_backup(deployment, NOW, ProcessDeadline.after(10))


def test_new_corrupt_backup_cannot_fall_back_or_partial_checkpoint_count(tmp_path):
    deployment = config(tmp_path).deployments[0]
    make_backup(deployment.backup_root / "old")
    new = deployment.backup_root / "new"
    make_backup(new)
    os.utime(new / "manifest.json", (NOW.timestamp() + 1, NOW.timestamp() + 1))
    (new / "app-data.tar.gz").unlink()
    with pytest.raises(ProbeError):
        check_backup(deployment, NOW, ProcessDeadline.after(10))
    new.rename(deployment.backup_root / "new.partial")
    assert check_backup(deployment, NOW, ProcessDeadline.after(10))["checkpoint"].endswith("old")


def test_touched_manifest_does_not_make_old_backup_fresh(tmp_path):
    deployment = config(tmp_path).deployments[0]
    make_backup(deployment.backup_root)
    os.utime(deployment.backup_root / "manifest.json", (NOW.timestamp() + 172800,) * 2)
    assert check_backup(deployment, NOW + timedelta(days=2), ProcessDeadline.after(10))["stale"]


def test_deprecation_format_changes_fail_closed():
    with pytest.raises(ProbeError):
        parse_deprecations("<html>a new layout</html>")
    assert parse_deprecations(RETIREMENTS)["old-transcribe"]["shutdown_on"] == "2027-02-26"


def test_http_total_deadline_and_response_limit(monkeypatch):
    original = httpx.AsyncClient

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(50):
                await asyncio.sleep(0.01)
                yield b"x"

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            **kwargs,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Trickle())),
        ),
    )
    with pytest.raises(ProbeError):
        fetch_bytes("https://example.com", 0.03)
    with pytest.raises(ProbeError):
        fetch_bytes("https://example.com", 1, limit=1)


def test_docker_arguments_are_bounded_read_only_and_errors_redacted(tmp_path):
    inventory = config(tmp_path)
    inventory.docker_context = "colima"

    class Executor:
        def run(self, command, **kwargs):
            assert command[:3] == ["docker", "--context", "colima"]
            assert kwargs["stdout_limit_bytes"] == 65536
            assert kwargs["deadline"].remaining() <= inventory.probe_timeout_seconds
            assert command[3] in {"inspect", "exec"}
            return ProcessResult(1, b"secret-token", 12, 0)

    with pytest.raises(ProbeError, match="Docker probe failed"):
        DeploymentProbes(inventory, Executor()).audit(inventory.deployments[0])


def test_inventory_validation_and_relative_paths(tmp_path):
    raw = config(tmp_path).model_dump(mode="json")
    raw["state_dir"] = "state"
    raw["deployments"][0]["backup_root"] = "backups"
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(raw))
    loaded = load_config(path)
    assert loaded.state_dir == tmp_path / "state"
    assert loaded.deployments[0].backup_root == tmp_path / "backups"
    raw["deployments"].append(raw["deployments"][0])
    with pytest.raises(ValueError):
        MaintenanceConfig.model_validate(raw)
    with pytest.raises(ValueError):
        Deployment.valid_url("https://username:secret@example.com")


def test_scheduler_quotes_paths_and_launchd_preserves_arguments(tmp_path):
    path = tmp_path / "a directory" / "inventory.json"
    assert "'" in scheduler_artifact(path, Path(sys.executable), tmp_path, 3600, "cron")
    plist = plistlib.loads(
        scheduler_artifact(path, Path(sys.executable), tmp_path, 3600, "launchd").encode()
    )
    assert plist["StartInterval"] == 3600
    assert str(path.resolve()) in plist["ProgramArguments"]
    for bad in ("evil\ncommand", "percent%path"):
        with pytest.raises(ValueError):
            scheduler_artifact(tmp_path / bad, Path(sys.executable), tmp_path, 3600, "cron")


def sample(**changes):
    return {
        "link_precision": 1.0,
        "link_recall": 1.0,
        "duplicate_create_rate": 0.0,
        "clarification_recall": 1.0,
        "ambiguity_link_rate": 0.0,
        "proposal_precision": 1.0,
        "proposal_recall": 1.0,
        "latency_seconds": 1.0,
        "operation_count": 14,
        "prompt_version": "v1",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"link_precision": 0.7},
        {"link_recall": 0.9},
        {"duplicate_create_rate": 0.2},
        {"latency_seconds": 3},
        {"prompt_version": "v2"},
        {"clarification_recall": 0.5},
        {"ambiguity_link_rate": 0.1},
        {"proposal_precision": 0.8},
        {"proposal_recall": 0.5},
    ],
)
def test_candidate_quality_and_latency_gate(changes):
    result = compare_samples([sample()], [sample(**changes)], EvaluationGates())
    assert not result["passed"] and result["failures"]
    assert result["approval_required"] and not result["cost_verified"]


@pytest.mark.parametrize(
    "changes",
    [
        {"link_precision": float("nan")},
        {"latency_seconds": float("inf")},
        {"operation_count": 0},
        {"provider_operation_count": 0},
        {"link_recall": 2},
    ],
)
def test_invalid_or_empty_evaluations_cannot_pass(changes):
    with pytest.raises(ValueError):
        compare_samples([sample()], [sample(**changes)], EvaluationGates())


def test_scripted_pair_is_isolated_and_records_nonempty_operations(tmp_path):
    production = tmp_path / "production.sqlite"
    production.write_bytes(b"must remain unchanged")
    result = evaluate_pair(
        Settings(_env_file=None, environment="local", database_url=f"sqlite:///{production}"),
        "old-model",
        "candidate-model",
        repeat=2,
        scripted=True,
        gates=EvaluationGates(max_latency_ratio=100),
    )
    assert result["passed"] and result["scripted"]
    assert all(run["operation_count"] > 0 for run in result["candidate_runs"])
    assert production.read_bytes() == b"must remain unchanged"


def test_an_empty_provider_patch_cannot_be_hidden_by_deterministic_day_logs(monkeypatch):
    from lab_tracker.golden_day import ScriptedGoldenDayDraftClient
    from lab_tracker.maintenance import evaluation

    client = ScriptedGoldenDayDraftClient(
        {"summary": "Empty", "operations": [], "uncertain_fields": [], "clarification_requests": []}
    )
    monkeypatch.setattr(evaluation, "make_graph_draft_client", lambda _settings: client)
    with pytest.raises(evaluation.DraftNotReady) as caught:
        evaluation.run_sample(Settings(_env_file=None), "candidate")
    assert caught.value.details["provider_attempts"] == 1
    assert len(client.calls) == 1 and client.closed


def test_model_failures_are_retained_without_skipping_other_trials(monkeypatch):
    from lab_tracker.maintenance import evaluation

    calls = []

    def attempt(_settings, model, **kwargs):
        calls.append(model)
        if model == "baseline":
            raise evaluation.DraftNotReady({"provider_attempts": 1})
        return sample(completed=True)

    monkeypatch.setattr(evaluation, "run_sample", attempt)
    gates = EvaluationGates(min_recall=0.9)
    report = evaluate_pair(Settings(_env_file=None), "baseline", "candidate", repeat=3, gates=gates)
    assert calls == ["baseline", "candidate"] * 3
    assert not report["completed"] and not report["passed"]
    assert len(report["failures"]) == 3
    assert len(report["candidate_runs"]) == 3
    assert all(not item["completed"] for item in report["baseline_runs"])
    assert report["gates"]["min_recall"] == 0.9
    assert report["prompt_version"] == evaluation.BATCH_PROMPT_VERSION


def test_candidate_controls_can_differ_without_changing_the_baseline(monkeypatch):
    from lab_tracker.maintenance import evaluation

    settings = Settings(_env_file=None, openai_reasoning_effort="medium")
    controls = []

    def attempt(configuration, model, **kwargs):
        controls.append(configuration.openai_reasoning_effort)
        return sample(completed=True)

    monkeypatch.setattr(evaluation, "run_sample", attempt)
    report = evaluate_pair(
        settings,
        "gpt-6.1-sol",
        "gpt-6.1-sol",
        repeat=2,
        candidate_settings=settings.model_copy(update={"openai_reasoning_effort": "low"}),
    )
    assert controls == ["medium", "low"] * 2
    assert report["reasoning_effort"] == "medium"
    assert report["candidate_reasoning_effort"] == "low"


def test_validation_diagnostics_redact_settings_credentials(monkeypatch):
    from types import SimpleNamespace

    from lab_tracker.api import LabTrackerAPI
    from lab_tracker.golden_day import ScriptedGoldenDayDraftClient
    from lab_tracker.maintenance import evaluation
    from lab_tracker.models import GraphChangeSetStatus

    secret = "private-provider-credential-123456"
    monkeypatch.setattr(
        evaluation, "make_graph_draft_client", lambda _settings: ScriptedGoldenDayDraftClient({})
    )
    monkeypatch.setattr(
        LabTrackerAPI,
        "create_batch_graph_draft",
        lambda *args, **kwargs: SimpleNamespace(
            status=GraphChangeSetStatus.FAILED,
            error_metadata={
                "category": "validation_error",
                "message": "Unsupported field: " + secret,
            },
        ),
    )
    with pytest.raises(evaluation.DraftNotReady) as caught:
        evaluation.run_sample(Settings(_env_file=None, openai_api_key=secret), "candidate")
    assert secret not in json.dumps(caught.value.details)
    assert "[REDACTED]" in caught.value.details["validation_detail"]


def test_usage_wrapper_preserves_the_provider_generation_lease():
    from lab_tracker.graph_drafting import OpenAIGraphDraftClient
    from lab_tracker.maintenance.evaluation import EvaluationBatchClient
    from lab_tracker.services.graph_draft_generation import provider_generation_lease_seconds

    client = OpenAIGraphDraftClient(api_key="test", model="candidate", timeout_seconds=300)
    wrapped = EvaluationBatchClient(client)
    try:
        assert provider_generation_lease_seconds(wrapped) == provider_generation_lease_seconds(
            client
        )
        assert wrapped.timeout_seconds == 300
    finally:
        wrapped.close()


def test_usage_capture_selects_numeric_token_counts_without_provider_secrets():
    from lab_tracker.golden_day import ScriptedGoldenDayDraftClient
    from lab_tracker.maintenance.evaluation import EvaluationBatchClient

    client = EvaluationBatchClient(ScriptedGoldenDayDraftClient({}))
    client.record_usage(
        httpx.Response(
            200,
            json={
                "api_key": "private credential",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 40},
                    "output_tokens_details": {"reasoning_tokens": 10},
                    "secret": "private credential",
                },
            },
        )
    )
    assert client.usage == [
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_tokens": 40,
            "reasoning_tokens": 10,
        }
    ]
    assert "private" not in json.dumps(client.usage)
    client.record_usage(httpx.Response(429, json={"error": "private credential"}))
    assert client.response_statuses == [200, 429]
    assert len(client.usage) == 1


def test_read_only_status_does_not_create_a_database(tmp_path):
    inventory = config(tmp_path)
    assert status(inventory)["status"] == "never_run"
    assert not inventory.state_dir.exists()
    run_once(inventory, probes=Probes(inventory), now=NOW)
    connection = sqlite3.connect(inventory.state_dir / "maintenance.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    finally:
        connection.close()


def test_evaluation_evidence_updates_a_packet_once_and_not_due_stays_quiet(tmp_path):
    inventory = config(tmp_path)
    probes = Probes(inventory)
    run_once(inventory, probes=probes, now=NOW)
    directory = inventory.state_dir / "evaluations"
    directory.mkdir()
    result = {
        "requested_baseline": "old",
        "requested_candidate": "new",
        "passed": True,
        "usable_upgrade_evidence": False,
        "scripted": True,
        "cost_verified": False,
    }
    (directory / "result.json").write_text(json.dumps(result))
    updated = run_once(inventory, probes=probes, now=NOW, force=True)
    assert len(updated["new_evaluations"]) == 1
    packet = json.loads(Path(updated["proposal"]).with_suffix(".json").read_text())
    assert packet["status"] == "needs_review"
    assert packet["evaluations"][0]["usable_upgrade_evidence"] is False
    assert run_once(inventory, probes=probes, now=NOW)["new_evaluations"] == []
    assert run_once(inventory, probes=probes, now=NOW, force=True)["new_evaluations"] == []


@pytest.mark.parametrize("rubric", ["current", "old", "missing", "scripted", "post_hoc"])
def test_only_the_current_live_rubric_is_upgrade_evidence(tmp_path, rubric):
    from lab_tracker.golden_day import GOLDEN_DAY_FIXTURE_VERSION, GOLDEN_DAY_SCORER_VERSION
    from lab_tracker.maintenance.coordinator import _evaluations

    inventory = config(tmp_path)
    directory = inventory.state_dir / "evaluations"
    directory.mkdir(parents=True)
    report = {
        "requested_baseline": "old",
        "requested_candidate": "new",
        "passed": True,
        "usable_upgrade_evidence": True,
        "scripted": rubric == "scripted",
        "post_hoc_rescore": rubric == "post_hoc",
    }
    if rubric != "missing":
        report["fixture_version"] = (
            GOLDEN_DAY_FIXTURE_VERSION if rubric != "old" else "golden-day-v1"
        )
        report["scorer_version"] = GOLDEN_DAY_SCORER_VERSION
    artifact = directory / "historical.json"
    original = json.dumps(report)
    artifact.write_text(original)
    [entry] = _evaluations(inventory)
    assert entry["usable_upgrade_evidence"] is (rubric == "current")
    assert artifact.read_text() == original


def test_missing_rubric_metrics_cannot_pass():
    incomplete = sample()
    del incomplete["clarification_recall"]
    with pytest.raises(ValueError, match="finite numbers"):
        compare_samples([sample()], [incomplete], EvaluationGates())


def test_failed_evaluation_worker_outputs_only_safe_failure_metadata(monkeypatch, capsys):
    from lab_tracker.maintenance import evaluation

    parameters = {
        "scripted": True,
        "provider_env": None,
        "baseline": "old",
        "candidate": "new",
        "repeat": 1,
        "gates": {},
    }
    monkeypatch.setattr(sys, "argv", ["worker", json.dumps(parameters)])

    def fail(*args, **kwargs):
        raise RuntimeError("must not expose a credential or provider response")

    monkeypatch.setattr(evaluation, "evaluate_pair", fail)
    assert evaluation.main() == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["error_type"] == "RuntimeError" and not report["passed"]
    assert "credential" not in output.out and output.err == ""


def test_sample_failure_identifies_phase_without_disclosing_provider_error(monkeypatch):
    from lab_tracker.maintenance import evaluation

    def fail(*args, **kwargs):
        raise ValueError("private provider response")

    monkeypatch.setattr(evaluation, "run_sample", fail)
    with pytest.raises(evaluation.EvaluationAttemptFailure) as caught:
        evaluation.evaluate_pair(Settings(_env_file=None), "old", "new", repeat=3)
    assert caught.value.phase == "baseline" and caught.value.sample_number == 1
    assert caught.value.error_type == "ValueError"
    assert "private" not in str(caught.value)


def test_evaluation_deadline_creates_failed_evidence(tmp_path, monkeypatch, capsys):
    from lab_tracker.bounded_subprocess import BoundedSubprocessExecutor, ProcessDeadlineExceeded
    from lab_tracker.cli import main

    inventory = config(tmp_path)
    path = tmp_path / "inventory.json"
    path.write_text(inventory.model_dump_json())

    def deadline(*args, **kwargs):
        raise ProcessDeadlineExceeded("private process detail")

    monkeypatch.setattr(BoundedSubprocessExecutor, "run", deadline)
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "maintenance",
                "evaluate",
                "--config",
                str(path),
                "--baseline",
                "old",
                "--candidate",
                "new",
                "--scripted",
            ]
        )
    assert caught.value.code == 2
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["error_type"] == "ProcessDeadlineExceeded"
    assert not report["usable_upgrade_evidence"]
    assert Path(report["report"]).is_file()
    assert "private process" not in output.out + output.err


def test_backup_check_runs_in_a_contained_worker(tmp_path):
    inventory = config(tmp_path)
    deployment = inventory.deployments[0]
    make_backup(deployment.backup_root)
    result = DeploymentProbes(inventory).backup(deployment, NOW)
    assert not result["stale"]
    assert set(result["manifest"]) == set(BACKUP_FILES)
