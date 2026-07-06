"""Shared helpers for SQLAlchemy mapper parts."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def uuid_from_db(raw: str | UUID) -> UUID:
    # Tolerant during the per-entity GUID migration (str for un-migrated columns,
    # UUID for migrated ones).
    return raw if isinstance(raw, UUID) else UUID(raw)


def uuid_to_db(value: UUID) -> str:
    return str(value)
