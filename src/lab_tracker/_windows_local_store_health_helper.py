"""Output-free, handle-bound Windows directory predicate.

The application executes this file directly with ``python -I -S -B``.  Keep
the module self-contained and standard-library-only: its only observable
protocol is the process exit status.

The walk deliberately anchors the drive root with ``CreateFileW`` and opens
each subsequent component relative to the retained directory handle with
``NtCreateFile``.  ``OBJ_DONT_REPARSE`` prevents a lookup from following a
reparse point; older filesystems that reject that object-attribute flag get one
explicit fallback without it, after which the opened handle's attributes and
normalized final DOS path still have to match exactly.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Protocol, cast

LOCAL_STORE_HEALTH_ROOT_ENV = "LAB_TRACKER_INTERNAL_LOCAL_STORE_HEALTH_ROOT"

_HEALTHY_EXIT = 0
_UNREACHABLE_EXIT = 1
_GENERIC_FAILURE_DETAIL = "Windows local store health verification failed."

_FILE_READ_ATTRIBUTES_AND_TRAVERSE = 0x000000A0
_FILE_SHARE_READ_WRITE_DELETE = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_CREATE_FILE_FLAGS = _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT

_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_OPEN = 1
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_DONT_REPARSE = 0x00001000
_STATUS_INVALID_PARAMETER = 0xC000000D

_FILE_NAME_NORMALIZED_AND_VOLUME_NAME_DOS = 0
_INITIAL_PATH_BUFFER_CHARS = 260
_MAX_PATH_BUFFER_CHARS = 32_768
_MAX_NATIVE_PATH_UTF16_UNITS = 32_767
_MAX_UNICODE_STRING_BYTES = 65_532

_EXTENDED_DOS_DRIVE_PATH = re.compile(r"^\\\\\?\\([A-Za-z]):\\")
_NATIVE_DOS_DRIVE_PATH = re.compile(r"^([A-Za-z]):\\")
_WINDOWS_RESERVED_CHARACTERS = frozenset('"*:<>?|')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)

_BOOL = ctypes.c_int32
_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_NTSTATUS = ctypes.c_int32
_ULONG = ctypes.c_uint32
_USHORT = ctypes.c_uint16

CreateFile = Callable[
    [object, object, object, object, object, object, object],
    object,
]
GetFileInformationByHandle = Callable[[object, object], object]
GetFinalPathNameByHandle = Callable[[object, object, object, object], object]
CloseHandle = Callable[[object], object]
NtCreateFile = Callable[
    [
        object,
        object,
        object,
        object,
        object,
        object,
        object,
        object,
        object,
        object,
        object,
    ],
    object,
]


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", _DWORD),
        ("dwHighDateTime", _DWORD),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", _DWORD),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", _DWORD),
        ("nFileSizeHigh", _DWORD),
        ("nFileSizeLow", _DWORD),
        ("nNumberOfLinks", _DWORD),
        ("nFileIndexHigh", _DWORD),
        ("nFileIndexLow", _DWORD),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", _USHORT),
        ("MaximumLength", _USHORT),
        ("Buffer", ctypes.POINTER(ctypes.c_wchar)),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", _ULONG),
        ("RootDirectory", _HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", _ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK_VALUE(ctypes.Union):
    _fields_ = [
        ("Status", _NTSTATUS),
        ("Pointer", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [
        ("value", _IO_STATUS_BLOCK_VALUE),
        ("Information", ctypes.c_size_t),
    ]


class WindowsLocalStoreHealthError(RuntimeError):
    """A Windows handle operation could not prove the directory safe."""


class WindowsDirectoryApi(Protocol):
    """Minimal owned-handle operations used by the directory walk."""

    def open_root(self, root: str) -> int:
        """Open and return an owned drive-root handle."""

    def open_component(self, parent: int, component: str) -> int:
        """Open one component relative to an owned parent directory handle."""

    def file_attributes(self, handle: int) -> int:
        """Return attributes for the exact borrowed handle."""

    def normalized_dos_path(self, handle: int) -> str:
        """Return the normalized DOS path for the exact borrowed handle."""

    def close(self, handle: int) -> None:
        """Attempt to close the supplied handle."""


class _CreateFileFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        path: object,
        desired_access: object,
        share_mode: object,
        security_attributes: object,
        creation_disposition: object,
        flags_and_attributes: object,
        template_file: object,
    ) -> object: ...


class _GetFileInformationFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, handle: object, information: object) -> object: ...


class _GetFinalPathFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        handle: object,
        path_buffer: object,
        path_buffer_chars: object,
        flags: object,
    ) -> object: ...


class _CloseHandleFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(self, handle: object) -> object: ...


class _NtCreateFileFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        file_handle: object,
        desired_access: object,
        object_attributes: object,
        io_status_block: object,
        allocation_size: object,
        file_attributes: object,
        share_access: object,
        create_disposition: object,
        create_options: object,
        ea_buffer: object,
        ea_length: object,
    ) -> object: ...


class CtypesWindowsDirectoryApi:
    """ctypes implementation of the small owned-handle protocol."""

    def __init__(
        self,
        *,
        create_file: CreateFile | None = None,
        get_file_information_by_handle: GetFileInformationByHandle | None = None,
        get_final_path_name_by_handle: GetFinalPathNameByHandle | None = None,
        close_handle: CloseHandle | None = None,
        nt_create_file: NtCreateFile | None = None,
    ) -> None:
        kernel32: object | None = None
        ntdll: object | None = None
        if (
            create_file is None
            or get_file_information_by_handle is None
            or get_final_path_name_by_handle is None
            or close_handle is None
        ):
            kernel32 = _load_windows_library("kernel32")
        if nt_create_file is None:
            ntdll = _load_windows_library("ntdll")

        self._create_file = (
            create_file
            if create_file is not None
            else self._bind_create_file(_required_library(kernel32))
        )
        self._get_file_information_by_handle = (
            get_file_information_by_handle
            if get_file_information_by_handle is not None
            else self._bind_get_file_information(_required_library(kernel32))
        )
        self._get_final_path_name_by_handle = (
            get_final_path_name_by_handle
            if get_final_path_name_by_handle is not None
            else self._bind_get_final_path(_required_library(kernel32))
        )
        self._close_handle = (
            close_handle
            if close_handle is not None
            else self._bind_close_handle(_required_library(kernel32))
        )
        self._nt_create_file = (
            nt_create_file
            if nt_create_file is not None
            else self._bind_nt_create_file(_required_library(ntdll))
        )

    def open_root(self, root: str) -> int:
        """Open a drive root without following a final reparse point."""

        try:
            canonical_root, components, canonical = _parse_native_dos_path(root)
            if components or canonical != canonical_root:
                raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
            raw_handle = self._create_file(
                f"\\\\?\\{canonical_root}",
                _DWORD(_FILE_READ_ATTRIBUTES_AND_TRAVERSE),
                _DWORD(_FILE_SHARE_READ_WRITE_DELETE),
                None,
                _DWORD(_OPEN_EXISTING),
                _DWORD(_CREATE_FILE_FLAGS),
                None,
            )
            handle = _as_int(raw_handle)
        except Exception:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
        if not _is_valid_handle(handle):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        return handle

    def open_component(self, parent: int, component: str) -> int:
        """Open one strict component relative to ``parent``."""

        if not _is_valid_handle(parent) or not _is_valid_component(component):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

        encoded_component = component.encode("utf-16-le", errors="strict")
        utf16_length = len(encoded_component)
        if not (0 < utf16_length <= _MAX_UNICODE_STRING_BYTES):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        # Build the native UTF-16 buffer explicitly.  Python counts a
        # non-BMP character as one code point, while Windows UNICODE_STRING
        # counts its surrogate pair as two WCHARs.
        name_buffer = ctypes.create_string_buffer(encoded_component + b"\0\0")
        object_name = _UNICODE_STRING(
            Length=_USHORT(utf16_length),
            MaximumLength=_USHORT(utf16_length + 2),
            Buffer=ctypes.cast(
                name_buffer,
                ctypes.POINTER(ctypes.c_wchar),
            ),
        )

        status, handle = self._nt_open_component(
            parent,
            object_name,
            _OBJ_DONT_REPARSE,
        )
        if _status_code(status) == _STATUS_INVALID_PARAMETER:
            self._close_failed_output(handle)
            status, handle = self._nt_open_component(parent, object_name, 0)

        if not _nt_success(status) or not _is_valid_handle(handle):
            self._close_failed_output(handle)
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        return cast(int, handle)

    def file_attributes(self, handle: int) -> int:
        """Read attributes from the exact open object."""

        if not _is_valid_handle(handle):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        information = _BY_HANDLE_FILE_INFORMATION()
        try:
            succeeded = self._get_file_information_by_handle(
                _HANDLE(handle),
                ctypes.byref(information),
            )
        except Exception:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
        if not _as_bool(succeeded):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        return int(information.dwFileAttributes)

    def normalized_dos_path(self, handle: int) -> str:
        """Return a strict normalized native DOS path for ``handle``."""

        if not _is_valid_handle(handle):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

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
                raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None

            if length <= 0:
                raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
            if length >= capacity:
                if length <= capacity or length > _MAX_PATH_BUFFER_CHARS:
                    raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
                capacity = length
                continue

            final_path = path_buffer.value
            try:
                final_path_units = len(final_path.encode("utf-16-le", errors="strict")) // 2
            except UnicodeError:
                raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
            if final_path_units != length:
                raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
            return _native_path_from_extended_final_path(final_path)

    def close(self, handle: int) -> None:
        """Attempt exactly one ``CloseHandle`` call for ``handle``."""

        if not _is_valid_handle(handle):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        try:
            succeeded = self._close_handle(_HANDLE(handle))
        except Exception:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
        if not _as_bool(succeeded):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

    def _nt_open_component(
        self,
        parent: int,
        object_name: _UNICODE_STRING,
        attributes: int,
    ) -> tuple[int, int | None]:
        output_handle = _HANDLE()
        io_status = _IO_STATUS_BLOCK()
        object_attributes = _OBJECT_ATTRIBUTES(
            Length=_ULONG(ctypes.sizeof(_OBJECT_ATTRIBUTES)),
            RootDirectory=_HANDLE(parent),
            ObjectName=ctypes.pointer(object_name),
            Attributes=_ULONG(attributes),
            SecurityDescriptor=None,
            SecurityQualityOfService=None,
        )
        try:
            raw_status = self._nt_create_file(
                ctypes.byref(output_handle),
                _DWORD(_FILE_READ_ATTRIBUTES_AND_TRAVERSE),
                ctypes.byref(object_attributes),
                ctypes.byref(io_status),
                None,
                _ULONG(0),
                _ULONG(_FILE_SHARE_READ_WRITE_DELETE),
                _ULONG(_FILE_OPEN),
                _ULONG(_FILE_OPEN_REPARSE_POINT),
                None,
                _ULONG(0),
            )
            status = _as_int(raw_status)
        except BaseException:
            self._close_failed_output(output_handle.value)
            raise
        return status, output_handle.value

    def _close_failed_output(self, handle: int | None) -> None:
        if _is_valid_handle(handle):
            self.close(cast(int, handle))

    @staticmethod
    def _bind_create_file(library: object) -> _CreateFileFunction:
        function = cast(
            _CreateFileFunction,
            _library_function(library, "CreateFileW"),
        )
        function.argtypes = [
            ctypes.c_wchar_p,
            _DWORD,
            _DWORD,
            ctypes.c_void_p,
            _DWORD,
            _DWORD,
            _HANDLE,
        ]
        function.restype = _HANDLE
        return function

    @staticmethod
    def _bind_get_file_information(
        library: object,
    ) -> _GetFileInformationFunction:
        function = cast(
            _GetFileInformationFunction,
            _library_function(library, "GetFileInformationByHandle"),
        )
        function.argtypes = [
            _HANDLE,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        function.restype = _BOOL
        return function

    @staticmethod
    def _bind_get_final_path(library: object) -> _GetFinalPathFunction:
        function = cast(
            _GetFinalPathFunction,
            _library_function(library, "GetFinalPathNameByHandleW"),
        )
        function.argtypes = [
            _HANDLE,
            ctypes.POINTER(ctypes.c_wchar),
            _DWORD,
            _DWORD,
        ]
        function.restype = _DWORD
        return function

    @staticmethod
    def _bind_close_handle(library: object) -> _CloseHandleFunction:
        function = cast(
            _CloseHandleFunction,
            _library_function(library, "CloseHandle"),
        )
        function.argtypes = [_HANDLE]
        function.restype = _BOOL
        return function

    @staticmethod
    def _bind_nt_create_file(library: object) -> _NtCreateFileFunction:
        function = cast(
            _NtCreateFileFunction,
            _library_function(library, "NtCreateFile"),
        )
        function.argtypes = [
            ctypes.POINTER(_HANDLE),
            _DWORD,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            _ULONG,
            _ULONG,
            _ULONG,
            _ULONG,
            ctypes.c_void_p,
            _ULONG,
        ]
        function.restype = _NTSTATUS
        return function


def _load_windows_library(name: str) -> object:
    try:
        loader = cast(
            Callable[..., object],
            getattr(ctypes, "WinDLL"),  # noqa: B009
        )
        return loader(name, use_last_error=True)
    except (AttributeError, OSError):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None


def _required_library(library: object | None) -> object:
    if library is None:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    return library


def _library_function(
    library: object,
    name: str,
) -> object:
    try:
        return getattr(library, name)  # noqa: B009
    except AttributeError:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None


def _as_int(value: object) -> int:
    raw = getattr(value, "value", value)
    if not isinstance(raw, int):
        raise TypeError
    return raw


def _as_bool(value: object) -> bool:
    return bool(_as_int(value))


def _is_valid_handle(handle: object) -> bool:
    if not isinstance(handle, int):
        return False
    invalid_pointer = ctypes.c_void_p(-1).value
    return handle not in (0, -1, invalid_pointer)


def _status_code(status: int) -> int:
    return status & 0xFFFFFFFF


def _nt_success(status: int) -> bool:
    return bool((_status_code(status) & 0x80000000) == 0)


def _is_reserved_component(component: str) -> bool:
    stem = component.partition(".")[0].rstrip(" ").upper()
    return stem in _WINDOWS_RESERVED_NAMES


def _is_valid_component(component: str) -> bool:
    if (
        not component
        or component in (".", "..")
        or component[-1:] in (".", " ")
        or "\\" in component
        or "/" in component
        or any(character in _WINDOWS_RESERVED_CHARACTERS for character in component)
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
        or _is_reserved_component(component)
    ):
        return False
    try:
        encoded_length = len(component.encode("utf-16-le", errors="strict"))
    except UnicodeError:
        return False
    return 0 < encoded_length <= _MAX_UNICODE_STRING_BYTES


def _parse_native_dos_path(path: str) -> tuple[str, tuple[str, ...], str]:
    if not isinstance(path, str):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    match = _NATIVE_DOS_DRIVE_PATH.match(path)
    if match is None or "/" in path:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

    drive_root = f"{match.group(1).upper()}:\\"
    tail = path[3:]
    components = () if not tail else tuple(tail.split("\\"))
    if any(not _is_valid_component(component) for component in components):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

    canonical = drive_root + "\\".join(components)
    try:
        utf16_units = len(canonical.encode("utf-16-le", errors="strict")) // 2
    except UnicodeError:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
    if utf16_units > _MAX_NATIVE_PATH_UTF16_UNITS:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    return drive_root, components, canonical


def _native_path_from_extended_final_path(final_path: str) -> str:
    match = _EXTENDED_DOS_DRIVE_PATH.match(final_path)
    if match is None:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    native = final_path[4:]
    _root, _components, canonical = _parse_native_dos_path(native)
    if canonical != native:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    return canonical


def _is_plain_directory(attributes: int) -> bool:
    return bool(attributes & _FILE_ATTRIBUTE_DIRECTORY) and not bool(
        attributes & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validate_open_directory(
    api: WindowsDirectoryApi,
    handle: int,
    expected_path: str,
) -> None:
    if not _is_plain_directory(api.file_attributes(handle)):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    if api.normalized_dos_path(handle) != expected_path:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)


def is_handle_bound_plain_directory(
    path: str,
    *,
    api: WindowsDirectoryApi | None = None,
) -> bool:
    """Return whether the exact handle-walked object is traversable and plain.

    Explicit handle cleanup is best effort. Helper-process exit backstops
    failed closes and asynchronous interruption windows.
    """

    owned_handles: list[int] = []
    healthy = False
    try:
        root, components, expected_path = _parse_native_dos_path(path)
        directory_api = CtypesWindowsDirectoryApi() if api is None else api

        current = directory_api.open_root(root)
        owned_handles.append(current)
        _validate_open_directory(directory_api, current, root)

        cumulative_path = root.rstrip("\\")
        for component in components:
            child = directory_api.open_component(current, component)
            owned_handles.append(child)
            cumulative_path = f"{cumulative_path}\\{component}"
            _validate_open_directory(directory_api, child, cumulative_path)

            parent = owned_handles.pop(0)
            directory_api.close(parent)
            current = child

        if cumulative_path != expected_path.rstrip("\\"):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        healthy = True
    except BaseException:
        healthy = False

    while owned_handles:
        handle = owned_handles.pop()
        try:
            directory_api.close(handle)
        except BaseException:
            healthy = False
    return healthy


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    api: WindowsDirectoryApi | None = None,
) -> int:
    """Return zero only for one valid, handle-bound directory request."""

    try:
        arguments = sys.argv if argv is None else argv
        environment = os.environ if environ is None else environ
        if len(arguments) != 1:
            return _UNREACHABLE_EXIT
        root = environment.get(LOCAL_STORE_HEALTH_ROOT_ENV)
        if not root or "\0" in root:
            return _UNREACHABLE_EXIT
        return (
            _HEALTHY_EXIT if is_handle_bound_plain_directory(root, api=api) else _UNREACHABLE_EXIT
        )
    except BaseException:
        return _UNREACHABLE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
