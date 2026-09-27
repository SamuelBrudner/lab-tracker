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
  stderr warning — never as clean, and never by blocking the caller;
* the commit filter (:class:`CommitFilter` / :func:`commit_skip_reason`) that
  decides which commits a post-commit capture records is defined once, so the
  ``lt repo`` hook and the legacy ``lt git snapshot`` hook skip the same
  commits (merges and ``fixup!``/``squash!`` subjects by default; ``wip``
  subjects and ignored path globs only when the repo config opts in).
"""

from __future__ import annotations

import fnmatch
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
    # git's own stderr when it ran and exited non-zero.
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def run_git(root: str | Path | None, *args: str, c_locale: bool = False) -> GitProbe:
    """Run ``git [-C root] args`` bounded by :func:`git_timeout_seconds`.

    Never raises for git trouble (missing executable, non-zero exit, timeout):
    capture adapters must not block their caller, so the failure is returned
    for the caller to record honestly. ``c_locale`` runs git untranslated so a
    caller can recognise specific git messages in ``stderr``.
    """

    timeout = git_timeout_seconds()
    location = [] if root is None else ["-C", str(root)]
    label = f"git {' '.join(args)}"
    env = {**os.environ, "LC_ALL": "C"} if c_locale else None
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable, no shell.
            ["git", *location, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return GitProbe("", f"{label} timed out after {timeout:g}s", timed_out=True)
    except OSError as exc:
        return GitProbe("", f"{label} could not run: {exc}", unavailable=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = stderr or f"exit status {result.returncode}"
        return GitProbe("", f"{label} failed: {detail}", stderr=stderr)
    return GitProbe(result.stdout.strip())


def git_output(root: str | Path | None, *args: str) -> str:
    """Stripped stdout of an optional git probe, or ``""`` if it failed."""

    return run_git(root, *args).stdout


@dataclass(frozen=True)
class HeadCommit:
    """HEAD commit SHA (``""`` when there is none), or why git could not say."""

    commit: str
    error: str = ""
    # git itself could not be started, so its warning already covers every probe.
    git_unavailable: bool = False


def _location_label(root: str | Path | None) -> str:
    """Where a probe ran, for a warning; never raises (the cwd may be deleted)."""

    if root is not None:
        return str(root)
    try:
        return os.getcwd()
    except OSError:
        return "the current directory"


# Untranslated (LC_ALL=C) git messages that mean "there is no commit here":
# not inside a repository at all, or a repository whose HEAD is unborn.
_NO_COMMIT_MESSAGES = (
    "not a git repository",
    "ambiguous argument 'HEAD': unknown revision",
)


def git_head_commit(root: str | Path | None) -> HeadCommit:
    """Ask ``git rev-parse HEAD`` for the commit a capture should record.

    Only git clearly saying there is no commit (not a repository, unborn
    HEAD) means there is no commit to record. Any other failure -- a timeout,
    git that cannot run, or git refusing the repository (e.g. safe.directory
    "dubious ownership") -- means the commit is *unknown*: the error is
    returned for the caller to record and a stderr warning is printed, so
    provenance is never dropped silently.
    """

    probe = run_git(root, "rev-parse", "HEAD", c_locale=True)
    if probe.ok:
        return HeadCommit(probe.stdout)
    if any(message in probe.stderr for message in _NO_COMMIT_MESSAGES):
        return HeadCommit("")
    hint = (
        f" Set {GIT_TIMEOUT_ENV} (seconds, default {DEFAULT_GIT_TIMEOUT_SECONDS:g}) to allow "
        "a slower git."
        if probe.timed_out
        else ""
    )
    print(
        f"lab-tracker: warning: could not determine the git commit at {_location_label(root)} "
        f"({probe.error}); recording the git commit as unknown.{hint}",
        file=sys.stderr,
    )
    return HeadCommit("", probe.error, git_unavailable=probe.unavailable)


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


def git_dirty_state(root: str | Path | None, *, head: HeadCommit) -> DirtyState:
    """Ask ``git status --porcelain`` whether the working tree is dirty.

    ``head`` is the HEAD probe the caller is about to record. When git cannot
    answer (timeout, or an error inside a repository with a commit, or where
    the commit itself is unknown) the state is unknown: ``DirtyState(None,
    reason)`` plus a stderr warning, so no caller pairs a commit with a false
    "clean" claim. Only where git said there is no commit, and status then
    failed normally, is there no working tree to be dirty. When git cannot
    run at all, the HEAD probe's warning already said so and no second
    warning is printed; the state is still recorded as unknown.
    """

    probe = run_git(root, "status", "--porcelain")
    if probe.ok:
        return DirtyState(bool(probe.stdout))
    if not head.commit and not head.error and not (probe.timed_out or probe.unavailable):
        return DirtyState(False)
    if probe.unavailable and head.git_unavailable:
        # git cannot run at all: the HEAD probe already warned about it.
        return DirtyState(None, probe.error)
    hint = (
        f" Set {GIT_TIMEOUT_ENV} (seconds, default {DEFAULT_GIT_TIMEOUT_SECONDS:g}) to allow "
        "a slower git status."
        if probe.timed_out
        else ""
    )
    print(
        f"lab-tracker: warning: could not determine whether the git working tree at "
        f"{_location_label(root)} is dirty ({probe.error}); recording git_dirty as unknown.{hint}",
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


# --- commit filter -----------------------------------------------------------

FIXUP_SUBJECT_PATTERN = re.compile(r"^(fixup!|squash!)")
WIP_SUBJECT_PATTERN = re.compile(r"^wip\b", re.IGNORECASE)
SKIP_REASON_MERGE = "merge_commit"
SKIP_REASON_FIXUP = "fixup_subject"
SKIP_REASON_WIP = "wip_subject"
SKIP_REASON_PATHS = "only_ignored_paths"
# ``git show -s --format``: parent hashes, NUL, subject.
COMMIT_FACTS_FORMAT = "%P%x00%s"
_COMMIT_FILTER_BOOL_KEYS = ("skip_merges", "skip_fixups", "skip_wip")
_COMMIT_FILTER_KEYS = (*_COMMIT_FILTER_BOOL_KEYS, "skip_path_globs")


@dataclass(frozen=True)
class CommitFilter:
    """Which commits a post-commit capture leaves out of the outbox.

    Merge commits and ``fixup!``/``squash!`` subjects are skipped by default:
    they carry no analysis of their own (the merged or fixed-up commits do).
    ``wip`` subjects and path globs are opt-in through ``commit_filter`` in
    ``.lab-tracker/repo.json``. A skipped commit is always logged and counted,
    never silently dropped.
    """

    skip_merges: bool = True
    skip_fixups: bool = True
    skip_wip: bool = False
    skip_path_globs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "skip_merges": self.skip_merges,
            "skip_fixups": self.skip_fixups,
            "skip_wip": self.skip_wip,
            "skip_path_globs": list(self.skip_path_globs),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> CommitFilter:
        """Parse the ``commit_filter`` object; absent keys keep their defaults."""

        if not isinstance(payload, Mapping):
            raise LTValidationError("commit_filter must be a JSON object.")
        unknown = sorted(str(key) for key in payload if key not in _COMMIT_FILTER_KEYS)
        if unknown:
            raise LTValidationError(
                f"commit_filter has unknown keys: {', '.join(unknown)}. "
                f"Allowed keys: {', '.join(_COMMIT_FILTER_KEYS)}."
            )
        values: dict[str, Any] = {}
        for key in _COMMIT_FILTER_BOOL_KEYS:
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise LTValidationError(f"commit_filter.{key} must be true or false.")
                values[key] = payload[key]
        if "skip_path_globs" in payload:
            globs = payload["skip_path_globs"]
            if not isinstance(globs, list) or not all(
                isinstance(item, str) and item.strip() for item in globs
            ):
                raise LTValidationError(
                    "commit_filter.skip_path_globs must be a list of non-empty glob strings."
                )
            values["skip_path_globs"] = tuple(item.strip() for item in globs)
        return cls(**values)


def commit_skip_reason(root: Path | str, commit: str, commit_filter: CommitFilter) -> str:
    """Why ``commit_filter`` would skip ``commit``, or ``""`` to record it.

    Reasons are checked in a fixed order (merge, fixup, wip, ignored paths) and
    the first match wins. A commit git cannot describe is never skipped: an
    unknown commit is recorded, not filtered away.
    """

    facts = run_git(root, "show", "-s", f"--format={COMMIT_FACTS_FORMAT}", commit)
    if not facts.ok:
        return ""
    parents_text, _sep, subject = facts.stdout.partition("\x00")
    if commit_filter.skip_merges and len(parents_text.split()) > 1:
        return SKIP_REASON_MERGE
    if commit_filter.skip_fixups and FIXUP_SUBJECT_PATTERN.match(subject):
        return SKIP_REASON_FIXUP
    if commit_filter.skip_wip and WIP_SUBJECT_PATTERN.match(subject):
        return SKIP_REASON_WIP
    if commit_filter.skip_path_globs:
        listing = run_git(root, "show", "--name-only", "--format=", commit)
        if listing.ok:
            paths = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
            globs = commit_filter.skip_path_globs
            if paths and all(_matches_any_glob(path, globs) for path in paths):
                return SKIP_REASON_PATHS
    return ""


def _matches_any_glob(path: str, globs: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, glob) for glob in globs)
