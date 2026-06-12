from __future__ import annotations

import fnmatch
from pathlib import Path

import tomllib


def test_frontend_package_data_covers_all_bundle_files():
    repo_root = Path(__file__).resolve().parent.parent
    package_root = repo_root / "src" / "lab_tracker"
    frontend_dir = package_root / "frontend"
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = pyproject["tool"]["setuptools"]["package-data"]["lab_tracker"]

    bundle_files = {
        path.relative_to(package_root).as_posix()
        for path in frontend_dir.iterdir()
        if path.is_file()
    }
    packaged_files = {
        file_path
        for file_path in bundle_files
        if any(fnmatch.fnmatch(file_path, pattern) for pattern in patterns)
    }

    assert packaged_files == bundle_files
