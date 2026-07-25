"""Private final-path verification for already-open Windows files.

The caller owns the Python file descriptor and its underlying Win32 handle.
This module borrows that handle long enough to ask Windows for the normalized
DOS path of the exact open file object; it never closes or duplicates either
resource.
"""

from __future__ import annotations

import ctypes
import importlib
import re
from collections.abc import Callable
from typing import Protocol, cast

_FILE_NAME_NORMALIZED_AND_VOLUME_NAME_DOS = 0
_INITIAL_PATH_BUFFER_CHARS = 260
_MAX_PATH_BUFFER_CHARS = 32_768
_EXTENDED_DOS_DRIVE_PATH = re.compile(r"^\\\\\?\\([A-Za-z]):\\")
_GENERIC_FAILURE_DETAIL = "Windows final-path verification failed."

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p

GetOsfhandle = Callable[[int], int]
GetFinalPathNameByHandle = Callable[[object, object, object, object], object]


class WindowsFinalPathError(RuntimeError):
    """An opened Windows file could not be mapped to a safe native DOS path."""


class WindowsFinalPathApi(Protocol):
    """Resolve the normalized path of an already-open Windows file descriptor."""

    def normalized_dos_path(self, fd: int) -> str:
        """Return a strict native drive path while retaining caller ownership."""


class _WinFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        handle: object,
        path_buffer: object,
        path_buffer_chars: object,
        flags: object,
    ) -> object: ...


class CtypesWindowsFinalPathApi:
    """ctypes-backed final-path query over a borrowed CRT file handle."""

    def __init__(
        self,
        *,
        get_osfhandle: GetOsfhandle | None = None,
        get_final_path_name_by_handle: GetFinalPathNameByHandle | None = None,
    ) -> None:
        if get_osfhandle is None:
            try:
                msvcrt = importlib.import_module("msvcrt")
                get_osfhandle = cast(
                    GetOsfhandle,
                    getattr(msvcrt, "get_osfhandle"),  # noqa: B009
                )
            except (AttributeError, ImportError):
                raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL) from None
        self._get_osfhandle = get_osfhandle

        self._kernel32: object | None = None
        if get_final_path_name_by_handle is None:
            try:
                loader = cast(
                    Callable[..., object],
                    getattr(ctypes, "WinDLL"),  # noqa: B009
                )
                self._kernel32 = loader("kernel32", use_last_error=True)
                get_final_path_name_by_handle = self._bind_get_final_path()
            except (AttributeError, OSError):
                raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL) from None
        self._get_final_path_name_by_handle = get_final_path_name_by_handle

    def normalized_dos_path(self, fd: int) -> str:
        """Return the normalized DOS path for ``fd`` without taking ownership."""

        try:
            handle = self._get_osfhandle(fd)
        except Exception:
            raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL) from None
        if not _is_valid_handle(handle):
            raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)

        capacity = _INITIAL_PATH_BUFFER_CHARS
        while True:
            path_buffer = ctypes.create_unicode_buffer(capacity)
            try:
                raw_length = self._get_final_path_name_by_handle(
                    _HANDLE(handle),
                    path_buffer,
                    _DWORD(capacity),
                    _DWORD(_FILE_NAME_NORMALIZED_AND_VOLUME_NAME_DOS),
                )
                length = _as_int(raw_length)
            except Exception:
                raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL) from None

            if length <= 0:
                raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)
            if length >= capacity:
                if length <= capacity or length > _MAX_PATH_BUFFER_CHARS:
                    raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)
                capacity = length
                continue

            final_path = path_buffer.value
            if len(final_path) != length:
                raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)
            return _native_drive_path(final_path)

    def _bind_get_final_path(self) -> _WinFunction:
        assert self._kernel32 is not None
        try:
            function = cast(
                _WinFunction,
                getattr(  # noqa: B009
                    self._kernel32,
                    "GetFinalPathNameByHandleW",
                ),
            )
        except AttributeError:
            raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL) from None
        function.argtypes = [
            _HANDLE,
            ctypes.POINTER(ctypes.c_wchar),
            _DWORD,
            _DWORD,
        ]
        function.restype = _DWORD
        return function


def _as_int(value: object) -> int:
    raw = getattr(value, "value", value)
    if not isinstance(raw, int):
        raise TypeError
    return raw


def _is_valid_handle(handle: object) -> bool:
    if not isinstance(handle, int):
        return False
    invalid_pointer = ctypes.c_void_p(-1).value
    return handle not in (0, -1, invalid_pointer)


def _native_drive_path(final_path: str) -> str:
    match = _EXTENDED_DOS_DRIVE_PATH.match(final_path)
    if match is None:
        raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)

    native = final_path[4:]
    if (
        "/" in native
        or "\x00" in native
        or any(ord(character) < 32 or ord(character) == 127 for character in native)
        or ":" in native[2:]
    ):
        raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)

    components = native[3:].split("\\")
    if any(
        component in (".", "..")
        or component == ""
        or component[-1:] in (".", " ")
        for component in components
    ):
        raise WindowsFinalPathError(_GENERIC_FAILURE_DETAIL)
    return f"{match.group(1)}:{native[2:]}"
