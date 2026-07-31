from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from lab_tracker import deployment_probe
from lab_tracker.config import Settings
from lab_tracker.db import Base
from lab_tracker.deployment_probe import (
    DeploymentProbeError,
    _expected_migration_heads,
    probe_deployment,
)

SOURCE_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _ready_settings(tmp_path: Path, *, with_user: bool = True) -> Settings:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'probe.db'}"
    engine = create_engine(database_url, future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        for revision in _expected_migration_heads():
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": revision},
            )
        if with_user:
            connection.execute(
                text(
                    "INSERT INTO users "
                    "(user_id, username, password_hash, role, created_at) "
                    "VALUES "
                    "('00000000-0000-4000-8000-000000000123', "
                    "'probe-admin', 'not-a-real-password-hash', 'admin', "
                    "'2026-07-25T00:00:00+00:00')"
                )
            )
    engine.dispose()
    return Settings(
        _env_file=None,
        app_name="marion-lab-tracker",
        environment="production",
        source_revision=SOURCE_REVISION,
        database_url=database_url,
        note_storage_path=str(tmp_path / "notes"),
        file_storage_path=str(tmp_path / "files"),
        auth_secret_key="deployment-probe-test-secret",
        graph_draft_provider="openai",
        graph_draft_scheduler_enabled=True,
        openai_api_key="test-provider-credential",
    )


def _probe(settings: Settings) -> dict:
    return probe_deployment(
        settings,
        expected_app_name="marion-lab-tracker",
        expected_environment="production",
        expected_source_revision=SOURCE_REVISION,
        require_provider_credential=True,
        require_scheduler=True,
        require_users=True,
        require_admin=True,
    )


def test_deployment_probe_checks_identity_database_storage_and_automation(
    tmp_path: Path,
) -> None:
    payload = _probe(_ready_settings(tmp_path))

    assert payload["status"] == "ready"
    assert payload["app"]["source_revision"] == SOURCE_REVISION
    assert payload["database"]["users_present"] is True
    assert payload["database"]["admins_present"] is True
    assert payload["storage"] == {"note": "writable", "file": "writable"}
    assert payload["automation"] == {
        "provider": "openai",
        "provider_credential_configured": True,
        "scheduler_enabled": True,
    }


def test_deployment_probe_rejects_wrong_revision(tmp_path: Path) -> None:
    settings = _ready_settings(tmp_path)

    with pytest.raises(DeploymentProbeError, match="source revision"):
        probe_deployment(
            settings,
            expected_app_name="marion-lab-tracker",
            expected_environment="production",
            expected_source_revision="f" * 40,
            require_provider_credential=True,
            require_scheduler=True,
            require_users=True,
            require_admin=True,
        )


def test_deployment_probe_rejects_empty_user_database(tmp_path: Path) -> None:
    with pytest.raises(DeploymentProbeError, match="no users"):
        _probe(_ready_settings(tmp_path, with_user=False))


def test_expected_heads_do_not_depend_on_installed_package_location(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    fake_module = tmp_path / ".venv/site-packages/lab_tracker/deployment_probe.py"
    monkeypatch.setattr(deployment_probe, "__file__", str(fake_module))
    monkeypatch.chdir(tmp_path)

    heads = _expected_migration_heads(repo_root / "alembic.ini")

    assert heads


def test_deployment_probe_rejects_database_without_admin(tmp_path: Path) -> None:
    settings = _ready_settings(tmp_path)
    engine = create_engine(settings.database_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("UPDATE users SET role = 'editor'"))
    engine.dispose()

    with pytest.raises(DeploymentProbeError, match="no administrator"):
        _probe(settings)
