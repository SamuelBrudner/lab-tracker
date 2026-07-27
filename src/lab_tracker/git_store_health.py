"""Authorized, bounded Git data-store health checks."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_DEADLINE_SECONDS,
    MAX_PROCESS_DEADLINE_SECONDS,
    ProcessDeadline,
    ProcessExecutor,
)
from lab_tracker.git_process import (
    DEFAULT_GIT_ALLOW_PROTOCOL,
    GIT_PROCESS_METADATA_LIMIT_BYTES,
    build_git_environment,
    git_http_config_args,
    git_remote_preflight_matches,
    run_git_command,
)
from lab_tracker.git_remote_policy import GitRemotePolicy
from lab_tracker.models import StoreKind
from lab_tracker.store_health import (
    GIT_STORE_HEALTH_FAILURE_DETAIL,
    StoreHealth,
    StoreHealthStatus,
    StoreProbeTarget,
)


@dataclass(frozen=True, slots=True)
class GitStoreHealthProbe:
    """Probe one approved Git remote from an app-owned empty work directory."""

    policy: GitRemotePolicy
    executor: ProcessExecutor
    workdir: Path
    binary: str = "git"
    allow_protocol: str | None = DEFAULT_GIT_ALLOW_PROTOCOL
    deadline_seconds: float = DEFAULT_PROCESS_DEADLINE_SECONDS
    clock: Callable[[], float] = field(
        default=time.monotonic,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _validate_binary(self.binary)
        if self.allow_protocol is not None and not isinstance(
            self.allow_protocol,
            str,
        ):
            raise TypeError("allow_protocol must be a string or None.")
        normalized_deadline = _validate_deadline_seconds(self.deadline_seconds)
        if not callable(self.clock):
            raise TypeError("clock must be callable.")
        object.__setattr__(self, "deadline_seconds", normalized_deadline)

    def __call__(self, target: StoreProbeTarget) -> StoreHealth:
        """Return only a static, redacted failure when probing cannot succeed."""

        try:
            return self._probe(target)
        except Exception:
            return _unreachable()

    def _probe(self, target: StoreProbeTarget) -> StoreHealth:
        if target.kind is not StoreKind.GIT:
            return _unreachable()

        # Authorization deliberately precedes even read-only local filesystem
        # inspection, so malformed or unlisted targets have no process or cwd
        # side effects.
        approved = self.policy.authorize(target.root)
        if approved is None:
            return _unreachable()

        workdir = os.path.realpath(os.fspath(self.workdir))
        if not os.path.isdir(workdir):
            return _unreachable()

        deadline = ProcessDeadline.after(
            self.deadline_seconds,
            clock=self.clock,
        )
        environment = build_git_environment(
            self.allow_protocol,
            cwd=workdir,
        )
        config_args = git_http_config_args(approved)

        preflight = run_git_command(
            runner=None,
            executor=self.executor,
            binary=self.binary,
            args=(
                "ls-remote",
                "--get-url",
                "--",
                approved.subprocess_value,
            ),
            cwd=workdir,
            env=environment,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=GIT_PROCESS_METADATA_LIMIT_BYTES,
        )
        if not git_remote_preflight_matches(
            preflight,
            approved.subprocess_value,
        ):
            return _unreachable()

        result = run_git_command(
            runner=None,
            executor=self.executor,
            binary=self.binary,
            args=(
                "ls-remote",
                "--",
                approved.subprocess_value,
                "HEAD",
            ),
            cwd=workdir,
            env=environment,
            config_args=config_args,
            deadline=deadline,
            stdout_limit_bytes=GIT_PROCESS_METADATA_LIMIT_BYTES,
        )
        deadline.check()
        if result.returncode == 0:
            return StoreHealth(StoreHealthStatus.HEALTHY)
        return _unreachable()


def _validate_binary(value: object) -> None:
    if not isinstance(value, str):
        raise TypeError("binary must be a string.")
    if not value or "\0" in value:
        raise ValueError("binary must be a non-empty string without NUL bytes.")


def _validate_deadline_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("deadline_seconds must be a number.")
    normalized = float(value)
    if (
        not math.isfinite(normalized)
        or normalized <= 0.0
        or normalized > MAX_PROCESS_DEADLINE_SECONDS
    ):
        raise ValueError(
            "deadline_seconds must be finite, positive, and no greater than "
            f"{MAX_PROCESS_DEADLINE_SECONDS:g}."
        )
    return normalized


def _unreachable() -> StoreHealth:
    return StoreHealth(
        StoreHealthStatus.UNREACHABLE,
        GIT_STORE_HEALTH_FAILURE_DETAIL,
    )
