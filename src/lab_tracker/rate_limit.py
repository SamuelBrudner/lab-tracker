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
_LIMITED_MESSAGE = "Too many authentication attempts. Try again later."


@dataclass
class _Bucket:
    attempts: int
    reset_at: float


class InMemoryRateLimiter:
    """Fixed-window limiter keyed by caller and endpoint.

    This deliberately avoids becoming account/security infrastructure. It
    protects local and small-lab deployments from unbounded auth probing; larger
    deployments should still add edge/proxy throttling.

    Memory is bounded without letting eviction lift a block:

    * ``check`` never allocates; only recorded failures create buckets.
    * Buckets are kept in window-start order and expired ones are pruned
      whenever a new bucket is needed.
    * Once ``max_buckets`` live buckets exist, the oldest bucket that is not yet
      blocking is evicted. Blocked buckets are never evicted, so flooding
      distinct keys cannot clear a key that has used up its attempts.
    * If every live bucket is blocking, a failure for a new key fails closed
      with ``RateLimitError`` until the oldest window expires. Keys without a
      bucket still pass ``check``, so correct credentials keep working.

    Keys longer than ``MAX_RATE_LIMIT_KEY_LENGTH`` are stored as their SHA-256
    digest.
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
        # Keys of live buckets that are not yet blocking, in the same order.
        # Attempts only grow within a window, so a key leaves this set once and
        # never returns until its bucket is recreated.
        self._evictable: OrderedDict[str, None] = OrderedDict()
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
            bucket = self._live_bucket(_storage_key(key))
            if bucket is not None and self._is_blocked(bucket):
                raise RateLimitError(_LIMITED_MESSAGE)

    def record_failure(self, key: str) -> None:
        with self._lock:
            stored_key = _storage_key(key)
            self._increment(stored_key, self._bucket_for_update(stored_key))

    def record_attempt(self, key: str) -> None:
        with self._lock:
            stored_key = _storage_key(key)
            bucket = self._bucket_for_update(stored_key)
            if self._is_blocked(bucket):
                raise RateLimitError(_LIMITED_MESSAGE)
            self._increment(stored_key, bucket)

    def reset(self, key: str) -> None:
        with self._lock:
            self._discard(_storage_key(key))

    def _is_blocked(self, bucket: _Bucket) -> bool:
        return bucket.attempts >= self.max_attempts

    def _increment(self, stored_key: str, bucket: _Bucket) -> None:
        bucket.attempts += 1
        if self._is_blocked(bucket):
            self._evictable.pop(stored_key, None)

    def _live_bucket(self, stored_key: str) -> _Bucket | None:
        bucket = self._buckets.get(stored_key)
        if bucket is None:
            return None
        if bucket.reset_at <= self._clock():
            self._discard(stored_key)
            return None
        return bucket

    def _bucket_for_update(self, stored_key: str) -> _Bucket:
        bucket = self._live_bucket(stored_key)
        if bucket is not None:
            return bucket
        now = self._clock()
        self._prune_expired(now)
        if len(self._buckets) >= self.max_buckets:
            if not self._evictable:
                # Every live bucket is blocking; evicting one would lift a block.
                raise RateLimitError(_LIMITED_MESSAGE)
            evicted_key, _ = self._evictable.popitem(last=False)
            del self._buckets[evicted_key]
        bucket = _Bucket(attempts=0, reset_at=now + self.window_seconds)
        self._buckets[stored_key] = bucket
        self._evictable[stored_key] = None
        return bucket

    def _discard(self, stored_key: str) -> None:
        self._buckets.pop(stored_key, None)
        self._evictable.pop(stored_key, None)

    def _prune_expired(self, now: float) -> None:
        while self._buckets:
            oldest_key, oldest = next(iter(self._buckets.items()))
            if oldest.reset_at > now:
                return
            self._discard(oldest_key)


def _storage_key(key: str) -> str:
    if len(key) <= MAX_RATE_LIMIT_KEY_LENGTH:
        return key
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{_HASHED_KEY_PREFIX}{digest}"
