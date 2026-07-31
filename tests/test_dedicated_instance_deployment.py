from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT_ROOT = REPO_ROOT / "deployments" / "dedicated-instance"
COMPOSE_PATH = DEPLOYMENT_ROOT / "docker-compose.yml"


def _deployment_text(relative_path: str) -> str:
    return (DEPLOYMENT_ROOT / relative_path).read_text(encoding="utf-8")


def test_dedicated_compose_is_parameterized_and_injects_one_provider() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "name: ${DEDICATED_COMPOSE_PROJECT_NAME:?" in compose
    assert "image: ${DEDICATED_IMAGE:?" in compose
    assert "\n    build:" not in compose
    assert '"127.0.0.1:${DEDICATED_HOST_PORT:?' in compose
    assert "LAB_TRACKER_APP_NAME: ${DEDICATED_APP_NAME:?" in compose
    assert "LAB_TRACKER_PUBLIC_BASE_URL: ${DEDICATED_PUBLIC_BASE_URL:?" in compose
    assert "${DEDICATED_POSTGRES_USER:?" in compose
    assert "${DEDICATED_POSTGRES_DB:?" in compose
    assert "${DEDICATED_POSTGRES_PASSWORD:?" in compose
    assert "LAB_TRACKER_GRAPH_DRAFT_PROVIDER: openai" in compose
    assert compose.count("LAB_TRACKER_OPENAI_API_KEY:") == 1
    assert "LAB_TRACKER_OPENAI_API_KEY: ${DEDICATED_OPENAI_API_KEY:?" in compose
    assert "LAB_TRACKER_OPENAI_MODEL: ${DEDICATED_OPENAI_MODEL:?" in compose
    assert (
        "LAB_TRACKER_OPENAI_REASONING_EFFORT: "
        "${DEDICATED_OPENAI_REASONING_EFFORT:?"
    ) in compose
    assert (
        "LAB_TRACKER_OPENAI_REASONING_MODE: "
        "${DEDICATED_OPENAI_REASONING_MODE:?"
    ) in compose
    assert (
        "LAB_TRACKER_OPENAI_TIMEOUT_SECONDS: "
        "${DEDICATED_OPENAI_TIMEOUT_SECONDS:?"
    ) in compose
    assert "\n    env_file:" not in compose
    assert "LAB_TRACKER_ANTHROPIC_API_KEY" not in compose
    assert "LAB_TRACKER_GOOGLE_API_KEY" not in compose
    assert "- app_data:/app/data" in compose
    assert "- postgres_data:/var/lib/postgresql/data" in compose


def test_common_script_requires_private_ignored_env_files() -> None:
    common = _deployment_text("common.sh")

    assert 'deployment_env="${script_dir}/.env"' in common
    assert 'provider_env="${script_dir}/provider/.env"' in common
    assert 'find "${lt_env_path}" -prune -perm -077 -print' in common
    assert "must not be accessible by group or others" in common
    assert '--project-name "${lt_compose_project_name}"' in common
    assert "COMPOSE_PROJECT_NAME conflicts with" in common
    assert '--env-file "${deployment_env}"' in common
    assert '--env-file "${provider_env}"' in common


def test_backup_is_one_paused_checkpoint_with_strict_permissions() -> None:
    script = _deployment_text("backup.sh")

    assert "umask 077" in script
    assert 'compose_project_name="$(deployment_value ' in script
    assert 'backup_root="${repo_root}/backups/dedicated-instance"' in script
    assert 'staging_dir="${backup_dir}.partial"' in script
    assert 'mkdir "${staging_dir}"' in script
    assert "chmod 700 \\" in script
    assert 'chmod 700 "${staging_dir}"' in script
    assert 'compose pause app' in script
    assert 'compose unpause app' in script
    assert "app_paused=1" in script
    assert 'pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"' in script
    assert '.Destination "/app/data"' in script
    assert "The app data mount is not a named Docker volume." in script
    assert '.Destination "/var/lib/postgresql/data"' in script
    assert "The Postgres data mount is not a named Docker volume." in script
    assert "/backup/postgres.dump" in script
    assert "/backup/app-data.tar.gz" in script
    assert 'chmod 600 \\' in script
    assert "sha256sum" in script
    assert "shasum -a 256" in script
    assert "MANIFEST.sha256" in script
    assert 'mv "${staging_dir}" "${backup_dir}"' in script


