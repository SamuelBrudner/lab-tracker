"""Unit tests for the in-process authentication rate limiter."""

from __future__ import annotations

import pytest
from starlette.requests import HTTPConnection

from lab_tracker.errors import RateLimitError
from lab_tracker.rate_limit import (
    MAX_RATE_LIMIT_KEY_LENGTH,
    InMemoryRateLimiter,
    rate_limit_client,
)


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


def test_limiter_rejects_an_invalid_per_client_bucket_cap() -> None:
    with pytest.raises(ValueError, match="max_buckets_per_client"):
        InMemoryRateLimiter(
            max_attempts=1, window_seconds=60, max_buckets=4, max_buckets_per_client=0
        )
    with pytest.raises(ValueError, match="max_buckets_per_client"):
        InMemoryRateLimiter(
            max_attempts=1, window_seconds=60, max_buckets=4, max_buckets_per_client=5
        )


def test_one_client_filling_its_quota_is_limited_without_saturating_others(
    clock: _Clock,
) -> None:
    """A single host that blocks its whole quota is limited itself, not the table."""
    limiter = InMemoryRateLimiter(
        max_attempts=1,
        window_seconds=60,
        max_buckets=4,
        max_buckets_per_client=2,
        clock=clock,
    )
    limiter.record_failure("login:attacker:a1", client="attacker")
    limiter.record_failure("login:attacker:a2", client="attacker")

    for index in range(50):
        with pytest.raises(RateLimitError):
            limiter.record_failure(f"login:attacker:junk-{index}", client="attacker")
    with pytest.raises(RateLimitError):
        limiter.record_attempt("register:attacker", client="attacker")

    # Other hosts still get their own buckets, and the attacker's blocks hold.
    limiter.record_failure("login:other:alice", client="other")
    limiter.record_attempt("register:other", client="other")
    assert limiter.bucket_count == 4
    with pytest.raises(RateLimitError):
        limiter.check("login:attacker:a1")
    with pytest.raises(RateLimitError):
        limiter.check("login:other:alice")

    clock.now += 60
    limiter.record_failure("login:attacker:a3", client="attacker")
    assert list(limiter.stored_keys()) == ["login:attacker:a3"]


def test_client_at_quota_fails_closed_without_evicting_its_own_buckets(
    clock: _Clock,
) -> None:
    """A client cannot flush its own partly used bucket by failing junk keys.

    Otherwise a host could make max_attempts - 1 guesses for one username, fail
    enough distinct junk usernames to evict that bucket, and guess again.
    """
    limiter = InMemoryRateLimiter(
        max_attempts=3,
        window_seconds=60,
        max_buckets=10,
        max_buckets_per_client=2,
        clock=clock,
    )
    limiter.record_failure("other-1", client="other")
    limiter.record_failure("login:mine:alice", client="mine")
    limiter.record_failure("login:mine:alice", client="mine")
    limiter.record_failure("login:mine:junk-0", client="mine")

    for index in range(1, 20):
        with pytest.raises(RateLimitError):
            limiter.record_failure(f"login:mine:junk-{index}", client="mine")

    assert list(limiter.stored_keys()) == [
        "other-1",
        "login:mine:alice",
        "login:mine:junk-0",
    ]
    limiter.check("login:mine:alice")  # correct credentials still get through
    limiter.record_failure("login:mine:alice", client="mine")
    with pytest.raises(RateLimitError):
        limiter.check("login:mine:alice")
    limiter.record_failure("other-2", client="other")


def test_per_client_quota_is_released_when_buckets_expire_or_reset(clock: _Clock) -> None:
    limiter = InMemoryRateLimiter(
        max_attempts=1,
        window_seconds=60,
        max_buckets=10,
        max_buckets_per_client=2,
        clock=clock,
    )
    limiter.record_failure("a", client="host")
    limiter.record_failure("b", client="host")
    with pytest.raises(RateLimitError):
        limiter.record_failure("c", client="host")

    limiter.reset("a")
    limiter.record_failure("c", client="host")
    with pytest.raises(RateLimitError):
        limiter.record_failure("d", client="host")

    clock.now += 60
    limiter.check("b")  # touching an expired bucket discards it
    limiter.record_failure("d", client="host")
    limiter.record_failure("e", client="host")
    assert sorted(limiter.stored_keys()) == ["d", "e"]


def test_global_cap_still_fails_closed_when_many_clients_block_their_quota(
    clock: _Clock,
) -> None:
    limiter = InMemoryRateLimiter(
        max_attempts=1,
        window_seconds=60,
        max_buckets=4,
        max_buckets_per_client=2,
        clock=clock,
    )
    for host in ("h1", "h2"):
        limiter.record_failure(f"{host}-a", client=host)
        limiter.record_failure(f"{host}-b", client=host)

    with pytest.raises(RateLimitError):
        limiter.record_failure("h3-a", client="h3")
    assert limiter.bucket_count == 4


def _connection(client: tuple[str, int] | None) -> HTTPConnection:
    return HTTPConnection({"type": "http", "headers": [], "client": client})


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2::1", "2001:db8:1:2::/64"),
        ("fe80::1%eth0", "fe80::/64"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("testclient", "testclient"),
    ],
)
def test_rate_limit_client_groups_ipv6_peers_by_their_64_prefix(
    peer: str, expected: str
) -> None:
    """One IPv6 /64 is one client: a host there can rotate through 2**64 addresses."""
    assert rate_limit_client(_connection((peer, 40000))) == expected


def test_rate_limit_client_distinguishes_ipv6_prefixes_and_missing_peers() -> None:
    assert rate_limit_client(_connection(("2001:db8:1:2::1", 1))) != rate_limit_client(
        _connection(("2001:db8:1:3::1", 1))
    )
    assert rate_limit_client(_connection(None)) == "unknown"
