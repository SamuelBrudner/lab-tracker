"""Shared, bounded process helpers for Git-backed stores.

This module owns the narrow subprocess boundary shared by Git artifact
resolution and Git store-health probes.  Callers remain responsible for
structurally authorizing a remote before invoking these helpers.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from lab_tracker.bounded_subprocess import (
    DEFAULT_PROCESS_STDERR_LIMIT_BYTES,
    ProcessDeadline,
    ProcessExecutionError,
    ProcessExecutor,
)
from lab_tracker.git_remote_policy import ApprovedGitRemote

# Restrict Git transports so a structurally approved remote cannot pivot into
# local reads or remote helpers such as ``file://``, ``ext::``, or ``fd::``.
DEFAULT_GIT_ALLOW_PROTOCOL = "https:ssh:git"
GIT_PROCESS_METADATA_LIMIT_BYTES = 64 * 1024
GIT_PROCESS_STDERR_LIMIT_BYTES = DEFAULT_PROCESS_STDERR_LIMIT_BYTES
GIT_GENERIC_HTTP_REDIRECT_CONFIG = "http.followRedirects=false"

_GENERIC_OUTPUT_LIMIT_DETAIL = "Git process output limit exceeded."


@dataclass(frozen=True)
class GitCompleted:
    """Result of one Git invocation."""

    returncode: int
    stdout: bytes
    stderr: bytes


# A trusted runner takes Git argv without the binary name and returns its result.
GitRunner = Callable[[list[str]], GitCompleted]


class GitProcessOutputLimitExceeded(ProcessExecutionError):
    """A trusted legacy runner returned more data than its declared cap."""


def build_git_environment(
    allow_protocol: str | None,
    *,
    cwd: str,
) -> dict[str, str]:
    """Capture one non-interactive Git environment for a logical operation.

    The snapshot retains operator-owned system and global Git configuration,
    while removing repository-selection variables that could redirect the
    operation to an ambient worktree.  The ceiling lets Git discover a cache
    repository at ``cwd`` but prevents discovery in its parent directories.
    """

    env = dict(os.environ)
    for variable in (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    ):
        env.pop(variable, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.realpath(cwd))
    if allow_protocol is None:
        env.pop("GIT_ALLOW_PROTOCOL", None)
    else:
        env["GIT_ALLOW_PROTOCOL"] = allow_protocol
    return env


def git_http_config_args(approved: ApprovedGitRemote) -> list[str]:
    """Disable generic and approved-URL redirects for every Git subcommand."""

    args = ["-c", GIT_GENERIC_HTTP_REDIRECT_CONFIG]
    if approved.scheme == "https":
        args.extend(
            [
                "-c",
                f"http.{approved.subprocess_value}.followRedirects=false",
            ]
        )
    return args


def run_git_command(
    *,
    runner: GitRunner | None,
    executor: ProcessExecutor,
    binary: str,
    args: Sequence[str],
    cwd: str,
    env: Mapping[str, str],
    config_args: Sequence[str],
    deadline: ProcessDeadline,
    stdout_limit_bytes: int = GIT_PROCESS_METADATA_LIMIT_BYTES,
    stdout_consumer: Callable[[bytes], None] | None = None,
) -> GitCompleted:
    """Run one Git command through the trusted or bounded production seam.

    ``deadline`` and ``env`` are passed through unchanged so one logical
    operation can share an absolute deadline and a single environment snapshot
    across all of its Git invocations.
    """

    if runner is not None:
        # This compatibility seam is for trusted, synchronous test runners.
        # Encoding cwd in argv preserves the established runner contract.
        deadline.check()
        legacy_completed = runner([*config_args, "-C", cwd, *args])
        deadline.check()
        if (
            len(legacy_completed.stdout) > stdout_limit_bytes
            or len(legacy_completed.stderr) > GIT_PROCESS_STDERR_LIMIT_BYTES
        ):
            raise GitProcessOutputLimitExceeded(_GENERIC_OUTPUT_LIMIT_DETAIL)
        stdout = legacy_completed.stdout
        if stdout_consumer is not None:
            stdout_consumer(stdout)
            deadline.check()
            stdout = b""
        return GitCompleted(
            legacy_completed.returncode,
            stdout,
            legacy_completed.stderr,
        )

    process_result = executor.run(
        [binary, *config_args, *args],
        cwd=cwd,
        deadline=deadline,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=GIT_PROCESS_STDERR_LIMIT_BYTES,
        stdout_consumer=stdout_consumer,
        env=env,
    )
    deadline.check()
    return GitCompleted(
        process_result.returncode,
        process_result.stdout,
        b"",
    )


def git_remote_preflight_matches(
    completed: GitCompleted,
    canonical_remote: str,
) -> bool:
    """Require the canonical remote followed by exactly one LF or CRLF."""

    if completed.returncode != 0:
        return False
    expected_lf = canonical_remote.encode("utf-8") + b"\n"
    expected_crlf = canonical_remote.encode("utf-8") + b"\r\n"
    return completed.stdout in (expected_lf, expected_crlf)


__all__ = [
    "DEFAULT_GIT_ALLOW_PROTOCOL",
    "GIT_GENERIC_HTTP_REDIRECT_CONFIG",
    "GIT_PROCESS_METADATA_LIMIT_BYTES",
    "GIT_PROCESS_STDERR_LIMIT_BYTES",
    "GitCompleted",
    "GitProcessOutputLimitExceeded",
    "GitRunner",
    "build_git_environment",
    "git_http_config_args",
    "git_remote_preflight_matches",
    "run_git_command",
]
