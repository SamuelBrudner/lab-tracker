from __future__ import annotations

import ctypes
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from lab_tracker._windows_local_file import (
    CtypesWindowsFinalPathApi,
    WindowsFinalPathError,
)

_HANDLE = 734
_STATIC_ERROR = "Windows final-path verification failed."


def _as_int(value: object) -> int:
    raw = getattr(value, "value", value)
    assert isinstance(raw, int)
    return raw


def _write_wide_string(raw_buffer: object, value: str) -> None:
    buffer = ctypes.cast(
        cast(Any, raw_buffer),
        ctypes.POINTER(ctypes.c_wchar),
    )
    for index, character in enumerate(value):
        buffer[index] = character
    buffer[len(value)] = "\x00"


class FakeGetFinalPath:
    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[tuple[int, int, int]] = []

    def __call__(
        self,
        raw_handle: object,
        raw_buffer: object,
        raw_capacity: object,
        raw_flags: object,
    ) -> int:
        handle = _as_int(raw_handle)
        capacity = _as_int(raw_capacity)
        flags = _as_int(raw_flags)
        self.calls.append((handle, capacity, flags))
        required = len(self.path) + 1
        if capacity < required:
            return required
        _write_wide_string(raw_buffer, self.path)
        return len(self.path)


def _api(
    get_final_path: Callable[[object, object, object, object], object],
    *,
    get_osfhandle: Callable[[int], int] | None = None,
) -> CtypesWindowsFinalPathApi:
    return CtypesWindowsFinalPathApi(
        get_osfhandle=get_osfhandle or (lambda _fd: _HANDLE),
        get_final_path_name_by_handle=get_final_path,
    )


def test_normalized_dos_path_queries_the_borrowed_handle_with_exact_flags() -> None:
    query = FakeGetFinalPath(r"\\?\C:\allowed\artifact.bin")
    borrowed_fds: list[int] = []

    def borrow_handle(fd: int) -> int:
        borrowed_fds.append(fd)
        return _HANDLE

    api = _api(
        query,
        get_osfhandle=borrow_handle,
    )

    assert api.normalized_dos_path(19) == r"C:\allowed\artifact.bin"
    assert borrowed_fds == [19]
    assert query.calls == [(_HANDLE, 260, 0)]


def test_normalized_dos_path_preserves_unicode_tail() -> None:
    query = FakeGetFinalPath("\\\\?\\D:\\données\\résultat.bin")

    assert _api(query).normalized_dos_path(4) == "D:\\données\\résultat.bin"


def test_normalized_dos_path_retries_with_the_reported_required_capacity() -> None:
    path = "\\\\?\\C:\\" + ("nested\\" * 45) + "artifact.bin"
    query = FakeGetFinalPath(path)

    assert _api(query).normalized_dos_path(8) == path[4:]
    assert query.calls == [
        (_HANDLE, 260, 0),
        (_HANDLE, len(path) + 1, 0),
    ]


@pytest.mark.parametrize("result", [0, -1])
def test_query_failure_is_static(result: int) -> None:
    def fail_query(*_args: object) -> int:
        return result

    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$"):
        _api(fail_query).normalized_dos_path(1)


def test_query_exception_is_static() -> None:
    def fail_query(*_args: object) -> int:
        raise OSError("sensitive path detail")

    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$") as caught:
        _api(fail_query).normalized_dos_path(1)

    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_get_osfhandle_failure_is_static() -> None:
    def fail_get_osfhandle(_fd: int) -> int:
        raise OSError("sensitive descriptor detail")

    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$") as caught:
        _api(
            FakeGetFinalPath(r"\\?\C:\allowed\artifact.bin"),
            get_osfhandle=fail_get_osfhandle,
        ).normalized_dos_path(1)

    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("handle", [0, -1, ctypes.c_void_p(-1).value])
def test_invalid_borrowed_handle_is_rejected(handle: int | None) -> None:
    def get_invalid_handle(_fd: int) -> int:
        assert handle is not None
        return handle

    query = FakeGetFinalPath(r"\\?\C:\allowed\artifact.bin")
    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$"):
        _api(query, get_osfhandle=get_invalid_handle).normalized_dos_path(1)

    assert query.calls == []


