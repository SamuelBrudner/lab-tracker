"""Small in-process rate limiter for authentication endpoints."""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from threading import Lock

from lab_tracker.errors import RateLimitError

# Keys embed attacker-chosen input (usernames, token digests), so both the
# number of buckets and the size of each key are bounded.
DEFAULT_RATE_LIMIT_MAX_BUCKETS = 10_000
MAX_RATE_LIMIT_KEY_LENGTH = 256
_HASHED_KEY_PREFIX = "sha256:"


@dataclass
class _Bucket:
    attempts: int
    reset_at: float


class InMemoryRateLimiter:
    """Fixed-window limiter keyed by caller and endpoint.

    This deliberately avoids becoming account/security infrastructure. It
    protects local and small-lab deployments from unbounded auth probing; larger
    deployments should still add edge/proxy throttling.

    Memory is bounded: buckets are kept in window-start order, expired buckets
    are pruned whenever a new one is created, and once ``max_buckets`` live
    buckets exist the oldest is evicted. Keys longer than
    ``MAX_RATE_LIMIT_KEY_LENGTH`` are stored as their SHA-256 digest.
    """

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        max_buckets: int = DEFAULT_RATE_LIMIT_MAX_BUCKETS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_buckets < 1:
            raise ValueError("Rate limiter max_buckets must be at least 1.")
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self.max_buckets = int(max_buckets)
        self._clock = clock
        # Ordered oldest window first. Every (re)created bucket is appended with
        # reset_at = now + window, and the monotonic clock never decreases, so the
        # order is also ascending reset_at.
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._lock = Lock()

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def stored_keys(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._buckets))

    def check(self, key: str) -> None:
        with self._lock:
            bucket = self._current_bucket(key)
            if bucket.attempts >= self.max_attempts:
                raise RateLimitError("Too many authentication attempts. Try again later.")

    def record_failure(self, key: str) -> None:
        with self._lock:
            bucket = self._current_bucket(key)
            bucket.attempts += 1

    def record_attempt(self, key: str) -> None:
        with self._lock:
            bucket = self._current_bucket(key)
            if bucket.attempts >= self.max_attempts:
                raise RateLimitError("Too many authentication attempts. Try again later.")
            bucket.attempts += 1

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(_storage_key(key), None)

    def _current_bucket(self, key: str) -> _Bucket:
        stored_key = _storage_key(key)
        now = self._clock()
        bucket = self._buckets.get(stored_key)
        if bucket is not None and bucket.reset_at > now:
            return bucket
        if bucket is not None:
            del self._buckets[stored_key]
        self._prune_expired(now)
        while len(self._buckets) >= self.max_buckets:
            self._buckets.popitem(last=False)
        bucket = _Bucket(attempts=0, reset_at=now + self.window_seconds)
        self._buckets[stored_key] = bucket
        return bucket

    def _prune_expired(self, now: float) -> None:
        while self._buckets:
            oldest = next(iter(self._buckets.values()))
            if oldest.reset_at > now:
                return
            self._buckets.popitem(last=False)


def _storage_key(key: str) -> str:
    if len(key) <= MAX_RATE_LIMIT_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{_HASHED_KEY_PREFIX}{digest}"
