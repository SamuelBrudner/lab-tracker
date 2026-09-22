"""Unit tests for the in-process authentication rate limiter."""

from __future__ import annotations

import pytest

from lab_tracker.errors import RateLimitError
from lab_tracker.rate_limit import MAX_RATE_LIMIT_KEY_LENGTH, InMemoryRateLimiter


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> _Clock:
    return _Clock()


def test_live_key_is_limited_until_its_window_expires(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=2, window_seconds=60, clock=clock)

    limiter.record_failure("login:10.0.0.1:alice")
    limiter.record_failure("login:10.0.0.1:alice")

    with pytest.raises(RateLimitError):
        limiter.check("login:10.0.0.1:alice")
    limiter.check("login:10.0.0.1:bob")

    clock.now += 60
    limiter.check("login:10.0.0.1:alice")


def test_expired_buckets_are_pruned_when_new_keys_arrive(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=5, window_seconds=60, clock=clock)
    for index in range(100):
        limiter.record_failure(f"login:10.0.0.1:user-{index}")
    assert limiter.bucket_count == 100

    clock.now += 61
    limiter.record_failure("login:10.0.0.1:fresh")

    assert limiter.bucket_count == 1


def test_bucket_count_is_capped_by_evicting_the_oldest_unblocked(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=2, window_seconds=60, max_buckets=3, clock=clock)
    limiter.record_failure("oldest")
    clock.now += 1
    limiter.record_failure("middle")
    clock.now += 1
    limiter.record_failure("newest")

    limiter.record_failure("fourth")

    assert limiter.bucket_count == 3
    assert list(limiter.stored_keys()) == ["middle", "newest", "fourth"]


def test_blocked_bucket_survives_a_flood_of_distinct_keys(clock: _Clock) -> None:
    """Flooding junk keys from one host must not lift that host's own block."""
    limiter = InMemoryRateLimiter(max_attempts=3, window_seconds=60, max_buckets=5, clock=clock)
    target = "login:10.0.0.9:alice"
    for _ in range(3):
        limiter.record_failure(target)

    for index in range(1000):
        clock.now += 0.01
        limiter.check(f"login:10.0.0.9:junk-{index}")
        limiter.record_failure(f"login:10.0.0.9:junk-{index}")
        assert limiter.bucket_count <= 5

    with pytest.raises(RateLimitError):
        limiter.check(target)


def test_new_failures_fail_closed_when_every_live_bucket_is_blocked(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=1, window_seconds=60, max_buckets=2, clock=clock)
    limiter.record_failure("first")
    limiter.record_failure("second")

    with pytest.raises(RateLimitError):
        limiter.record_failure("third")
    with pytest.raises(RateLimitError):
        limiter.record_attempt("third")

    # Nothing was evicted, and an untracked key can still be checked, so a
    # correct credential is never refused by a saturated limiter.
    assert sorted(limiter.stored_keys()) == ["first", "second"]
    limiter.check("third")
    with pytest.raises(RateLimitError):
        limiter.check("first")

    clock.now += 60
    limiter.record_failure("third")
    assert list(limiter.stored_keys()) == ["third"]


def test_check_does_not_allocate_a_bucket(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=2, window_seconds=60, clock=clock)

    for index in range(100):
        limiter.check(f"login:10.0.0.1:user-{index}")

    assert limiter.bucket_count == 0


def test_capped_limiter_keeps_limiting_live_keys_under_the_cap(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=2, window_seconds=60, max_buckets=3, clock=clock)
    limiter.record_failure("a")
    limiter.record_failure("a")
    limiter.record_failure("b")

    with pytest.raises(RateLimitError):
        limiter.check("a")
    limiter.check("b")
    assert limiter.bucket_count == 2


def test_touching_an_expired_key_restarts_its_window_as_the_newest(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=2, window_seconds=60, max_buckets=2, clock=clock)
    limiter.record_failure("first")
    clock.now += 30
    limiter.record_failure("second")
    clock.now += 31  # "first" expired, "second" still live
    limiter.record_failure("first")  # new window for "first"
    limiter.record_failure("third")  # prunes nothing live; evicts oldest live ("second")

    assert list(limiter.stored_keys()) == ["first", "third"]
    limiter.record_failure("first")
    with pytest.raises(RateLimitError):
        limiter.check("first")


def test_reset_removes_the_bucket(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=1, window_seconds=60, clock=clock)
    limiter.record_failure("login:10.0.0.1:alice")

    limiter.reset("login:10.0.0.1:alice")

    limiter.check("login:10.0.0.1:alice")
    assert limiter.bucket_count == 0


def test_long_keys_are_stored_as_bounded_digests(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(max_attempts=1, window_seconds=60, clock=clock)
    long_key = "login:10.0.0.1:" + "a" * 100_000
    other_long_key = "login:10.0.0.1:" + "a" * 99_999 + "b"

    limiter.record_failure(long_key)

    with pytest.raises(RateLimitError):
        limiter.check(long_key)
    limiter.check(other_long_key)
    assert all(len(key) <= MAX_RATE_LIMIT_KEY_LENGTH for key in limiter.stored_keys())
    limiter.reset(long_key)
    limiter.check(long_key)


def test_limiter_rejects_a_non_positive_bucket_cap() -> None:
    with pytest.raises(ValueError, match="max_buckets"):
        InMemoryRateLimiter(max_attempts=1, window_seconds=60, max_buckets=0)
