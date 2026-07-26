from __future__ import annotations

import math

import pytest
from http_security_fakes import FakeClock

from lab_tracker.outbound_http import (
    OutboundHttpDeadline,
    OutboundHttpDeadlineExceeded,
)


def test_deadline_uses_one_absolute_monotonic_expiry() -> None:
    clock = FakeClock(100.0)
    deadline = OutboundHttpDeadline.after(5.0, clock=clock)

    assert deadline.expires_at == 105.0
    assert deadline.remaining() == 5.0

    clock.advance(3.25)

    assert deadline.remaining() == 1.75
    assert deadline.timeout() == 1.75


def test_expired_deadline_clamps_remaining_and_raises_generic_error() -> None:
    clock = FakeClock()
    deadline = OutboundHttpDeadline.after(1.0, clock=clock)

    clock.advance(1.0)

    assert deadline.remaining() == 0.0
    with pytest.raises(OutboundHttpDeadlineExceeded) as exc_info:
        deadline.check()
    assert str(exc_info.value) == "Outbound HTTP request failed."


@pytest.mark.parametrize("seconds", (0.0, -1.0, math.inf, -math.inf, math.nan))
def test_deadline_rejects_non_positive_or_non_finite_budgets(
    seconds: float,
) -> None:
    with pytest.raises(ValueError):
        OutboundHttpDeadline.after(seconds)
