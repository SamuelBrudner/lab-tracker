from __future__ import annotations

import pytest

from lab_tracker.collection_manifest import (
    MAX_COLLECTION_MEMBERS,
    canonicalize_collection_key,
    canonicalize_collection_manifest,
)
from lab_tracker.errors import ValidationError

_A_HASH = "a" * 64
_B_HASH = "b" * 64


@pytest.mark.parametrize(
    "value",
    ["trials", "rig-2.session_001", "A", "9" + ("x" * 119)],
)
def test_collection_key_accepts_the_shared_client_server_contract(value):
    assert canonicalize_collection_key(value) == value


@pytest.mark.parametrize(
    "value",
    ["", "-trials", ".trials", "trial folder", "trials/raw", "trials@rig", "x" * 121],
)
def test_collection_key_rejects_values_that_cannot_reach_the_server(value):
    with pytest.raises(ValidationError, match="collection_key"):
        canonicalize_collection_key(value)


def test_collection_manifest_has_stable_order_independent_identity():
    first = canonicalize_collection_manifest(
        schema_version=1,
        members=[
            {"path": "trial-2/data.bin", "checksum": _B_HASH, "size_bytes": 12},
            {"path": "trial-1/data.bin", "checksum": _A_HASH, "size_bytes": 7},
        ],
    )
    reordered = canonicalize_collection_manifest(
        schema_version=1,
        members=list(reversed(first.members)),
    )

    assert first.as_dict() == {
        "schema_version": 1,
        "members": [
            {"path": "trial-1/data.bin", "checksum": _A_HASH, "size_bytes": 7},
            {"path": "trial-2/data.bin", "checksum": _B_HASH, "size_bytes": 12},
        ],
    }
    assert first.canonical_bytes == reordered.canonical_bytes
    assert first.manifest_hash == reordered.manifest_hash
    assert first.manifest_hash == (
        "81361c72ef90db47676e56058e6901bc76b31fe48920564ce8e576c5c283a331"
    )
    assert first.total_size_bytes == 19


@pytest.mark.parametrize(
    "member",
    [
        {"path": "../escape.bin", "checksum": _A_HASH, "size_bytes": 1},
        {"path": "/absolute.bin", "checksum": _A_HASH, "size_bytes": 1},
        {"path": "trial//data.bin", "checksum": _A_HASH, "size_bytes": 1},
        {"path": "trial\\data.bin", "checksum": _A_HASH, "size_bytes": 1},
        {"path": "trial/data.bin", "checksum": "A" * 64, "size_bytes": 1},
        {"path": "trial/data.bin", "checksum": _A_HASH, "size_bytes": -1},
        {"path": "trial/data.bin", "checksum": _A_HASH, "size_bytes": True},
    ],
)
def test_collection_manifest_rejects_invalid_members(member):
    with pytest.raises(ValidationError):
        canonicalize_collection_manifest(schema_version=1, members=[member])


def test_collection_manifest_rejects_duplicate_paths():
    with pytest.raises(ValidationError, match="Duplicate"):
        canonicalize_collection_manifest(
            schema_version=1,
            members=[
                {"path": "same.bin", "checksum": _A_HASH, "size_bytes": 1},
                {"path": "same.bin", "checksum": _B_HASH, "size_bytes": 2},
            ],
        )


def test_collection_manifest_rejects_unsupported_schema_and_member_overflow():
    with pytest.raises(ValidationError, match="schema_version"):
        canonicalize_collection_manifest(schema_version=2, members=[])

    member = {"path": "data.bin", "checksum": _A_HASH, "size_bytes": 1}
    with pytest.raises(ValidationError, match=str(MAX_COLLECTION_MEMBERS)):
        canonicalize_collection_manifest(
            schema_version=1,
            members=[member] * (MAX_COLLECTION_MEMBERS + 1),
        )


def test_collection_manifest_identity_includes_size():
    first = canonicalize_collection_manifest(
        schema_version=1,
        members=[{"path": "data.bin", "checksum": _A_HASH, "size_bytes": 1}],
    )
    changed = canonicalize_collection_manifest(
        schema_version=1,
        members=[{"path": "data.bin", "checksum": _A_HASH, "size_bytes": 2}],
    )

    assert first.manifest_hash != changed.manifest_hash