def test_required_buffer_over_the_bound_is_rejected_without_retry() -> None:
    calls = 0

    def oversized_query(*_args: object) -> int:
        nonlocal calls
        calls += 1
        return 32_769

    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$"):
        _api(oversized_query).normalized_dos_path(1)

    assert calls == 1


def test_non_growing_resize_response_is_rejected() -> None:
    def malformed_query(
        _handle: object,
        _buffer: object,
        capacity: object,
        _flags: object,
    ) -> int:
        return _as_int(capacity)

    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$"):
        _api(malformed_query).normalized_dos_path(1)


def test_inconsistent_success_length_is_rejected_as_truncated() -> None:
    path = r"\\?\C:\allowed\artifact.bin"

    def truncated_query(
        _handle: object,
        buffer: object,
        _capacity: object,
        _flags: object,
    ) -> int:
        _write_wide_string(buffer, path)
        return len(path) - 1

    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$"):
        _api(truncated_query).normalized_dos_path(1)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        r"\\?\UNC\server\share\artifact.bin",
        r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\artifact.bin",
        r"\\?\GLOBALROOT\Device\HarddiskVolume1\artifact.bin",
        r"\\?\PIPE\artifact",
        r"\\?\PhysicalDrive0",
        r"\\.\C:\allowed\artifact.bin",
        r"\Device\HarddiskVolume1\allowed\artifact.bin",
        r"\??\C:\allowed\artifact.bin",
        r"C:\allowed\artifact.bin",
        r"\\?\1:\allowed\artifact.bin",
        "\\\\?\\é:\\allowed\\artifact.bin",
        r"\\?\C:relative\artifact.bin",
        r"\\?\C:/allowed/artifact.bin",
        r"\\?\C:\allowed\..\artifact.bin",
        r"\\?\C:\allowed\artifact.bin:stream",
        "\\\\?\\C:\\allowed\\line\nbreak.bin",
        r"\\?\C:\allowed\\artifact.bin",
        "\\\\?\\C:\\allowed\\artifact.bin\x00suffix",
        r"\\?\C:\allowed\artifact.bin.",
        "\\\\?\\C:\\allowed\\artifact.bin ",
    ],
)
def test_non_drive_or_malformed_namespaces_are_rejected(unsafe_path: str) -> None:
    with pytest.raises(WindowsFinalPathError, match=f"^{_STATIC_ERROR}$"):
        _api(FakeGetFinalPath(unsafe_path)).normalized_dos_path(1)


def test_adapter_never_requests_or_closes_the_borrowed_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKernel32:
        def __init__(self) -> None:
            self.GetFinalPathNameByHandleW = FakeGetFinalPath(
                r"\\?\C:\allowed\artifact.bin"
            )

        @property
        def CloseHandle(self) -> object:
            raise AssertionError("borrowed handle must never be closed")

    kernel32 = FakeKernel32()
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )
    api = CtypesWindowsFinalPathApi(get_osfhandle=lambda _fd: _HANDLE)

    assert api.normalized_dos_path(7) == r"C:\allowed\artifact.bin"
    assert kernel32.GetFinalPathNameByHandleW.calls == [(_HANDLE, 260, 0)]


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows file handle")
def test_real_windows_file_handle_round_trips_without_closing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "artifact.bin"
    path.write_bytes(b"content")
    api = CtypesWindowsFinalPathApi()

    with path.open("rb") as handle:
        final_path = Path(api.normalized_dos_path(handle.fileno()))
        assert os.path.samefile(final_path, path)
        assert handle.read() == b"content"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_real_windows_junction_reports_the_opened_target_path(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    target_file = target / "artifact.bin"
    target_file.write_bytes(b"content")
    junction = tmp_path / "junction"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    api = CtypesWindowsFinalPathApi()

    with (junction / target_file.name).open("rb") as handle:
        final_path = Path(api.normalized_dos_path(handle.fileno()))

    assert os.path.samefile(final_path, target_file)
    assert junction.name not in final_path.parts
