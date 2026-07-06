"""Shared helpers for LabTrackerAPI and its per-domain mixins."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))


def _first_uuid(values: tuple[Any, ...]) -> UUID | None:
    for value in values:
        if isinstance(value, UUID):
            return value
    return None


def _uuid_attr(value: Any, attr: str | None) -> UUID | None:
    if attr is None:
        return None
    candidate = getattr(value, attr, None)
    return candidate if isinstance(candidate, UUID) else None
