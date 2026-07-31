"""Fail-closed deployment probe for release automation.

This is intentionally run inside the built application container. It verifies
the exact runtime configuration plus database, migration, and storage state
without accepting or printing credentials.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from lab_tracker.config import Settings, get_settings


class DeploymentProbeError(RuntimeError):
    """A safe, operator-facing deployment readiness failure."""


def _expected_migration_heads(
    migration_config_path: str | Path | None = None,
) -> set[str]:
    config_path = Path(migration_config_path or Path.cwd() / "alembic.ini").resolve()
    if not config_path.is_file():
        raise DeploymentProbeError("Migration configuration is unavailable.")
    config = Config(str(config_path))
    return set(ScriptDirectory.from_config(config).get_heads())


def _provider_readiness(settings: Settings) -> tuple[str, bool]:
    provider = (settings.graph_draft_provider or "openai").strip().lower()
    provider = {
        "claude": "anthropic",
        "gemini": "google",
        "agentic-openai": "agentic",
        "agentic_openai": "agentic",
    }.get(provider, provider)
    credential = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
        "google": settings.google_api_key,
        "agentic": settings.openai_api_key,
    }.get(provider, "")
    return provider, bool(str(credential or "").strip())


def _probe_storage(name: str, raw_path: str) -> None:
    path = Path(raw_path).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise OSError("not a directory")
        with tempfile.NamedTemporaryFile(
            dir=path,
            prefix=".lab-tracker-deployment-probe-",
        ):
            pass
    except OSError as exc:
        raise DeploymentProbeError(
            f"{name} storage is not writable ({exc.__class__.__name__})."
        ) from None


def probe_deployment(
    settings: Settings,
    *,
    expected_app_name: str,
    expected_environment: str,
    expected_source_revision: str,
    require_provider_credential: bool,
    require_scheduler: bool,
    require_users: bool,
    require_admin: bool,
    migration_config_path: str | Path | None = None,
) -> dict[str, Any]:
    if settings.app_name != expected_app_name:
        raise DeploymentProbeError("Running app name does not match this deployment.")
    if settings.environment != expected_environment:
        raise DeploymentProbeError("Running environment does not match this deployment.")
    if settings.source_revision != expected_source_revision:
        raise DeploymentProbeError("Running source revision does not match the release.")

    provider, credential_configured = _provider_readiness(settings)
    if require_provider_credential and not credential_configured:
        raise DeploymentProbeError("The configured draft provider has no credential.")
    if require_scheduler and not settings.graph_draft_scheduler_enabled:
        raise DeploymentProbeError("The graph-draft scheduler is disabled.")

    engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            database_heads = {
                str(row[0])
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            }
            user_count = int(
                connection.execute(text("SELECT count(*) FROM users")).scalar_one()
            )
            admin_count = int(
                connection.execute(
                    text("SELECT count(*) FROM users WHERE role = 'admin'")
                ).scalar_one()
            )
    except SQLAlchemyError as exc:
        raise DeploymentProbeError(
            f"Database readiness failed ({exc.__class__.__name__})."
        ) from None
    finally:
        engine.dispose()

    expected_heads = _expected_migration_heads(migration_config_path)
    if not expected_heads or database_heads != expected_heads:
        raise DeploymentProbeError("Database migration revision is not at image head.")
    if require_users and user_count < 1:
        raise DeploymentProbeError("The deployment database has no users.")
    if require_admin and admin_count < 1:
        raise DeploymentProbeError("The deployment database has no administrator.")

    _probe_storage("note", settings.note_storage_path)
    _probe_storage("file", settings.file_storage_path)

    return {
        "status": "ready",
        "app": {
            "name": settings.app_name,
            "environment": settings.environment,
            "source_revision": settings.source_revision,
        },
        "database": {
            "migration_heads": sorted(database_heads),
            "users_present": user_count > 0,
            "admins_present": admin_count > 0,
        },
        "storage": {"note": "writable", "file": "writable"},
        "automation": {
            "provider": provider,
            "provider_credential_configured": credential_configured,
            "scheduler_enabled": settings.graph_draft_scheduler_enabled,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-app-name", required=True)
    parser.add_argument("--expected-environment", required=True)
    parser.add_argument("--expected-source-revision", required=True)
    parser.add_argument("--require-provider-credential", action="store_true")
    parser.add_argument("--require-scheduler", action="store_true")
    parser.add_argument("--require-users", action="store_true")
    parser.add_argument("--require-admin", action="store_true")
    parser.add_argument("--alembic-config")
    args = parser.parse_args(argv)
    try:
        payload = probe_deployment(
            get_settings(),
            expected_app_name=args.expected_app_name,
            expected_environment=args.expected_environment,
            expected_source_revision=args.expected_source_revision,
            require_provider_credential=args.require_provider_credential,
            require_scheduler=args.require_scheduler,
            require_users=args.require_users,
            require_admin=args.require_admin,
            migration_config_path=args.alembic_config,
        )
    except DeploymentProbeError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}))
        return 1
    except Exception as exc:  # pragma: no cover - final credential-safe boundary
        print(
            json.dumps(
                {
                    "status": "fail",
                    "error": f"Unexpected probe failure ({exc.__class__.__name__}).",
                }
            )
        )
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
