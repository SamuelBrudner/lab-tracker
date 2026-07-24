from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

import pytest
import tomllib


def _packaged_files(package_root: Path, subdir: str) -> set[str]:
    root = package_root / subdir
    return {
        path.relative_to(package_root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _package_data_patterns(repo_root: Path) -> list[str]:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["tool"]["setuptools"]["package-data"]["lab_tracker"]


def _package_data_matches(file_path: str, patterns: list[str]) -> bool:
    path = PurePosixPath(file_path)
    return any(path.match(pattern) for pattern in patterns)


def _build_wheel(repo_root: Path, wheelhouse: Path) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the wheel smoke test")

    build_dir = repo_root / "build"
    had_build_dir = build_dir.exists()
    try:
        subprocess.run(
            [
                uv,
                "build",
                "--wheel",
                "--out-dir",
                str(wheelhouse),
                str(repo_root),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        if not had_build_dir:
            shutil.rmtree(build_dir, ignore_errors=True)
    return next(wheelhouse.glob("lab_tracker-*.whl"))


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    return _build_wheel(repo_root, tmp_path_factory.mktemp("wheelhouse"))


def test_dockerfile_copy_sources_exist_in_build_context():
    """Guard against COPYing a relocated/missing path (breaks docker + Render build)."""
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = repo_root / "Dockerfile"
    missing: list[str] = []
    for raw_line in dockerfile.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.upper().startswith("COPY "):
            continue
        tokens = line.split()[1:]
        if any(token.startswith("--from") for token in tokens):
            continue  # multi-stage copy: source is a build stage, not the context
        sources = [token for token in tokens[:-1] if not token.startswith("--")]
        missing.extend(source for source in sources if not (repo_root / source).exists())
    assert not missing, f"Dockerfile COPYs missing build-context paths: {missing}"


def test_dockerfile_runs_app_as_non_root_user():
    repo_root = Path(__file__).resolve().parent.parent
    dockerfile = (repo_root / "Dockerfile").read_text(encoding="utf-8")

    assert "adduser --system --ingroup labtracker" in dockerfile
    assert "chown -R labtracker:labtracker /app /var/data" in dockerfile
    assert "USER labtracker" in dockerfile


def test_docker_entrypoint_has_short_migration_retry_budget():
    repo_root = Path(__file__).resolve().parent.parent
    entrypoint = (repo_root / "deploy" / "docker-entrypoint.sh").read_text(
        encoding="utf-8"
    )

    assert 'max_attempts="${MIGRATION_MAX_ATTEMPTS:-3}"' in entrypoint
    assert "fix the migration or database before restarting the container" in entrypoint


def test_frontend_package_data_covers_all_bundle_files():
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "src" / "lab_tracker"
    patterns = _package_data_patterns(repo_root)

    bundle_files = _packaged_files(package_root, "frontend")
    packaged_files = {
        file_path
        for file_path in bundle_files
        if _package_data_matches(file_path, patterns)
    }

    assert packaged_files == bundle_files


def test_wheel_contains_all_frontend_bundle_files(built_wheel: Path):
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "src" / "lab_tracker"
    bundle_files = _packaged_files(package_root, "frontend")

    with zipfile.ZipFile(built_wheel) as archive:
        wheel_files = {
            name.removeprefix("lab_tracker/")
            for name in archive.namelist()
            if name.startswith("lab_tracker/frontend/") and not name.endswith("/")
        }

    assert wheel_files == bundle_files


def test_alembic_package_data_covers_all_migration_files():
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "src" / "lab_tracker"
    patterns = _package_data_patterns(repo_root)

    migration_files = _packaged_files(package_root, "alembic")
    packaged_files = {
        file_path
        for file_path in migration_files
        if _package_data_matches(file_path, patterns)
    }

    assert packaged_files == migration_files


def test_wheel_installed_migrations_can_upgrade_sqlite(tmp_path: Path, built_wheel: Path):
    target = tmp_path / "site"
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
    assert "lab_tracker/alembic/env.py" in names
    assert any(
        name.startswith("lab_tracker/alembic/versions/") and name.endswith(".py")
        for name in names
    )

    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(target)

    db_path = tmp_path / "wheel-install.db"
    smoke_script = f"""
import os
from alembic import command
from lab_tracker.cli import _alembic_config

os.environ["LAB_TRACKER_DATABASE_URL"] = "sqlite+pysqlite:///{db_path.as_posix()}"
command.upgrade(_alembic_config(), "head")
"""
    subprocess.run(
        [sys.executable, "-c", smoke_script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(target)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert db_path.exists()


def test_wheel_installed_frontend_serves_pwa_assets(tmp_path: Path, built_wheel: Path):
    target = tmp_path / "site"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(target)

    smoke_script = """
from fastapi.testclient import TestClient
from lab_tracker.app import create_app

client = TestClient(create_app())
sw_response = client.get("/app/sw.js")
assert sw_response.status_code == 200
assert "lab-tracker-shell-" in sw_response.text
assert "/app/static/manifest.json" in sw_response.text

manifest_response = client.get("/app/static/manifest.json")
assert manifest_response.status_code == 200
manifest = manifest_response.json()
assert manifest["start_url"] == "/app/capture"
assert any(icon["src"] == "/app/static/icon-192.png" for icon in manifest["icons"])
"""
    subprocess.run(
        [sys.executable, "-c", smoke_script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(target)},
        check=True,
        capture_output=True,
        text=True,
    )
