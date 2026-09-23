"""Shared git helpers for the client capture adapters.

Every adapter that records git provenance (``lt repo``, ``lt hpc``, ``lt git
snapshot`` and figure ``run_context``) goes through this module, so that:

* a remote URL is always passed through :func:`sanitize_remote_url` before it
  is stored in an outbox event, rendered into a note body or copied into note
  metadata, because git remotes routinely embed credentials;
* git probes share one timeout, ``LAB_TRACKER_GIT_TIMEOUT_SECONDS`` (default
  :data:`DEFAULT_GIT_TIMEOUT_SECONDS`), sized for slow parallel/network
  filesystems (Lustre, GPFS, NFS) rather than a laptop SSD;
* a working tree whose state git could not report (timeout or error) is
  recorded as *unknown* (``git_dirty: None`` plus ``git_status_error``) with a
  stderr warning — never as clean, and never by blocking the caller.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab_tracker.models import NoteMetadataScalar
from lab_tracker_client.client import LTValidationError

GIT_TIMEOUT_ENV = "LAB_TRACKER_GIT_TIMEOUT_SECONDS"
DEFAULT_GIT_TIMEOUT_SECONDS = 10.0

# ``scheme://authority rest`` — authority is everything up to the first
# '/', '?' or '#', so a raw '@' inside a password stays in the authority.
_SCHEME_URL = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#]*)(?P<rest>.*)\Z",
    re.DOTALL,
)
# Transports whose login name is addressing (``ssh://git@host``), not a secret.
_SSH_SCHEMES = frozenset({"ssh", "git+ssh", "ssh+git"})


def sanitize_remote_url(remote: str) -> str:
    """Return ``remote`` with every credential-bearing part removed.

    * ``http(s)://`` and every other non-ssh scheme: all userinfo is dropped —
      a bare ``https://<token>@host`` username is indistinguishable from a
      personal access token, so it is never kept.
    * ``ssh://`` (and ``git+ssh://``/``ssh+git://``): the login name is kept
      (``ssh://git@host/...`` is addressing); any password is dropped.
    * Query strings and fragments are dropped from scheme URLs; git never needs
      them and they are a common place for ``access_token=`` style secrets.
    * scp-like ``[user@]host:path`` addressing (``git@github.com:lab/repo``) and
      local paths are returned unchanged: git's scp-like syntax has no password
      or query component.

    Credential-free remotes are returned unchanged (apart from surrounding
    whitespace), so values derived from them — such as
    :func:`lab_tracker_client.repo.normalize_remote` identities — stay stable.
    """

    cleaned = remote.strip()
    if not cleaned:
        return ""
    match = _SCHEME_URL.match(cleaned)
    if match is None:
        return cleaned
    scheme = match["scheme"]
    authority = match["authority"]
    userinfo, at, host = authority.rpartition("@")
    if at:
        login = userinfo.partition(":")[0]
        keep_login = scheme.lower() in _SSH_SCHEMES and bool(login)
        authority = f"{login}@{host}" if keep_login else host
    path = re.split(r"[?#]", match["rest"], maxsplit=1)[0]
    return f"{scheme}://{authority}{path}"


def git_timeout_seconds() -> float:
    """Timeout for one git probe: ``LAB_TRACKER_GIT_TIMEOUT_SECONDS`` or 10 s."""

    raw = os.getenv(GIT_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_GIT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise LTValidationError(
            f"{GIT_TIMEOUT_ENV} must be a positive number of seconds; got {raw!r}."
        )
    return value


@dataclass(frozen=True)
class GitProbe:
    """Outcome of one git invocation: stripped stdout, or why it failed."""

    stdout: str
    error: str = ""
    timed_out: bool = False
    # git itself could not be started (not installed, not executable).
    unavailable: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def run_git(root: str | Path | None, *args: str) -> GitProbe:
    """Run ``git [-C root] args`` bounded by :func:`git_timeout_seconds`.

    Never raises for git trouble (missing executable, non-zero exit, timeout):
    capture adapters must not block their caller, so the failure is returned
    for the caller to record honestly.
    """

    timeout = git_timeout_seconds()
    location = [] if root is None else ["-C", str(root)]
    label = f"git {' '.join(args)}"
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", *location, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GitProbe("", f"{label} timed out after {timeout:g}s", timed_out=True)
    except OSError as exc:
        return GitProbe("", f"{label} could not run: {exc}", unavailable=True)
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit status {result.returncode}"
        return GitProbe("", f"{label} failed: {detail}")
    return GitProbe(result.stdout.strip())


def git_output(root: str | Path | None, *args: str) -> str:
    """Stripped stdout of an optional git probe, or ``""`` if it failed."""

    return run_git(root, *args).stdout


@dataclass(frozen=True)
class HeadCommit:
    """HEAD commit SHA (``""`` when there is none), or why git could not say."""

    commit: str
    error: str = ""


def git_head_commit(root: str | Path | None) -> HeadCommit:
    """Ask ``git rev-parse HEAD`` for the commit a capture should record.

    A normal non-zero exit (not a repository, unborn HEAD) means there is no
    commit to record. A timeout, or git that cannot run at all, means the
    commit is *unknown*: the error is returned for the caller to record and a
    stderr warning is printed, so provenance is never dropped silently.
    """

    probe = run_git(root, "rev-parse", "HEAD")
    if probe.ok:
        return HeadCommit(probe.stdout)
    if not (probe.timed_out or probe.unavailable):
        return HeadCommit("")
    hint = (
        f" Set {GIT_TIMEOUT_ENV} (seconds, default {DEFAULT_GIT_TIMEOUT_SECONDS:g}) to allow "
        "a slower git."
        if probe.timed_out
        else ""
    )
    location = str(root) if root is not None else str(Path.cwd())
    print(
        f"lab-tracker: warning: could not determine the git commit at {location} "
        f"({probe.error}); recording the git commit as unknown.{hint}",
        file=sys.stderr,
    )
    return HeadCommit("", probe.error)


def head_commit_fields(head: HeadCommit) -> dict[str, Any]:
    """Event ``source`` fields for a HEAD probe (``git_commit_error`` if unknown)."""

    fields: dict[str, Any] = {"git_commit": head.commit}
    if head.error:
        fields["git_commit_error"] = head.error
    return fields


@dataclass(frozen=True)
class DirtyState:
    """Working-tree state: ``True``/``False`` when git said so, else ``None``."""

    dirty: bool | None
    error: str = ""


def git_dirty_state(root: str | Path | None, *, commit: str) -> DirtyState:
    """Ask ``git status --porcelain`` whether the working tree is dirty.

    ``commit`` is the HEAD the caller is about to record. When git cannot
    answer (timeout, or an error inside a repository with a commit) the state is
    unknown: ``DirtyState(None, reason)`` plus a stderr warning, so no caller
    pairs a real commit SHA with a false "clean" claim. Outside a repository
    (no commit and git exited normally) there is no working tree to be dirty.
    """

    probe = run_git(root, "status", "--porcelain")
    if probe.ok:
        return DirtyState(bool(probe.stdout))
    if not commit and not (probe.timed_out or probe.unavailable):
        return DirtyState(False)
    hint = (
        f" Set {GIT_TIMEOUT_ENV} (seconds, default {DEFAULT_GIT_TIMEOUT_SECONDS:g}) to allow "
        "a slower git status."
        if probe.timed_out
        else ""
    )
    location = str(root) if root is not None else str(Path.cwd())
    print(
        f"lab-tracker: warning: could not determine whether the git working tree at "
        f"{location} is dirty ({probe.error}); recording git_dirty as unknown.{hint}",
        file=sys.stderr,
    )
    return DirtyState(None, probe.error)


def dirty_state_fields(state: DirtyState) -> dict[str, Any]:
    """Event ``source`` fields for a dirty state (``git_status_error`` if unknown)."""

    fields: dict[str, Any] = {"git_dirty": state.dirty}
    if state.dirty is None:
        fields["git_status_error"] = state.error or "unknown"
    return fields


def dirty_label(source: Mapping[str, Any]) -> str:
    """Human-readable dirty flag for a rendered note: ``True``/``False``/``unknown``."""

    dirty = source.get("git_dirty")
    if dirty is None:
        error = str(source.get("git_status_error") or "").strip()
        return f"unknown ({error})" if error else "unknown"
    return str(bool(dirty))


def dirty_metadata(source: Mapping[str, Any], prefix: str) -> dict[str, NoteMetadataScalar]:
    """Note metadata for a dirty flag: ``<prefix>git_dirty`` only when known.

    An unknown state is recorded as ``<prefix>git_status_error`` instead, so no
    consumer can read a missing answer as ``False``.
    """

    dirty = source.get("git_dirty")
    if dirty is None:
        error = str(source.get("git_status_error") or "").strip()
        return {f"{prefix}git_status_error": error or "unknown"}
    return {f"{prefix}git_dirty": bool(dirty)}
