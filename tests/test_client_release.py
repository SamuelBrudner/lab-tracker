"""Release-boundary comparison shared by lt setup status, lt-mcp, and the server."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab_tracker import client_release
from lab_tracker.client_release import (
    ReleaseComparison,
    ReleaseIdentity,
    client_install_command,
    release_from_health,
    release_key,
    update_steps,
)

REVISION_A = "a" * 40
REVISION_B = "b" * 40


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.1.0", "0.2.0"),
        ("0.9.9", "0.10.0"),
        ("1.2", "1.2.1"),
        ("2026.9.1", "2026.10.1"),
    ],
)
def test_release_key_orders_numerically_not_lexically(older: str, newer: str) -> None:
    assert release_key(older) < release_key(newer)


def test_release_key_ignores_trailing_zero_components() -> None:
    assert release_key("0.1") == release_key("0.1.0") == release_key("0.1.0.0")


@pytest.mark.parametrize("version", [None, "", "0+unknown", "0.2.0rc1", "1.0.0+local", "v1.0"])
def test_release_key_refuses_anything_but_a_plain_release(version: str | None) -> None:
    assert release_key(version) is None


@pytest.mark.parametrize(
    ("client_version", "server_version", "status"),
    [
        ("0.1.0", "0.2.0", "behind"),
        ("0.2.0", "0.1.0", "ahead"),
        ("0.1.0", "0.1", "current"),
        ("0.1.0", None, "unknown"),
        (None, "0.2.0", "unknown"),
        ("0.1.0", "0.2.0rc1", "unknown"),
    ],
)
def test_release_status_only_reports_behind_across_a_release_boundary(
    client_version: str | None, server_version: str | None, status: str
) -> None:
    comparison = ReleaseComparison(
        client=ReleaseIdentity(version=client_version),
        server=ReleaseIdentity(version=server_version),
    )

    assert comparison.status == status
    assert comparison.as_dict()["client_behind_server"] is (status == "behind")


def test_revision_drift_within_a_release_is_reported_but_not_behind() -> None:
    comparison = ReleaseComparison(
        client=ReleaseIdentity(version="0.1.0", revision=REVISION_A),
        server=ReleaseIdentity(version="0.1.0", revision=REVISION_B),
    )

    assert comparison.status == "current"
    assert comparison.same_revision is False
    assert comparison.as_dict() == {
        "status": "current",
        "client_behind_server": False,
        "same_revision": False,
        "client": {"version": "0.1.0", "revision": REVISION_A},
        "server": {"version": "0.1.0", "revision": REVISION_B},
    }


def test_same_revision_is_unknown_without_both_revisions() -> None:
    comparison = ReleaseComparison(
        client=ReleaseIdentity(version="0.1.0", revision=None),
        server=ReleaseIdentity(version="0.1.0", revision=REVISION_B),
    )

    assert comparison.same_revision is None


def test_release_from_health_reads_the_app_identity() -> None:
    payload = {"status": "ok", "app": {"version": " 0.2.0 ", "source_revision": REVISION_B.upper()}}

    assert release_from_health(payload) == ReleaseIdentity(version="0.2.0", revision=REVISION_B)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"app": "lab-tracker"},
        # Servers from before /health reported a version, and unpinned deploys.
        {"app": {"name": "lab-tracker", "source_revision": "unknown"}},
    ],
)
def test_release_from_health_degrades_to_unknown(payload: object) -> None:
    assert release_from_health(payload) == ReleaseIdentity()


def test_install_command_matches_the_agents_page() -> None:
    client_setup = Path("src/lab_tracker/frontend_src/features/client-setup.js").read_text(
        encoding="utf-8"
    )
    match = re.search(r'const SOURCE_REPOSITORY =\s*"([^"]+)";', client_setup)

    assert match is not None
    assert match.group(1) == client_release.SOURCE_REPOSITORY_URL
    assert "toolInstallCommand: `uv tool install --force \"${installRequirement}\"`" in (
        client_setup
    )
    assert client_install_command(REVISION_B) == (
        f'uv tool install --force "lab-tracker @ git+{match.group(1)}@{REVISION_B}"'
    )


def test_update_steps_pin_the_server_revision_when_known() -> None:
    pinned = update_steps(ReleaseIdentity(version="0.2.0", revision=REVISION_B))
    unpinned = update_steps(ReleaseIdentity(version="0.2.0"))

    assert f"@{REVISION_B}" in pinned
    assert "`lt update`" in pinned
    assert "Agents page" in unpinned
    assert "uv tool install" not in unpinned


def test_installed_release_reads_version_and_pep610_revision(monkeypatch) -> None:
    direct_url = json.dumps({"vcs_info": {"vcs": "git", "commit_id": REVISION_A.upper()}})

    def distribution(name: str) -> SimpleNamespace:
        assert name == "lab-tracker"
        return SimpleNamespace(read_text=lambda _filename: direct_url)

    monkeypatch.setattr(client_release.importlib.metadata, "distribution", distribution)
    monkeypatch.setattr(client_release.importlib.metadata, "version", lambda _name: "0.3.0")

    assert client_release.installed_release() == ReleaseIdentity(
        version="0.3.0", revision=REVISION_A
    )


def test_installed_release_without_an_installed_distribution(monkeypatch) -> None:
    def missing(_name: str) -> None:
        raise client_release.importlib.metadata.PackageNotFoundError("lab-tracker")

    monkeypatch.setattr(client_release.importlib.metadata, "distribution", missing)
    monkeypatch.setattr(client_release.importlib.metadata, "version", missing)

    assert client_release.installed_release() == ReleaseIdentity()
