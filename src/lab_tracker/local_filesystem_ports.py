"""Application-owned roles for bounded local filesystem operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias

from lab_tracker.bounded_subprocess import ProcessDeadline, StdoutConsumer
from lab_tracker.local_resolution_budget import (
    MAX_LOCAL_RECOVERY_MAX_DIRECTORIES,
    MAX_LOCAL_RECOVERY_MAX_FILES,
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


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class EnumeratedLocalRegularFileTarget:
    """Opaque root-slot target returned only by bounded recovery enumeration."""

    root_index: int
    locator: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.root_index) is not int
            or self.root_index < 0
            or type(self.locator) is not tuple
            or not self.locator
            or any(type(component) is not str for component in self.locator)
        ):
            raise TypeError("Local regular-file target is invalid.")


LocalRegularFileTarget: TypeAlias = (
    DirectLocalRegularFileTarget
    | RegisteredLocalRegularFileTarget
    | EnumeratedLocalRegularFileTarget
)


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


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class DirectLocalRecoveryScope:
    """Nominal request to enumerate every explicit operator root once."""


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class RegisteredLocalRecoveryScope:
    """Nominal request to enumerate beneath one retained registered store."""

    store_root: str

    def __post_init__(self) -> None:
        if type(self.store_root) is not str:
            raise TypeError("Local recovery scope is invalid.")


LocalRecoveryScope: TypeAlias = DirectLocalRecoveryScope | RegisteredLocalRecoveryScope


@dataclass(frozen=True, slots=True)
class LocalRecoveryCandidate:
    """One path-free recovery candidate that can be returned to the reader."""

    target: LocalRegularFileTarget
    name: str

    def __post_init__(self) -> None:
        if type(self.target) not in (
            EnumeratedLocalRegularFileTarget,
            RegisteredLocalRegularFileTarget,
        ):
            raise TypeError("Local recovery candidate is invalid.")
        if type(self.name) is not str or not self.name:
            raise TypeError("Local recovery candidate is invalid.")


class LocalRecoveryEnumerationOutcome(Enum):
    """Finite result of one bounded, capability-owned recovery traversal."""

    COMPLETE = "complete"
    LIMIT_REACHED = "limit"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LocalRecoveryEnumerationResult:
    """All-or-nothing, path-free candidate metadata from one traversal."""

    outcome: LocalRecoveryEnumerationOutcome
    candidates: tuple[LocalRecoveryCandidate, ...]
    directories_visited: int

    def __post_init__(self) -> None:
        if type(self.outcome) is not LocalRecoveryEnumerationOutcome:
            raise TypeError("Local recovery enumeration result is invalid.")
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > MAX_LOCAL_RECOVERY_MAX_FILES
            or any(type(candidate) is not LocalRecoveryCandidate for candidate in self.candidates)
        ):
            raise TypeError("Local recovery enumeration result is invalid.")
        if (
            type(self.directories_visited) is not int
            or not 0
            <= self.directories_visited
            <= MAX_LOCAL_RECOVERY_MAX_DIRECTORIES
        ):
            raise ValueError("Local recovery enumeration result is invalid.")
        if (
            self.outcome is LocalRecoveryEnumerationOutcome.FAILED
            and (self.candidates or self.directories_visited != 0)
        ):
            raise ValueError("Local recovery enumeration result is invalid.")


class LocalRecoveryEnumerator(Protocol):
    """Narrow port for one traversal under the logical resolution budget."""

    def enumerate_recovery_candidates(
        self,
        scope: LocalRecoveryScope,
        *,
        target_name: str | None,
        max_files: int,
        max_directories: int,
        budget: LocalResolutionBudget,
    ) -> LocalRecoveryEnumerationResult: ...
