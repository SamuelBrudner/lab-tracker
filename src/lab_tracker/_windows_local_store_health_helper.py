"""Output-free, pre-follow-safe Windows directory inspection helper.

The application executes this file directly with ``python -I -S -B``.  Keep
the module self-contained and standard-library-only.  The parent supplies one
small, versioned JSON request in a dedicated environment variable; this helper
communicates only through its exit status.

Admission is deliberately lexical and performs no Windows API call.  Once a
single operator grant has been selected, the helper anchors that root and
resolves every name-surrogate reparse point itself.  Components are opened one
at a time relative to retained handles with final-component following disabled.
At no point is a followed handle converted back into a pathname.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast

LOCAL_FILESYSTEM_REQUEST_ENV = "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
LOCAL_FILESYSTEM_PROTOCOL_VERSION = 1
LOCAL_FILESYSTEM_REQUEST_VERSION = LOCAL_FILESYSTEM_PROTOCOL_VERSION
LOCAL_FILESYSTEM_INSPECT_DIRECTORY_OP = "inspect-directory"
LOCAL_FILESYSTEM_ACCESSIBLE_EXIT = 0
LOCAL_FILESYSTEM_DENIED_EXIT = 2
LOCAL_FILESYSTEM_FAILED_EXIT = 3

MAX_LOCAL_FILESYSTEM_REQUEST_BYTES = 24 * 1024
MAX_LOCAL_FILESYSTEM_ROOTS = 64
MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS = MAX_LOCAL_FILESYSTEM_ROOTS

_GENERIC_FAILURE_DETAIL = "Windows local filesystem verification failed."

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
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_NAME_NORMALIZED_AND_VOLUME_NAME_DOS = 0
_OBJ_DONT_REPARSE = 0x00001000
_REPARSE_TAG_DIRECTORY = 0x10000000
_REPARSE_TAG_NAME_SURROGATE = 0x20000000
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_SYMLINK_FLAG_RELATIVE = 0x00000001
_STATUS_INVALID_PARAMETER = 0xC000000D
_STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B

_FSCTL_GET_REPARSE_POINT = 0x000900A8
_MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024
_REPARSE_HEADER_BYTES = 8
_MOUNT_POINT_FIXED_PAYLOAD_BYTES = 8
_SYMLINK_FIXED_PAYLOAD_BYTES = 12

_MAX_NATIVE_PATH_UTF16_UNITS = 32_767
_MAX_UNICODE_STRING_BYTES = 65_532
_INITIAL_PATH_BUFFER_CHARS = 260
_MAX_PATH_BUFFER_CHARS = 32_768
_MAX_RESOLUTION_COMPONENTS = 4_096
_MAX_NAME_SURROGATE_EXPANSIONS = 256

_NATIVE_DOS_DRIVE_PATH = re.compile(r"^([A-Za-z]):\\")
_NT_DOS_DRIVE_TARGET = re.compile(r"^\\\?\?\\([A-Za-z]):\\")
_WINDOWS_RESERVED_CHARACTERS = frozenset('"*:<>?|')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{suffix}" for suffix in "123456789¹²³"}
    | {f"LPT{suffix}" for suffix in "123456789¹²³"}
)

_BOOL = ctypes.c_int32
_DWORD = ctypes.c_uint32
_FILE_INFO_BY_HANDLE_CLASS = ctypes.c_int
_HANDLE = ctypes.c_void_p
_NTSTATUS = ctypes.c_int32
_ULONG = ctypes.c_uint32
_USHORT = ctypes.c_uint16

CreateFile = Callable[
    [object, object, object, object, object, object, object],
    object,
]
GetFileInformationByHandleEx = Callable[
    [object, object, object, object],
    object,
]
GetFinalPathNameByHandle = Callable[[object, object, object, object], object]
DeviceIoControl = Callable[
    [object, object, object, object, object, object, object, object],
    object,
]
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


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("FileAttributes", _DWORD),
        ("ReparseTag", _DWORD),
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
    """The helper or adapter could not safely complete an operation."""


class WindowsLocalStoreHealthDenied(RuntimeError):
    """The requested directory could not be proven accessible and authorized."""


@dataclass(frozen=True)
class _DosPath:
    drive: str
    components: tuple[str, ...]

    @property
    def root(self) -> str:
        return f"{self.drive}:\\"

    @property
    def native(self) -> str:
        return self.root + "\\".join(self.components)


@dataclass(frozen=True)
class _InspectionRequest:
    candidate: _DosPath
    selected_root: _DosPath


@dataclass(frozen=True)
class _ReparseTarget:
    absolute: _DosPath | None
    relative_tokens: tuple[str, ...]


@dataclass
class _OwnedDirectory:
    handle: int
    drive: str
    components: tuple[str, ...]


class WindowsDirectoryApi(Protocol):
    """Minimal owned-handle operations used by the resolver."""

    def open_root(self, root: str) -> int:
        """Open and return an owned DOS drive-root handle."""

    def open_component(self, parent: int, component: str) -> int:
        """Open one component relative to an owned parent handle."""

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        """Return attributes and reparse tag for the exact borrowed handle."""

    def read_reparse_point(self, handle: int) -> bytes:
        """Return the bounded raw reparse buffer for the exact borrowed handle."""

    def normalized_dos_path(self, handle: int) -> str:
        """Return the normalized strict DOS path of a non-surrogate handle."""

    def close(self, handle: int) -> None:
        """Close the supplied owned handle or raise."""


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


class _GetFileInformationExFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        handle: object,
        information_class: object,
        information: object,
        information_size: object,
    ) -> object: ...


class _DeviceIoControlFunction(Protocol):
    argtypes: list[object]
    restype: object

    def __call__(
        self,
        handle: object,
        control_code: object,
        input_buffer: object,
        input_size: object,
        output_buffer: object,
        output_size: object,
        bytes_returned: object,
        overlapped: object,
    ) -> object: ...


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
        get_file_information_by_handle_ex: GetFileInformationByHandleEx | None = None,
        get_final_path_name_by_handle: GetFinalPathNameByHandle | None = None,
        device_io_control: DeviceIoControl | None = None,
        close_handle: CloseHandle | None = None,
        nt_create_file: NtCreateFile | None = None,
    ) -> None:
        kernel32: object | None = None
        ntdll: object | None = None
        if (
            create_file is None
            or get_file_information_by_handle_ex is None
            or get_final_path_name_by_handle is None
            or device_io_control is None
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
        self._get_file_information_by_handle_ex = (
            get_file_information_by_handle_ex
            if get_file_information_by_handle_ex is not None
            else self._bind_get_file_information_ex(_required_library(kernel32))
        )
        self._device_io_control = (
            device_io_control
            if device_io_control is not None
            else self._bind_device_io_control(_required_library(kernel32))
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
        """Open a strict drive root without following a final reparse point."""

        parsed = _parse_dos_path(root)
        if parsed.components:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        try:
            raw_handle = self._create_file(
                f"\\\\?\\{parsed.root}",
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
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return handle

    def open_component(self, parent: int, component: str) -> int:
        """Open one strict component relative to ``parent``."""

        if not _is_valid_handle(parent) or not _is_valid_component(component):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

        encoded_component = component.encode("utf-16-le", errors="strict")
        utf16_length = len(encoded_component)
        if not (0 < utf16_length <= _MAX_UNICODE_STRING_BYTES):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        name_buffer = ctypes.create_string_buffer(encoded_component + b"\0\0")
        object_name = _UNICODE_STRING(
            Length=_USHORT(utf16_length),
            MaximumLength=_USHORT(utf16_length + 2),
            Buffer=ctypes.cast(name_buffer, ctypes.POINTER(ctypes.c_wchar)),
        )

        status, handle = self._nt_open_component(
            parent,
            object_name,
            _OBJ_DONT_REPARSE,
        )
        if _status_code(status) in (
            _STATUS_INVALID_PARAMETER,
            _STATUS_REPARSE_POINT_ENCOUNTERED,
        ):
            self._close_failed_output(handle)
            status, handle = self._nt_open_component(parent, object_name, 0)

        if _status_code(status) != 0 or not _is_valid_handle(handle):
            self._close_failed_output(handle)
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return cast(int, handle)

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        """Read attributes and tag from the exact open object."""

        if not _is_valid_handle(handle):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        information = _FILE_ATTRIBUTE_TAG_INFO()
        try:
            succeeded = self._get_file_information_by_handle_ex(
                _HANDLE(handle),
                _FILE_INFO_BY_HANDLE_CLASS(_FILE_ATTRIBUTE_TAG_INFO_CLASS),
                ctypes.byref(information),
                _DWORD(ctypes.sizeof(information)),
            )
        except Exception:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
        if not _as_bool(succeeded):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return int(information.FileAttributes), int(information.ReparseTag)

    def read_reparse_point(self, handle: int) -> bytes:
        """Read exactly one bounded ``REPARSE_DATA_BUFFER`` from ``handle``."""

        if not _is_valid_handle(handle):
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        buffer = ctypes.create_string_buffer(_MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = _DWORD()
        try:
            succeeded = self._device_io_control(
                _HANDLE(handle),
                _DWORD(_FSCTL_GET_REPARSE_POINT),
                None,
                _DWORD(0),
                buffer,
                _DWORD(_MAXIMUM_REPARSE_DATA_BUFFER_SIZE),
                ctypes.byref(returned),
                None,
            )
        except Exception:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
        if not _as_bool(succeeded):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        length = int(returned.value)
        if not (_REPARSE_HEADER_BYTES <= length <= _MAXIMUM_REPARSE_DATA_BUFFER_SIZE):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return bytes(buffer.raw[:length])

    def normalized_dos_path(self, handle: int) -> str:
        """Return a strict normalized DOS-drive path for ``handle``."""

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
                raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
            if length >= capacity:
                if length > _MAX_PATH_BUFFER_CHARS:
                    raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
                capacity = length + 1
                continue
            value = path_buffer.value
            try:
                units = len(value.encode("utf-16-le", errors="strict")) // 2
            except UnicodeError:
                raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL) from None
            if units != length or not value.startswith("\\\\?\\"):
                raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
            return _parse_dos_path(value[4:]).native

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
    def _bind_get_file_information_ex(
        library: object,
    ) -> _GetFileInformationExFunction:
        function = cast(
            _GetFileInformationExFunction,
            _library_function(library, "GetFileInformationByHandleEx"),
        )
        function.argtypes = [
            _HANDLE,
            _FILE_INFO_BY_HANDLE_CLASS,
            ctypes.c_void_p,
            _DWORD,
        ]
        function.restype = _BOOL
        return function

    @staticmethod
    def _bind_device_io_control(library: object) -> _DeviceIoControlFunction:
        function = cast(
            _DeviceIoControlFunction,
            _library_function(library, "DeviceIoControl"),
        )
        function.argtypes = [
            _HANDLE,
            _DWORD,
            ctypes.c_void_p,
            _DWORD,
            ctypes.c_void_p,
            _DWORD,
            ctypes.POINTER(_DWORD),
            ctypes.c_void_p,
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
        loader = cast(Callable[..., object], getattr(ctypes, "WinDLL"))  # noqa: B009
        return loader(name, use_last_error=True)
    except (AttributeError, OSError):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None


def _required_library(library: object | None) -> object:
    if library is None:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    return library


def _library_function(library: object, name: str) -> object:
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


def _parse_dos_path(path: str) -> _DosPath:
    return _parse_native_path(path, allow_dot_tokens=False)


def _parse_candidate_dos_path(path: str) -> _DosPath:
    return _parse_native_path(path, allow_dot_tokens=True)


def _parse_native_path(path: str, *, allow_dot_tokens: bool) -> _DosPath:
    if not isinstance(path, str):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    match = _NATIVE_DOS_DRIVE_PATH.match(path)
    if match is None or "/" in path or "\0" in path:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    drive = match.group(1).upper()
    tail = path[3:]
    components = () if not tail else tuple(tail.split("\\"))
    if any(
        not (
            (allow_dot_tokens and component in (".", ".."))
            or _is_valid_component(component)
        )
        for component in components
    ):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    parsed = _DosPath(drive, components)
    try:
        units = len(parsed.native.encode("utf-16-le", errors="strict")) // 2
    except UnicodeError:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL) from None
    if units > _MAX_NATIVE_PATH_UTF16_UNITS:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return parsed


def _contains(root: _DosPath, candidate: _DosPath) -> bool:
    return (
        root.drive == candidate.drive
        and len(root.components) <= len(candidate.components)
        and candidate.components[: len(root.components)] == root.components
    )


def _parse_request(raw: str | None) -> _InspectionRequest:
    if not isinstance(raw, str) or not raw:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeError:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
    if len(encoded) > MAX_LOCAL_FILESYSTEM_REQUEST_BYTES:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
    if not isinstance(payload, dict) or set(payload) != {
        "v",
        "op",
        "candidate",
        "roots",
    }:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
    if canonical != raw:
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
    if (
        type(payload["v"]) is not int
        or payload["v"] != LOCAL_FILESYSTEM_REQUEST_VERSION
        or payload["op"] != LOCAL_FILESYSTEM_INSPECT_DIRECTORY_OP
        or not isinstance(payload["candidate"], str)
        or not isinstance(payload["roots"], list)
        or not payload["roots"]
        or len(payload["roots"]) > MAX_LOCAL_FILESYSTEM_ROOTS
        or any(not isinstance(root, str) for root in payload["roots"])
    ):
        raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)

    candidate = _parse_candidate_dos_path(payload["candidate"])
    roots = tuple(_parse_dos_path(root) for root in payload["roots"])
    containing = tuple(root for root in roots if _contains(root, candidate))
    if not containing:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    selected = max(containing, key=lambda root: len(root.components))
    suffix = candidate.components[len(selected.components) :]
    first_effective = next((token for token in suffix if token != "."), None)
    if first_effective == "..":
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return _InspectionRequest(candidate, selected)


def _read_u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return int.from_bytes(data[offset : offset + 2], "little")


def _read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return int.from_bytes(data[offset : offset + 4], "little")


def _decode_reparse_name(
    path_buffer: bytes,
    offset: int,
    length: int,
) -> str:
    if (
        offset % 2
        or length % 2
        or offset < 0
        or length <= 0
        or offset + length > len(path_buffer)
    ):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    try:
        value = path_buffer[offset : offset + length].decode(
            "utf-16-le",
            errors="strict",
        )
    except UnicodeError:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL) from None
    if not value or "\0" in value:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return value


def _validate_optional_print_name(
    path_buffer: bytes,
    offset: int,
    length: int,
) -> None:
    if offset % 2 or length % 2 or offset < 0 or offset + length > len(path_buffer):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    if length:
        try:
            value = path_buffer[offset : offset + length].decode(
                "utf-16-le",
                errors="strict",
            )
        except UnicodeError:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL) from None
        if "\0" in value:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)


def _relative_target_tokens(target: str) -> tuple[str, ...]:
    if not target or target.startswith("\\") or "/" in target or ":" in target:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    tokens = tuple(target.split("\\"))
    for token in tokens:
        if token not in (".", "..") and not _is_valid_component(token):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    if not tokens or len(tokens) > _MAX_RESOLUTION_COMPONENTS:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return tokens


def _absolute_nt_dos_target(target: str) -> _DosPath:
    match = _NT_DOS_DRIVE_TARGET.match(target)
    if match is None:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return _parse_candidate_dos_path(target[4:])


def _parse_reparse_target(raw: bytes, expected_tag: int) -> _ReparseTarget:
    if not (
        isinstance(raw, bytes)
        and _REPARSE_HEADER_BYTES <= len(raw) <= _MAXIMUM_REPARSE_DATA_BUFFER_SIZE
    ):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    tag = _read_u32(raw, 0)
    payload_length = _read_u16(raw, 4)
    if tag != expected_tag or payload_length + _REPARSE_HEADER_BYTES != len(raw):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)

    if tag == _IO_REPARSE_TAG_MOUNT_POINT:
        fixed = _MOUNT_POINT_FIXED_PAYLOAD_BYTES
        if payload_length < fixed:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        substitute_offset = _read_u16(raw, 8)
        substitute_length = _read_u16(raw, 10)
        print_offset = _read_u16(raw, 12)
        print_length = _read_u16(raw, 14)
        path_buffer = raw[_REPARSE_HEADER_BYTES + fixed :]
        substitute = _decode_reparse_name(
            path_buffer,
            substitute_offset,
            substitute_length,
        )
        _validate_optional_print_name(path_buffer, print_offset, print_length)
        return _ReparseTarget(
            absolute=_absolute_nt_dos_target(substitute),
            relative_tokens=(),
        )

    if tag == _IO_REPARSE_TAG_SYMLINK:
        fixed = _SYMLINK_FIXED_PAYLOAD_BYTES
        if payload_length < fixed:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        substitute_offset = _read_u16(raw, 8)
        substitute_length = _read_u16(raw, 10)
        print_offset = _read_u16(raw, 12)
        print_length = _read_u16(raw, 14)
        flags = _read_u32(raw, 16)
        if flags not in (0, _SYMLINK_FLAG_RELATIVE):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        path_buffer = raw[_REPARSE_HEADER_BYTES + fixed :]
        substitute = _decode_reparse_name(
            path_buffer,
            substitute_offset,
            substitute_length,
        )
        _validate_optional_print_name(path_buffer, print_offset, print_length)
        if flags == _SYMLINK_FLAG_RELATIVE:
            return _ReparseTarget(
                absolute=None,
                relative_tokens=_relative_target_tokens(substitute),
            )
        return _ReparseTarget(
            absolute=_absolute_nt_dos_target(substitute),
            relative_tokens=(),
        )

    raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)


def _directory_kind(
    api: WindowsDirectoryApi,
    handle: int,
) -> tuple[bool, int]:
    attributes, tag = api.file_attributes_and_reparse_tag(handle)
    if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    if not attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False, tag
    if not tag & _REPARSE_TAG_NAME_SURROGATE:
        if not tag & _REPARSE_TAG_DIRECTORY:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return False, tag
    if tag not in (_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK):
        raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
    return True, tag


class _HandleResolver:
    def __init__(
        self,
        api: WindowsDirectoryApi,
        *,
        owned_handles: list[int] | None = None,
    ) -> None:
        self._api = api
        self._owned = [] if owned_handles is None else owned_handles
        self._cleanup_failed = False
        self._expansions = 0
        self._steps = 0

    def inspect(self, request: _InspectionRequest) -> bool:
        root = self._bootstrap_root(request.selected_root)
        configured = request.selected_root
        resolved = _DosPath(root.drive, root.components)
        equivalent_roots = (configured, resolved)

        suffix = request.candidate.components[len(configured.components) :]
        stack = [root]
        self._walk_beneath_root(stack, suffix, equivalent_roots)
        return True

    def close_all(self) -> bool:
        while self._owned:
            handle = self._owned.pop()
            try:
                self._api.close(handle)
            except BaseException:
                self._cleanup_failed = True
        return not self._cleanup_failed

    def _open_drive(self, drive: str) -> _OwnedDirectory:
        handle = self._api.open_root(f"{drive}:\\")
        self._take_ownership(handle)
        attributes, _tag = self._api.file_attributes_and_reparse_tag(handle)
        if (
            not attributes & _FILE_ATTRIBUTE_DIRECTORY
            or attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        normalized = _parse_dos_path(self._api.normalized_dos_path(handle))
        if normalized.components:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return _OwnedDirectory(handle, normalized.drive, ())

    def _open_child(
        self,
        parent: _OwnedDirectory,
        component: str,
    ) -> tuple[_OwnedDirectory, bool, int]:
        self._step()
        handle = self._api.open_component(parent.handle, component)
        self._take_ownership(handle)
        surrogate, tag = _directory_kind(self._api, handle)
        if not surrogate:
            normalized = _parse_dos_path(self._api.normalized_dos_path(handle))
            if normalized.drive != parent.drive:
                raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        return (
            _OwnedDirectory(
                handle,
                parent.drive,
                parent.components + (component,),
            ),
            surrogate,
            tag,
        )

    def _take_ownership(self, handle: int) -> None:
        if handle in self._owned:
            # A repeated numeric value is the already-owned live handle, not a
            # second ownership interest.  Closing it here would invalidate the
            # retained parent; close_all() remains its sole owner.
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL)
        try:
            self._owned.append(handle)
        except BaseException:
            try:
                self._api.close(handle)
            except BaseException:
                self._cleanup_failed = True
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None

    def _bootstrap_root(self, configured: _DosPath) -> _OwnedDirectory:
        stack = [self._open_drive(configured.drive)]
        pending = list(configured.components)
        while pending:
            token = pending.pop(0)
            if token == ".":
                continue
            if token == "..":
                if len(stack) <= 1:
                    raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
                self._close_owned(stack.pop().handle)
                continue
            child, surrogate, tag = self._open_child(stack[-1], token)
            if not surrogate:
                stack.append(child)
                continue
            target = self._read_target_and_close(child, tag)
            if target.absolute is not None:
                self._close_stack(stack)
                stack = [self._open_drive(target.absolute.drive)]
                pending = list(target.absolute.components) + pending
            else:
                pending = list(target.relative_tokens) + pending
            self._expanded(pending)

        root = stack[-1]
        for ancestor in reversed(stack[:-1]):
            self._close_owned(ancestor.handle)
        return root

    def _walk_beneath_root(
        self,
        stack: list[_OwnedDirectory],
        suffix: tuple[str, ...],
        equivalent_roots: tuple[_DosPath, ...],
    ) -> None:
        pending = list(suffix)
        while pending:
            token = pending.pop(0)
            if token == ".":
                continue
            if token == "..":
                if len(stack) <= 1:
                    raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
                self._close_owned(stack.pop().handle)
                continue

            child, surrogate, tag = self._open_child(stack[-1], token)
            if not surrogate:
                stack.append(child)
                continue
            target = self._read_target_and_close(child, tag)
            if target.absolute is not None:
                relative = self._absolute_target_suffix(
                    target.absolute,
                    equivalent_roots,
                )
                while len(stack) > 1:
                    self._close_owned(stack.pop().handle)
                pending = list(relative) + pending
            else:
                pending = list(target.relative_tokens) + pending
            self._expanded(pending)

    def _absolute_target_suffix(
        self,
        target: _DosPath,
        equivalent_roots: tuple[_DosPath, ...],
    ) -> tuple[str, ...]:
        matches = tuple(root for root in equivalent_roots if _contains(root, target))
        if not matches:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)
        root = max(matches, key=lambda item: len(item.components))
        return target.components[len(root.components) :]

    def _read_target_and_close(
        self,
        child: _OwnedDirectory,
        tag: int,
    ) -> _ReparseTarget:
        try:
            raw = self._api.read_reparse_point(child.handle)
            target = _parse_reparse_target(raw, tag)
        finally:
            self._close_owned(child.handle)
        return target

    def _close_stack(self, stack: list[_OwnedDirectory]) -> None:
        while stack:
            self._close_owned(stack.pop().handle)

    def _close_owned(self, handle: int) -> None:
        try:
            self._owned.remove(handle)
        except ValueError:
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None
        try:
            self._api.close(handle)
        except BaseException:
            self._cleanup_failed = True
            raise WindowsLocalStoreHealthError(_GENERIC_FAILURE_DETAIL) from None

    def _expanded(self, pending: list[str]) -> None:
        self._expansions += 1
        if (
            self._expansions > _MAX_NAME_SURROGATE_EXPANSIONS
            or len(pending) > _MAX_RESOLUTION_COMPONENTS
        ):
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)

    def _step(self) -> None:
        self._steps += 1
        if self._steps > _MAX_RESOLUTION_COMPONENTS:
            raise WindowsLocalStoreHealthDenied(_GENERIC_FAILURE_DETAIL)


def inspect_directory_request(
    raw_request: str | None,
    *,
    api: WindowsDirectoryApi | None = None,
) -> int:
    """Inspect one request and return the fixed helper protocol exit code."""

    try:
        request = _parse_request(raw_request)
    except WindowsLocalStoreHealthDenied:
        return LOCAL_FILESYSTEM_DENIED_EXIT
    except BaseException:
        return LOCAL_FILESYSTEM_FAILED_EXIT

    resolver: _HandleResolver | None = None
    outcome = LOCAL_FILESYSTEM_FAILED_EXIT
    try:
        directory_api = CtypesWindowsDirectoryApi() if api is None else api
        resolver = _HandleResolver(directory_api)
        resolver.inspect(request)
        outcome = LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    except WindowsLocalStoreHealthDenied:
        outcome = LOCAL_FILESYSTEM_DENIED_EXIT
    except BaseException:
        outcome = LOCAL_FILESYSTEM_FAILED_EXIT
    finally:
        if resolver is not None and not resolver.close_all():
            outcome = LOCAL_FILESYSTEM_FAILED_EXIT
    return outcome


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    *,
    api: WindowsDirectoryApi | None = None,
) -> int:
    """Return only the fixed, output-free local-filesystem exit statuses."""

    try:
        arguments = sys.argv if argv is None else argv
        environment = os.environ if environ is None else environ
        if len(arguments) != 1:
            return LOCAL_FILESYSTEM_FAILED_EXIT
        return inspect_directory_request(
            environment.get(LOCAL_FILESYSTEM_REQUEST_ENV),
            api=api,
        )
    except BaseException:
        return LOCAL_FILESYSTEM_FAILED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
