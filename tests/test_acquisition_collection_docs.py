"""Drift guards for the acquisition-collection operating contract."""

from __future__ import annotations

from pathlib import Path

from lab_tracker.auth import device_principal_can_access
from lab_tracker.collection_manifest import (
    ACQUISITION_COLLECTION_CAPABILITY,
    MAX_COLLECTION_MANIFEST_BYTES,
    MAX_COLLECTION_MEMBERS,
)
from lab_tracker.schema_metadata import build_schema_description

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GUIDE = _REPO_ROOT / "docs" / "acquisition-collections.md"
_CONTRACT = _REPO_ROOT / "docs" / "acquisition-collection-contract.md"
_WATCH_GUIDE = _REPO_ROOT / "docs" / "watch-folder-capture.md"
_RETAINED_SURFACE = _REPO_ROOT / "docs" / "retained-v1-surface.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalized_text(path: Path) -> str:
    return " ".join(_text(path).split()).casefold()


def test_collection_docs_match_advertised_limits_and_capability() -> None:
    capability = ACQUISITION_COLLECTION_CAPABILITY
    member_limit = f"{MAX_COLLECTION_MEMBERS:,}"
    size_limit = f"{MAX_COLLECTION_MANIFEST_BYTES // (1024 * 1024)} MiB"
    schema = build_schema_description()

    assert capability in schema["capabilities"]
    for path in (_GUIDE, _CONTRACT, _WATCH_GUIDE, _RETAINED_SURFACE):
        content = _text(path)
        assert capability in content, f"{path.name} omits the capability name"

    for path in (_GUIDE, _CONTRACT):
        content = _text(path)
        assert member_limit in content, f"{path.name} has a stale member limit"
        assert size_limit in content, f"{path.name} has a stale request-size limit"


def test_rollout_docs_preserve_additive_compatibility_contract() -> None:
    guide = _normalized_text(_GUIDE)
    retained = _normalized_text(_RETAINED_SURFACE)
    watch = _normalized_text(_WATCH_GUIDE)

    assert guide.index("server first") < guide.index("upgrade clients")
    assert "no per-file fallback is attempted" in guide
    assert "no acquisition outputs are backfilled" in guide
    assert "raw file bytes stay at the source" in guide
    assert "one durable local outbox event" in guide
    assert "do not use an in-place alembic downgrade" in guide
    assert "before pydantic body validation" in guide
    assert "chunked or has no length header" in guide

    assert "never falls back to per-file writes" in retained
    assert "not backfilled" in retained
    assert "never falls back to thousands" in watch


def test_documented_paired_device_collection_scope_is_fail_closed() -> None:
    collection_path = (
        "/sessions/00000000-0000-0000-0000-000000000001/"
        "collections/rig-2/snapshots"
    )

    assert device_principal_can_access("POST", collection_path)
    assert device_principal_can_access("GET", collection_path)
    assert not device_principal_can_access("PUT", collection_path)
    assert not device_principal_can_access("DELETE", collection_path)
    assert not device_principal_can_access(
        "POST",
        "/sessions/00000000-0000-0000-0000-000000000001/outputs",
    )
    assert not device_principal_can_access("POST", "/experiments")

    guide = _text(_GUIDE)
    assert "/sessions/{session_id}/collections/{collection_key}/snapshots" in guide
    assert "They cannot create or mutate Projects, Questions, Experiments" in guide


def test_primary_docs_link_to_collection_operations_guide() -> None:
    expected_links = {
        _REPO_ROOT / "README.md": "docs/acquisition-collections.md",
        _REPO_ROOT / "docs" / "setup.md": "acquisition-collections.md",
        _REPO_ROOT / "docs" / "self-hosted-operations.md": (
            "acquisition-collections.md"
        ),
        _CONTRACT: "acquisition-collections.md",
        _WATCH_GUIDE: "acquisition-collections.md",
    }

    for path, link in expected_links.items():
        assert link in _text(path), f"{path.name} does not link to the guide"
