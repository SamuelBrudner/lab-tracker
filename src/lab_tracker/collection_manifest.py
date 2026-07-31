"""Canonical acquisition-collection manifest handling.

Collection members are data-level facts, not Lab Tracker graph entities. This
module deliberately has no persistence dependencies so the server and consumer
client can share one frozen content-identity contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from lab_tracker.errors import ValidationError

ACQUISITION_COLLECTION_CAPABILITY = "acquisition_collections_v1"
COLLECTION_MANIFEST_SCHEMA_VERSION = 1
MAX_COLLECTION_MEMBERS = 10_000
MAX_COLLECTION_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_COLLECTION_MEMBER_PATH_LENGTH = 1_000
MAX_SIGNED_BIGINT = (2**63) - 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COLLECTION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


@dataclass(frozen=True)
class CanonicalCollectionMember:
    path: str
    checksum: str
    size_bytes: int

    def as_dict(self) -> dict[str, str | int]:
        return {
            "path": self.path,
            "checksum": self.checksum,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class CanonicalCollectionManifest:
    schema_version: int
    members: tuple[CanonicalCollectionMember, ...]
    canonical_bytes: bytes
    manifest_hash: str
    total_size_bytes: int

    @property
    def member_count(self) -> int:
        return len(self.members)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "members": [member.as_dict() for member in self.members],
        }


def canonicalize_collection_key(value: Any) -> str:
    """Validate the immutable URL-safe identifier shared by clients and server."""

    if not isinstance(value, str) or not _COLLECTION_KEY_RE.fullmatch(value):
        raise ValidationError(
            "collection_key must be 1-120 characters using letters, digits, "
            "periods, underscores, or hyphens, and must start with a letter "
            "or digit."
        )
    return value


def canonicalize_collection_manifest(
    *,
    schema_version: int,
    members: Iterable[Mapping[str, Any] | object],
) -> CanonicalCollectionManifest:
    """Validate, sort, encode, and hash one collection manifest."""

    if schema_version != COLLECTION_MANIFEST_SCHEMA_VERSION:
        raise ValidationError(
            f"Collection manifest schema_version must be {COLLECTION_MANIFEST_SCHEMA_VERSION}."
        )

    raw_members = list(members)
    if len(raw_members) > MAX_COLLECTION_MEMBERS:
        raise ValidationError(
            f"Collection manifests may contain at most {MAX_COLLECTION_MEMBERS} members."
        )

    normalized: list[CanonicalCollectionMember] = []
    seen_paths: set[str] = set()
    total_size_bytes = 0
    for raw_member in raw_members:
        path = _canonical_path(_member_value(raw_member, "path"))
        if path in seen_paths:
            raise ValidationError(f"Duplicate collection member path: {path}")
        seen_paths.add(path)

        checksum_value = _member_value(raw_member, "checksum")
        if not isinstance(checksum_value, str) or not _SHA256_RE.fullmatch(checksum_value):
            raise ValidationError(f"Collection member checksum must be lowercase SHA-256: {path}")

        size_value = _member_value(raw_member, "size_bytes")
        if (
            isinstance(size_value, bool)
            or not isinstance(size_value, int)
            or size_value < 0
            or size_value > MAX_SIGNED_BIGINT
        ):
            raise ValidationError(
                f"Collection member size_bytes must be a nonnegative 64-bit integer: {path}"
            )
        total_size_bytes += size_value
        if total_size_bytes > MAX_SIGNED_BIGINT:
            raise ValidationError(
                "Collection total_size_bytes exceeds the signed 64-bit database limit."
            )
        normalized.append(
            CanonicalCollectionMember(
                path=path,
                checksum=checksum_value,
                size_bytes=size_value,
            )
        )

    normalized.sort(key=lambda member: member.path)
    payload = {
        "schema_version": COLLECTION_MANIFEST_SCHEMA_VERSION,
        "members": [member.as_dict() for member in normalized],
    }
    canonical_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(canonical_bytes) > MAX_COLLECTION_MANIFEST_BYTES:
        raise ValidationError("Canonical collection manifest exceeds the 16 MiB request limit.")
    return CanonicalCollectionManifest(
        schema_version=COLLECTION_MANIFEST_SCHEMA_VERSION,
        members=tuple(normalized),
        canonical_bytes=canonical_bytes,
        manifest_hash=hashlib.sha256(canonical_bytes).hexdigest(),
        total_size_bytes=total_size_bytes,
    )


def _member_value(member: Mapping[str, Any] | object, field_name: str) -> Any:
    if isinstance(member, Mapping):
        if field_name not in member:
            raise ValidationError(f"Collection member requires {field_name}.")
        return member[field_name]
    try:
        return getattr(member, field_name)
    except AttributeError as exc:
        raise ValidationError(f"Collection member requires {field_name}.") from exc


def _canonical_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError("Collection member path must not be empty.")
    if value != value.strip():
        raise ValidationError("Collection member path must not have surrounding whitespace.")
    if len(value) > MAX_COLLECTION_MEMBER_PATH_LENGTH:
        raise ValidationError("Collection member path must be 1000 characters or fewer.")
    if "\x00" in value or "\\" in value:
        raise ValidationError("Collection member path must be a NUL-free relative POSIX path.")
    if value.startswith("/"):
        raise ValidationError("Collection member path must be relative.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(
            "Collection member path must be normalized and cannot traverse directories."
        )
    return value
