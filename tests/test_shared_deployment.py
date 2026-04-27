from __future__ import annotations

from pathlib import Path
import tomllib


def test_postgres_extra_declares_psycopg_dependency():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    postgres_extra = pyproject["project"]["optional-dependencies"]["postgres"]

    assert any(dependency.startswith("psycopg[binary]") for dependency in postgres_extra)


def test_docker_compose_persists_shared_metadata_and_assets():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "postgresql+psycopg://lab_tracker" in compose
    assert "LAB_TRACKER_FILE_STORAGE_PATH" in compose
    assert "LAB_TRACKER_NOTE_STORAGE_PATH" in compose
    assert "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN" in compose
    assert "file_storage:/app/data/file_storage" in compose
    assert "note_storage:/app/data/note_storage" in compose
    assert "postgres_data:/var/lib/postgresql/data" in compose


def test_shared_deployment_docs_and_env_template_cover_required_settings():
    docs = Path("docs/shared-deployment.md").read_text(encoding="utf-8")
    env_template = Path("deploy/shared-metadata.env.example").read_text(encoding="utf-8")

    for required in [
        "LAB_TRACKER_DATABASE_URL",
        "LAB_TRACKER_AUTH_SECRET_KEY",
        "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN",
        "LAB_TRACKER_FILE_STORAGE_PATH",
        "LAB_TRACKER_NOTE_STORAGE_PATH",
        "Postgres",
    ]:
        assert required in docs
        assert required in env_template or required == "Postgres"

    assert "uv pip install -e \".[postgres,mcp]\"" in docs
    assert "Run migrations from one controlled place" in docs