def test_release_uses_revision_gates_probe_and_immutable_rollback() -> None:
    script = _deployment_text("release.sh")

    assert 'git -C "${repo_root}" status --porcelain' in script
    assert "not the pushed upstream tip" in script
    assert 'release_lock="${TMPDIR:-/tmp}/lab-tracker-' in script
    assert "Another release may be active" in script
    assert "cleanup_release_lock" in script
    assert "docker ps --all" in script
    assert "Expected exactly one existing app container" in script
    assert "The existing app container is stopped" in script
    assert 'release_image="${compose_project_name}-app:sha-${revision}"' in script
    assert '"${script_dir}/backup.sh"' in script
    assert '"${script_dir}/restore-smoke.sh" "${backup_dir}"' in script
    assert 'archive --format=tar "${revision}"' in script
    assert '--build-arg "LAB_TRACKER_SOURCE_REVISION=${revision}"' in script
    assert "org.opencontainers.image.revision" in script
    assert "previous_auth_secret_digest" in script
    assert "previous_image_id" in script
    assert "docker inspect --format '{{.Image}}'" in script
    assert 'DEDICATED_IMAGE="${previous_image_id}"' in script
    assert "restoring the previous immutable app image" in script
    assert "up -d --no-deps app" in script
    assert "reserved example hostname" in script
    assert "Database URL components must use" in script
    assert "provider credential file still contains" in script
    assert 'auth_secret_file="${LAB_TRACKER_AUTH_SECRET_KEY_FILE:-' in script
    assert 'LAB_TRACKER_AUTH_SECRET_KEY="$(cat "${auth_secret_file}")"' in script
    assert 'exec python -m lab_tracker.deployment_probe "$@"' in script
    assert '--expected-app-name "${app_name}"' in script
    assert "--require-provider-credential" in script
    assert "--require-scheduler" in script
    assert "--require-users" in script
    assert "--require-admin" in script
    assert "--alembic-config /app/alembic.ini" in script
    assert "verify_health_with_retries()" in script
    assert (
        'verify_health_with_retries "http://127.0.0.1:${host_port}/health"'
        in script
    )
    assert 'verify_health_with_retries "${public_base_url}/health"' in script
    assert "Deployment identity check failed after" in script
    assert '--connect-timeout "${connect_timeout}"' in script
    assert '--max-time "${request_timeout}"' in script
    assert 'if [ "${container_revision}" != "${revision}" ]' in script


def test_restore_smoke_is_disposable_and_identity_neutral() -> None:
    script = _deployment_text("restore-smoke.sh")

    assert 'case "${backup_dir}" in' in script
    assert "Refusing an incomplete backup directory" in script
    assert "MANIFEST.sha256" in script
    assert "lab-tracker-restore-${scratch_suffix}" in script
    assert "trap cleanup_on_exit 0" in script
    assert 'docker rm -f "${postgres_container}"' in script
    assert 'docker volume rm "${postgres_volume}" "${app_volume}"' in script
    assert "--no-owner" in script
    assert "--no-privileges" in script
    assert 'SELECT version_num FROM alembic_version;' in script
    assert 'SELECT count(*) AS restored_users FROM users;' in script
    assert 'if [ -z "${restored_revision}" ]' in script
    assert 'if [ "${restored_users}" -lt 1 ]' in script
    assert "tar -xzf /backup/app-data.tar.gz -C /restore" in script


def test_examples_and_build_exclusions_are_generic() -> None:
    dockerignore = set(
        (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    )
    assert "/backups" in dockerignore
    assert "/deployments/dedicated-instance/.env" in dockerignore
    assert "/deployments/dedicated-instance/provider/.env" in dockerignore

    deployment_example = _deployment_text(".env.example")
    assert "DEDICATED_APP_NAME=dedicated-lab-tracker" in deployment_example
    assert "DEDICATED_PUBLIC_BASE_URL=https://lab-tracker.example.org" in (
        deployment_example
    )
    assert "DEDICATED_POSTGRES_DB=example_lab_tracker" in deployment_example
    assert "replace-with-existing-url-safe-database-password" in deployment_example
    assert "DEDICATED_OPENAI_MODEL=" in deployment_example
    assert "DEDICATED_OPENAI_REASONING_EFFORT=" in deployment_example
    assert "DEDICATED_OPENAI_REASONING_MODE=" in deployment_example
    assert "DEDICATED_OPENAI_TIMEOUT_SECONDS=" in deployment_example

    provider_example = _deployment_text("provider/.env.example")
    assert "DEDICATED_OPENAI_API_KEY=replace-with-provider-api-key" in provider_example

    readme = _deployment_text("README.md")
    assert "only for an existing instance with exactly one running app" in readme
    assert "image-only" in readme
    assert "backward-compatible" in readme
    assert "MANIFEST.sha256" in readme


def test_deployment_scripts_are_executable() -> None:
    for filename in ("backup.sh", "release.sh", "restore-smoke.sh"):
        assert os.access(DEPLOYMENT_ROOT / filename, os.X_OK)


def test_deployment_material_has_no_private_instance_identifiers() -> None:
    deployment_material = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(DEPLOYMENT_ROOT.rglob("*"))
        if path.is_file()
    ).lower()
    forbidden = (
        "mari" + "on",
        "tail" + "scale",
        "tail79" + "f9d8",
        ".ts." + "net",
        "gpt-5." + "6-sol",
        "x" + "high",
        "54" + "00",
        "127.0.0.1:81" + "00",
        "postgres_db: lab_" + "tracker",
        "postgres_user: lab_" + "tracker",
    )

    for value in forbidden:
        assert value not in deployment_material
