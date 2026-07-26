from __future__ import annotations

import errno
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

import pytest

import lab_tracker.local_file_access as local_file_access
from lab_tracker.local_file_access import (
    HandleBoundLocalFileAccess,
    LocalOpenFailure,
    LocalOpenFailureReason,
    OpenedLocalFile,
)
from lab_tracker.local_path_policy import LocalPathPolicy


def _reader(*roots: Path) -> HandleBoundLocalFileAccess:
    return HandleBoundLocalFileAccess(LocalPathPolicy(list(roots)))


def _unscoped_reader() -> HandleBoundLocalFileAccess:
    return HandleBoundLocalFileAccess(LocalPathPolicy())


def _require_posix_openat() -> None:
    if os.name == "nt":
        pytest.skip("requires POSIX descriptor-relative opening")
    if (
        local_file_access._posix_directory_flags() is None
        or local_file_access._posix_leaf_flags() is None
    ):
        pytest.skip("required POSIX no-follow open flags are unavailable")
    if os.open not in os.supports_dir_fd:
        pytest.skip("os.open(dir_fd=...) is unavailable")


def _make_symlink(
    link: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


def _require_directory_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "symlink-capability-target"
    target.mkdir()
    link = tmp_path / "symlink-capability-link"
    _make_symlink(link, target, target_is_directory=True)
    link.unlink()


def _create_windows_junction(junction: Path, target: Path) -> None:
    completed = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(junction),
            str(target),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} "
        f"stderr={completed.stderr!r}"
    )


def _call_open(
    opener: Callable[..., int],
    path: object,
    flags: int,
    mode: int,
    dir_fd: int | None,
) -> int:
    if dir_fd is None:
        return opener(path, flags, mode)
    return opener(path, flags, mode, dir_fd=dir_fd)


def _is_directory_open(flags: int) -> bool:
    directory_flag = int(getattr(os, "O_DIRECTORY", 0))
    return bool(directory_flag and flags & directory_flag)


class _RecordingStream:
    def __init__(self, wrapped: BinaryIO) -> None:
        self._wrapped = wrapped
        self.close_calls = 0

    @property
    def closed(self) -> bool:
        return self._wrapped.closed

    def fileno(self) -> int:
        return self._wrapped.fileno()

    def read(self, size: int = -1) -> bytes:
        return self._wrapped.read(size)

    def close(self) -> None:
        self.close_calls += 1
        self._wrapped.close()


class _DescriptorTracker:
    """Track descriptor lifetimes even when the OS reuses integer fd values."""

    def __init__(self) -> None:
        self._real_open = os.open
        self._real_close = os.close
        self._next_token = 0
        self.live_tokens: dict[int, int] = {}
        self.opened_tokens: list[int] = []
        self.closed_tokens: list[int] = []
        self.leaf_descriptor: int | None = None
        self.leaf_token: int | None = None

    def open(
        self,
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = _call_open(
            self._real_open,
            path,
            flags,
            mode,
            dir_fd,
        )
        token = self._next_token
        self._next_token += 1
        self.live_tokens[descriptor] = token
        self.opened_tokens.append(token)
        if not _is_directory_open(flags):
            self.leaf_descriptor = descriptor
            self.leaf_token = token
        return descriptor

    def close(self, descriptor: int) -> None:
        token = self.live_tokens.pop(descriptor, None)
        if token is not None:
            self.closed_tokens.append(token)
        self._real_close(descriptor)


class _BodyAbort(BaseException):
    pass


def test_matching_regular_file_yields_same_live_stream_and_size(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    content = b"handle-bound artifact"
    path.write_bytes(content)
    stream: BinaryIO | None = None

    with _reader(root).open_regular_file(path) as opened:
        assert isinstance(opened, OpenedLocalFile)
        stream = opened.stream
        assert stream.closed is False
        assert opened.display_path == os.path.realpath(path)
        assert opened.size_hint_bytes == len(content)
        assert stream.read() == content

    assert stream is not None
    assert stream.closed is True


def test_missing_file_has_typed_missing_failure(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()

    with _reader(root).open_regular_file(root / "missing.bin") as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.MISSING)


def test_deny_all_rejects_without_opening_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"must not be opened")

    def unexpected_open(_path: str) -> int:
        raise AssertionError("deny-all must not invoke a platform opener")

    monkeypatch.setattr(
        local_file_access,
        "_open_posix_descriptor",
        unexpected_open,
    )
    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        unexpected_open,
    )

    with HandleBoundLocalFileAccess(
        LocalPathPolicy([])
    ).open_regular_file(path) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)


