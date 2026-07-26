from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.bounded_subprocess import (
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadlineExceeded,
)
from lab_tracker.local_filesystem_ports import (
    DirectLocalRegularFileTarget,
    LocalRegularFileReader,
    LocalRegularFileReadOutcome,
    LocalRegularFileReadResult,
    RegisteredLocalRegularFileTarget,
)
from lab_tracker.local_resolution_budget import (
    DEFAULT_LOCAL_RESOLUTION_DEADLINE_SECONDS,
    DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES,
    MAX_LOCAL_RESOLUTION_MAX_READ_BYTES,
    LocalResolutionBudget,
    LocalResolutionBudgetError,
    LocalResolutionLimits,
    LocalResolutionReservation,
)


class _Clock:
    def __init__(self, now: float = 10.0) -> None:
        self.now = now
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now


class _FatalSignal(BaseException):
    pass


def test_limits_default_to_historical_recovery_cap_and_process_deadline() -> None:
    limits = LocalResolutionLimits()

    assert limits.max_read_bytes == 512 * 1024 * 1024
    assert limits.max_read_bytes == DEFAULT_LOCAL_RESOLUTION_MAX_READ_BYTES
    assert limits.max_read_bytes == MAX_LOCAL_RESOLUTION_MAX_READ_BYTES
    assert limits.deadline_seconds == DEFAULT_LOCAL_RESOLUTION_DEADLINE_SECONDS


@pytest.mark.parametrize(
    "max_read_bytes",
    [
        True,
        0,
        -1,
        1.0,
        "1",
        MAX_LOCAL_RESOLUTION_MAX_READ_BYTES + 1,
    ],
)
def test_limits_reject_non_exact_or_out_of_range_byte_caps(
    max_read_bytes: object,
) -> None:
    with pytest.raises(ValueError, match=r"^Local resolution limits are invalid\.$"):
        LocalResolutionLimits(max_read_bytes=max_read_bytes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "deadline_seconds",
    [
        True,
        0,
        -1,
        float("nan"),
        float("inf"),
        "1",
        10**1_000,
        MAX_PROCESS_DEADLINE_SECONDS + 1,
    ],
)
def test_limits_reject_non_exact_or_out_of_range_deadlines(
    deadline_seconds: object,
) -> None:
    with pytest.raises(ValueError, match=r"^Local resolution limits are invalid\.$"):
        LocalResolutionLimits(deadline_seconds=deadline_seconds)  # type: ignore[arg-type]


def test_limits_normalize_an_exact_integer_deadline_and_are_immutable() -> None:
    limits = LocalResolutionLimits(max_read_bytes=1, deadline_seconds=2)

    assert limits.deadline_seconds == 2.0
    assert type(limits.deadline_seconds) is float
    with pytest.raises(FrozenInstanceError):
        limits.max_read_bytes = 2  # type: ignore[misc]


def test_budget_owns_one_identity_stable_deadline() -> None:
    clock = _Clock()
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=12, deadline_seconds=4),
        clock=clock,
    )

    deadline = budget.deadline

    assert clock.calls == 1
    assert budget.deadline is deadline
    assert deadline.expires_at == 14.0
    assert budget.remaining_bytes == 12
    assert budget.terminal is False


def test_reservation_withholds_all_remaining_bytes_until_clean_release() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with budget.reserve() as reservation:
        assert reservation.allowance_bytes == 12
        assert budget.remaining_bytes == 0
        with pytest.raises(
            LocalResolutionBudgetError,
            match=r"^Local resolution budget is unavailable\.$",
        ):
            budget.reserve()
        reservation.release_clean_zero(stdout_bytes=0)

    assert budget.remaining_bytes == 12
    assert budget.terminal is False


def test_clean_settlement_debits_exact_payload_and_next_attempt_gets_remainder() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with budget.reserve() as first:
        first.settle_clean(payload_bytes=5)

    assert budget.remaining_bytes == 7
    with budget.reserve() as second:
        assert second.allowance_bytes == 7
        second.settle_clean(payload_bytes=7)

    assert budget.remaining_bytes == 0
    assert budget.terminal is False
    with pytest.raises(LocalResolutionBudgetError):
        budget.reserve()


