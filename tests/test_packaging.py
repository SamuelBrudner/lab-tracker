from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

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


def test_frontend_package_data_covers_all_bundle_files():
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "src" / "lab_tracker"
    patterns = _package_data_patterns(repo_root)

    bundle_files = _packaged_files(package_root, "frontend")
    packaged_files = {
        file_path
        for file_path in bundle_files
        if any(fnmatch.fnmatch(file_path, pattern) for pattern in patterns)
    }

    assert packaged_files == bundle_files


def test_alembic_package_data_covers_all_migration_files():
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "src" / "lab_tracker"
    patterns = _package_data_patterns(repo_root)

    migration_files = _packaged_files(package_root, "alembic")
    packaged_files = {
        file_path
        for file_path in migration_files
        if any(fnmatch.fnmatch(file_path, pattern) for pattern in patterns)
    }

    assert packaged_files == migration_files


def test_wheel_installed_migrations_can_upgrade_sqlite(tmp_path: Path):
    repo_root = Path(__file__).resolve().parent.parent
    wheelhouse = tmp_path / "wheelhouse"
    target = tmp_path / "site"
    wheelhouse.mkdir()
    build_dir = repo_root / "build"
    had_build_dir = build_dir.exists()

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required for the wheel smoke test")

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
    wheel = next(wheelhouse.glob("lab_tracker-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "lab_tracker/alembic/env.py" in names
    assert any(
        name.startswith("lab_tracker/alembic/versions/") and name.endswith(".py")
        for name in names
    )

    with zipfile.ZipFile(wheel) as archive:
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
