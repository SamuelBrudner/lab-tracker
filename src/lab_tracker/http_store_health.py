"""Pinned, SSRF-safe HTTP data-store health checks."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final

from lab_tracker.models import StoreKind
from lab_tracker.outbound_http import (
    DEFAULT_MAX_HTTP_REDIRECTS,
    DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS,
    HTTP_REDIRECT_STATUS_CODES,
    MAX_OUTBOUND_HTTP_DEADLINE_SECONDS,
    ApprovedHttpTarget,
    OutboundHttpClient,
    OutboundHttpDeadline,
    OutboundHttpPolicy,
    OutboundHttpResponse,
    RegisteredHttpPrefix,
    resolve_direct_http_redirect,
)
from lab_tracker.store_health import (
    HTTP_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)

_HEALTHY_TERMINAL_STATUS_CODES: Final = frozenset({403, 405})


@dataclass(frozen=True, slots=True)
class HttpStoreHealthProbe:
    """Probe one registered HTTP store through the approved pinned transport."""

    policy: OutboundHttpPolicy
    client: OutboundHttpClient
    deadline_seconds: float = DEFAULT_OUTBOUND_HTTP_DEADLINE_SECONDS
    clock: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )
    max_redirects: int = DEFAULT_MAX_HTTP_REDIRECTS

    def __post_init__(self) -> None:
        if isinstance(self.deadline_seconds, bool) or not isinstance(
            self.deadline_seconds,
            (int, float),
        ):
            raise TypeError("deadline_seconds must be a number.")
        normalized_deadline = float(self.deadline_seconds)
        if (
            not math.isfinite(normalized_deadline)
            or normalized_deadline <= 0.0
            or normalized_deadline > MAX_OUTBOUND_HTTP_DEADLINE_SECONDS
        ):
            raise ValueError(
                "deadline_seconds must be finite, positive, and no greater than "
                f"{MAX_OUTBOUND_HTTP_DEADLINE_SECONDS:g}."
            )
        if isinstance(self.max_redirects, bool) or not isinstance(
            self.max_redirects,
            int,
        ):
            raise TypeError("max_redirects must be an integer.")
        if self.max_redirects < 0 or self.max_redirects > DEFAULT_MAX_HTTP_REDIRECTS:
            raise ValueError(
                "max_redirects must be between 0 and "
                f"{DEFAULT_MAX_HTTP_REDIRECTS}."
            )
        if not callable(self.clock):
            raise TypeError("clock must be callable.")
        object.__setattr__(self, "deadline_seconds", normalized_deadline)

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        """Return a redacted reachability result without exposing probe failures."""

        try:
            return self._probe(target)
        except Exception:
            return _unreachable()

    def _probe(self, target: StoreProbeTarget) -> StoreHealth:
        if target.kind is not StoreKind.HTTP:
            return _unreachable()

        deadline = OutboundHttpDeadline.after(
            self.deadline_seconds,
            clock=self.clock,
        )
        deadline.check()
        selected_base = (
            target.endpoint if target.endpoint is not None else target.root
        )
        prefix = RegisteredHttpPrefix.parse(selected_base)
        if prefix is None:
            return _unreachable()

        current_url = prefix.canonical_url
        seen_targets: set[str] = set()
        for redirect_count in range(self.max_redirects + 1):
            deadline.check()
            approved = self.policy.authorize(current_url, deadline=deadline)
            deadline.check()
            if approved.absolute_url in seen_targets:
                return _unreachable()
            seen_targets.add(approved.absolute_url)

            response = self.client.open(
                "HEAD",
                approved,
                deadline=deadline,
            )
            try:
                result, next_url = self._inspect_response(
                    response,
                    approved,
                    redirect_count=redirect_count,
                    deadline=deadline,
                )
            except BaseException:
                with suppress(BaseException):
                    response.close()
                raise
            response.close()
            deadline.check()
            if result is not None:
                return result
            if next_url is None:  # pragma: no cover - inspection invariant
                return _unreachable()
            current_url = next_url

        return _unreachable()

    def _inspect_response(
        self,
        response: OutboundHttpResponse,
        approved: ApprovedHttpTarget,
        *,
        redirect_count: int,
        deadline: OutboundHttpDeadline,
    ) -> tuple[StoreHealth | None, str | None]:
        deadline.check()
        status_code = response.status_code
        deadline.check()
        if status_code in HTTP_REDIRECT_STATUS_CODES:
            location = response.get_header("location")
            deadline.check()
            if not location or redirect_count >= self.max_redirects:
                return _unreachable(), None
            next_url = resolve_direct_http_redirect(
                approved.absolute_url,
                location,
            )
            deadline.check()
            if next_url is None:
                return _unreachable(), None
            return None, next_url
        if (
            200 <= status_code < 300
            or status_code in _HEALTHY_TERMINAL_STATUS_CODES
        ):
            return StoreHealth(StoreHealthStatus.HEALTHY), None
        return _unreachable(), None


def _unreachable() -> StoreHealth:
    return StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        HTTP_STORE_HEALTH_FAILURE_DETAIL,
    )