def test_outside_path_is_denied_before_platform_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")

    def unexpected_open(_path: str) -> int:
        raise AssertionError("outside candidate must not reach a platform opener")

    monkeypatch.setattr(
        local_file_access,
        "_open_posix_descriptor",
        unexpected_open,
    )
    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        unexpected_open,
    )

    with _reader(root).open_regular_file(outside) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)


def test_safe_in_root_symlink_opens_its_canonical_target(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    target = root / "target.bin"
    content = b"inside through a safe link"
    target.write_bytes(content)
    link = root / "artifact.bin"
    _make_symlink(link, target)

    with _reader(root).open_regular_file(link) as opened:
        assert isinstance(opened, OpenedLocalFile)
        assert opened.display_path == os.path.realpath(target)
        assert opened.stream.read() == content


def test_escaping_symlink_is_denied_before_platform_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    link = root / "artifact.bin"
    _make_symlink(link, outside)

    def unexpected_open(_path: str) -> int:
        raise AssertionError("escaping link must not reach a platform opener")

    monkeypatch.setattr(
        local_file_access,
        "_open_posix_descriptor",
        unexpected_open,
    )
    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        unexpected_open,
    )

    with _reader(root).open_regular_file(link) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)


def test_directory_is_denied_as_non_regular(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    directory = root / "directory"
    directory.mkdir()

    with _reader(root).open_regular_file(directory) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)


def test_fifo_is_rejected_before_leaf_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO support is unavailable")

    root = tmp_path / "store"
    root.mkdir()
    fifo = root / "artifact.fifo"
    os.mkfifo(fifo)
    reader = _reader(root)
    real_open = os.open
    leaf_opened = False

    def recording_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal leaf_opened
        if not _is_directory_open(flags):
            leaf_opened = True
        return _call_open(real_open, path, flags, mode, dir_fd)

    monkeypatch.setattr(local_file_access.os, "open", recording_open)

    with reader.open_regular_file(fifo) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)

    assert leaf_opened is False


