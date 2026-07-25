from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_PATH = REPO_ROOT / "deployments" / "marion" / "docker-compose.yml"


def test_marion_compose_is_isolated_and_fails_closed_on_image() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "name: lab-tracker-marion" in compose
    assert (
        "image: ${MARION_IMAGE:?Set MARION_IMAGE to a reviewed immutable tag}"
        in compose
    )
    assert "\n    build:" not in compose
    assert '- "127.0.0.1:8100:8000"' in compose
    assert "LAB_TRACKER_APP_NAME: marion-lab-tracker" in compose
    assert (
        "LAB_TRACKER_PUBLIC_BASE_URL: "
        "https://lab-tracker.tail79f9d8.ts.net:8443"
    ) in compose
    assert "- app_data:/app/data" in compose
    assert "- postgres_data:/var/lib/postgresql/data" in compose


def test_release_image_exposes_auditable_revision() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG LAB_TRACKER_SOURCE_REVISION=unknown" in dockerfile
    assert (
        'org.opencontainers.image.revision="${LAB_TRACKER_SOURCE_REVISION}"'
        in dockerfile
    )
    assert 'LAB_TRACKER_SOURCE_REVISION="${LAB_TRACKER_SOURCE_REVISION}"' in dockerfile


def test_backup_script_targets_only_marion_and_validates_archives() -> None:
    script = (
        REPO_ROOT / "deployments" / "marion" / "backup.sh"
    ).read_text(encoding="utf-8")

    assert 'app_volume="lab-tracker-marion_app_data"' in script
    assert "umask 077" in script
    assert 'mkdir "${backup_dir}"' in script
    assert 'chmod 700 "${repo_root}/backups" "${backup_root}"' in script
    assert 'chmod 700 "${backup_dir}"' in script
    assert 'chmod 600 \\' in script
    assert '--project-directory "${script_dir}"' in script
    assert "pg_dump -U lab_tracker -d lab_tracker --format=custom" in script
    assert "pg_restore --list /backup/marion-postgres.dump" in script
    assert 'tar -tzf "${backup_dir}/marion-app-data.tar.gz"' in script
    assert 'shasum -a 256 \\' in script


def test_release_script_requires_a_clean_revision_and_verifies_live_health() -> None:
    script = (
        REPO_ROOT / "deployments" / "marion" / "release.sh"
    ).read_text(encoding="utf-8")

    assert 'git -C "${repo_root}" status --porcelain' in script
    assert "not the pushed upstream tip" in script
    assert 'release_image="lab-tracker-marion:sha-${revision}"' in script
    assert '"${script_dir}/backup.sh"' in script
    assert '"${script_dir}/restore-smoke.sh" "${backup_dir}"' in script
    assert 'archive --format=tar "${revision}"' in script
    assert '--build-arg "LAB_TRACKER_SOURCE_REVISION=${revision}"' in script
    assert "org.opencontainers.image.revision" in script
    assert "previous_auth_secret_digest" in script
    assert "The persistent Marion auth-secret identity changed" in script
    assert "rollback_needed=1" in script
    assert "restoring the previous app image" in script
    assert "up -d --no-deps app" in script
    assert "python -m lab_tracker.deployment_probe" in script
    assert "--require-provider-credential" in script
    assert 'verify_health_identity "http://127.0.0.1:8100/health"' in script
    assert 'verify_health_identity "${public_health_url}"' in script
    assert 'if [ "${container_revision}" != "${revision}" ]' in script
    assert "--connect-timeout 5" in script
    assert "--max-time 15" in script
    assert '"${health_url}"' in script
    assert "--require-admin" in script
    assert "--alembic-config /app/alembic.ini" in script


def test_restore_smoke_is_disposable_and_checks_both_artifacts() -> None:
    script = (
        REPO_ROOT / "deployments" / "marion" / "restore-smoke.sh"
    ).read_text(encoding="utf-8")

    assert 'case "${backup_dir}" in' in script
    assert "lab-tracker-marion-restore-${scratch_suffix}" in script
    assert "trap cleanup EXIT HUP INT TERM" in script
    assert 'docker rm -f "${postgres_container}"' in script
    assert 'docker volume rm "${postgres_volume}" "${app_volume}"' in script
    assert "pg_restore \\" in script
    assert 'SELECT version_num FROM alembic_version;' in script
    assert 'SELECT count(*) AS restored_users FROM users;' in script
    assert 'if [ -z "${restored_revision}" ]' in script
    assert 'if [ "${restored_users}" -lt 1 ]' in script
    assert "tar -xzf /backup/marion-app-data.tar.gz -C /restore" in script


def test_backups_and_local_deployment_secrets_are_outside_build_context() -> None:
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    ignored = set(dockerignore.splitlines())

    assert "/backups" in ignored
    assert "/deployments/marion/.env" in ignored
    assert "/deployments/shared-provider/.env" in ignored

    example = (
        REPO_ROOT / "deployments" / "marion" / ".env.example"
    ).read_text(encoding="utf-8")
    assert "MARION_POSTGRES_PASSWORD=" in example
    assert "replace-with-existing" in example