@pytest.mark.parametrize("payload_bytes", [True, -1, 13, 1.0, "1"])
def test_invalid_clean_settlement_consumes_terminally(
    payload_bytes: object,
) -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with (
        pytest.raises(
            LocalResolutionBudgetError,
            match=r"^Local resolution budget state is invalid\.$",
        ),
        budget.reserve() as reservation,
    ):
        reservation.settle_clean(payload_bytes=payload_bytes)  # type: ignore[arg-type]

    assert budget.remaining_bytes == 0
    assert budget.terminal is True


@pytest.mark.parametrize("stdout_bytes", [True, -1, 1, 1.0, "0"])
def test_nonzero_or_nonexact_release_consumes_terminally(
    stdout_bytes: object,
) -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with (
        pytest.raises(
            LocalResolutionBudgetError,
            match=r"^Local resolution budget state is invalid\.$",
        ),
        budget.reserve() as reservation,
    ):
        reservation.release_clean_zero(stdout_bytes=stdout_bytes)  # type: ignore[arg-type]

    assert budget.remaining_bytes == 0
    assert budget.terminal is True


def test_explicit_fatal_consumption_is_terminal() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with budget.reserve() as reservation:
        reservation.consume_terminal()

    assert budget.remaining_bytes == 0
    assert budget.terminal is True
    with pytest.raises(LocalResolutionBudgetError):
        budget.reserve()


def test_normal_unsettled_context_exit_consumes_terminally() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with budget.reserve():
        pass

    assert budget.remaining_bytes == 0
    assert budget.terminal is True


def test_base_exception_exit_consumes_without_swallowing_primary() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    signal = _FatalSignal()

    with pytest.raises(_FatalSignal) as caught, budget.reserve():
        raise signal

    assert caught.value is signal
    assert budget.remaining_bytes == 0
    assert budget.terminal is True


def test_deadline_expiry_terminally_disables_budget_before_reservation() -> None:
    clock = _Clock()
    budget = LocalResolutionBudget(
        LocalResolutionLimits(max_read_bytes=12, deadline_seconds=1),
        clock=clock,
    )
    clock.now = 11.0

    with pytest.raises(ProcessDeadlineExceeded):
        budget.reserve()

    assert budget.remaining_bytes == 0
    assert budget.terminal is True


def test_explicit_abort_is_idempotent_after_a_clean_settlement() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    with budget.reserve() as reservation:
        reservation.settle_clean(payload_bytes=4)

    assert budget.remaining_bytes == 8
    budget.abort_terminal()
    budget.abort_terminal()

    assert budget.remaining_bytes == 0
    assert budget.terminal is True
    with pytest.raises(LocalResolutionBudgetError):
        budget.reserve()


def test_explicit_abort_invalidates_a_leaked_active_reservation() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    reservation = budget.reserve()

    budget.abort_terminal()

    assert budget.remaining_bytes == 0
    assert budget.terminal is True
    with pytest.raises(LocalResolutionBudgetError):
        reservation.consume_terminal()


def test_stale_and_double_finalization_are_rejected_without_touching_new_attempt() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    first = budget.reserve()
    first.release_clean_zero(stdout_bytes=0)
    second = budget.reserve()

    with pytest.raises(
        LocalResolutionBudgetError,
        match=r"^Local resolution budget state is invalid\.$",
    ):
        first.consume_terminal()

    assert second.allowance_bytes == 12
    assert budget.remaining_bytes == 0
    assert budget.terminal is False
    second.settle_clean(payload_bytes=4)

    with pytest.raises(LocalResolutionBudgetError):
        second.settle_clean(payload_bytes=0)
    assert budget.remaining_bytes == 8
    assert budget.terminal is False


def test_reservation_constructor_rejects_forged_capability() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))

    with pytest.raises(
        TypeError,
        match=r"^Local resolution budget state is invalid\.$",
    ):
        LocalResolutionReservation(budget, _factory_token=object())

    assert budget.remaining_bytes == 12
    assert budget.terminal is False


def test_forged_reservation_cannot_consume_an_active_real_reservation() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    real = budget.reserve()
    forged = object.__new__(LocalResolutionReservation)
    object.__setattr__(forged, "_budget", budget)

    with pytest.raises(
        LocalResolutionBudgetError,
        match=r"^Local resolution budget state is invalid\.$",
    ):
        forged.consume_terminal()

    assert real.allowance_bytes == 12
    assert budget.terminal is False
    real.release_clean_zero(stdout_bytes=0)
    assert budget.remaining_bytes == 12


