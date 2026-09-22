"""Git resolver cache root, quota, and cache-safety controls (M20, L42)."""

from __future__ import annotations

import gc
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from lab_tracker.app_parts.runtime import build_app_runtime
from lab_tracker.artifact_resolution import (
    GitCacheSettings,
    GitResolver,
    ResolutionStatus,
    registry_from_env,
    validate_git_cache_config,
)
from lab_tracker.config import Settings
from lab_tracker.git_process import GitCompleted
from lab_tracker.git_remote_policy import GitRemotePolicy
from lab_tracker.models import ExternalArtifactReference

_REMOTE = "https://example.com/org/repo.git"
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and modes")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _ref(data: bytes) -> ExternalArtifactReference:
    return ExternalArtifactReference(
        source_system="git",
        uri=f"git+{_REMOTE}#{'a' * 40}:analysis/run.py",
        content_hash=_sha256(data),
    )


class _Runner:
    """Fake Git runner recording (argv, cwd) and serving one blob."""

    def __init__(self, blob: bytes, resolver_ref: list[GitResolver] | None = None) -> None:
        self.blob = blob
        self.calls: list[list[str]] = []
        self.lock_held: list[bool] = []
        self._resolver_ref = resolver_ref

    def cwd(self, args: list[str]) -> str:
        return args[args.index("-C") + 1]

    def __call__(self, args: list[str]) -> GitCompleted:
        self.calls.append(args)
        if self._resolver_ref:
            lock = self._resolver_ref[0]._cache_lock_for(self.cwd(args))
            self.lock_held.append(lock.locked())
        if "init" in args:
            return GitCompleted(0, b"", b"")
        if "ls-remote" in args:
            return GitCompleted(0, f"{_REMOTE}\n".encode(), b"")
        if "fetch" in args:
            return GitCompleted(0, b"", b"")
        if "-s" in args:
            return GitCompleted(0, f"{len(self.blob)}\n".encode(), b"")
        return GitCompleted(0, self.blob, b"")


def _resolver(runner: _Runner, **kwargs: object) -> GitResolver:
    return GitResolver(
        runner=runner,
        remote_policy=GitRemotePolicy.from_config(_REMOTE),
        **kwargs,  # type: ignore[arg-type]
    )


# --- Settings / configuration (M20) -------------------------------------------------


