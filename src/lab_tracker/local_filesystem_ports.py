"""Application-owned roles for bounded local filesystem operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias

from lab_tracker.bounded_subprocess import ProcessDeadline, StdoutConsumer
from lab_tracker.local_resolution_budget import (
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
    LocalResolutionBudget,
)


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


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DirectLocalRegularFileTarget:
    """Nominal, identity-opaque target carrying one raw direct candidate."""

    candidate: str

    def __post_init__(self) -> None:
        if type(self.candidate) is not str:
            raise TypeError("Local regular-file target is invalid.")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class RegisteredLocalRegularFileTarget:
    """Nominal target beneath one raw registered root and portable locator."""

    store_root: str
    locator: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.store_root) is not str
            or type(self.locator) is not tuple
            or not self.locator
            or any(type(component) is not str for component in self.locator)
        ):
            raise TypeError("Local regular-file target is invalid.")


LocalRegularFileTarget: TypeAlias = DirectLocalRegularFileTarget | RegisteredLocalRegularFileTarget


class LocalRegularFileReadOutcome(Enum):
    """Finite, path-free outcome of one bounded regular-file attempt."""

    COMPLETE = "complete"
    MISSING = "missing"
    DENIED = "denied"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LocalRegularFileReadResult:
    """Outcome plus the exact clean payload count delivered to the consumer."""

    outcome: LocalRegularFileReadOutcome
    bytes_read: int

    def __post_init__(self) -> None:
        if type(self.outcome) is not LocalRegularFileReadOutcome:
            raise TypeError("Local regular-file result is invalid.")
        if (
            type(self.bytes_read) is not int
            or self.bytes_read < 0
            or self.bytes_read > MAX_LOCAL_RESOLUTION_MAX_READ_BYTES
            or (self.outcome is not LocalRegularFileReadOutcome.COMPLETE and self.bytes_read != 0)
        ):
            raise ValueError("Local regular-file result is invalid.")


class LocalRegularFileReader(Protocol):
    """Narrow port for a streamed, budgeted regular-file attempt."""

    def read_regular_file(
        self,
        target: LocalRegularFileTarget,
        *,
        budget: LocalResolutionBudget,
        stdout_consumer: StdoutConsumer,
    ) -> LocalRegularFileReadResult: ...
