"""One deadline and byte allowance for a logical local artifact resolution.

The budget deliberately exposes only an opaque, one-shot reservation.  An
attempt reserves the complete remaining allowance before any helper process is
spawned.  Callers must then prove one of three finite endings:

* a clean, output-free pre-read miss or denial releases the reservation;
* a clean complete read settles the exact payload byte count; or
* every fatal or ambiguous outcome consumes the reservation and makes the
  logical budget terminal.

Leaving the reservation context without one of the clean endings is the fatal
case by default, including when a :class:`BaseException` crosses the boundary.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
)

DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES: Final = 512 * 1024 * 1024
MAX_LOCAL_RESOLUTION_MAX_READ_BYTES: Final = DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES
DEFAULT_LOCAL_RECOVERY_MAX_FILES: Final = 4096
MAX_LOCAL_RECOVERY_MAX_FILES: Final = DEFAULT_LOCAL_RECOVERY_MAX_FILES
DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES: Final = 4096
MAX_LOCAL_RECOVERY_MAX_DIRECTORIES: Final = DEFAULT_LOCAL_RECOVERY_MAX_DIRECTORIES
DEFAULT_LOCAL_RESOLUTION_DEADLINE_SECONDS: Final = DEFAULT_PROCESS_DEADLINE_SECONDS
MAX_LOCAL_RESOLUTION_DEADLINE_SECONDS: Final = MAX_PROCESS_DEADLINE_SECONDS

_INVALID_LIMITS_DETAIL: Final = "Local resolution limits are invalid."
_INVALID_BUDGET_DETAIL: Final = "Local resolution budget state is invalid."
_UNAVAILABLE_BUDGET_DETAIL: Final = "Local resolution budget is unavailable."
_RESERVATION_FACTORY_TOKEN: Final = object()

Clock = Callable[[], float]


class LocalResolutionBudgetError(RuntimeError):
    """A redacted invalid, exhausted, stale, or forged budget operation."""


@dataclass(frozen=True, slots=True)
class LocalResolutionLimits:
    """Validated hard limits for one logical local artifact resolution."""

    max_read_bytes: int = DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES
    deadline_seconds: float = DEFAULT_LOCAL_RESOLUTION_DEADLINE_SECONDS

    def __post_init__(self) -> None:
        """Reject coercible and subclassed values at direct construction."""

        if (
            type(self.max_read_bytes) is not int
            or not 1 <= self.max_read_bytes <= MAX_LOCAL_RESOLUTION_MAX_READ_BYTES
        ):
            raise ValueError(_INVALID_LIMITS_DETAIL)
        if type(self.deadline_seconds) not in (int, float):
            raise ValueError(_INVALID_LIMITS_DETAIL)
        try:
            deadline_seconds = float(self.deadline_seconds)
        except (OverflowError, ValueError):
            raise ValueError(_INVALID_LIMITS_DETAIL) from None
        if (
            not math.isfinite(deadline_seconds)
            or deadline_seconds <= 0
            or deadline_seconds > MAX_LOCAL_RESOLUTION_DEADLINE_SECONDS
        ):
            raise ValueError(_INVALID_LIMITS_DETAIL)
        object.__setattr__(self, "deadline_seconds", deadline_seconds)


_DEFAULT_LOCAL_RESOLUTION_LIMITS: Final = LocalResolutionLimits()


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class LocalResolutionReservation:
    """Opaque one-shot reservation of a budget's complete remaining bytes."""

    _budget: LocalResolutionBudget

    def __init__(
        self,
        budget: LocalResolutionBudget,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _RESERVATION_FACTORY_TOKEN:
            raise TypeError(_INVALID_BUDGET_DETAIL)
        object.__setattr__(self, "_budget", budget)

    @property
    def allowance_bytes(self) -> int:
        """Return the exact byte ceiling reserved for this one attempt."""

        return self._budget._allowance_for(self)

    def __enter__(self) -> LocalResolutionReservation:
        """Assert that this exact reservation is still the active one."""

        self._budget._assert_active(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        """Consume any reservation not already ended by a clean operation."""

        del exc_type, exc_value, traceback
        self._budget._consume_if_active(self)
        return False

    def release_clean_zero(self, *, stdout_bytes: int) -> None:
        """Release only a clean pre-read outcome that emitted zero bytes."""

        if type(stdout_bytes) is not int or stdout_bytes != 0:
            self._reject_and_consume()
        self._budget._release(self)

    def settle_clean(self, *, payload_bytes: int) -> None:
        """Debit the exact validated bytes from one clean complete read."""

        if (
            type(payload_bytes) is not int
            or payload_bytes < 0
            or payload_bytes > self.allowance_bytes
        ):
            self._reject_and_consume()
        self._budget._settle(self, payload_bytes)

    def consume_terminal(self) -> None:
        """Consume this reservation after a fatal or ambiguous attempt."""

        self._budget._consume(self)

    def _reject_and_consume(self) -> None:
        self._budget._consume(self)
        raise LocalResolutionBudgetError(_INVALID_BUDGET_DETAIL)

    def __repr__(self) -> str:
        return "LocalResolutionReservation(<redacted>)"


class LocalResolutionBudget:
    """Identity-stable owner of one absolute deadline and cumulative allowance."""

    __slots__ = (
        "_active",
        "_deadline",
        "_lock",
        "_remaining_bytes",
        "_reserved_bytes",
        "_terminal",
    )

    def __init__(
        self,
        limits: LocalResolutionLimits = _DEFAULT_LOCAL_RESOLUTION_LIMITS,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        if type(limits) is not LocalResolutionLimits:
            raise TypeError(_INVALID_LIMITS_DETAIL)
        self._deadline = ProcessDeadline.after(
            limits.deadline_seconds,
            clock=clock,
        )
        self._remaining_bytes = limits.max_read_bytes
        self._reserved_bytes = 0
        self._terminal = False
        self._active: LocalResolutionReservation | None = None
        self._lock = threading.Lock()

    @property
    def deadline(self) -> ProcessDeadline:
        """Return the one deadline object owned by this logical budget."""

        return self._deadline

    @property
    def remaining_bytes(self) -> int:
        """Return unreserved bytes; an active full reservation leaves zero."""

        with self._lock:
            return self._remaining_bytes

    @property
    def terminal(self) -> bool:
        """Return whether a fatal or ambiguous attempt ended this budget."""

        with self._lock:
            return self._terminal

    def reserve(self) -> LocalResolutionReservation:
        """Reserve the complete remaining allowance for exactly one attempt."""

        try:
            self._deadline.check()
        except BaseException:
            self.abort_terminal()
            raise

        with self._lock:
            if self._terminal or self._active is not None or self._remaining_bytes <= 0:
                raise LocalResolutionBudgetError(_UNAVAILABLE_BUDGET_DETAIL)
            reservation = LocalResolutionReservation(
                self,
                _factory_token=_RESERVATION_FACTORY_TOKEN,
            )
            self._reserved_bytes = self._remaining_bytes
            self._remaining_bytes = 0
            self._active = reservation
            return reservation

    def abort_terminal(self) -> None:
        """Irrevocably consume the budget after an ambiguous port outcome.

        This operation is deliberately idempotent and also clears a leaked
        active reservation.  Application code uses it when a reader returns or
        raises without proving a valid clean reservation ending.
        """

        with self._lock:
            self._remaining_bytes = 0
            self._reserved_bytes = 0
            self._terminal = True
            self._active = None

    def _assert_active(self, reservation: LocalResolutionReservation) -> None:
        with self._lock:
            if self._terminal or self._active is not reservation:
                raise LocalResolutionBudgetError(_INVALID_BUDGET_DETAIL)

    def _allowance_for(
        self,
        reservation: LocalResolutionReservation,
    ) -> int:
        with self._lock:
            if self._terminal or self._active is not reservation:
                raise LocalResolutionBudgetError(_INVALID_BUDGET_DETAIL)
            return self._reserved_bytes

    def _release(self, reservation: LocalResolutionReservation) -> None:
        with self._lock:
            if self._terminal or self._active is not reservation:
                raise LocalResolutionBudgetError(_INVALID_BUDGET_DETAIL)
            self._remaining_bytes = self._reserved_bytes
            self._reserved_bytes = 0
            self._active = None

    def _settle(
        self,
        reservation: LocalResolutionReservation,
        payload_bytes: int,
    ) -> None:
        with self._lock:
            if self._terminal or self._active is not reservation:
                raise LocalResolutionBudgetError(_INVALID_BUDGET_DETAIL)
            self._remaining_bytes = self._reserved_bytes - payload_bytes
            self._reserved_bytes = 0
            self._active = None

    def _consume(self, reservation: LocalResolutionReservation) -> None:
        with self._lock:
            if self._terminal or self._active is not reservation:
                raise LocalResolutionBudgetError(_INVALID_BUDGET_DETAIL)
            self._remaining_bytes = 0
            self._reserved_bytes = 0
            self._terminal = True
            self._active = None

    def _consume_if_active(
        self,
        reservation: LocalResolutionReservation,
    ) -> None:
        with self._lock:
            if self._active is reservation:
                self._remaining_bytes = 0
                self._reserved_bytes = 0
                self._terminal = True
                self._active = None

    def __repr__(self) -> str:
        return "LocalResolutionBudget(<redacted>)"
