"""Persist maintenance runs, suppress repeats, and prepare reviewable proposals."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lab_tracker.golden_day import GOLDEN_DAY_FIXTURE_VERSION, GOLDEN_DAY_SCORER_VERSION
from lab_tracker.maintenance.config import MaintenanceConfig
from lab_tracker.maintenance.probes import (
    CHANGELOG_URL,
    DEPRECATIONS_URL,
    DeploymentProbes,
    Finding,
    ProbeError,
    collect_deployment,
    failed_probe,
    model_release_digest,
    parse_deprecations,
)


def write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".maintenance-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _database(config: MaintenanceConfig) -> sqlite3.Connection:
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = config.state_dir / "maintenance.sqlite3"
    if path.is_symlink():
        raise ValueError("Maintenance state must not be a symlink.")
    if not path.exists():
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        except FileExistsError:
            # Another first-run scheduler may have created it after the existence check.
            if path.is_symlink():
                raise ValueError("Maintenance state must not be a symlink.") from None
    connection = sqlite3.connect(path, timeout=0)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS runs "
            "(id INTEGER PRIMARY KEY, checked_at TEXT, body TEXT NOT NULL)"
        )
        return connection
    except Exception:
        connection.close()
        raise


def status(config: MaintenanceConfig) -> dict[str, Any]:
    path = config.state_dir / "maintenance.sqlite3"
    if not path.exists():
        return {"status": "never_run", "attention_needed": True}
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
    finally:
        connection.close()
    return json.loads(row[0]) if row else {"status": "never_run", "attention_needed": True}


def _sources(
    evidence: dict[str, Any], probes: DeploymentProbes, previous: dict[str, Any], now: datetime
) -> tuple[list[Finding], dict[str, Any]]:
    models = [
        (name, model)
        for name, item in evidence.items()
        for model in item.get("models", {}).get("models", [])
        if model.get("active")
        and model.get("provider") == "openai"
        and model.get("currency") != "custom_endpoint"
    ]
    if not models or not probes.config.check_openai_sources:
        return [], previous
    findings = []
    sources = dict(previous)
    try:
        notices = parse_deprecations(
            probes.http(f"{DEPRECATIONS_URL}.md", 1024 * 1024).decode("utf-8")
        )
        for name, model in models:
            notice = notices.get(model["configured_model"])
            if notice:
                findings.append(
                    Finding(
                        name,
                        f"retirement:{model['setting']}",
                        "error" if notice["shutdown_on"] <= now.date().isoformat() else "review",
                        "An active model has an official retirement deadline.",
                        {
                            "setting": model["setting"],
                            "configured_model": model["configured_model"],
                            **notice,
                        },
                        "Evaluate a supported replacement for this workload "
                        "before its shutdown date.",
                    )
                )
    except (ProbeError, UnicodeError, ValueError):
        findings.append(failed_probe("operator", "openai_retirements"))
    try:
        digest = model_release_digest(
            probes.http(f"{CHANGELOG_URL}.md", 1024 * 1024).decode("utf-8")
        )
        # A later catalog review is the acknowledgement; seeing the same page is not one.
        reviewed_on = min(model["reviewed_on"] for _, model in models)
        old = previous.get("openai_changelog", {})
        pending = bool(old.get("review_needed"))
        if old and reviewed_on > old.get("reviewed_on", reviewed_on):
            pending = False
        if old.get("digest") and digest != old["digest"]:
            pending = True
        sources["openai_changelog"] = {
            "digest": digest,
            "reviewed_on": reviewed_on,
            "review_needed": pending,
        }
        if pending:
            findings.append(
                Finding(
                    "operator",
                    "openai_model_releases",
                    "review",
                    "Official model release information changed since the recorded baseline.",
                    {"source_url": CHANGELOG_URL, "digest": digest, "reviewed_on": reviewed_on},
                    "Review changed model guidance, evaluate explicit candidates, "
                    "then update the catalog.",
                )
            )
    except (ProbeError, UnicodeError, ValueError):
        findings.append(failed_probe("operator", "openai_model_releases_source"))
    return findings, sources


def _packet(report: dict[str, Any], config: MaintenanceConfig) -> str:
    packet_id = hashlib.sha256(
        json.dumps(
            {key: report[key] for key in ("checked_at", "changed", "resolved")}, sort_keys=True
        ).encode()
    ).hexdigest()[:20]
    runtime_changes = []
    for name, instance in report["evidence"].items():
        for model in instance.get("models", {}).get("models", []):
            if model.get("active") and model.get("currency") == "superseded":
                runtime_changes.append(
                    {
                        "instance": name,
                        "setting": model["setting"],
                        "before": model["configured_model"],
                        "after": model["recommended_model"],
                        "workloads": model["workloads"],
                        "source_url": model["source_url"],
                        "quality_evaluation_required": True,
                        "apply_automatically": False,
                    }
                )
    packet = {
        "packet_id": packet_id,
        "status": (
            "needs_review"
            if report["attention_needed"] or report["new_evaluations"]
            else "recovered"
        ),
        "checked_at": report["checked_at"],
        "findings": report["findings"],
        "changed": report["changed"],
        "resolved": report["resolved"],
        "deployments": [
            {
                "name": item.name,
                "expected_revision": item.expected_revision,
                "expected_graph_model": item.expected_graph_model,
            }
            for item in config.deployments
        ],
        "validation": [
            "Compare explicit baseline and candidate models with maintenance evaluate.",
            "Run the affected tests, full backend quality gates, and release CI.",
            "Verify cost and workload compatibility against the official provider source.",
        ],
        "rollout": [
            "Use a clean, pushed and reviewed source revision; retain the previous image.",
            "Create and restore-check a coherent database/file checkpoint for the first instance.",
            "Use the existing release procedure with health, revision and provider checks.",
            "Observe the first instance before releasing remaining instances.",
            "If cutover checks fail, invoke the release procedure's image rollback.",
        ],
        "approval_required": True,
        "proposed_runtime_changes": runtime_changes,
        "rollout_ready": False,
        "evaluations": report["evaluations"],
    }
    directory = config.state_dir / "proposals"
    write_private(directory / f"{packet_id}.json", json.dumps(packet, indent=2) + "\n")
    lines = [
        f"# Maintenance review {packet_id}",
        "",
        f"Checked: {report['checked_at']}",
        "",
        f"Status: {packet['status']}",
        "",
    ]
    for finding in report["findings"].values():
        lines.extend(
            [
                f"## {finding['instance']}: {finding['summary']}",
                "",
                finding["action"],
                "",
                "Evidence:",
                "",
                json.dumps(finding["evidence"], indent=2),
                "",
            ]
        )
    if runtime_changes:
        lines.extend(["## Proposed runtime changes", ""])
        for change in runtime_changes:
            lines.append(
                f"- {change['instance']}: {change['setting']} "
                f"from {change['before']} to {change['after']}."
            )
        lines.extend(["", "Candidate quality, availability, and cost review are required.", ""])
    if report["evaluations"]:
        lines.extend(["## Evaluation evidence", ""])
        for evaluation in report["evaluations"]:
            lines.append(
                f"- {evaluation['requested_baseline']} versus "
                f"{evaluation['requested_candidate']}: passed={evaluation['passed']}; "
                f"usable={evaluation['usable_upgrade_evidence']}. "
                f"Report: {evaluation['path']}"
            )
        lines.append("")
    if report["resolved"]:
        lines.extend(["Resolved findings:", "", *[f"- {key}" for key in report["resolved"]], ""])
    for name in ("validation", "rollout"):
        lines.extend([f"## {name.capitalize()}", "", *[f"- {step}" for step in packet[name]], ""])
    lines.extend(["Deployment and provider configuration changes require approval.", ""])
    write_private(directory / f"{packet_id}.md", "\n".join(lines))
    return str(directory / f"{packet_id}.md")


def _evaluations(config: MaintenanceConfig) -> list[dict[str, Any]]:
    directory = config.state_dir / "evaluations"
    if not directory.is_dir():
        return []
    paths = []
    for index, path in enumerate(directory.iterdir()):
        if index >= 1000:
            raise ValueError("Evaluation archive needs operator retention.")
        if path.suffix == ".json" and path.is_file() and not path.is_symlink():
            paths.append(path)
    results = []
    for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True)[:20]:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Evaluation artifact exceeds its limit.")
        result = json.loads(path.read_text())
        current_rubric = (
            result.get("fixture_version") == GOLDEN_DAY_FIXTURE_VERSION
            and result.get("scorer_version") == GOLDEN_DAY_SCORER_VERSION
        )
        results.append(
            {
                "path": str(path),
                "requested_baseline": result["requested_baseline"],
                "requested_candidate": result["requested_candidate"],
                "passed": result["passed"],
                "usable_upgrade_evidence": bool(
                    result["usable_upgrade_evidence"]
                    and result["passed"]
                    and current_rubric
                    and not result.get("scripted", False)
                    and not result.get("post_hoc_rescore", False)
                ),
                "current_rubric": current_rubric,
                "completed": result.get("completed", True),
                "cost_verified": result.get("cost_verified", False),
            }
        )
    return results


def run_once(
    config: MaintenanceConfig,
    *,
    force: bool = False,
    now: datetime | None = None,
    probes: DeploymentProbes | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Maintenance timestamps require a timezone.")
    connection = None
    try:
        connection = _database(config)
        # Hold the transaction across probes: a second scheduler cannot race the same run.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT body FROM state WHERE id=1").fetchone()
        previous = json.loads(row[0]) if row else {}
        inventory_digest = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
        if previous and not force and previous.get("inventory_digest") == inventory_digest:
            elapsed = (now - datetime.fromisoformat(previous["checked_at"])).total_seconds()
            if 0 <= elapsed < config.interval_seconds:
                return {
                    **previous,
                    "status": "not_due",
                    "changed": [],
                    "resolved": [],
                    "new_evaluations": [],
                }
        probes = probes or DeploymentProbes(config)
        findings = []
        evidence = {}
        for deployment in config.deployments:
            found, observed = collect_deployment(deployment, probes, now)
            findings.extend(found)
            evidence[deployment.name] = observed
        source_findings, sources = _sources(evidence, probes, previous.get("sources", {}), now)
        findings.extend(source_findings)
        current = {item.key: item.as_dict() for item in findings}
        old = previous.get("findings", {})
        # Missing evidence is not recovery. Keep dependent incidents until their check succeeds.
        for key, finding in old.items():
            instance, check = finding["instance"], finding["check"]
            blocked = (
                (
                    check.startswith(("model:", "model_pin", "retirement:"))
                    and f"{instance}:models" in current
                )
                or (check.startswith("retirement:") and "operator:openai_retirements" in current)
                or (check == "container_state" and f"{instance}:container" in current)
                or (check == "health_identity" and f"{instance}:health" in current)
                or (check in {"backup_age", "backup_restore"} and f"{instance}:backup" in current)
                or (
                    check == "openai_model_releases"
                    and "operator:openai_model_releases_source" in current
                )
            )
            if blocked:
                current.setdefault(key, finding)
        changed = sorted(key for key, item in current.items() if old.get(key) != item)
        resolved = sorted(set(old) - set(current))
        evaluations = _evaluations(config)
        known_evaluations = {item["path"] for item in previous.get("evaluations", [])}
        new_evaluations = [
            item["path"] for item in evaluations if item["path"] not in known_evaluations
        ]
        report = {
            "status": "checked",
            "inventory_digest": inventory_digest,
            "checked_at": now.isoformat(),
            "attention_needed": bool(current),
            "findings": current,
            "changed": changed,
            "resolved": resolved,
            "evidence": evidence,
            "sources": sources,
            "proposal": previous.get("proposal"),
            "evaluations": evaluations,
            "new_evaluations": new_evaluations,
        }
        if changed or resolved or new_evaluations:
            report["proposal"] = _packet(report, config)
        body = json.dumps(report, sort_keys=True)
        connection.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (body,))
        connection.execute(
            "INSERT INTO runs (checked_at, body) VALUES (?, ?)", (now.isoformat(), body)
        )
        connection.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY id DESC LIMIT 100)"
        )
        connection.commit()
        return report
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc):
            return {"status": "busy", "changed": [], "resolved": [], "attention_needed": True}
        raise
    finally:
        if connection is not None:
            connection.close()
