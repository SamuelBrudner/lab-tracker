"""Nonsecret inventory for the host that supervises deployments."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    container: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    base_url: str
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_graph_model: str | None = Field(default=None, max_length=128)
    backup_root: Path
    backup_max_age_hours: float = Field(default=24, gt=0, le=8760)
    restore_evidence: Path | None = None

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"https", "http"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Use an HTTP(S) base URL without credentials, query or fragment.")
        if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote deployment health requires HTTPS.")
        _ = parts.port
        return value.rstrip("/")


class MaintenanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state_dir: Path
    deployments: list[Deployment] = Field(min_length=1, max_length=20)
    docker_context: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]+$")
    interval_seconds: int = Field(default=3600, ge=60, le=86400)
    probe_timeout_seconds: float = Field(default=15, gt=0, le=60)
    backup_timeout_seconds: float = Field(default=60, gt=0, le=600)
    run_timeout_seconds: float = Field(default=300, ge=10, le=3600)
    check_availability: bool = True
    check_openai_sources: bool = True

    @model_validator(mode="after")
    def unique_deployments(self) -> MaintenanceConfig:
        if len({item.name for item in self.deployments}) != len(self.deployments):
            raise ValueError("Deployment names must be unique.")
        if len({item.container for item in self.deployments}) != len(self.deployments):
            raise ValueError("Each container may appear once in an inventory.")
        return self


def load_config(path: Path) -> MaintenanceConfig:
    if path.stat().st_size > 65536:
        raise ValueError("Maintenance inventory exceeds 64 KiB.")
    config = MaintenanceConfig.model_validate(json.loads(path.read_text()))
    parent = path.resolve().parent
    config.state_dir = (parent / config.state_dir).resolve()
    for deployment in config.deployments:
        deployment.backup_root = (parent / deployment.backup_root).resolve()
        if deployment.restore_evidence is not None:
            deployment.restore_evidence = (parent / deployment.restore_evidence).resolve()
    return config
