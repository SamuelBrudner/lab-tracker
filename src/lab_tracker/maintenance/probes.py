"""Bounded read-only probes and stable findings for supervised maintenance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from lab_tracker.bounded_subprocess import (
    BoundedSubprocessExecutor,
    ProcessDeadline,
    ProcessExecutionError,
    ProcessExecutor,
)
from lab_tracker.maintenance.config import Deployment, MaintenanceConfig

DEPRECATIONS_URL = "https://developers.openai.com/api/docs/deprecations"
CHANGELOG_URL = "https://developers.openai.com/api/docs/changelog"
BACKUP_FILES = ("postgres.dump", "app-data.tar.gz")


@dataclass(frozen=True)
class Finding:
    instance: str
    check: str
    severity: str
    summary: str
    evidence: dict[str, Any]
    action: str

    @property
    def key(self) -> str:
        return f"{self.instance}:{self.check}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProbeError(RuntimeError):
    """A probe failed without exposing provider, Docker or filesystem output."""


async def _fetch(url: str, timeout: float, limit: int) -> bytes:
    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != 200:
            raise ProbeError(f"HTTP {response.status_code}")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ProbeError("Response exceeds the probe limit.")
        return bytes(body)


def fetch_bytes(url: str, timeout: float, limit: int = 65536) -> bytes:
    try:
        return asyncio.run(asyncio.wait_for(_fetch(url, timeout, limit), timeout=timeout))
    except (httpx.HTTPError, httpx.InvalidURL, asyncio.TimeoutError, ValueError) as exc:
        raise ProbeError("HTTP probe failed or timed out.") from exc


def parse_deprecations(markdown: str) -> dict[str, dict[str, str]]:
    """Read explicit model table rows, never infer retirement from model names."""
    if "Shutdown date" not in markdown or "deprecat" not in markdown.lower():
        raise ProbeError("Official retirement document has an unrecognized format.")
    notices = {}
    for line in markdown.splitlines():
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        if len(columns) != 3:
            continue
        deadline, models, replacement = columns
        try:
            retired_on = datetime.strptime(deadline, "%b %d, %Y").date()
        except ValueError:
            try:
                retired_on = datetime.strptime(deadline.replace("\u2011", "-"), "%Y-%m-%d").date()
            except ValueError:
                continue
        for identifier in re.findall(r"\x60([^\x60]+)\x60", models):
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", identifier):
                notices[identifier] = {
                    "shutdown_on": retired_on.isoformat(),
                    "replacement": replacement[:256],
                    "source_url": DEPRECATIONS_URL,
                }
    if not notices:
        raise ProbeError("Official retirement document contains no recognized model rows.")
    return notices


def model_release_digest(markdown: str) -> str:
    """Ignore unrelated changelog entries; changes still require a source review."""
    relevant = [
        line.strip()
        for line in markdown.splitlines()
        if re.search(r"\b(?:gpt-|o[134]-|whisper-|tts-)", line, flags=re.IGNORECASE)
    ]
    if not relevant:
        raise ProbeError("Official changelog contains no recognized model entries.")
    return hashlib.sha256("\n".join(relevant).encode()).hexdigest()


class DeploymentProbes:
    def __init__(self, config: MaintenanceConfig, executor: ProcessExecutor | None = None):
        self.config = config
        self.executor = executor or BoundedSubprocessExecutor()
        self.deadline = ProcessDeadline.after(config.run_timeout_seconds)

    def docker_json(self, args: list[str]) -> Any:
        command = ["docker"]
        if self.config.docker_context:
            command.extend(["--context", self.config.docker_context])
        command.extend(args)
        return self.process_json(command, self.config.probe_timeout_seconds)

    def process_json(self, command: list[str], timeout: float) -> Any:
        try:
            self.deadline.check()
            result = self.executor.run(
                command,
                deadline=ProcessDeadline.after(min(timeout, self.deadline.remaining())),
                stdout_limit_bytes=65536,
                stderr_limit_bytes=65536,
            )
            if result.returncode != 0:
                raise ProbeError("Docker probe failed.")
            return json.loads(result.stdout)
        except (ProcessExecutionError, OSError, ValueError) as exc:
            raise ProbeError("Docker probe failed or returned invalid metadata.") from exc

    def http(self, url: str, limit: int = 65536) -> bytes:
        try:
            self.deadline.check()
            return fetch_bytes(
                url, min(self.config.probe_timeout_seconds, self.deadline.remaining()), limit
            )
        except ProcessExecutionError as exc:
            raise ProbeError("Maintenance run deadline exceeded.") from exc

    def inspect(self, deployment: Deployment) -> dict[str, Any]:
        # Select only public identity/state fields. Never inspect environment or health logs.
        template = (
            '{"running":{{.State.Running}},"health":"'
            '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}",'
            '"revision":"{{index .Config.Labels "org.opencontainers.image.revision"}}",'
            '"image":"{{.Image}}"}'
        )
        payload = self.docker_json(["inspect", "--format", template, deployment.container])
        if not isinstance(payload, dict):
            raise ProbeError("Container identity is malformed.")
        return payload

    def audit(self, deployment: Deployment) -> dict[str, Any]:
        # Match the dedicated release probe's runtime secret loading. The entrypoint's
        # exported secret is not inherited by a subsequent docker exec process.
        script = (
            'auth_secret_file="$'
            '{LAB_TRACKER_AUTH_SECRET_KEY_FILE:-/app/data/runtime-env/auth-secret-key}"; '
            'if [ -f "$auth_secret_file" ]; then '
            'LAB_TRACKER_AUTH_SECRET_KEY="$(cat "$auth_secret_file")"; '
            "export LAB_TRACKER_AUTH_SECRET_KEY; fi; "
            "exec lab-tracker models --json"
        )
        if self.config.check_availability:
            script += " --check-availability"
        args = ["exec", deployment.container, "sh", "-eu", "-c", script]
        payload = self.docker_json(args)
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ProbeError("Installed model audit is malformed or unsupported.")
        active = [
            model for model in payload["models"] if isinstance(model, dict) and model.get("active")
        ]
        if not active or any(not isinstance(model, dict) for model in payload["models"]):
            raise ProbeError("Installed model audit contains invalid or absent active workloads.")
        for model in active:
            for key in ("setting", "provider", "configured_model", "reviewed_on", "review_due_on"):
                if not isinstance(model.get(key), str) or not model[key]:
                    raise ProbeError("Installed model audit omits a required field.")
        return payload

    def backup(self, deployment: Deployment, now: datetime) -> dict[str, Any]:
        # Filesystem I/O can block inside a read; contain the entire check in a child.
        payload = self.process_json(
            [
                sys.executable,
                "-m",
                "lab_tracker.maintenance.backup_probe",
                deployment.model_dump_json(),
                now.isoformat(),
                str(self.config.backup_timeout_seconds),
            ],
            self.config.backup_timeout_seconds,
        )
        if not isinstance(payload, dict) or "checkpoint_at" not in payload:
            raise ProbeError("Backup worker returned invalid evidence.")
        return payload


def check_backup(
    deployment: Deployment, now: datetime, deadline: ProcessDeadline
) -> dict[str, Any]:
    """A corrupt newest checkpoint cannot silently fall back to an older backup."""
    root = deployment.backup_root
    if not root.is_dir():
        raise ProbeError("Backup directory is missing.")
    candidates = []
    directories = [root]
    for index, path in enumerate(root.iterdir()):
        if index >= 1000:
            raise ProbeError("Backup directory inventory exceeds 1000 entries.")
        if path.is_dir() and not path.is_symlink() and not path.name.endswith(".partial"):
            directories.append(path)
    for directory in directories:
        for name in ("MANIFEST.sha256", "manifest.json"):
            manifest = directory / name
            if manifest.is_file() and not manifest.is_symlink():
                candidates.append(manifest)
    if not candidates:
        raise ProbeError("No complete backup manifest was found.")
    manifest = max(candidates, key=lambda path: path.stat().st_mtime)
    if manifest.stat().st_size > 65536:
        raise ProbeError("Backup manifest exceeds 64 KiB.")
    if manifest.name == "manifest.json":
        hashes = json.loads(manifest.read_text())
    else:
        hashes = {}
        for line in manifest.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                hashes[parts[1].removeprefix("*")] = parts[0]
    if not isinstance(hashes, dict) or set(hashes) != set(BACKUP_FILES):
        raise ProbeError("Backup manifest must bind both database and file-volume artifacts.")
    modified = []
    for name in BACKUP_FILES:
        expected = hashes[name]
        path = manifest.parent / name
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size == 0
        ):
            raise ProbeError("Backup artifact is missing or its digest is invalid.")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                deadline.check()
                digest.update(chunk)
        after = path.stat()
        if digest.hexdigest() != expected or (
            before.st_size,
            before.st_mtime_ns,
            before.st_ino,
        ) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ProbeError(
                "Backup artifact failed integrity verification or changed during check."
            )
        modified.append(before.st_mtime)
    age_hours = (now.timestamp() - min(modified)) / 3600
    if age_hours < -0.1:
        raise ProbeError("Backup checkpoint is dated in the future.")
    restored = False
    if deployment.restore_evidence:
        proof_path = deployment.restore_evidence
        if proof_path.is_symlink() or proof_path.stat().st_size > 65536:
            raise ProbeError("Restore evidence is invalid.")
        proof = json.loads(proof_path.read_text())
        restored = (
            proof.get("all_restored_rows_preserved") is True
            and proof.get("backup_manifest") == hashes
            and proof.get("scratch_resources_cleaned") is True
        )
    return {
        "checkpoint": str(manifest.parent),
        "manifest": hashes,
        "checkpoint_at": datetime.fromtimestamp(min(modified), timezone.utc).isoformat(),
        "stale": age_hours > deployment.backup_max_age_hours,
        "restore_verified": restored,
    }


def failed_probe(instance: str, check: str) -> Finding:
    return Finding(
        instance,
        check,
        "error",
        f"{check.replace('_', ' ').capitalize()} could not be verified.",
        {},
        "Inspect the operator connection and repeat the check; keep rollout blocked.",
    )


def collect_deployment(
    deployment: Deployment, probes: DeploymentProbes, now: datetime
) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    evidence: dict[str, Any] = {}
    for check, operation in (
        ("container", lambda: probes.inspect(deployment)),
        ("health", lambda: json.loads(probes.http(f"{deployment.base_url}/health"))),
        ("models", lambda: probes.audit(deployment)),
        ("backup", lambda: probes.backup(deployment, now)),
    ):
        try:
            payload = operation()
            if not isinstance(payload, dict) or not payload:
                raise ProbeError("Probe returned empty or invalid metadata.")
            evidence[check] = payload
        except (ProbeError, ProcessExecutionError, OSError, ValueError, TypeError, AttributeError):
            findings.append(failed_probe(deployment.name, check))
    identity = evidence.get("container", {})
    if identity and (
        identity.get("running") is not True
        or identity.get("health") != "healthy"
        or identity.get("revision") != deployment.expected_revision
    ):
        findings.append(
            Finding(
                deployment.name,
                "container_state",
                "error",
                "Container state or revision differs.",
                identity,
                "Confirm the intended image and running revision before a release.",
            )
        )
    health = evidence.get("health", {})
    if health and (
        health.get("status") != "ok"
        or not isinstance(health.get("app"), dict)
        or health["app"].get("source_revision") != deployment.expected_revision
    ):
        findings.append(
            Finding(
                deployment.name,
                "health_identity",
                "error",
                "HTTP health or revision differs.",
                {"expected_revision": deployment.expected_revision},
                "Inspect the HTTP route and running image; repeat health and identity checks.",
            )
        )
    audit = evidence.get("models", {})
    for model in audit.get("models", []):
        if not model.get("active"):
            continue
        if (
            model.get("currency") != "recommended"
            or model.get("review_overdue")
            or model.get("warnings")
            or model.get("availability", {}).get("status")
            in {"credential_missing", "error", "unavailable"}
            or model.get("recommendation_availability", {}).get("status")
            in {"error", "unavailable"}
        ):
            findings.append(
                Finding(
                    deployment.name,
                    f"model:{model['setting']}",
                    "review",
                    "An active AI workload needs maintenance review.",
                    {
                        key: model.get(key)
                        for key in (
                            "setting",
                            "configured_model",
                            "recommended_model",
                            "currency",
                            "review_due_on",
                            "source_url",
                        )
                    },
                    "Review the official source and compare candidate quality "
                    "before changing the pin.",
                )
            )
        if (
            deployment.expected_graph_model
            and "daily_review" in model.get("workloads", [])
            and model["configured_model"] != deployment.expected_graph_model
        ):
            findings.append(
                Finding(
                    deployment.name,
                    "model_pin",
                    "error",
                    "Deployed graph model differs from its pin.",
                    {
                        "expected_model": deployment.expected_graph_model,
                        "configured_model": model["configured_model"],
                    },
                    "Reconcile the approved inventory and actual deployment model configuration.",
                )
            )
    backup = evidence.get("backup", {})
    if backup.get("stale"):
        findings.append(
            Finding(
                deployment.name,
                "backup_age",
                "error",
                "Verified backup is overdue.",
                {"checkpoint_at": backup["checkpoint_at"]},
                "Create a coherent database/file-volume checkpoint and verify its restore.",
            )
        )
    if backup and not backup.get("restore_verified"):
        findings.append(
            Finding(
                deployment.name,
                "backup_restore",
                "review",
                "Backup has no matching restore proof.",
                {"checkpoint": backup["checkpoint"]},
                "Restore this exact checkpoint into scratch resources and record matching digests.",
            )
        )
    return findings, evidence