@pytest.mark.parametrize("value", ["abc", "0", "-5", "2GB", "1.5"])
def test_settings_reject_invalid_git_cache_max_bytes(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_MAX_BYTES", value)

    with pytest.raises(ValidationError, match="LAB_TRACKER_GIT_CACHE_MAX_BYTES"):
        Settings()


def test_settings_accept_git_cache_controls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_MAX_BYTES", "2048")
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_ROOT", str(tmp_path / "git-cache"))

    settings = Settings()

    assert settings.git_cache_max_bytes == 2048
    assert settings.git_cache_root == str(tmp_path / "git-cache")


def test_settings_git_cache_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAB_TRACKER_GIT_CACHE_MAX_BYTES", raising=False)
    monkeypatch.delenv("LAB_TRACKER_GIT_CACHE_ROOT", raising=False)

    settings = Settings()

    assert settings.git_cache_max_bytes is None
    assert settings.git_cache_root == ""


def test_settings_reject_relative_git_cache_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_ROOT", "relative/cache")

    with pytest.raises(ValidationError, match="LAB_TRACKER_GIT_CACHE_ROOT"):
        Settings()


def test_settings_expand_home_in_git_cache_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_ROOT", "~/lab-tracker-git-cache")

    assert Settings().git_cache_root == os.path.expanduser("~/lab-tracker-git-cache")


@pytest.mark.parametrize("value", ["abc", "0", "-5", ""])
def test_registry_from_env_rejects_invalid_git_cache_max_bytes(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_MAX_BYTES", value)

    with pytest.raises(ValueError, match="LAB_TRACKER_GIT_CACHE_MAX_BYTES"):
        registry_from_env()


def test_explicit_git_cache_settings_ignore_the_process_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_MAX_BYTES", "not-a-number")
    monkeypatch.setenv("LAB_TRACKER_GIT_CACHE_ROOT", "/ignored")

    registry = registry_from_env(
        git_cache=GitCacheSettings(root=str(tmp_path), max_bytes=4096)
    )
    resolver = next(r for r in registry._resolvers if isinstance(r, GitResolver))

    assert resolver._cache_root == str(tmp_path)
    assert resolver._max_cache_bytes == 4096


def test_git_cache_settings_validate_their_inputs() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        GitCacheSettings(max_bytes=0)
    with pytest.raises(ValueError, match="absolute"):
        GitCacheSettings(root="relative")
    with pytest.raises(ValueError, match="max_cache_bytes"):
        GitResolver(max_cache_bytes=-1)


def test_runtime_passes_git_cache_settings_to_the_resolver(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAB_TRACKER_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'x.db'}")
    settings = Settings(
        git_cache_root=str(tmp_path / "cache"),
        git_cache_max_bytes=12345,
    )

    runtime = build_app_runtime(settings, verify_schema=False)
    try:
        resolver = next(
            r for r in runtime.resolver_registry._resolvers if isinstance(r, GitResolver)
        )
        assert resolver._cache_root == str(tmp_path / "cache")
        assert resolver._max_cache_bytes == 12345
    finally:
        runtime.engine.dispose()
        runtime.cleanup_git_health_workdir()


# --- Default cache location (L42) ----------------------------------------------------


@_POSIX_ONLY
def test_default_cache_is_a_private_unpredictable_directory_removed_with_the_resolver() -> None:
    data = b"pinned"
    runner = _Runner(data)
    resolver = _resolver(runner)

    result = resolver.resolve(_ref(data))

    assert result.status is ResolutionStatus.VERIFIED
    cache = Path(runner.cwd(runner.calls[0]))
    base = cache.parent
    assert base != Path(tempfile.gettempdir()) / "lab-tracker-git-cache"
    assert base.name.startswith("lab-tracker-git-cache-")
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert base.stat().st_uid == os.geteuid()

    del resolver
    gc.collect()
    assert not base.exists()


# --- Cache directory safety (L42) ----------------------------------------------------


@_POSIX_ONLY
def test_foreign_owned_cache_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    owner = root.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    runner = _Runner(b"data")

    result = _resolver(runner, cache_root=root).resolve(_ref(b"data"))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert runner.calls == []


@_POSIX_ONLY
def test_symlinked_cache_root_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = tmp_path / "cache"
    link.symlink_to(target, target_is_directory=True)
    runner = _Runner(b"data")

    result = _resolver(runner, cache_root=link).resolve(_ref(b"data"))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert runner.calls == []
    assert list(target.iterdir()) == []


@_POSIX_ONLY
def test_symlinked_per_remote_cache_is_refused(tmp_path: Path) -> None:
    data = b"data"
    root = tmp_path / "cache"
    runner = _Runner(data)
    assert _resolver(runner, cache_root=root).resolve(_ref(data)).status is (
        ResolutionStatus.VERIFIED
    )
    remote_cache = Path(runner.cwd(runner.calls[0]))
    shutil.rmtree(remote_cache)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    remote_cache.symlink_to(elsewhere, target_is_directory=True)
    second = _Runner(data)

    result = _resolver(second, cache_root=root).resolve(_ref(data))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert second.calls == []


@_POSIX_ONLY
def test_group_or_world_accessible_cache_root_is_tightened(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    root.chmod(0o777)
    data = b"data"

    result = _resolver(_Runner(data), cache_root=root).resolve(_ref(data))

    assert result.status is ResolutionStatus.VERIFIED
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "planted",
    [
        "[core]\n\tfsmonitor = touch /tmp/pwned\n",
        "[core]\n\thooksPath = /tmp/hooks\n",
        "[core]\n\tsshCommand = sh -c 'touch /tmp/pwned'\n",
        '[credential]\n\thelper = "!touch /tmp/pwned"\n',
        "[include]\n\tpath = /tmp/evil.config\n",
        '[url "https://evil.example/"]\n\tinsteadOf = https://example.com/\n',
        "[core]\n\trepositoryformatversion = 0\n[core]garbage\n",
    ],
)
def test_planted_repository_local_config_is_refused(tmp_path: Path, planted: str) -> None:
    data = b"data"
    root = tmp_path / "cache"
    runner = _Runner(data)
    assert _resolver(runner, cache_root=root).resolve(_ref(data)).status is (
        ResolutionStatus.VERIFIED
    )
    remote_cache = Path(runner.cwd(runner.calls[0]))
    (remote_cache / ".git").mkdir()
    (remote_cache / ".git" / "config").write_text(planted, encoding="utf-8")
    second = _Runner(data)

    result = _resolver(second, cache_root=root).resolve(_ref(data))

    assert result.status is ResolutionStatus.UNRESOLVED
    assert second.calls == []


@pytest.mark.parametrize("object_format", [None, "sha256"])
def test_config_written_by_real_git_init_is_accepted(
    tmp_path: Path, object_format: str | None
) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is unavailable")
    args = [git, "init", "-q"]
    if object_format is not None:
        args.append(f"--object-format={object_format}")
    subprocess.run([*args, str(tmp_path)], check=True, capture_output=True)  # noqa: S603

    validate_git_cache_config(str(tmp_path))


def test_every_resolver_git_command_neutralises_hooks_and_fsmonitor(tmp_path: Path) -> None:
    data = b"data"
    runner = _Runner(data)

    _resolver(runner, cache_root=tmp_path / "cache").resolve(_ref(data))

    assert runner.calls
    for call in runner.calls:
        assert ["-c", f"core.hooksPath={os.devnull}"] == call[
            call.index(f"core.hooksPath={os.devnull}") - 1 : call.index(
                f"core.hooksPath={os.devnull}"
            )
            + 1
        ]
        assert "core.fsmonitor=false" in call


# --- Per-remote lock ------------------------------------------------------------------


def test_resolution_holds_the_per_remote_cache_lock(tmp_path: Path) -> None:
    data = b"data"
    holder: list[GitResolver] = []
    runner = _Runner(data, resolver_ref=holder)
    resolver = _resolver(runner, cache_root=tmp_path / "cache")
    holder.append(resolver)

    assert resolver.resolve(_ref(data)).status is ResolutionStatus.VERIFIED
    assert runner.lock_held and all(runner.lock_held)


def test_quota_eviction_skips_a_cache_in_use(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir(mode=0o700)
    busy = root / "busy"
    idle = root / "idle"
    for path in (busy, idle):
        path.mkdir()
        (path / "pack").write_bytes(b"x" * 4096)
        os.utime(path, (1, 1))
    resolver = _resolver(_Runner(b""), cache_root=root, max_cache_bytes=1024)

    lock = resolver._cache_lock_for(str(busy))
    with lock:
        resolver._enforce_cache_quota(str(root))

    assert busy.exists()
    assert not idle.exists()


def test_resolvers_sharing_a_cache_root_share_its_locks(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    first = _resolver(_Runner(b""), cache_root=root)
    second = _resolver(_Runner(b""), cache_root=root)

    assert first._cache_lock_for(str(root / "abc")) is second._cache_lock_for(
        str(root / "abc")
    )
