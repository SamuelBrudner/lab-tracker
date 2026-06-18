"""Small in-process rate limiter for authentication endpoints."""

from __future__ import annotations

import time
from dataclasses import dataclass

from lab_tracker.errors import RateLimitError


@dataclass
class _Bucket:
    attempts: int
    reset_at: float


class InMemoryRateLimiter:
    """Fixed-window limiter keyed by caller and endpoint.

    This deliberately avoids becoming account/security infrastructure. It
    protects local and small-lab deployments from unbounded auth probing; larger
    deployments should still add edge/proxy throttling.
    """

    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str) -> None:
        bucket = self._current_bucket(key)
        if bucket.attempts >= self.max_attempts:
            raise RateLimitError("Too many authentication attempts. Try again later.")

    def record_failure(self, key: str) -> None:
        bucket = self._current_bucket(key)
        bucket.attempts += 1

    def record_attempt(self, key: str) -> None:
        self.check(key)
        self.record_failure(key)

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)

    def _current_bucket(self, key: str) -> _Bucket:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None or bucket.reset_at <= now:
            bucket = _Bucket(attempts=0, reset_at=now + self.window_seconds)
            self._buckets[key] = bucket
        return bucket
