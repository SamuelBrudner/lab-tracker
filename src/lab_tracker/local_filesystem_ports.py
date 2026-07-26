"""Application-owned roles for bounded local filesystem operations."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from lab_tracker.bounded_subprocess import ProcessDeadline


class LocalDirectoryInspection(Enum):
    """Finite, identity-free outcome of inspecting one local directory."""

    ACCESSIBLE = "accessible"
    DENIED = "denied"
    FAILED = "failed"


class LocalDirectoryInspector(Protocol):
    """Narrow application port for one bounded directory inspection."""

    def inspect_directory(
        self,
        candidate: str,
        *,
        deadline: ProcessDeadline,
    ) -> LocalDirectoryInspection: ...