def _assert_leaf_replacement_reads_open_descriptor(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reader: HandleBoundLocalFileAccess,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    root.mkdir(exist_ok=True)
    path = root / "artifact.bin"
    moved = root / "opened-original.bin"
    outside = tmp_path / "outside.bin"
    original_content = b"original descriptor bytes"
    outside_content = b"outside replacement bytes"
    path.write_bytes(original_content)
    outside.write_bytes(outside_content)
    _require_directory_symlinks(tmp_path)
    real_open = os.open
    replaced = False

    def replacing_open(
        component: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        descriptor = _call_open(real_open, component, flags, mode, dir_fd)
        if (
            not replaced
            and not _is_directory_open(flags)
            and os.fspath(component) == path.name
        ):
            path.rename(moved)
            path.symlink_to(outside)
            replaced = True
        return descriptor

    monkeypatch.setattr(local_file_access.os, "open", replacing_open)

    with reader.open_regular_file(path) as opened:
        assert isinstance(opened, OpenedLocalFile)
        assert opened.stream.read() == original_content

    assert replaced is True


def test_pathname_replacement_after_open_reads_original_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    reader = _reader(root)

    _assert_leaf_replacement_reads_open_descriptor(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        reader=reader,
    )


def test_unscoped_mode_remains_handle_bound_after_pathname_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_leaf_replacement_reads_open_descriptor(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        reader=_unscoped_reader(),
    )


def test_parent_swap_before_component_open_fails_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    _require_directory_symlinks(tmp_path)
    root = tmp_path / "store"
    parent = root / "victim-parent"
    parent.mkdir(parents=True)
    path = parent / "artifact.bin"
    path.write_bytes(b"inside")
    moved_parent = root / "opened-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    (outside_parent / path.name).write_bytes(b"outside")
    reader = _reader(root)
    real_open = os.open
    swapped = False
    observed_flags: list[int] = []

    def swapping_open(
        component: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and _is_directory_open(flags)
            and os.fspath(component) == parent.name
        ):
            observed_flags.append(flags)
            parent.rename(moved_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            swapped = True
        return _call_open(real_open, component, flags, mode, dir_fd)

    monkeypatch.setattr(local_file_access.os, "open", swapping_open)

    with reader.open_regular_file(path) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)

    assert swapped is True
    assert len(observed_flags) == 1
    assert observed_flags[0] & os.O_NOFOLLOW


def test_parent_rename_after_descriptor_open_remains_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    _require_directory_symlinks(tmp_path)
    root = tmp_path / "store"
    parent = root / "victim-parent"
    parent.mkdir(parents=True)
    path = parent / "artifact.bin"
    inside_content = b"inside pinned directory"
    outside_content = b"outside junction target"
    path.write_bytes(inside_content)
    moved_parent = root / "opened-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    (outside_parent / path.name).write_bytes(outside_content)
    reader = _reader(root)
    real_open = os.open
    swapped = False

    def swapping_after_open(
        component: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = _call_open(real_open, component, flags, mode, dir_fd)
        if (
            not swapped
            and _is_directory_open(flags)
            and os.fspath(component) == parent.name
        ):
            parent.rename(moved_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(local_file_access.os, "open", swapping_after_open)

    with reader.open_regular_file(path) as opened:
        assert isinstance(opened, OpenedLocalFile)
        assert opened.stream.read() == inside_content

    assert swapped is True


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_safe_in_root_junction_opens_its_canonical_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    target_parent = root / "target-parent"
    target_parent.mkdir(parents=True)
    target = target_parent / "artifact.bin"
    content = b"safe in-root junction bytes"
    target.write_bytes(content)
    junction = root / "junction"
    _create_windows_junction(junction, target_parent)

    with _reader(root).open_regular_file(junction / target.name) as opened:
        assert isinstance(opened, OpenedLocalFile)
        assert os.path.samefile(opened.display_path, target)
        assert opened.stream.read() == content


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_parent_junction_swap_before_open_denies_outside_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    parent = root / "victim-parent"
    parent.mkdir(parents=True)
    path = parent / "artifact.bin"
    path.write_bytes(b"inside")
    moved_parent = root / "original-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    outside_content = b"outside bytes must never reach a stream"
    (outside_parent / path.name).write_bytes(outside_content)
    reader = _reader(root)
    real_open = local_file_access._open_windows_descriptor
    real_close = os.close
    swapped = False
    captured_descriptor: int | None = None
    closed_target_descriptors: list[int] = []

    def swap_then_open(planned_path: str) -> int | LocalOpenFailure:
        nonlocal captured_descriptor, swapped
        parent.rename(moved_parent)
        _create_windows_junction(parent, outside_parent)
        swapped = True
        opened = real_open(planned_path)
        assert isinstance(opened, int)
        captured_descriptor = opened
        return opened

    def recording_close(descriptor: int) -> None:
        if descriptor == captured_descriptor:
            closed_target_descriptors.append(descriptor)
        real_close(descriptor)

    def unexpected_fdopen(*_args: object, **_kwargs: object) -> BinaryIO:
        raise AssertionError("outside target reached the content-stream boundary")

    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        swap_then_open,
    )
    monkeypatch.setattr(local_file_access.os, "close", recording_close)
    monkeypatch.setattr(local_file_access.os, "fdopen", unexpected_fdopen)

    with reader.open_regular_file(path) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.DENIED)

    assert swapped is True
    assert captured_descriptor is not None
    assert closed_target_descriptors == [captured_descriptor]
    assert (outside_parent / path.name).read_bytes() == outside_content


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_junction_retarget_after_open_reads_original_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    parent = root / "victim-parent"
    parent.mkdir(parents=True)
    path = parent / "artifact.bin"
    inside_content = b"original opened Windows descriptor bytes"
    path.write_bytes(inside_content)
    opened_parent = root / "opened-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_parent.mkdir()
    outside_content = b"outside replacement pathname bytes"
    (outside_parent / path.name).write_bytes(outside_content)
    reader = _reader(root)
    real_open = local_file_access._open_windows_descriptor
    retargeted = False

    def open_then_retarget(planned_path: str) -> int | LocalOpenFailure:
        nonlocal retargeted
        parent.rename(opened_parent)
        _create_windows_junction(parent, opened_parent)
        opened = real_open(planned_path)
        assert isinstance(opened, int)
        parent.rmdir()
        _create_windows_junction(parent, outside_parent)
        retargeted = True
        return opened

    monkeypatch.setattr(
        local_file_access,
        "_open_windows_descriptor",
        open_then_retarget,
    )

    with reader.open_regular_file(path) as opened:
        assert isinstance(opened, OpenedLocalFile)
        assert os.path.samefile(opened.display_path, opened_parent / path.name)
        assert opened.stream.read() == inside_content

    assert retargeted is True
    assert path.read_bytes() == outside_content


def test_success_closes_stream_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    path.write_bytes(b"content")
    real_fdopen = os.fdopen
    streams: list[_RecordingStream] = []

    def recording_fdopen(
        descriptor: int,
        mode: str,
        *,
        closefd: bool = True,
    ) -> _RecordingStream:
        stream = _RecordingStream(
            real_fdopen(descriptor, mode, closefd=closefd)
        )
        streams.append(stream)
        return stream

    monkeypatch.setattr(local_file_access.os, "fdopen", recording_fdopen)

    with _reader(root).open_regular_file(path) as opened:
        assert isinstance(opened, OpenedLocalFile)
        assert opened.stream.read() == b"content"
        assert streams[0].closed is False

    assert len(streams) == 1
    assert streams[0].close_calls == 1
    assert streams[0].closed is True


def test_read_body_base_exception_propagates_after_exact_stream_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    path.write_bytes(b"content")
    real_fdopen = os.fdopen
    streams: list[_RecordingStream] = []
    abort = _BodyAbort()

    def recording_fdopen(
        descriptor: int,
        mode: str,
        *,
        closefd: bool = True,
    ) -> _RecordingStream:
        stream = _RecordingStream(
            real_fdopen(descriptor, mode, closefd=closefd)
        )
        streams.append(stream)
        return stream

    monkeypatch.setattr(local_file_access.os, "fdopen", recording_fdopen)

    with (
        pytest.raises(_BodyAbort) as caught,
        _reader(root).open_regular_file(path) as opened,
    ):
        assert isinstance(opened, OpenedLocalFile)
        raise abort

    assert caught.value is abort
    assert len(streams) == 1
    assert streams[0].close_calls == 1
    assert streams[0].closed is True


def test_leaf_open_failure_closes_every_opened_directory_descriptor_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    path = nested / "artifact.bin"
    path.write_bytes(b"content")
    reader = _reader(root)
    tracker = _DescriptorTracker()

    def failing_leaf_open(
        component: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if not _is_directory_open(flags):
            raise OSError(errno.EIO, "sensitive leaf-open failure")
        return tracker.open(component, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(local_file_access.os, "open", failing_leaf_open)
    monkeypatch.setattr(local_file_access.os, "close", tracker.close)

    with reader.open_regular_file(path) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)

    assert tracker.opened_tokens != []
    assert sorted(tracker.closed_tokens) == sorted(tracker.opened_tokens)
    assert tracker.live_tokens == {}


def test_directory_close_base_exception_cleans_new_child_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    path = nested / "artifact.bin"
    path.write_bytes(b"content")
    reader = _reader(root)
    tracker = _DescriptorTracker()
    abort = _BodyAbort()
    injected = False

    def abort_after_first_transfer_close(descriptor: int) -> None:
        nonlocal injected
        tracker.close(descriptor)
        if not injected and len(tracker.opened_tokens) >= 2:
            injected = True
            raise abort

    monkeypatch.setattr(local_file_access.os, "open", tracker.open)
    monkeypatch.setattr(
        local_file_access.os,
        "close",
        abort_after_first_transfer_close,
    )

    with (
        pytest.raises(_BodyAbort) as caught,
        reader.open_regular_file(path),
    ):
        raise AssertionError("the context body must not run")

    assert caught.value is abort
    assert injected is True
    assert sorted(tracker.closed_tokens) == sorted(tracker.opened_tokens)
    assert len(tracker.closed_tokens) == len(set(tracker.closed_tokens))
    assert tracker.live_tokens == {}


def test_final_parent_close_base_exception_cleans_leaf_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    path.write_bytes(b"content")
    reader = _reader(root)
    tracker = _DescriptorTracker()
    abort = _BodyAbort()
    injected = False

    def abort_after_final_parent_close(descriptor: int) -> None:
        nonlocal injected
        tracker.close(descriptor)
        if not injected and tracker.leaf_token is not None:
            injected = True
            raise abort

    monkeypatch.setattr(local_file_access.os, "open", tracker.open)
    monkeypatch.setattr(
        local_file_access.os,
        "close",
        abort_after_final_parent_close,
    )

    with (
        pytest.raises(_BodyAbort) as caught,
        reader.open_regular_file(path),
    ):
        raise AssertionError("the context body must not run")

    assert caught.value is abort
    assert injected is True
    assert tracker.leaf_token is not None
    assert sorted(tracker.closed_tokens) == sorted(tracker.opened_tokens)
    assert len(tracker.closed_tokens) == len(set(tracker.closed_tokens))
    assert tracker.live_tokens == {}


def test_fstat_failure_closes_final_descriptor_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    path.write_bytes(b"content")
    reader = _reader(root)
    tracker = _DescriptorTracker()

    def failing_fstat(descriptor: int) -> os.stat_result:
        assert descriptor == tracker.leaf_descriptor
        raise OSError(errno.EIO, "sensitive fstat failure")

    monkeypatch.setattr(local_file_access.os, "open", tracker.open)
    monkeypatch.setattr(local_file_access.os, "close", tracker.close)
    monkeypatch.setattr(local_file_access.os, "fstat", failing_fstat)

    with reader.open_regular_file(path) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)

    assert tracker.leaf_token is not None
    assert tracker.closed_tokens.count(tracker.leaf_token) == 1
    assert tracker.leaf_token not in tracker.live_tokens.values()


def test_fdopen_failure_closes_final_descriptor_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    path.write_bytes(b"content")
    reader = _reader(root)
    tracker = _DescriptorTracker()

    def failing_fdopen(
        _descriptor: int,
        _mode: str,
        *,
        closefd: bool = True,
    ) -> BinaryIO:
        assert closefd is True
        raise OSError(errno.EIO, "sensitive fdopen failure")

    monkeypatch.setattr(local_file_access.os, "open", tracker.open)
    monkeypatch.setattr(local_file_access.os, "close", tracker.close)
    monkeypatch.setattr(local_file_access.os, "fdopen", failing_fdopen)

    with reader.open_regular_file(path) as opened:
        assert opened == LocalOpenFailure(LocalOpenFailureReason.IO_ERROR)

    assert tracker.leaf_token is not None
    assert tracker.closed_tokens.count(tracker.leaf_token) == 1
    assert tracker.leaf_token not in tracker.live_tokens.values()


def test_validation_base_exception_closes_final_descriptor_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_posix_openat()
    root = tmp_path / "store"
    root.mkdir()
    path = root / "artifact.bin"
    path.write_bytes(b"content")
    reader = _reader(root)
    tracker = _DescriptorTracker()
    abort = _BodyAbort()

    def abort_validation(_descriptor: int, _planned_path: str) -> str:
        raise abort

    monkeypatch.setattr(local_file_access.os, "open", tracker.open)
    monkeypatch.setattr(local_file_access.os, "close", tracker.close)
    monkeypatch.setattr(reader, "_validate_open_descriptor", abort_validation)

    with (
        pytest.raises(_BodyAbort) as caught,
        reader.open_regular_file(path),
    ):
        raise AssertionError("the context body must not run")

    assert caught.value is abort
    assert tracker.leaf_token is not None
    assert tracker.closed_tokens.count(tracker.leaf_token) == 1
    assert tracker.leaf_token not in tracker.live_tokens.values()
