"""Tests for scripts/build-pages-demo.mjs (the hosted GitHub Pages demo)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urljoin

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "build-pages-demo.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")

_ASSET_ATTR = re.compile(r'\s(?:href|src)="([^"]+)"')


def _build(out_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    return subprocess.run(
        [NODE, str(SCRIPT), str(out_dir), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _asset_urls(html: str) -> list[str]:
    return _ASSET_ATTR.findall(html)


def _published_path(out_dir: Path, base_path: str, url: str) -> Path:
    path = url.split("?", 1)[0]
    assert path.startswith(base_path), path
    return out_dir / path[len(base_path) :]


@pytest.mark.parametrize(
    "route",
    [
        "app/",
        "app/questions/6e3bfd66-ce26-4a66-ab08-1b951bb79d56",
        "app/projects/6e3bfd66-ce26-4a66-ab08-1b951bb79d56/onboarding",
    ],
)
def test_404_shell_assets_resolve_from_any_nested_route(
    tmp_path: Path, route: str
) -> None:
    out_dir = tmp_path / "site"
    result = _build(out_dir)
    assert result.returncode == 0, result.stderr

    html = (out_dir / "404.html").read_text(encoding="utf-8")
    urls = _asset_urls(html)
    assert urls, html
    request_url = f"https://example.github.io/lab-tracker/{route}"
    for url in urls:
        resolved = urljoin(request_url, url)
        assert resolved.startswith("https://example.github.io/lab-tracker/app/static/"), (
            url,
            resolved,
        )
        assert _published_path(
            out_dir, "/lab-tracker/", resolved.removeprefix("https://example.github.io")
        ).is_file()


def test_base_path_is_configurable(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    result = _build(out_dir, "--base-path=/demo/")
    assert result.returncode == 0, result.stderr

    html = (out_dir / "404.html").read_text(encoding="utf-8")
    for url in _asset_urls(html):
        assert url.startswith("/demo/app/static/"), url
        assert _published_path(out_dir, "/demo/", url).is_file()


def test_app_index_keeps_relative_assets(tmp_path: Path) -> None:
    out_dir = tmp_path / "site"
    result = _build(out_dir)
    assert result.returncode == 0, result.stderr

    html = (out_dir / "app" / "index.html").read_text(encoding="utf-8")
    for url in _asset_urls(html):
        assert url.startswith("static/"), url
        assert (out_dir / "app" / url.split("?", 1)[0]).is_file()


@pytest.mark.parametrize("base_path", ["lab-tracker/", "/lab-tracker", "", "//evil/"])
def test_invalid_base_path_fails_loudly(tmp_path: Path, base_path: str) -> None:
    result = _build(tmp_path / "site", f"--base-path={base_path}")

    assert result.returncode != 0
    assert "--base-path" in result.stderr