def test_real_reservation_is_immutable() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    reservation = budget.reserve()

    with pytest.raises(FrozenInstanceError):
        reservation._budget = LocalResolutionBudget()  # type: ignore[misc]

    assert reservation.allowance_bytes == 12
    reservation.release_clean_zero(stdout_bytes=0)


def test_budget_and_reservation_repr_redact_state() -> None:
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=12))
    reservation = budget.reserve()

    assert repr(budget) == "LocalResolutionBudget(<redacted>)"
    assert repr(reservation) == "LocalResolutionReservation(<redacted>)"
    assert "12" not in repr(budget)
    assert "12" not in repr(reservation)

    reservation.consume_terminal()


@pytest.mark.parametrize("candidate", [None, b"path", 1, True])
def test_direct_target_rejects_non_exact_strings(candidate: object) -> None:
    with pytest.raises(TypeError, match=r"^Local regular-file target is invalid\.$"):
        DirectLocalRegularFileTarget(candidate=candidate)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("store_root", "locator"),
    [
        (None, ("artifact",)),
        ("/store", []),
        ("/store", ()),
        ("/store", ("artifact", 1)),
    ],
)
def test_registered_target_rejects_malformed_nominal_values(
    store_root: object,
    locator: object,
) -> None:
    with pytest.raises(TypeError, match=r"^Local regular-file target is invalid\.$"):
        RegisteredLocalRegularFileTarget(  # type: ignore[arg-type]
            store_root=store_root,
            locator=locator,
        )


def test_nominal_targets_do_not_render_sensitive_paths_or_compare_by_value() -> None:
    direct = DirectLocalRegularFileTarget("/secret/direct")
    same_direct = DirectLocalRegularFileTarget("/secret/direct")
    registered = RegisteredLocalRegularFileTarget(
        "/secret/store",
        ("private", "artifact"),
    )

    assert "/secret" not in repr(direct)
    assert "/secret" not in repr(registered)
    assert direct != same_direct


@pytest.mark.parametrize(
    "bytes_read",
    [True, -1, 1.0, "1", MAX_LOCAL_RESOLUTION_MAX_READ_BYTES + 1],
)
def test_read_result_rejects_nonexact_or_out_of_range_counts(
    bytes_read: object,
) -> None:
    with pytest.raises(ValueError, match=r"^Local regular-file result is invalid\.$"):
        LocalRegularFileReadResult(  # type: ignore[arg-type]
            LocalRegularFileReadOutcome.COMPLETE,
            bytes_read,
        )


@pytest.mark.parametrize(
    "outcome",
    [
        LocalRegularFileReadOutcome.MISSING,
        LocalRegularFileReadOutcome.DENIED,
        LocalRegularFileReadOutcome.FAILED,
    ],
)
def test_only_complete_read_results_can_report_nonzero_bytes(
    outcome: LocalRegularFileReadOutcome,
) -> None:
    with pytest.raises(ValueError, match=r"^Local regular-file result is invalid\.$"):
        LocalRegularFileReadResult(outcome, 1)

    assert LocalRegularFileReadResult(outcome, 0).bytes_read == 0


def test_read_result_rejects_forged_outcome() -> None:
    with pytest.raises(TypeError, match=r"^Local regular-file result is invalid\.$"):
        LocalRegularFileReadResult("complete", 0)  # type: ignore[arg-type]


class _Reader:
    def read_regular_file(
        self,
        target: DirectLocalRegularFileTarget | RegisteredLocalRegularFileTarget,
        *,
        budget: LocalResolutionBudget,
        stdout_consumer,
    ) -> LocalRegularFileReadResult:
        del target, budget
        stdout_consumer(b"data")
        return LocalRegularFileReadResult(
            LocalRegularFileReadOutcome.COMPLETE,
            4,
        )


def _accept_reader(reader: LocalRegularFileReader) -> LocalRegularFileReader:
    return reader


def test_reader_port_accepts_exact_budget_and_streaming_consumer() -> None:
    reader = _accept_reader(_Reader())
    budget = LocalResolutionBudget(LocalResolutionLimits(max_read_bytes=4))
    chunks: list[bytes] = []

    result = reader.read_regular_file(
        DirectLocalRegularFileTarget("/artifact"),
        budget=budget,
        stdout_consumer=chunks.append,
    )

    assert result == LocalRegularFileReadResult(
        LocalRegularFileReadOutcome.COMPLETE,
        4,
    )
    assert chunks == [b"data"]
