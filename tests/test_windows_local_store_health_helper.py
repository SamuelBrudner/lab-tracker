from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import lab_tracker._windows_local_store_health_helper as helper

_DIRECTORY = 0x10
_REPARSE_DIRECTORY = 0x410
_CLOUD_TAG = 0x9000001A
_MOUNT_TAG = 0xA0000003
_SYMLINK_TAG = 0xA000000C
_ROOT_HANDLE = 101
_CHILD_HANDLE = 202
_FILE_HANDLE = 303


def _request(candidate: str, roots: Sequence[str]) -> str:
    return json.dumps(
        {
            "v": 1,
            "op": "inspect-directory",
            "candidate": candidate,
            "roots": list(roots),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_request(candidate: str, root: str, max_bytes: int) -> str:
    return json.dumps(
        {
            "v": 1,
            "op": "read-file",
            "candidate": candidate,
            "roots": [root],
            "max_bytes": max_bytes,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _registered_read_request(
    store_root: str,
    locator: Sequence[str],
    root: str,
    max_bytes: int,
) -> str:
    return json.dumps(
        {
            "v": 1,
            "op": "read-registered-file",
            "store_root": store_root,
            "locator": list(locator),
            "roots": [root],
            "max_bytes": max_bytes,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _enumerate_request(
    roots: Sequence[str],
    *,
    target_name: str | None = None,
    max_files: int = 16,
    max_directories: int = 16,
) -> str:
    return json.dumps(
        {
            "v": 1,
            "op": "enumerate-files",
            "roots": list(roots),
            "target_name": target_name,
            "max_files": max_files,
            "max_directories": max_directories,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _registered_enumerate_request(
    store_root: str,
    root: str,
    *,
    target_name: str | None = None,
    max_files: int = 16,
    max_directories: int = 16,
) -> str:
    return json.dumps(
        {
            "v": 1,
            "op": "enumerate-registered-files",
            "roots": [root],
            "store_root": store_root,
            "target_name": target_name,
            "max_files": max_files,
            "max_directories": max_directories,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _enumeration_payload(output: io.BytesIO) -> dict[str, object]:
    return cast(dict[str, object], json.loads(output.getvalue().decode("ascii")))


def _snapshot(
    size: int,
    *,
    volume: int = 7,
    file_id: bytes = b"0123456789abcdef",
    last_write: int = 11,
    change: int = 12,
    file_type: int = 1,
    attributes: int = 0x80,
    tag: int = 0,
) -> helper._RegularFileSnapshot:
    return helper._RegularFileSnapshot(
        volume_serial_number=volume,
        file_id=file_id,
        end_of_file=size,
        last_write_time=last_write,
        change_time=change,
        file_type=file_type,
        file_attributes=attributes,
        reparse_tag=tag,
    )


def _as_int(value: object) -> int:
    raw = getattr(value, "value", value)
    assert isinstance(raw, int)
    return raw


def _write_wide_string(raw_buffer: object, value: str) -> None:
    buffer = ctypes.cast(cast(Any, raw_buffer), ctypes.POINTER(ctypes.c_wchar))
    for index, character in enumerate(value):
        buffer[index] = character
    buffer[len(value)] = "\x00"


def _reparse_buffer(
    *,
    tag: int,
    substitute: str,
    print_name: str = "",
    relative: bool = False,
) -> bytes:
    substitute_raw = substitute.encode("utf-16-le")
    print_raw = print_name.encode("utf-16-le")
    path_buffer = substitute_raw + print_raw
    fixed = 12 if tag == _SYMLINK_TAG else 8
    payload_length = fixed + len(path_buffer)
    header = tag.to_bytes(4, "little") + payload_length.to_bytes(2, "little") + b"\0\0"
    names = (
        (0).to_bytes(2, "little")
        + len(substitute_raw).to_bytes(2, "little")
        + len(substitute_raw).to_bytes(2, "little")
        + len(print_raw).to_bytes(2, "little")
    )
    flags = (1 if relative else 0).to_bytes(4, "little") if tag == _SYMLINK_TAG else b""
    return header + names + flags + path_buffer


class FakeCreateFile:
    def __init__(self, handle: int = _ROOT_HANDLE) -> None:
        self.handle = handle
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> int:
        self.calls.append(tuple(_as_int(arg) if hasattr(arg, "value") else arg for arg in args))
        return self.handle


class FakeGetInformationEx:
    def __init__(
        self,
        *,
        attributes: Mapping[int, int] | None = None,
        tags: Mapping[int, int] | None = None,
        identities: Mapping[int, tuple[int, bytes]] | None = None,
        result: int = 1,
    ) -> None:
        self.attributes = dict(attributes or {})
        self.tags = dict(tags or {})
        self.identities = dict(identities or {})
        self.result = result
        self.calls: list[tuple[int, int, int]] = []

    def __call__(
        self,
        raw_handle: object,
        raw_class: object,
        raw_information: object,
        raw_size: object,
    ) -> int:
        handle = _as_int(raw_handle)
        information_class = _as_int(raw_class)
        self.calls.append((handle, information_class, _as_int(raw_size)))
        if self.result:
            if information_class == helper._FILE_ID_INFO_CLASS:
                volume, file_id = self.identities.get(
                    handle,
                    (1, b"i" * 16),
                )
                information = ctypes.cast(
                    cast(Any, raw_information),
                    ctypes.POINTER(helper._FILE_ID_INFO),
                )
                information.contents.VolumeSerialNumber = volume
                for index, value in enumerate(file_id):
                    information.contents.FileId.Identifier[index] = value
            else:
                information = ctypes.cast(
                    cast(Any, raw_information),
                    ctypes.POINTER(helper._FILE_ATTRIBUTE_TAG_INFO),
                )
                information.contents.FileAttributes = self.attributes.get(
                    handle,
                    _DIRECTORY,
                )
                information.contents.ReparseTag = self.tags.get(handle, 0)
        return self.result


class FakeGetFinalPath:
    def __init__(self, paths: Mapping[int, str] | None = None) -> None:
        self.paths = dict(
            paths
            or {
                _ROOT_HANDLE: "\\\\?\\C:\\",
                _CHILD_HANDLE: r"\\?\C:\store",
            }
        )
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
        path = self.paths[handle]
        units = len(path.encode("utf-16-le")) // 2
        if capacity <= units:
            return units
        _write_wide_string(raw_buffer, path)
        return units


class FakeDeviceIoControl:
    def __init__(self, data: bytes, *, result: int = 1) -> None:
        self.data = data
        self.result = result
        self.calls: list[tuple[int, int, object, int, int, object]] = []

    def __call__(
        self,
        raw_handle: object,
        raw_code: object,
        input_buffer: object,
        raw_input_size: object,
        output_buffer: object,
        raw_output_size: object,
        raw_returned: object,
        overlapped: object,
    ) -> int:
        self.calls.append(
            (
                _as_int(raw_handle),
                _as_int(raw_code),
                input_buffer,
                _as_int(raw_input_size),
                _as_int(raw_output_size),
                overlapped,
            )
        )
        if self.result:
            ctypes.memmove(output_buffer, self.data, len(self.data))
            returned = ctypes.cast(
                cast(Any, raw_returned),
                ctypes.POINTER(ctypes.c_uint32),
            )
            returned.contents.value = len(self.data)
        return self.result


class FakeCloseHandle:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[int] = []

    def __call__(self, raw_handle: object) -> int:
        self.calls.append(_as_int(raw_handle))
        return self.result


class FakeGetFileType:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[int] = []

    def __call__(self, raw_handle: object) -> int:
        self.calls.append(_as_int(raw_handle))
        return self.result


class FakeReadFile:
    def __init__(self, chunks: Sequence[bytes] = ()) -> None:
        self.chunks = list(chunks)
        self.calls: list[tuple[int, int]] = []

    def __call__(
        self,
        raw_handle: object,
        raw_buffer: object,
        raw_size: object,
        raw_returned: object,
        _overlapped: object,
    ) -> int:
        handle = _as_int(raw_handle)
        size = _as_int(raw_size)
        self.calls.append((handle, size))
        chunk = self.chunks.pop(0) if self.chunks else b""
        assert len(chunk) <= size
        ctypes.memmove(raw_buffer, chunk, len(chunk))
        returned = ctypes.cast(
            cast(Any, raw_returned),
            ctypes.POINTER(ctypes.c_uint32),
        )
        returned.contents.value = len(chunk)
        return 1


class FakeNtCreateFile:
    def __init__(
        self,
        outcomes: Sequence[tuple[int, int | None]] = ((0, _CHILD_HANDLE),),
        *,
        after_write: BaseException | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.after_write = after_write
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        raw_output_handle: object,
        raw_desired_access: object,
        raw_object_attributes: object,
        _raw_io_status: object,
        allocation_size: object,
        raw_file_attributes: object,
        raw_share_access: object,
        raw_create_disposition: object,
        raw_create_options: object,
        ea_buffer: object,
        raw_ea_length: object,
    ) -> int:
        attributes = ctypes.cast(
            cast(Any, raw_object_attributes),
            ctypes.POINTER(helper._OBJECT_ATTRIBUTES),
        ).contents
        name = attributes.ObjectName.contents
        component = ctypes.string_at(name.Buffer, name.Length).decode("utf-16-le")
        self.calls.append(
            {
                "desired_access": _as_int(raw_desired_access),
                "parent": _as_int(attributes.RootDirectory),
                "component": component,
                "name_length": int(name.Length),
                "name_maximum_length": int(name.MaximumLength),
                "attributes": int(attributes.Attributes),
                "create_options": _as_int(raw_create_options),
                "share_access": _as_int(raw_share_access),
                "create_disposition": _as_int(raw_create_disposition),
                "file_attributes": _as_int(raw_file_attributes),
                "allocation_size": allocation_size,
                "ea_buffer": ea_buffer,
                "ea_length": _as_int(raw_ea_length),
            }
        )
        status, handle = self.outcomes.pop(0)
        if handle is not None:
            output = ctypes.cast(
                cast(Any, raw_output_handle),
                ctypes.POINTER(ctypes.c_void_p),
            )
            output.contents.value = handle
        if self.after_write is not None:
            raise self.after_write
        return status


def _raw_api(
    *,
    create_file: Callable[..., object] | None = None,
    get_information: Callable[..., object] | None = None,
    get_final_path: Callable[..., object] | None = None,
    device_io: Callable[..., object] | None = None,
    close_handle: Callable[..., object] | None = None,
    get_file_type: Callable[..., object] | None = None,
    read_file: Callable[..., object] | None = None,
    nt_create_file: Callable[..., object] | None = None,
    get_last_error: Callable[[], object] | None = None,
) -> helper.CtypesWindowsDirectoryApi:
    return helper.CtypesWindowsDirectoryApi(
        create_file=create_file or FakeCreateFile(),
        get_file_information_by_handle_ex=get_information or FakeGetInformationEx(),
        get_final_path_name_by_handle=get_final_path or FakeGetFinalPath(),
        device_io_control=device_io
        or FakeDeviceIoControl(
            _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute=r"\??\C:\store",
            )
        ),
        get_file_type=get_file_type or FakeGetFileType(),
        read_file=read_file or FakeReadFile(),
        close_handle=close_handle or FakeCloseHandle(),
        nt_create_file=nt_create_file or FakeNtCreateFile(),
        get_last_error=get_last_error,
    )


def test_public_protocol_constants_are_fixed() -> None:
    assert helper.LOCAL_FILESYSTEM_REQUEST_ENV == "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
    assert helper.LOCAL_FILESYSTEM_PROTOCOL_VERSION == 1
    assert helper.LOCAL_FILESYSTEM_REQUEST_VERSION == 1
    assert helper.LOCAL_FILESYSTEM_INSPECT_DIRECTORY_OP == "inspect-directory"
    assert helper.LOCAL_FILESYSTEM_READ_FILE_OP == "read-file"
    assert helper.LOCAL_FILESYSTEM_READ_REGISTERED_FILE_OP == "read-registered-file"
    assert helper.LOCAL_FILESYSTEM_ENUMERATE_FILES_OP == "enumerate-files"
    assert (
        helper.LOCAL_FILESYSTEM_ENUMERATE_REGISTERED_FILES_OP
        == "enumerate-registered-files"
    )
    assert helper.LOCAL_FILESYSTEM_COMPLETE_EXIT == 0
    assert helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT == 0
    assert helper.LOCAL_FILESYSTEM_DENIED_EXIT == 2
    assert helper.LOCAL_FILESYSTEM_FAILED_EXIT == 3
    assert helper.LOCAL_FILESYSTEM_MISSING_EXIT == 4
    assert helper.INSPECT_DIRECTORY_OPERATION == "inspect-directory"
    assert helper.READ_FILE_OPERATION == "read-file"
    assert helper.READ_REGISTERED_FILE_OPERATION == "read-registered-file"
    assert helper.ENUMERATE_FILES_OPERATION == "enumerate-files"
    assert (
        helper.ENUMERATE_REGISTERED_FILES_OPERATION
        == "enumerate-registered-files"
    )
    assert helper.COMPLETE_EXIT == 0
    assert helper.ACCESSIBLE_EXIT == 0
    assert helper.DENIED_EXIT == 2
    assert helper.FAILED_EXIT == 3
    assert helper.MISSING_EXIT == 4
    assert helper.MAX_LOCAL_FILESYSTEM_REQUEST_BYTES == 24 * 1024
    assert helper.MAX_LOCAL_FILESYSTEM_ROOTS == 64
    assert helper.MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS == 64
    assert helper.MAX_LOCAL_FILESYSTEM_READ_BYTES == 512 * 1024 * 1024
    assert helper.MAX_LOCAL_FILESYSTEM_ENUMERATION_ITEMS == 4096
    assert helper.MAX_LOCAL_FILESYSTEM_ENUMERATED_FILES == 4096
    assert helper.MAX_LOCAL_FILESYSTEM_ENUMERATED_DIRECTORIES == 4096
    assert helper.MAX_LOCAL_FILESYSTEM_ENUMERATION_METADATA_BYTES == 8 * 1024 * 1024


def test_windows_ctypes_structures_match_the_native_abi() -> None:
    assert ctypes.sizeof(helper._FILE_ATTRIBUTE_TAG_INFO) == 8
    assert helper._FILE_ATTRIBUTE_TAG_INFO.FileAttributes.offset == 0
    assert helper._FILE_ATTRIBUTE_TAG_INFO.ReparseTag.offset == 4
    assert helper._OBJECT_ATTRIBUTES.RootDirectory.offset in (4, 8)
    assert ctypes.sizeof(helper._IO_STATUS_BLOCK) in (8, 16)
    assert ctypes.sizeof(helper._FILE_BASIC_INFO) == 40
    assert ctypes.sizeof(helper._FILE_STANDARD_INFO) == 24
    assert ctypes.sizeof(helper._FILE_ID_INFO) == 24


def test_create_file_anchors_only_a_drive_root_with_exact_flags() -> None:
    create = FakeCreateFile()
    api = _raw_api(create_file=create)

    assert api.open_root("C:\\") == _ROOT_HANDLE
    assert create.calls == [
        (
            "\\\\?\\C:\\",
            0xA0,
            0x7,
            None,
            3,
            0x02200000,
            None,
        )
    ]


def test_ntcreatefile_uses_one_component_no_follow_contract() -> None:
    nt_create = FakeNtCreateFile()
    api = _raw_api(nt_create_file=nt_create)

    assert api.open_component(_ROOT_HANDLE, "données-🧪") == _CHILD_HANDLE
    assert nt_create.calls == [
        {
            "desired_access": 0xA0,
            "parent": _ROOT_HANDLE,
            "component": "données-🧪",
            "name_length": len("données-🧪".encode("utf-16-le")),
            "name_maximum_length": len("données-🧪".encode("utf-16-le")) + 2,
            "attributes": 0x1000,
            "create_options": 0x00200000,
            "share_access": 0x7,
            "create_disposition": 1,
            "file_attributes": 0,
            "allocation_size": None,
            "ea_buffer": None,
            "ea_length": 0,
        }
    ]


def test_ntcreatefile_opens_final_file_once_for_synchronous_bounded_read() -> None:
    nt_create = FakeNtCreateFile(((0, _FILE_HANDLE),))
    api = _raw_api(nt_create_file=nt_create)

    assert api.open_file_component(_ROOT_HANDLE, "artifact.bin") == _FILE_HANDLE
    assert nt_create.calls == [
        {
            "desired_access": 0x00100081,
            "parent": _ROOT_HANDLE,
            "component": "artifact.bin",
            "name_length": len("artifact.bin".encode("utf-16-le")),
            "name_maximum_length": len("artifact.bin".encode("utf-16-le")) + 2,
            "attributes": 0x1000,
            "create_options": 0x00200060,
            "share_access": 0x7,
            "create_disposition": 1,
            "file_attributes": 0,
            "allocation_size": None,
            "ea_buffer": None,
            "ea_length": 0,
        }
    ]


def test_ntcreatefile_opens_enumerable_directory_with_exact_contract() -> None:
    nt_create = FakeNtCreateFile(((0, _CHILD_HANDLE),))
    api = _raw_api(nt_create_file=nt_create)

    assert (
        api.open_enumerable_component(_ROOT_HANDLE, "recovery")
        == _CHILD_HANDLE
    )
    assert nt_create.calls == [
        {
            "desired_access": 0xA1,
            "parent": _ROOT_HANDLE,
            "component": "recovery",
            "name_length": len("recovery".encode("utf-16-le")),
            "name_maximum_length": len("recovery".encode("utf-16-le")) + 2,
            "attributes": 0x1000,
            "create_options": 0x00200001,
            "share_access": 0x7,
            "create_disposition": 1,
            "file_attributes": 0,
            "allocation_size": None,
            "ea_buffer": None,
            "ea_length": 0,
        }
    ]


def test_ntcreatefile_opens_candidate_for_attributes_only_without_recall() -> None:
    nt_create = FakeNtCreateFile(((0, _FILE_HANDLE),))
    api = _raw_api(nt_create_file=nt_create)

    assert (
        api.open_candidate_component(_ROOT_HANDLE, "artifact.bin")
        == _FILE_HANDLE
    )
    assert nt_create.calls == [
        {
            "desired_access": 0x80,
            "parent": _ROOT_HANDLE,
            "component": "artifact.bin",
            "name_length": len("artifact.bin".encode("utf-16-le")),
            "name_maximum_length": len("artifact.bin".encode("utf-16-le")) + 2,
            "attributes": 0x1000,
            "create_options": 0x00600000,
            "share_access": 0x7,
            "create_disposition": 1,
            "file_attributes": 0,
            "allocation_size": None,
            "ea_buffer": None,
            "ea_length": 0,
        }
    ]


def test_candidate_identity_reads_only_exact_disk_handle_identity() -> None:
    file_id = bytes(range(16))
    information = FakeGetInformationEx(
        identities={_FILE_HANDLE: (0x1234, file_id)}
    )
    file_type = FakeGetFileType(1)
    api = _raw_api(
        get_information=information,
        get_file_type=file_type,
    )

    assert api.candidate_file_identity(_FILE_HANDLE) == (0x1234, file_id)
    assert file_type.calls == [_FILE_HANDLE]
    assert information.calls == [
        (_FILE_HANDLE, helper._FILE_ID_INFO_CLASS, 24)
    ]


def test_candidate_identity_skips_special_handle_without_identity_query() -> None:
    information = FakeGetInformationEx()
    file_type = FakeGetFileType(2)
    api = _raw_api(
        get_information=information,
        get_file_type=file_type,
    )

    assert api.candidate_file_identity(_FILE_HANDLE) is None
    assert file_type.calls == [_FILE_HANDLE]
    assert information.calls == []


def test_create_file_opens_enumerable_drive_root_with_exact_access() -> None:
    create = FakeCreateFile()
    api = _raw_api(create_file=create)

    assert api.open_enumerable_root("C:\\") == _ROOT_HANDLE
    assert create.calls == [
        (
            "\\\\?\\C:\\",
            0xA1,
            0x7,
            None,
            3,
            0x02200000,
            None,
        )
    ]


@pytest.mark.parametrize("status", (0xC000000D, 0xC000050B))
def test_enumerable_component_keeps_exact_dont_reparse_retry(
    status: int,
) -> None:
    nt_create = FakeNtCreateFile(((status, 303), (0, _CHILD_HANDLE)))
    close = FakeCloseHandle()
    api = _raw_api(nt_create_file=nt_create, close_handle=close)

    assert (
        api.open_enumerable_component(_ROOT_HANDLE, "recovery")
        == _CHILD_HANDLE
    )
    assert [call["attributes"] for call in nt_create.calls] == [0x1000, 0]
    assert [call["desired_access"] for call in nt_create.calls] == [0xA1, 0xA1]
    assert [call["create_options"] for call in nt_create.calls] == [
        0x00200001,
        0x00200001,
    ]
    assert close.calls == [303]


def _directory_information_buffer(
    records: Sequence[tuple[str, int, int, bytes]],
) -> bytes:
    buffer = bytearray(64 * 1024)
    offset = 0
    for index, (name, attributes, tag, file_id) in enumerate(records):
        raw_name = name.encode("utf-16-le")
        assert len(file_id) == 16
        record_size = (88 + len(raw_name) + 7) & ~7
        next_offset = 0 if index == len(records) - 1 else record_size
        buffer[offset : offset + 4] = next_offset.to_bytes(4, "little")
        buffer[offset + 56 : offset + 60] = attributes.to_bytes(4, "little")
        buffer[offset + 60 : offset + 64] = len(raw_name).to_bytes(4, "little")
        buffer[offset + 68 : offset + 72] = tag.to_bytes(4, "little")
        buffer[offset + 72 : offset + 88] = file_id
        buffer[offset + 88 : offset + 88 + len(raw_name)] = raw_name
        offset += record_size
    return bytes(buffer)


class FakeDirectoryQuery:
    def __init__(self, buffers: Sequence[bytes]) -> None:
        self.buffers = list(buffers)
        self.calls: list[tuple[int, int, int]] = []

    def __call__(
        self,
        raw_handle: object,
        raw_class: object,
        raw_information: object,
        raw_size: object,
    ) -> int:
        self.calls.append(
            (_as_int(raw_handle), _as_int(raw_class), _as_int(raw_size))
        )
        if not self.buffers:
            return 0
        data = self.buffers.pop(0)
        ctypes.memmove(raw_information, data, len(data))
        return 1


def test_native_directory_query_restarts_then_continues_on_same_handle() -> None:
    first = _directory_information_buffer(
        (("first.bin", 0x80, 0, b"a" * 16),)
    )
    second = _directory_information_buffer(
        (("nested", _DIRECTORY, 0, b"b" * 16),)
    )
    query = FakeDirectoryQuery((first, second))
    api = _raw_api(get_information=query, get_last_error=lambda: 18)

    assert list(api.enumerate_directory(_ROOT_HANDLE)) == [
        helper._DirectoryEntry("first.bin", 0x80, 0, b"a" * 16),
        helper._DirectoryEntry("nested", _DIRECTORY, 0, b"b" * 16),
    ]
    assert query.calls == [
        (_ROOT_HANDLE, 0x14, 64 * 1024),
        (_ROOT_HANDLE, 0x13, 64 * 1024),
        (_ROOT_HANDLE, 0x13, 64 * 1024),
    ]


def test_directory_query_accepts_only_no_more_files_as_clean_end() -> None:
    query = FakeDirectoryQuery(())
    api = _raw_api(get_information=query, get_last_error=lambda: 5)

    with pytest.raises(helper.WindowsLocalStoreHealthDenied):
        list(api.enumerate_directory(_ROOT_HANDLE))


def test_directory_query_skips_dot_entries_before_clean_end() -> None:
    query = FakeDirectoryQuery(
        (
            _directory_information_buffer(
                (
                    (".", _DIRECTORY, 0, b"." * 16),
                    ("..", _DIRECTORY, 0, b":" * 16),
                )
            ),
        )
    )
    api = _raw_api(get_information=query, get_last_error=lambda: 18)

    assert list(api.enumerate_directory(_ROOT_HANDLE)) == []
    assert [information_class for _, information_class, _ in query.calls] == [
        0x14,
        0x13,
    ]


def test_file_id_extd_directory_parser_validates_the_native_abi() -> None:
    valid = _directory_information_buffer(
        (
            (".", _DIRECTORY, 0, b"." * 16),
            ("..", _DIRECTORY, 0, b":" * 16),
            ("données-🧪.bin", 0x80, 0, b"f" * 16),
        )
    )
    assert helper._parse_file_id_extd_directory_info(valid) == (
        helper._DirectoryEntry("données-🧪.bin", 0x80, 0, b"f" * 16),
    )

    malformed: list[bytes] = []
    for next_offset in (80, 89, 64 * 1024 + 8):
        changed = bytearray(valid)
        changed[:4] = next_offset.to_bytes(4, "little")
        malformed.append(bytes(changed))
    odd_length = bytearray(
        _directory_information_buffer((("name", 0x80, 0, b"x" * 16),))
    )
    odd_length[60:64] = (3).to_bytes(4, "little")
    malformed.append(bytes(odd_length))
    overrun = bytearray(valid)
    overrun[60:64] = (0x1_0000).to_bytes(4, "little")
    malformed.append(bytes(overrun))
    invalid_utf16 = bytearray(
        _directory_information_buffer((("x", 0x80, 0, b"x" * 16),))
    )
    invalid_utf16[88:90] = b"\x00\xd8"
    malformed.append(bytes(invalid_utf16))
    for raw in malformed:
        with pytest.raises(helper.WindowsLocalStoreHealthDenied):
            helper._parse_file_id_extd_directory_info(raw)


def test_ctypes_readfile_is_binary_and_strictly_bounded() -> None:
    raw = b"\x00line\r\n\x1a\xff"
    read = FakeReadFile((raw,))
    api = _raw_api(read_file=read)

    assert api.read_file(_FILE_HANDLE, len(raw)) == raw
    assert read.calls == [(_FILE_HANDLE, len(raw))]


@pytest.mark.parametrize("status", (0xC000000D, 0xC000050B))
def test_ntcreatefile_safe_fallback_closes_stray_output(status: int) -> None:
    nt_create = FakeNtCreateFile(((status, 303), (0, _CHILD_HANDLE)))
    close = FakeCloseHandle()
    api = _raw_api(nt_create_file=nt_create, close_handle=close)

    assert api.open_component(_ROOT_HANDLE, "store") == _CHILD_HANDLE
    assert [call["attributes"] for call in nt_create.calls] == [0x1000, 0]
    assert [call["create_options"] for call in nt_create.calls] == [
        0x00200000,
        0x00200000,
    ]
    assert close.calls == [303]


def test_ntcreatefile_baseexception_closes_written_output() -> None:
    nt_create = FakeNtCreateFile(
        ((0, _CHILD_HANDLE),),
        after_write=KeyboardInterrupt("private detail"),
    )
    close = FakeCloseHandle()
    api = _raw_api(nt_create_file=nt_create, close_handle=close)

    with pytest.raises(KeyboardInterrupt, match="private detail"):
        api.open_component(_ROOT_HANDLE, "store")
    assert close.calls == [_CHILD_HANDLE]


def test_device_io_control_uses_exact_fsctl_and_bounded_buffer() -> None:
    raw = _reparse_buffer(tag=_MOUNT_TAG, substitute=r"\??\C:\allowed")
    device_io = FakeDeviceIoControl(raw)
    api = _raw_api(device_io=device_io)

    assert api.read_reparse_point(_CHILD_HANDLE) == raw
    assert device_io.calls == [(_CHILD_HANDLE, 0x000900A8, None, 0, 16 * 1024, None)]


def test_final_path_adapter_accepts_only_strict_dos_namespace() -> None:
    api = _raw_api(get_final_path=FakeGetFinalPath({_CHILD_HANDLE: r"\\?\D:\data\store"}))
    assert api.normalized_dos_path(_CHILD_HANDLE) == r"D:\data\store"

    for unsafe in (
        r"\\?\UNC\server\share",
        r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\\",
        r"\\?\GLOBALROOT\Device\HarddiskVolume1",
    ):
        api = _raw_api(get_final_path=FakeGetFinalPath({_CHILD_HANDLE: unsafe}))
        with pytest.raises(helper.WindowsLocalStoreHealthDenied):
            api.normalized_dos_path(_CHILD_HANDLE)


@pytest.mark.parametrize(
    "component",
    (
        "",
        ".",
        "..",
        r"nested\store",
        "nested/store",
        "store.",
        "store ",
        "artifact:stream",
        "NUL",
        "con.txt",
        "line\nbreak",
        "\ud800",
    ),
)
def test_native_component_adapter_rejects_aliasing_names_before_io(
    component: str,
) -> None:
    nt_create = FakeNtCreateFile()
    api = _raw_api(nt_create_file=nt_create)

    with pytest.raises(helper.WindowsLocalStoreHealthError):
        api.open_component(_ROOT_HANDLE, component)
    assert nt_create.calls == []


class ScriptedDirectoryApi:
    def __init__(
        self,
        *,
        roots: Mapping[str, int],
        children: Mapping[tuple[int, str], int],
        final_paths: Mapping[int, str],
        file_children: Mapping[tuple[int, str], int] | None = None,
        file_data: Mapping[int, bytes] | None = None,
        snapshots: Mapping[int, Sequence[helper._RegularFileSnapshot]] | None = None,
        read_results: Mapping[int, Sequence[bytes]] | None = None,
        attributes: Mapping[int, int] | None = None,
        tags: Mapping[int, int] | None = None,
        reparses: Mapping[int, bytes] | None = None,
        close_failures: Mapping[int, BaseException] | None = None,
        attribute_failure: BaseException | None = None,
    ) -> None:
        self.roots = dict(roots)
        self.children = dict(children)
        self.file_children = dict(file_children or {})
        self.file_data = dict(file_data or {})
        self.snapshots = {handle: list(values) for handle, values in (snapshots or {}).items()}
        self.read_results = {
            handle: list(values) for handle, values in (read_results or {}).items()
        }
        self.read_offsets: dict[int, int] = {}
        self.final_paths = dict(final_paths)
        self.attributes = dict(attributes or {})
        self.tags = dict(tags or {})
        self.reparses = dict(reparses or {})
        self.close_failures = dict(close_failures or {})
        self.attribute_failure = attribute_failure
        self.root_calls: list[str] = []
        self.component_calls: list[tuple[int, str]] = []
        self.file_component_calls: list[tuple[int, str]] = []
        self.snapshot_calls: list[int] = []
        self.read_calls: list[tuple[int, int]] = []
        self.attribute_calls: list[int] = []
        self.final_path_calls: list[int] = []
        self.reparse_calls: list[int] = []
        self.close_calls: list[int] = []

    def open_root(self, root: str) -> int:
        self.root_calls.append(root)
        try:
            return self.roots[root]
        except KeyError:
            raise helper.WindowsLocalStoreHealthDenied from None

    def open_component(self, parent: int, component: str) -> int:
        self.component_calls.append((parent, component))
        try:
            return self.children[(parent, component)]
        except KeyError:
            raise helper.WindowsLocalStoreHealthMissing from None

    def open_file_component(self, parent: int, component: str) -> int:
        self.file_component_calls.append((parent, component))
        try:
            return self.file_children[(parent, component)]
        except KeyError:
            raise helper.WindowsLocalStoreHealthMissing from None

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        self.attribute_calls.append(handle)
        if self.attribute_failure is not None:
            raise self.attribute_failure
        return (
            self.attributes.get(handle, _DIRECTORY),
            self.tags.get(handle, 0),
        )

    def normalized_dos_path(self, handle: int) -> str:
        self.final_path_calls.append(handle)
        return self.final_paths[handle]

    def read_reparse_point(self, handle: int) -> bytes:
        self.reparse_calls.append(handle)
        return self.reparses[handle]

    def regular_file_snapshot(self, handle: int) -> helper._RegularFileSnapshot:
        self.snapshot_calls.append(handle)
        values = self.snapshots[handle]
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    def read_file(self, handle: int, byte_count: int) -> bytes:
        self.read_calls.append((handle, byte_count))
        scripted = self.read_results.get(handle)
        if scripted:
            return scripted.pop(0)
        data = self.file_data.get(handle, b"")
        offset = self.read_offsets.get(handle, 0)
        chunk = data[offset : offset + byte_count]
        self.read_offsets[handle] = offset + len(chunk)
        return chunk

    def close(self, handle: int) -> None:
        self.close_calls.append(handle)
        failure = self.close_failures.get(handle)
        if failure is not None:
            raise failure


def _plain_api() -> ScriptedDirectoryApi:
    return ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "allowed"): 20, (20, "store"): 30},
        final_paths={10: "C:\\", 20: r"C:\allowed", 30: r"C:\allowed\store"},
    )


def _direct_file_api(
    data: bytes,
    *,
    announced_size: int | None = None,
    snapshots: Sequence[helper._RegularFileSnapshot] | None = None,
) -> ScriptedDirectoryApi:
    size = len(data) if announced_size is None else announced_size
    stable = _snapshot(size)
    return ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20},
        file_children={(20, "artifact.bin"): _FILE_HANDLE},
        final_paths={10: "C:\\", 20: r"C:\grant"},
        file_data={_FILE_HANDLE: data},
        snapshots={_FILE_HANDLE: snapshots or (stable, stable)},
        attributes={_FILE_HANDLE: 0x80},
    )


class TrackingDirectoryIterator:
    def __init__(
        self,
        entries: Sequence[helper._DirectoryEntry],
        *,
        failure: BaseException | None = None,
        close_failure: BaseException | None = None,
    ) -> None:
        self.entries = list(entries)
        self.failure = failure
        self.close_failure = close_failure
        self.closed = False

    def __iter__(self) -> TrackingDirectoryIterator:
        return self

    def __next__(self) -> helper._DirectoryEntry:
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        if not self.entries:
            raise StopIteration
        return self.entries.pop(0)

    def close(self) -> None:
        assert not self.closed
        self.closed = True
        if self.close_failure is not None:
            raise self.close_failure


class ScriptedEnumerationApi:
    def __init__(
        self,
        *,
        roots: Mapping[str, int],
        children: Mapping[tuple[int, str], int],
        candidate_children: Mapping[tuple[int, str], int] | None = None,
        final_paths: Mapping[int, str],
        identities: Mapping[int, tuple[int, bytes]],
        candidate_identities: Mapping[int, tuple[int, bytes]] | None = None,
        candidate_file_types: Mapping[int, int] | None = None,
        listings: Mapping[int, Sequence[helper._DirectoryEntry]],
        attributes: Mapping[int, int] | None = None,
        tags: Mapping[int, int] | None = None,
        reparses: Mapping[int, bytes] | None = None,
        iterator_failures: Mapping[int, BaseException] | None = None,
        iterator_close_failures: Mapping[int, BaseException] | None = None,
        close_failures: Mapping[int, BaseException] | None = None,
    ) -> None:
        self.roots = dict(roots)
        self.children = dict(children)
        self.child_keys_by_handle = {
            handle: key for key, handle in self.children.items()
        }
        self.candidate_children = dict(candidate_children or {})
        self.final_paths = dict(final_paths)
        self.identities = dict(identities)
        self.candidate_identities = dict(candidate_identities or {})
        self.candidate_file_types = dict(candidate_file_types or {})
        self.listings = {handle: list(entries) for handle, entries in listings.items()}
        self.candidate_records = {
            (parent, entry.name): entry
            for parent, entries in self.listings.items()
            for entry in entries
            if not entry.file_attributes & _DIRECTORY
        }
        self.candidate_keys_by_handle = {
            handle: key for key, handle in self.candidate_children.items()
        }
        self.next_candidate_handle = 10_000
        self.attributes = dict(attributes or {})
        self.tags = dict(tags or {})
        self.reparses = dict(reparses or {})
        self.iterator_failures = dict(iterator_failures or {})
        self.iterator_close_failures = dict(iterator_close_failures or {})
        self.close_failures = dict(close_failures or {})
        self.root_calls: list[str] = []
        self.component_calls: list[tuple[int, str]] = []
        self.alias_component_calls: list[tuple[int, str]] = []
        self.candidate_component_calls: list[tuple[int, str]] = []
        self.attribute_calls: list[int] = []
        self.final_path_calls: list[int] = []
        self.identity_calls: list[int] = []
        self.candidate_identity_calls: list[int] = []
        self.enumeration_calls: list[int] = []
        self.reparse_calls: list[int] = []
        self.close_calls: list[int] = []
        self.iterators: list[TrackingDirectoryIterator] = []

    def open_enumerable_root(self, root: str) -> int:
        self.root_calls.append(root)
        try:
            return self.roots[root]
        except KeyError:
            raise helper.WindowsLocalStoreHealthDenied from None

    def open_enumerable_component(self, parent: int, component: str) -> int:
        self.component_calls.append((parent, component))
        try:
            return self.children[(parent, component)]
        except KeyError:
            raise helper.WindowsLocalStoreHealthMissing from None

    def open_component(self, parent: int, component: str) -> int:
        self.alias_component_calls.append((parent, component))
        try:
            return self.children[(parent, component)]
        except KeyError:
            raise helper.WindowsLocalStoreHealthMissing from None

    def open_candidate_component(self, parent: int, component: str) -> int:
        self.candidate_component_calls.append((parent, component))
        key = (parent, component)
        record = self.candidate_records.get(key)
        if record is None:
            record = next(
                (
                    entry
                    for entry in self.listings.get(parent, ())
                    if entry.name == component
                    and (
                        not entry.file_attributes & _DIRECTORY
                        or key in self.candidate_children
                    )
                ),
                None,
            )
        if record is None:
            raise helper.WindowsLocalStoreHealthMissing
        self.candidate_records[key] = record
        handle = self.candidate_children.get(key)
        if handle is None:
            while (
                self.next_candidate_handle in self.candidate_keys_by_handle
                or self.next_candidate_handle in self.roots.values()
                or self.next_candidate_handle in self.children.values()
            ):
                self.next_candidate_handle += 1
            handle = self.next_candidate_handle
            self.next_candidate_handle += 1
            self.candidate_children[key] = handle
            self.candidate_keys_by_handle[handle] = key
        return handle

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        self.attribute_calls.append(handle)
        record = (
            self.candidate_records[self.candidate_keys_by_handle[handle]]
            if handle in self.candidate_keys_by_handle
            else None
        )
        return (
            self.attributes.get(
                handle,
                _DIRECTORY if record is None else record.file_attributes,
            ),
            self.tags.get(
                handle,
                0 if record is None else record.reparse_tag,
            ),
        )

    def normalized_dos_path(self, handle: int) -> str:
        self.final_path_calls.append(handle)
        return self.final_paths[handle]

    def directory_identity(self, handle: int) -> tuple[int, bytes]:
        self.identity_calls.append(handle)
        exact = self.identities.get(handle)
        if exact is not None:
            return exact
        parent, component = self.child_keys_by_handle[handle]
        record = next(
            entry
            for entry in self.listings[parent]
            if entry.name == component
            and entry.file_attributes & _DIRECTORY
        )
        return (self.identities[parent][0], record.file_id)

    def candidate_file_identity(
        self,
        handle: int,
    ) -> tuple[int, bytes] | None:
        self.candidate_identity_calls.append(handle)
        if self.candidate_file_types.get(handle, 1) != 1:
            return None
        exact = self.candidate_identities.get(handle)
        if exact is not None:
            return exact
        parent, component = self.candidate_keys_by_handle[handle]
        return (
            self.identities[parent][0],
            self.candidate_records[(parent, component)].file_id,
        )

    def enumerate_directory(
        self,
        handle: int,
    ) -> TrackingDirectoryIterator:
        self.enumeration_calls.append(handle)
        iterator = TrackingDirectoryIterator(
            self.listings[handle],
            failure=self.iterator_failures.get(handle),
            close_failure=self.iterator_close_failures.get(handle),
        )
        self.iterators.append(iterator)
        return iterator

    def read_reparse_point(self, handle: int) -> bytes:
        self.reparse_calls.append(handle)
        return self.reparses[handle]

    def close(self, handle: int) -> None:
        self.close_calls.append(handle)
        failure = self.close_failures.get(handle)
        if failure is not None:
            raise failure


def _entry(
    name: str,
    *,
    attributes: int = 0x80,
    tag: int = 0,
    identity_byte: bytes | None = None,
    identity: bytes | None = None,
) -> helper._DirectoryEntry:
    assert identity is None or len(identity) == 16
    assert identity is None or identity_byte is None
    if identity is not None:
        file_id = identity
    else:
        file_id = (
            (name.encode("utf-8") + b"\0" * 16)[:16]
            if identity_byte is None
            else identity_byte * 16
        )
    return helper._DirectoryEntry(
        name,
        attributes,
        tag,
        file_id,
    )


def _enumeration_api() -> ScriptedEnumerationApi:
    return ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "sub"): 20},
        final_paths={10: "C:\\", 20: r"C:\sub"},
        identities={10: (1, b"r" * 16), 20: (1, b"s" * 16)},
        listings={
            10: [
                _entry("fallback.bin"),
                _entry(
                    "sub",
                    attributes=_DIRECTORY,
                    identity_byte=b"s",
                ),
            ],
            20: [_entry("artifact.dat")],
        },
    )


def _deep_enumeration_api(
    directory_components: Sequence[str],
    leaf: str,
) -> tuple[ScriptedEnumerationApi, int]:
    children: dict[tuple[int, str], int] = {}
    final_paths = {10: "C:\\"}
    identities = {10: (1, (1).to_bytes(16, "little"))}
    listings: dict[int, list[helper._DirectoryEntry]] = {}
    current = 10
    rendered_components: list[str] = []
    for index, component in enumerate(directory_components, start=1):
        child = 10 + index
        children[(current, component)] = child
        child_identity = (index + 1).to_bytes(16, "little")
        listings[current] = [
            _entry(
                component,
                attributes=_DIRECTORY,
                identity=child_identity,
            )
        ]
        rendered_components.append(component)
        final_paths[child] = "C:\\" + "\\".join(rendered_components)
        identities[child] = (1, child_identity)
        current = child
    listings[current] = [_entry(leaf)]
    return (
        ScriptedEnumerationApi(
            roots={"C:\\": 10},
            children=children,
            final_paths=final_paths,
            identities=identities,
            listings=listings,
        ),
        current,
    )


def test_enumeration_zero_limits_are_output_only_and_perform_no_native_io() -> None:
    api = ScriptedEnumerationApi(
        roots={},
        children={},
        final_paths={},
        identities={},
        listings={},
    )
    no_files = io.BytesIO()
    no_directories = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\",),
                max_files=0,
                max_directories=4,
            ),
            api=api,
            output=no_files,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(no_files) == {
        "v": 1,
        "status": "complete",
        "directories": 0,
        "candidates": [],
    }
    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\",),
                max_files=4,
                max_directories=0,
            ),
            api=api,
            output=no_directories,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(no_directories) == {
        "v": 1,
        "status": "limit",
        "directories": 0,
        "candidates": [],
    }
    assert api.root_calls == []


def test_direct_enumeration_rejects_an_empty_root_set() -> None:
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request((), max_files=1, max_directories=0),
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_files", True),
        ("max_files", -1),
        ("max_files", 4097),
        ("max_directories", True),
        ("max_directories", -1),
        ("max_directories", 4097),
        ("target_name", ""),
        ("target_name", "nested/name"),
    ),
)
def test_enumeration_protocol_rejects_nonexact_limits_and_target_names(
    field: str,
    value: object,
) -> None:
    payload = cast(
        dict[str, object],
        json.loads(_enumerate_request(("C:\\",))),
    )
    payload[field] = value
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    output = io.BytesIO()

    assert helper.execute_request(raw, output=output) == helper.FAILED_EXIT
    assert output.getvalue() == b""


@pytest.mark.parametrize(
    ("target_name", "expected_exit"),
    (
        ("é" * 128, helper.COMPLETE_EXIT),
        ("é" * 255, helper.COMPLETE_EXIT),
        ("😀" * 127 + "a", helper.COMPLETE_EXIT),
        ("é" * 256, helper.FAILED_EXIT),
        ("😀" * 128, helper.FAILED_EXIT),
    ),
)
def test_target_name_uses_windows_utf16_component_budget(
    target_name: str,
    expected_exit: int,
) -> None:
    api = ScriptedEnumerationApi(
        roots={},
        children={},
        final_paths={},
        identities={},
        listings={},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\",),
                target_name=target_name,
                max_files=0,
            ),
            api=api,
            output=output,
        )
        == expected_exit
    )
    if expected_exit == helper.COMPLETE_EXIT:
        assert _enumeration_payload(output)["status"] == "complete"
    else:
        assert output.getvalue() == b""
    assert api.root_calls == []


@pytest.mark.parametrize("name", ("é" * 128, "é" * 255, "😀" * 127 + "a"))
def test_legal_utf16_component_is_retained_even_when_utf8_exceeds_255(
    name: str,
) -> None:
    api, _parent = _deep_enumeration_api((), name)
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "complete",
        "directories": 1,
        "candidates": [{"root_index": 0, "locator": [name]}],
    }


def test_overlong_utf16_file_component_is_limited_before_child_open() -> None:
    name = "é" * 256
    api, _parent = _deep_enumeration_api((), name)
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [],
    }
    assert api.candidate_component_calls == []
    assert api.candidate_identity_calls == []
    assert api.candidate_children == {}
    assert api.close_calls == [10]


def test_overlong_utf16_directory_component_is_limited_before_child_open() -> None:
    name = "é" * 256
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, name): 20},
        final_paths={10: "C:\\", 20: "C:\\" + name},
        identities={
            10: (1, b"r" * 16),
            20: (1, b"d" * 16),
        },
        listings={
            10: [_entry(name, attributes=_DIRECTORY)],
            20: [_entry("never.bin")],
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [],
    }
    assert api.component_calls == []
    assert api.identity_calls == [10]
    assert api.close_calls == [10]


def test_exact_windows_utf16_locator_boundary_is_retained() -> None:
    directories = ("d" * 255,) * 63 + ("e" * 254,)
    leaf = "f"
    assert (
        sum(len(component) for component in (*directories, leaf))
        + len(directories)
        == 16 * 1024
    )
    api, _parent = _deep_enumeration_api(directories, leaf)
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=65),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["status"] == "complete"
    assert _enumeration_payload(output)["directories"] == 65
    assert _enumeration_payload(output)["candidates"] == [
        {"root_index": 0, "locator": [*directories, leaf]}
    ]


def test_overlong_windows_utf16_file_locator_is_limited_before_child_open() -> None:
    directories = ("d" * 255,) * 63 + ("e" * 254,)
    leaf = "ff"
    assert (
        sum(len(component) for component in (*directories, leaf))
        + len(directories)
        == 16 * 1024 + 1
    )
    api, parent = _deep_enumeration_api(directories, leaf)
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=65),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 65,
        "candidates": [],
    }
    assert (parent, leaf) not in api.candidate_component_calls
    assert api.candidate_identity_calls == []
    assert api.candidate_children == {}


def test_overlong_windows_utf16_directory_locator_stops_before_child_open() -> None:
    directories = ("d" * 255,) * 63 + ("e" * 254,)
    api, parent = _deep_enumeration_api(directories, "placeholder.bin")
    child_name = "ff"
    child_handle = 999
    api.listings[parent] = [_entry(child_name, attributes=_DIRECTORY)]
    api.children[(parent, child_name)] = child_handle
    api.final_paths[child_handle] = (
        api.final_paths[parent] + "\\" + child_name
    )
    api.identities[child_handle] = (1, b"x" * 16)
    api.listings[child_handle] = []
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=66),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 65,
        "candidates": [],
    }
    assert (parent, child_name) not in api.component_calls
    assert child_handle not in api.identity_calls
    assert child_handle not in api.close_calls


def test_one_pass_global_preference_displaces_fallback_at_candidate_cap() -> None:
    api = _enumeration_api()
    api.listings[10].append(_entry("later.bin"))
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\",),
                target_name="artifact.dat",
                max_files=2,
            ),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 2,
        "candidates": [
            {"root_index": 0, "locator": ["sub", "artifact.dat"]},
            {"root_index": 0, "locator": ["fallback.bin"]},
        ],
    }
    assert api.enumeration_calls == [10, 20]
    assert all(iterator.closed for iterator in api.iterators)


def test_regular_identity_alias_dedup_promotes_preferred_without_limit() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry("first-alias.bin", identity_byte=b"x"),
                _entry("artifact.dat", identity_byte=b"x"),
                _entry("other.bin", identity_byte=b"y"),
            ]
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\",),
                target_name="artifact.dat",
                max_files=2,
            ),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "complete",
        "directories": 1,
        "candidates": [
            {"root_index": 0, "locator": ["artifact.dat"]},
            {"root_index": 0, "locator": ["other.bin"]},
        ],
    }


def test_candidate_identity_mismatch_discards_uncertain_enumeration() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "first.bin"): 101,
            (10, "second.bin"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        candidate_identities={
            101: (7, b"a" * 16),
            102: (7, b"b" * 16),
        },
        listings={
            10: [
                _entry("first.bin", identity_byte=b"x"),
                _entry("second.bin", identity_byte=b"x"),
            ]
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_files=2),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_component_calls == [(10, "first.bin")]
    assert api.candidate_identity_calls == [101]
    assert api.close_calls == [101, 10]


def test_file_symlink_identity_mismatch_fails_before_reparse_read() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={(10, "link.bin"): 101},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        candidate_identities={101: (7, b"x" * 16)},
        listings={10: [_entry("link.bin", identity_byte=b"l")]},
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target.bin",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_identity_calls == [101]
    assert api.reparse_calls == []
    assert api.close_calls == [101, 10]


def test_exact_handle_classification_skips_unsupported_and_special_files() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "unsupported.bin"): 101,
            (10, "symlink.bin"): 102,
            (10, "special.bin"): 103,
            (10, "ordinary.bin"): 104,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        candidate_file_types={103: 2},
        listings={
            10: [
                _entry("unsupported.bin"),
                _entry("symlink.bin"),
                _entry("special.bin"),
                _entry("ordinary.bin"),
            ]
        },
        attributes={101: 0x400, 102: 0x400},
        tags={101: 0x8000001B, 102: _SYMLINK_TAG},
        reparses={
            102: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="unsupported.bin",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_files=1),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "complete",
        "directories": 1,
        "candidates": [
            {"root_index": 0, "locator": ["ordinary.bin"]}
        ],
    }
    assert api.candidate_component_calls == [
        (10, "unsupported.bin"),
        (10, "symlink.bin"),
        (10, "unsupported.bin"),
        (10, "special.bin"),
        (10, "ordinary.bin"),
    ]
    assert api.candidate_identity_calls == [101, 102, 101, 103, 104]
    assert api.reparse_calls == [102]
    assert api.close_calls == [101, 102, 101, 103, 104, 10]


def test_file_symlink_promotes_preferred_alias_and_deduplicates_later_target() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "decoy.bin"): 101,
            (10, "artifact.dat"): 102,
            (10, "target.bin"): 103,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry("decoy.bin", identity_byte=b"d"),
                _entry(
                    "artifact.dat",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                ),
                _entry("target.bin", identity_byte=b"t"),
            ]
        },
        attributes={102: 0x400},
        tags={102: _SYMLINK_TAG},
        reparses={
            102: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target.bin",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\",),
                target_name="artifact.dat",
                max_files=1,
            ),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [
            {"root_index": 0, "locator": ["artifact.dat"]}
        ],
    }
    assert api.candidate_component_calls == [
        (10, "decoy.bin"),
        (10, "artifact.dat"),
        (10, "target.bin"),
        (10, "target.bin"),
    ]
    assert api.candidate_identity_calls == [101, 102, 103, 103]
    assert api.reparse_calls == [102]
    assert api.close_calls == [101, 102, 103, 103, 10]


def test_chained_file_symlinks_preserve_first_alias_and_final_identity() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "link-one.bin"): 101,
            (10, "link-two.bin"): 102,
            (10, "target.bin"): 103,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link-one.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"a",
                ),
                _entry(
                    "link-two.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"b",
                ),
                _entry("target.bin", identity_byte=b"t"),
            ]
        },
        attributes={101: 0x400, 102: 0x400},
        tags={101: _SYMLINK_TAG, 102: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="link-two.bin",
                relative=True,
            ),
            102: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target.bin",
                relative=True,
            ),
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "complete",
        "directories": 1,
        "candidates": [
            {"root_index": 0, "locator": ["link-one.bin"]}
        ],
    }
    assert api.reparse_calls == [101, 102, 102]
    assert api.close_calls == [101, 102, 103, 102, 103, 103, 10]


def test_file_symlink_cycle_discards_all_output_and_closes_exact_handles() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "link-one.bin"): 101,
            (10, "link-two.bin"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link-one.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"a",
                )
            ]
        },
        attributes={101: 0x400, 102: 0x400},
        tags={101: _SYMLINK_TAG, 102: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="link-two.bin",
                relative=True,
            ),
            102: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="link-one.bin",
                relative=True,
            ),
        },
    )
    api.candidate_records[(10, "link-two.bin")] = _entry(
        "link-two.bin",
        attributes=0x400,
        tag=_SYMLINK_TAG,
        identity_byte=b"b",
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_component_calls == [
        (10, "link-one.bin"),
        (10, "link-two.bin"),
        (10, "link-one.bin"),
    ]
    assert api.reparse_calls == [101, 102, 101]
    assert api.close_calls == [101, 102, 101, 10]
    assert all(iterator.closed for iterator in api.iterators)


@pytest.mark.parametrize(
    ("substitute", "relative"),
    (
        (r"..\outside.bin", True),
        (r"\??\D:\outside.bin", False),
    ),
)
def test_file_symlink_escape_fails_before_target_side_open(
    substitute: str,
    relative: bool,
) -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={(10, "link.bin"): 101},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                )
            ]
        },
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute=substitute,
                relative=relative,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.DENIED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_component_calls == [(10, "link.bin")]
    assert api.alias_component_calls == []
    assert api.close_calls == [101, 10]


def test_file_symlink_missing_target_race_discards_retained_candidates() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "decoy.bin"): 101,
            (10, "link.bin"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry("decoy.bin", identity_byte=b"d"),
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                ),
            ]
        },
        attributes={102: 0x400},
        tags={102: _SYMLINK_TAG},
        reparses={
            102: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="missing.bin",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_component_calls == [
        (10, "decoy.bin"),
        (10, "link.bin"),
        (10, "missing.bin"),
    ]
    assert api.close_calls == [101, 102, 10]
    assert all(iterator.closed for iterator in api.iterators)


def test_file_symlink_target_becoming_directory_is_fatal() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "link.bin"): 101,
            (10, "target"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                ),
                _entry(
                    "target",
                    attributes=_DIRECTORY,
                    identity_byte=b"d",
                ),
            ]
        },
        attributes={101: 0x400, 102: _DIRECTORY},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_identity_calls == [101]
    assert api.close_calls == [101, 102, 10]


def test_file_symlink_without_final_leaf_is_fatal() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={(10, "link.bin"): 101},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                )
            ]
        },
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute=".",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_component_calls == [(10, "link.bin")]
    assert api.close_calls == [101, 10]


def test_file_symlink_target_cleanup_failure_discards_all_output() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "link.bin"): 101,
            (10, "target.bin"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                ),
                _entry("target.bin", identity_byte=b"t"),
            ]
        },
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target.bin",
                relative=True,
            )
        },
        close_failures={
            102: KeyboardInterrupt("private exact-target cleanup")
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.close_calls == [101, 102, 10]
    assert all(iterator.closed for iterator in api.iterators)


def test_file_symlink_at_directory_cap_is_limited_without_target_follow() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "link.bin"): 101,
            (10, "target.bin"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                )
            ]
        },
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target.bin",
                relative=True,
            )
        },
    )
    api.candidate_records[(10, "target.bin")] = _entry(
        "target.bin",
        identity_byte=b"t",
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=1),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [],
    }
    assert api.candidate_component_calls == [(10, "link.bin")]
    assert api.candidate_identity_calls == [101]
    assert api.reparse_calls == []
    assert api.close_calls == [101, 10]


def test_file_symlink_intermediate_directory_is_not_enumerated_or_counted() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "sub"): 20},
        candidate_children={
            (10, "link.bin"): 101,
            (20, "target.bin"): 102,
        },
        final_paths={10: "C:\\", 20: r"C:\sub"},
        identities={
            10: (7, b"r" * 16),
            20: (7, b"s" * 16),
        },
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                )
            ],
            20: [_entry("target.bin", identity_byte=b"t")],
        },
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute=r"sub\target.bin",
                relative=True,
            )
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "complete",
        "directories": 1,
        "candidates": [
            {"root_index": 0, "locator": ["link.bin"]}
        ],
    }
    assert api.alias_component_calls == [(10, "sub")]
    assert api.component_calls == []
    assert api.enumeration_calls == [10]
    assert api.close_calls == [101, 102, 20, 10]


def test_file_symlink_expansion_ceiling_returns_limit_with_no_candidate() -> None:
    names = [f"link-{index}.bin" for index in range(257)]
    handles = {
        (10, name): 1_000 + index
        for index, name in enumerate(names)
    }
    reparses = {
        1_000 + index: _reparse_buffer(
            tag=_SYMLINK_TAG,
            substitute=(
                names[index + 1]
                if index + 1 < len(names)
                else "never-opened.bin"
            ),
            relative=True,
        )
        for index in range(len(names))
    }
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children=handles,
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    names[0],
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity=(1).to_bytes(16, "little"),
                )
            ]
        },
        attributes={handle: 0x400 for handle in handles.values()},
        tags={handle: _SYMLINK_TAG for handle in handles.values()},
        reparses=reparses,
    )
    for index, name in enumerate(names[1:], start=2):
        api.candidate_records[(10, name)] = _entry(
            name,
            attributes=0x400,
            tag=_SYMLINK_TAG,
            identity=index.to_bytes(16, "little"),
        )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [],
    }
    assert len(api.candidate_component_calls) == 257
    assert len(api.reparse_calls) == 257
    assert (10, "never-opened.bin") not in api.candidate_component_calls
    assert api.close_calls == [*handles.values(), 10]


def test_file_symlink_target_metadata_pressure_returns_limit_before_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helper,
        "MAX_LOCAL_FILESYSTEM_ENUMERATION_METADATA_BYTES",
        128,
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={
            (10, "link.bin"): 101,
            (10, "target.bin"): 102,
        },
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link.bin",
                    attributes=0x400,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"l",
                )
            ]
        },
        attributes={101: 0x400},
        tags={101: _SYMLINK_TAG},
        reparses={
            101: _reparse_buffer(
                tag=_SYMLINK_TAG,
                substitute="target.bin",
                relative=True,
            )
        },
    )
    api.candidate_records[(10, "target.bin")] = _entry(
        "target.bin",
        identity_byte=b"t",
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [],
    }
    assert api.candidate_component_calls == [(10, "link.bin")]
    assert api.reparse_calls == [101]
    assert api.close_calls == [101, 10]


def test_record_to_directory_race_discards_uncertain_enumeration() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={(10, "raced.bin"): 101},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={10: [_entry("raced.bin")]},
        attributes={101: _DIRECTORY},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.candidate_identity_calls == []
    assert api.close_calls == [101, 10]
    assert all(iterator.closed for iterator in api.iterators)


def test_directory_record_identity_mismatch_fails_before_target_follow() -> None:
    target = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"\??\C:\target",
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "link"): 20, (10, "target"): 30},
        final_paths={10: "C:\\", 30: r"C:\target"},
        identities={
            10: (7, b"r" * 16),
            20: (7, b"x" * 16),
            30: (7, b"t" * 16),
        },
        listings={
            10: [
                _entry(
                    "link",
                    attributes=_REPARSE_DIRECTORY,
                    tag=_SYMLINK_TAG,
                    identity_byte=b"y",
                )
            ],
            30: [_entry("inside.bin")],
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _SYMLINK_TAG},
        reparses={20: target},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.identity_calls == [10, 20]
    assert api.reparse_calls == []
    assert api.enumeration_calls == [10]
    assert api.close_calls == [20, 10]
    assert all(iterator.closed for iterator in api.iterators)


def test_exact_cloud_file_is_eligible_without_payload_or_data_read() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={(10, "cloud.bin"): 101},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={10: [_entry("cloud.bin")]},
        attributes={101: 0x400},
        tags={101: _CLOUD_TAG},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["candidates"] == [
        {"root_index": 0, "locator": ["cloud.bin"]}
    ]
    assert api.reparse_calls == []
    assert api.candidate_identity_calls == [101]
    assert api.close_calls == [101, 10]


def test_candidate_handle_cleanup_failure_discards_the_entire_response() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        candidate_children={(10, "artifact.bin"): 101},
        final_paths={10: "C:\\"},
        identities={10: (7, b"r" * 16)},
        listings={10: [_entry("artifact.bin")]},
        close_failures={101: KeyboardInterrupt("private candidate close")},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.close_calls == [101, 10]
    assert all(iterator.closed for iterator in api.iterators)


def test_directory_limit_counts_before_query_without_off_by_one() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "one"): 20, (20, "two"): 30},
        final_paths={10: "C:\\", 20: r"C:\one", 30: r"C:\one\two"},
        identities={
            10: (1, b"a" * 16),
            20: (1, b"b" * 16),
            30: (1, b"c" * 16),
        },
        listings={
            10: [
                _entry(
                    "one",
                    attributes=_DIRECTORY,
                    identity_byte=b"b",
                )
            ],
            20: [
                _entry(
                    "two",
                    attributes=_DIRECTORY,
                    identity_byte=b"c",
                )
            ],
            30: [_entry("never.bin")],
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=2),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["status"] == "limit"
    assert _enumeration_payload(output)["directories"] == 2
    assert api.enumeration_calls == [10, 20]
    assert 30 not in api.identity_calls
    assert 30 not in api.close_calls


def test_exhausted_directory_cap_opens_no_remaining_child() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={
            (10, "one"): 20,
            (10, "two"): 30,
            (10, "three"): 40,
        },
        final_paths={
            10: "C:\\",
            20: r"C:\one",
            30: r"C:\two",
            40: r"C:\three",
        },
        identities={
            10: (1, b"a" * 16),
            20: (1, b"b" * 16),
            30: (1, b"c" * 16),
            40: (1, b"d" * 16),
        },
        listings={
            10: [
                _entry("one", attributes=_DIRECTORY),
                _entry("two", attributes=_DIRECTORY),
                _entry("three", attributes=_DIRECTORY),
            ],
            20: [],
            30: [],
            40: [],
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=1),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["status"] == "limit"
    assert _enumeration_payload(output)["directories"] == 1
    assert api.enumeration_calls == [10]
    assert api.identity_calls == [10]
    assert api.close_calls == [10]


def test_metadata_pressure_returns_a_clean_limit_without_querying_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_name = "directory-" + "x" * 64
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, long_name): 20},
        final_paths={10: "C:\\", 20: rf"C:\{long_name}"},
        identities={10: (1, b"a" * 16), 20: (1, b"b" * 16)},
        listings={10: [_entry(long_name, attributes=_DIRECTORY)], 20: []},
    )
    monkeypatch.setattr(
        helper,
        "MAX_LOCAL_FILESYSTEM_ENUMERATION_METADATA_BYTES",
        160,
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "limit",
        "directories": 1,
        "candidates": [],
    }
    assert api.enumeration_calls == [10]
    assert 20 not in api.identity_calls
    assert 20 not in api.close_calls


def test_root_alias_identity_is_enumerated_once_across_all_roots() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10, "D:\\": 20},
        children={},
        final_paths={10: "C:\\", 20: "D:\\"},
        identities={10: (7, b"x" * 16), 20: (7, b"x" * 16)},
        listings={10: [_entry("only.bin")]},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(
                ("C:\\", "D:\\"),
                max_directories=2,
            ),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output) == {
        "v": 1,
        "status": "complete",
        "directories": 2,
        "candidates": [{"root_index": 0, "locator": ["only.bin"]}],
    }
    assert api.enumeration_calls == [10]
    assert api.close_calls == [10_000, 10, 20]


def test_safe_relative_junction_is_resolved_beneath_retained_boundary() -> None:
    target = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\target",
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "link"): 20, (10, "target"): 30},
        final_paths={10: "C:\\", 30: r"C:\target"},
        identities={10: (1, b"r" * 16), 30: (1, b"t" * 16)},
        listings={
            10: [_entry("link", attributes=_REPARSE_DIRECTORY, tag=_MOUNT_TAG)],
            30: [_entry("inside.bin")],
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _MOUNT_TAG},
        reparses={20: target},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["candidates"] == [
        {"root_index": 0, "locator": ["link", "inside.bin"]}
    ]
    assert api.reparse_calls == [20]
    assert api.enumeration_calls == [10, 30]


def test_escaping_relative_junction_fails_before_target_open_and_output() -> None:
    escaping = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"..\outside",
        relative=True,
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "link"): 20},
        final_paths={10: "C:\\"},
        identities={10: (1, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "link",
                    attributes=_REPARSE_DIRECTORY,
                    tag=_SYMLINK_TAG,
                )
            ]
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _SYMLINK_TAG},
        reparses={20: escaping},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.DENIED_EXIT
    )
    assert output.getvalue() == b""
    assert all(component != "outside" for _, component in api.component_calls)
    assert all(iterator.closed for iterator in api.iterators)


@pytest.mark.parametrize(
    "substitute",
    (
        r"\??\Volume{01234567-89ab-cdef-0123-456789abcdef}\\",
        r"\??\UNC\server\share",
        r"\Device\HarddiskVolume1\outside",
    ),
)
def test_unsupported_junction_namespaces_emit_no_partial_response(
    substitute: str,
) -> None:
    raw = _reparse_buffer(tag=_MOUNT_TAG, substitute=substitute)
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "link"): 20},
        final_paths={10: "C:\\"},
        identities={10: (1, b"r" * 16)},
        listings={
            10: [_entry("link", attributes=_REPARSE_DIRECTORY, tag=_MOUNT_TAG)]
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _MOUNT_TAG},
        reparses={20: raw},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.DENIED_EXIT
    )
    assert output.getvalue() == b""


def test_cloud_directory_uses_the_same_handle_without_payload_or_reopen() -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "cloud"): 20},
        final_paths={10: "C:\\", 20: r"C:\cloud"},
        identities={10: (1, b"r" * 16), 20: (1, b"c" * 16)},
        listings={
            10: [
                _entry(
                    "cloud",
                    attributes=_REPARSE_DIRECTORY,
                    tag=_CLOUD_TAG,
                    identity_byte=b"c",
                )
            ],
            20: [_entry("hydrated.bin")],
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _CLOUD_TAG},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert api.component_calls == [(10, "cloud")]
    assert api.reparse_calls == []
    assert api.enumeration_calls == [10, 20]


def test_registered_store_is_the_non_popable_enumeration_boundary() -> None:
    escaping = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"..\sibling",
        relative=True,
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={
            (10, "grant"): 20,
            (20, "store"): 30,
            (30, "escape"): 40,
            (20, "sibling"): 50,
        },
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            30: r"C:\grant\store",
            50: r"C:\grant\sibling",
        },
        identities={30: (1, b"s" * 16)},
        listings={
            30: [
                _entry(
                    "escape",
                    attributes=_REPARSE_DIRECTORY,
                    tag=_SYMLINK_TAG,
                )
            ]
        },
        attributes={40: _REPARSE_DIRECTORY},
        tags={40: _SYMLINK_TAG},
        reparses={40: escaping},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _registered_enumerate_request(
                r"C:\grant\store",
                r"C:\grant",
            ),
            api=api,
            output=output,
        )
        == helper.DENIED_EXIT
    )
    assert output.getvalue() == b""
    assert (20, "sibling") not in api.component_calls


def test_registered_store_rebind_enumerates_the_retained_original_handle() -> None:
    class RebindingStoreApi(ScriptedEnumerationApi):
        def open_enumerable_component(
            self,
            parent: int,
            component: str,
        ) -> int:
            handle = super().open_enumerable_component(parent, component)
            if (parent, component) == (20, "store"):
                assert handle == 30
                self.children[(parent, component)] = 31
            return handle

    api = RebindingStoreApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20, (20, "store"): 30},
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            30: r"C:\grant\retained-store",
            31: r"C:\grant\replacement-store",
        },
        identities={30: (1, b"s" * 16), 31: (1, b"x" * 16)},
        listings={
            30: [_entry("original.bin")],
            31: [_entry("replacement.bin")],
        },
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _registered_enumerate_request(
                r"C:\grant\store",
                r"C:\grant",
            ),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["candidates"] == [
        {"root_index": 0, "locator": ["original.bin"]}
    ]
    assert api.enumeration_calls == [30]
    assert 31 not in api.identity_calls


def test_directory_cycle_attempt_consumes_the_exact_directory_budget() -> None:
    self_link = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=".",
        relative=True,
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={(10, "self"): 20},
        final_paths={10: "C:\\"},
        identities={10: (1, b"r" * 16)},
        listings={
            10: [
                _entry(
                    "self",
                    attributes=_REPARSE_DIRECTORY,
                    tag=_SYMLINK_TAG,
                )
            ]
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _SYMLINK_TAG},
        reparses={20: self_link},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=2),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["status"] == "complete"
    assert _enumeration_payload(output)["directories"] == 2
    assert api.enumeration_calls == [10]


def test_many_cycle_alias_attempts_cannot_evade_the_directory_ceiling() -> None:
    self_link = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=".",
        relative=True,
    )
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={
            (10, "self-one"): 20,
            (10, "self-two"): 21,
            (10, "self-three"): 22,
        },
        final_paths={10: "C:\\"},
        identities={10: (1, b"r" * 16)},
        listings={
            10: [
                _entry(
                    name,
                    attributes=_REPARSE_DIRECTORY,
                    tag=_SYMLINK_TAG,
                )
                for name in ("self-one", "self-two", "self-three")
            ]
        },
        attributes={
            20: _REPARSE_DIRECTORY,
            21: _REPARSE_DIRECTORY,
            22: _REPARSE_DIRECTORY,
        },
        tags={20: _SYMLINK_TAG, 21: _SYMLINK_TAG, 22: _SYMLINK_TAG},
        reparses={20: self_link, 21: self_link, 22: self_link},
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",), max_directories=3),
            api=api,
            output=output,
        )
        == helper.COMPLETE_EXIT
    )
    assert _enumeration_payload(output)["status"] == "limit"
    assert _enumeration_payload(output)["directories"] == 3
    assert api.enumeration_calls == [10]
    assert set(api.close_calls) == {10, 20, 21}
    assert 22 not in api.identity_calls


@pytest.mark.parametrize(
    ("iterator_failure", "iterator_close_failure", "close_failure"),
    (
        (KeyboardInterrupt("private next"), None, None),
        (None, KeyboardInterrupt("private iterator close"), None),
        (None, None, KeyboardInterrupt("private handle close")),
    ),
)
def test_enumeration_cleanup_or_baseexception_emits_zero_stdout(
    iterator_failure: BaseException | None,
    iterator_close_failure: BaseException | None,
    close_failure: BaseException | None,
) -> None:
    api = ScriptedEnumerationApi(
        roots={"C:\\": 10},
        children={},
        final_paths={10: "C:\\"},
        identities={10: (1, b"r" * 16)},
        listings={10: [_entry("would-have-been.bin")]},
        iterator_failures=(
            {} if iterator_failure is None else {10: iterator_failure}
        ),
        iterator_close_failures=(
            {}
            if iterator_close_failure is None
            else {10: iterator_close_failure}
        ),
        close_failures=(
            {} if close_failure is None else {10: close_failure}
        ),
    )
    output = io.BytesIO()

    assert (
        helper.execute_request(
            _enumerate_request(("C:\\",)),
            api=api,
            output=output,
        )
        == helper.FAILED_EXIT
    )
    assert output.getvalue() == b""
    assert api.close_calls[-1] == 10
    assert set(api.close_calls) == {
        10,
        *api.candidate_children.values(),
    }
    assert all(iterator.closed for iterator in api.iterators)


def test_direct_read_preserves_opaque_bytes_at_exact_reservation_cap() -> None:
    data = b"\x00line one\r\nline two\x1a\xff"
    api = _direct_file_api(data)
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", len(data)),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_COMPLETE_EXIT
    assert output.getvalue() == data
    assert api.file_component_calls == [(20, "artifact.bin")]
    assert api.read_calls == [(_FILE_HANDLE, len(data)), (_FILE_HANDLE, 1)]
    assert api.snapshot_calls == [_FILE_HANDLE, _FILE_HANDLE]
    assert api.close_calls == [10, _FILE_HANDLE, 20]


def test_empty_file_uses_only_one_byte_eof_proof() -> None:
    api = _direct_file_api(b"")
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", 1),
        api=api,
        output=output,
    )

    assert result == helper.COMPLETE_EXIT
    assert output.getvalue() == b""
    assert api.read_calls == [(_FILE_HANDLE, 1)]


def test_large_file_is_streamed_in_fixed_bounded_readfile_chunks() -> None:
    data = b"a" * (64 * 1024) + b"\x00\r\n\x1a\xff"
    api = _direct_file_api(data)
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", len(data)),
        api=api,
        output=output,
    )

    assert result == helper.COMPLETE_EXIT
    assert output.getvalue() == data
    assert api.read_calls == [
        (_FILE_HANDLE, 64 * 1024),
        (_FILE_HANDLE, 5),
        (_FILE_HANDLE, 1),
    ]


def test_registered_nested_read_retains_store_as_its_scope_boundary() -> None:
    data = b"registered nested bytes"
    stable = _snapshot(len(data))
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={
            (10, "grant"): 20,
            (20, "store"): 30,
            (30, "nested"): 40,
        },
        file_children={(40, "artifact.bin"): _FILE_HANDLE},
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            30: r"C:\grant\store",
            40: r"C:\grant\store\nested",
        },
        file_data={_FILE_HANDLE: data},
        snapshots={_FILE_HANDLE: (stable, stable)},
        attributes={_FILE_HANDLE: 0x80},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _registered_read_request(
            r"C:\grant\store",
            ("nested", "artifact.bin"),
            r"C:\grant",
            len(data),
        ),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_COMPLETE_EXIT
    assert output.getvalue() == data
    assert api.component_calls == [
        (10, "grant"),
        (20, "store"),
        (30, "nested"),
    ]
    assert api.file_component_calls == [(40, "artifact.bin")]
    assert api.close_calls == [10, 20, _FILE_HANDLE, 40, 30]


def test_file_larger_than_reservation_fails_before_read_or_output() -> None:
    data = b"12345"
    api = _direct_file_api(data)
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", len(data) - 1),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert output.getvalue() == b""
    assert api.read_calls == []
    assert api.snapshot_calls == [_FILE_HANDLE]


def test_misleading_smaller_size_emits_only_bounded_eof_proof_then_fails() -> None:
    api = _direct_file_api(b"abc", announced_size=2)
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", 2),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert output.getvalue() == b"abc"
    assert api.read_calls == [(_FILE_HANDLE, 2), (_FILE_HANDLE, 1)]
    assert api.snapshot_calls == [_FILE_HANDLE]


def test_short_read_against_announced_size_fails_without_unbounded_retry() -> None:
    api = _direct_file_api(b"abc", announced_size=4)
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", 4),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert output.getvalue() == b""
    assert api.read_calls == [(_FILE_HANDLE, 4)]
    assert api.snapshot_calls == [_FILE_HANDLE]


@pytest.mark.parametrize(
    "after",
    (
        _snapshot(3, volume=8),
        _snapshot(3, file_id=b"fedcba9876543210"),
        _snapshot(4),
        _snapshot(3, last_write=13),
        _snapshot(3, change=14),
        _snapshot(3, file_type=2),
        _snapshot(3, attributes=0x400, tag=_CLOUD_TAG),
    ),
)
def test_same_handle_identity_size_timestamp_and_type_drift_fails(
    after: helper._RegularFileSnapshot,
) -> None:
    before = _snapshot(3)
    api = _direct_file_api(b"abc", snapshots=(before, after))
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", 3),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert output.getvalue() == b"abc"
    assert api.snapshot_calls == [_FILE_HANDLE, _FILE_HANDLE]


def test_non_name_surrogate_cloud_file_is_an_eligible_regular_object() -> None:
    data = b"hydrated cloud bytes"
    cloud = _snapshot(
        len(data),
        attributes=0x400,
        tag=_CLOUD_TAG,
    )
    api = _direct_file_api(data, snapshots=(cloud, cloud))
    api.attributes[_FILE_HANDLE] = 0x400
    api.tags[_FILE_HANDLE] = _CLOUD_TAG
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", len(data)),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_COMPLETE_EXIT
    assert output.getvalue() == data


@pytest.mark.parametrize(
    ("attributes", "tag"),
    (
        (_DIRECTORY, 0),
        (0x400, 0x8000001B),
    ),
)
def test_nonregular_or_ineligible_final_object_is_denied(
    attributes: int,
    tag: int,
) -> None:
    api = _direct_file_api(b"x")
    api.attributes[_FILE_HANDLE] = attributes
    api.tags[_FILE_HANDLE] = tag
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\artifact.bin", r"C:\grant", 1),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert output.getvalue() == b""
    assert api.snapshot_calls == []


def test_safe_final_symlink_is_explicitly_resolved_inside_direct_grant() -> None:
    data = b"safe target"
    stable = _snapshot(len(data))
    link = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"\??\C:\grant\target.bin",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20},
        file_children={
            (20, "link.bin"): 30,
            (20, "target.bin"): _FILE_HANDLE,
        },
        final_paths={10: "C:\\", 20: r"C:\grant"},
        file_data={_FILE_HANDLE: data},
        snapshots={_FILE_HANDLE: (stable, stable)},
        attributes={30: 0x400, _FILE_HANDLE: 0x80},
        tags={30: _SYMLINK_TAG},
        reparses={30: link},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\link.bin", r"C:\grant", len(data)),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_COMPLETE_EXIT
    assert output.getvalue() == data
    assert api.file_component_calls == [
        (20, "link.bin"),
        (20, "target.bin"),
    ]
    assert api.reparse_calls == [30]


def test_safe_intermediate_junction_is_resolved_before_final_file_open() -> None:
    data = b"junction target"
    stable = _snapshot(len(data))
    junction = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\grant\target",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={
            (10, "grant"): 20,
            (20, "link"): 30,
            (20, "target"): 40,
        },
        file_children={(40, "artifact.bin"): _FILE_HANDLE},
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            40: r"C:\grant\target",
        },
        file_data={_FILE_HANDLE: data},
        snapshots={_FILE_HANDLE: (stable, stable)},
        attributes={30: _REPARSE_DIRECTORY, _FILE_HANDLE: 0x80},
        tags={30: _MOUNT_TAG},
        reparses={30: junction},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(
            r"C:\grant\link\artifact.bin",
            r"C:\grant",
            len(data),
        ),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_COMPLETE_EXIT
    assert output.getvalue() == data
    assert api.component_calls == [
        (10, "grant"),
        (20, "link"),
        (20, "target"),
    ]
    assert api.file_component_calls == [(40, "artifact.bin")]


def test_escaping_final_symlink_is_denied_before_target_side_open() -> None:
    escaping = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"\??\C:\outside\secret.bin",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20},
        file_children={(20, "link.bin"): 30},
        final_paths={10: "C:\\", 20: r"C:\grant"},
        attributes={30: 0x400},
        tags={30: _SYMLINK_TAG},
        reparses={30: escaping},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\link.bin", r"C:\grant", 16),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert output.getvalue() == b""
    assert api.file_component_calls == [(20, "link.bin")]
    assert all(component != "outside" for _, component in api.component_calls)


def test_escaping_intermediate_junction_is_denied_before_outside_file_open() -> None:
    escaping = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\outside",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20, (20, "link"): 30},
        file_children={},
        final_paths={10: "C:\\", 20: r"C:\grant"},
        attributes={30: _REPARSE_DIRECTORY},
        tags={30: _MOUNT_TAG},
        reparses={30: escaping},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(
            r"C:\grant\link\secret.bin",
            r"C:\grant",
            16,
        ),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert output.getvalue() == b""
    assert api.component_calls == [(10, "grant"), (20, "link")]
    assert api.file_component_calls == []


def test_registered_locator_cannot_follow_alias_to_operator_grant_sibling() -> None:
    escaping = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"\??\C:\grant\sibling\secret.bin",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20, (20, "store"): 30},
        file_children={(30, "link.bin"): 40},
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            30: r"C:\grant\store",
        },
        attributes={40: 0x400},
        tags={40: _SYMLINK_TAG},
        reparses={40: escaping},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _registered_read_request(
            r"C:\grant\store",
            ("link.bin",),
            r"C:\grant",
            16,
        ),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert output.getvalue() == b""
    assert api.file_component_calls == [(30, "link.bin")]
    assert all(component != "sibling" for _, component in api.component_calls)


def test_replaced_registered_scope_uses_retained_original_store_handle() -> None:
    data = b"original store bytes"
    stable = _snapshot(len(data))

    class ReplacedStoreApi(ScriptedDirectoryApi):
        def open_component(self, parent: int, component: str) -> int:
            handle = super().open_component(parent, component)
            if (parent, component) == (20, "store"):
                assert handle == 30
                self.children[(20, "store")] = 31
            return handle

    api = ReplacedStoreApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20, (20, "store"): 30},
        file_children={
            (30, "artifact.bin"): _FILE_HANDLE,
            (31, "artifact.bin"): 99,
        },
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            30: r"C:\grant\store",
        },
        file_data={_FILE_HANDLE: data, 99: b"outside replacement"},
        snapshots={_FILE_HANDLE: (stable, stable)},
        attributes={_FILE_HANDLE: 0x80, 99: 0x80},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _registered_read_request(
            r"C:\grant\store",
            ("artifact.bin",),
            r"C:\grant",
            len(data),
        ),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_COMPLETE_EXIT
    assert output.getvalue() == data
    assert api.children[(20, "store")] == 31
    assert api.file_component_calls == [(30, "artifact.bin")]
    assert 99 not in api.snapshot_calls


def test_missing_final_file_has_distinct_fixed_status_and_no_output() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20},
        final_paths={10: "C:\\", 20: r"C:\grant"},
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _read_request(r"C:\grant\missing.bin", r"C:\grant", 16),
        api=api,
        output=output,
    )

    assert result == helper.LOCAL_FILESYSTEM_MISSING_EXIT
    assert output.getvalue() == b""


def test_missing_registered_intermediate_has_distinct_status_and_no_output() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "grant"): 20, (20, "store"): 30},
        final_paths={
            10: "C:\\",
            20: r"C:\grant",
            30: r"C:\grant\store",
        },
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _registered_read_request(
            r"C:\grant\store",
            ("missing", "artifact.bin"),
            r"C:\grant",
            16,
        ),
        api=api,
        output=output,
    )

    assert result == helper.MISSING_EXIT
    assert output.getvalue() == b""
    assert api.file_component_calls == []


@pytest.mark.parametrize(
    "raw",
    (
        json.dumps(
            {
                "v": 1,
                "op": "read-file",
                "candidate": r"C:\grant\a",
                "roots": [r"C:\grant", r"C:\other"],
                "max_bytes": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "v": 1,
                "op": "read-file",
                "candidate": r"C:\grant\a",
                "roots": [r"C:\grant"],
                "max_bytes": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "v": 1,
                "op": "read-file",
                "candidate": r"C:\grant\a",
                "roots": [r"C:\grant"],
                "max_bytes": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "v": 1,
                "op": "read-file",
                "candidate": r"C:\grant\a",
                "roots": [r"C:\grant"],
                "max_bytes": 512 * 1024 * 1024 + 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        json.dumps(
            {
                "v": 1,
                "op": "read-registered-file",
                "store_root": r"C:\grant\store",
                "locator": ["..", "escape"],
                "roots": [r"C:\grant"],
                "max_bytes": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    ),
)
def test_malformed_read_protocol_fails_before_native_io(raw: str) -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})

    result = helper.execute_request(raw, api=api, output=io.BytesIO())

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert api.root_calls == []


@pytest.mark.parametrize(
    "raw",
    (
        _read_request(r"C:\sibling\artifact.bin", r"C:\grant", 1),
        _registered_read_request(
            r"C:\sibling\store",
            ("artifact.bin",),
            r"C:\grant",
            1,
        ),
        _read_request(r"C:\grant\..\outside.bin", r"C:\grant", 1),
    ),
)
def test_lexically_denied_read_scope_performs_zero_native_io(raw: str) -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})

    result = helper.execute_request(raw, api=api, output=io.BytesIO())

    assert result == helper.DENIED_EXIT
    assert api.root_calls == []
    assert api.component_calls == []
    assert api.file_component_calls == []


def test_plain_handle_walk_is_accessible_and_cleanup_is_reverse_owned_order() -> None:
    api = _plain_api()

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\store", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.root_calls == ["C:\\"]
    assert api.component_calls == [(10, "allowed"), (20, "store")]
    assert api.final_path_calls == [10, 20, 30]
    assert api.reparse_calls == []
    assert api.close_calls == [10, 30, 20]


@pytest.mark.parametrize(
    "payload",
    (
        None,
        "",
        "{",
        "[]",
        json.dumps({"v": 1}),
        json.dumps(
            {
                "v": True,
                "op": "inspect-directory",
                "candidate": r"C:\store",
                "roots": ["C:\\"],
            }
        ),
        json.dumps(
            {
                "v": 2,
                "op": "inspect-directory",
                "candidate": r"C:\store",
                "roots": ["C:\\"],
            }
        ),
        json.dumps(
            {
                "v": 1,
                "op": "read-file",
                "candidate": r"C:\store",
                "roots": ["C:\\"],
            }
        ),
        json.dumps(
            {
                "v": 1,
                "op": "inspect-directory",
                "candidate": r"C:\store",
                "roots": [],
            }
        ),
    ),
)
def test_malformed_protocol_fails_without_native_io(payload: str | None) -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})
    assert helper.inspect_directory_request(payload, api=api) == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert api.root_calls == []


def test_bounded_protocol_rejects_too_many_roots_and_oversize_bytes() -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})
    too_many = _request(r"C:\store", tuple("C:\\" for _ in range(65)))
    oversize = " " * (24 * 1024 + 1)

    assert (
        helper.inspect_directory_request(too_many, api=api) == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    )
    assert (
        helper.inspect_directory_request(oversize, api=api) == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    )
    assert api.root_calls == []


def test_protocol_rejects_semantically_equivalent_noncanonical_json() -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})
    canonical = _request(r"C:\données", ("C:\\",))
    noncanonical = json.dumps(
        {
            "v": 1,
            "op": "inspect-directory",
            "candidate": r"C:\données",
            "roots": ["C:\\"],
        },
        ensure_ascii=False,
    )

    assert canonical != noncanonical
    assert (
        helper.inspect_directory_request(noncanonical, api=api)
        == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    )
    assert api.root_calls == []


@pytest.mark.parametrize(
    ("candidate", "roots"),
    (
        (r"C:\sibling\store", (r"C:\allowed",)),
        (r"C:\allowed-other\store", (r"C:\allowed",)),
        (r"C:\ALLOWED\store", (r"C:\allowed",)),
        (r"\\server\share\store", (r"C:\allowed",)),
        (r"\\?\C:\allowed\store", (r"C:\allowed",)),
        (r"C:\allowed\..\outside", (r"C:\allowed",)),
        (r"C:/allowed/store", (r"C:\allowed",)),
        (r"C:\allowed\NUL.txt", (r"C:\allowed",)),
    ),
)
def test_lexical_denials_perform_zero_native_io(
    candidate: str,
    roots: Sequence[str],
) -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})
    assert (
        helper.inspect_directory_request(_request(candidate, roots), api=api)
        == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    )
    assert api.root_calls == []
    assert api.component_calls == []


def test_most_specific_root_prevents_fallback_to_broader_grant() -> None:
    junction = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\allowed\sibling",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "allowed"): 20, (20, "specific"): 30, (30, "link"): 40},
        final_paths={10: "C:\\", 20: r"C:\allowed", 30: r"C:\allowed\specific"},
        attributes={40: _REPARSE_DIRECTORY},
        tags={40: _MOUNT_TAG},
        reparses={40: junction},
    )

    result = helper.inspect_directory_request(
        _request(
            r"C:\allowed\specific\link",
            (r"C:\allowed", r"C:\allowed\specific"),
        ),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert api.component_calls == [
        (10, "allowed"),
        (20, "specific"),
        (30, "link"),
    ]
    assert all(component != "sibling" for _, component in api.component_calls)


def test_operator_root_alias_establishes_resolved_grant_spelling() -> None:
    root_alias = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\D:\actual",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10, "D:\\": 50},
        children={
            (10, "alias"): 20,
            (50, "actual"): 60,
            (60, "store"): 70,
        },
        final_paths={
            10: "C:\\",
            50: "D:\\",
            60: r"D:\actual",
            70: r"D:\actual\store",
        },
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _MOUNT_TAG},
        reparses={20: root_alias},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\alias\store", (r"C:\alias",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.root_calls == ["C:\\", "D:\\"]
    assert api.component_calls == [
        (10, "alias"),
        (50, "actual"),
        (60, "store"),
    ]
    assert 20 not in api.final_path_calls
    assert api.reparse_calls == [20]


@pytest.mark.parametrize(
    "target",
    (r"\??\C:\allowed\root\actual", r"\??\D:\resolved\actual"),
)
def test_candidate_absolute_alias_accepts_either_equivalent_root_spelling(
    target: str,
) -> None:
    root_alias = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\D:\resolved",
    )
    candidate_alias = _reparse_buffer(tag=_MOUNT_TAG, substitute=target)
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10, "D:\\": 50},
        children={
            (10, "allowed"): 11,
            (11, "root"): 12,
            (50, "resolved"): 60,
            (60, "link"): 70,
            (60, "actual"): 80,
        },
        final_paths={
            10: "C:\\",
            11: r"C:\allowed",
            50: "D:\\",
            60: r"D:\resolved",
            80: r"D:\resolved\actual",
        },
        attributes={12: _REPARSE_DIRECTORY, 70: _REPARSE_DIRECTORY},
        tags={12: _MOUNT_TAG, 70: _MOUNT_TAG},
        reparses={12: root_alias, 70: candidate_alias},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\root\link", (r"C:\allowed\root",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.component_calls[-1] == (60, "actual")


def test_escaping_junction_is_denied_before_outside_target_open() -> None:
    escaping = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\outside\remote",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "allowed"): 20, (20, "link"): 30},
        final_paths={10: "C:\\", 20: r"C:\allowed"},
        attributes={30: _REPARSE_DIRECTORY},
        tags={30: _MOUNT_TAG},
        reparses={30: escaping},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\link", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert api.component_calls == [(10, "allowed"), (20, "link")]
    assert 30 not in api.final_path_calls


def test_relative_symlink_rewrites_link_then_dotdot_with_native_semantics() -> None:
    relative = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"..\b",
        relative=True,
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={
            (10, "allowed"): 20,
            (20, "a"): 30,
            (30, "link"): 40,
            (20, "b"): 50,
            (50, "child"): 60,
        },
        final_paths={
            10: "C:\\",
            20: r"C:\allowed",
            30: r"C:\allowed\a",
            50: r"C:\allowed\b",
            60: r"C:\allowed\b\child",
        },
        attributes={40: _REPARSE_DIRECTORY},
        tags={40: _SYMLINK_TAG},
        reparses={40: relative},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\a\link\child", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.component_calls == [
        (10, "allowed"),
        (20, "a"),
        (30, "link"),
        (20, "b"),
        (50, "child"),
    ]


def test_candidate_link_then_dotdot_is_not_pre_normalized() -> None:
    link = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\allowed\a\b",
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={
            (10, "allowed"): 20,
            (20, "link"): 30,
            (20, "a"): 40,
            (40, "b"): 50,
            (40, "store"): 60,
        },
        final_paths={
            10: "C:\\",
            20: r"C:\allowed",
            40: r"C:\allowed\a",
            50: r"C:\allowed\a\b",
            60: r"C:\allowed\a\store",
        },
        attributes={30: _REPARSE_DIRECTORY},
        tags={30: _MOUNT_TAG},
        reparses={30: link},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\link\..\store", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.component_calls == [
        (10, "allowed"),
        (20, "link"),
        (20, "a"),
        (40, "b"),
        (40, "store"),
    ]


def test_leading_candidate_dotdot_is_denied_before_native_io() -> None:
    api = ScriptedDirectoryApi(roots={}, children={}, final_paths={})

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\..\outside", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert api.root_calls == []


def test_relative_symlink_cannot_pop_above_retained_root() -> None:
    escaping = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"..\outside",
        relative=True,
    )
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "allowed"): 20, (20, "link"): 30},
        final_paths={10: "C:\\", 20: r"C:\allowed"},
        attributes={30: _REPARSE_DIRECTORY},
        tags={30: _SYMLINK_TAG},
        reparses={30: escaping},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\link", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    assert all(component != "outside" for _, component in api.component_calls)


@pytest.mark.parametrize(
    "substitute",
    (
        r"\??\Volume{01234567-89ab-cdef-0123-456789abcdef}\\",
        r"\??\UNC\server\share",
        r"\Device\HarddiskVolume1\outside",
        r"\\?\C:\allowed",
    ),
)
def test_mount_and_device_namespaces_fail_closed(substitute: str) -> None:
    raw = _reparse_buffer(tag=_MOUNT_TAG, substitute=substitute)
    with pytest.raises(helper.WindowsLocalStoreHealthDenied):
        helper._parse_reparse_target(raw, _MOUNT_TAG)


def test_cloud_directory_placeholder_remains_eligible_without_payload_read() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "OneDrive"): 20, (20, "store"): 30},
        final_paths={10: "C:\\", 20: r"C:\OneDrive", 30: r"C:\OneDrive\store"},
        attributes={20: _REPARSE_DIRECTORY},
        tags={20: _CLOUD_TAG},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\OneDrive\store", (r"C:\OneDrive",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.reparse_calls == []


@pytest.mark.parametrize(
    ("attributes", "tag"),
    (
        (0x80, 0),
        (_REPARSE_DIRECTORY, 0),
        (_REPARSE_DIRECTORY, 0xA000001F),
        (0x400, _CLOUD_TAG),
    ),
)
def test_non_directory_or_unclassifiable_reparse_fails_closed(
    attributes: int,
    tag: int,
) -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "allowed"): 20},
        final_paths={10: "C:\\"},
        attributes={20: attributes},
        tags={20: tag},
    )
    assert (
        helper.inspect_directory_request(
            _request(r"C:\allowed", ("C:\\",)),
            api=api,
        )
        == helper.LOCAL_FILESYSTEM_DENIED_EXIT
    )


def test_raw_parser_checks_exact_header_lengths_offsets_and_utf16() -> None:
    valid = _reparse_buffer(
        tag=_SYMLINK_TAG,
        substitute=r"..\target",
        relative=True,
    )
    malformed = [
        valid + b"\0\0",
        valid[:4] + (1).to_bytes(2, "little") + valid[6:],
        valid[:8] + (1).to_bytes(2, "little") + valid[10:],
        valid[:10] + (0xFFFF).to_bytes(2, "little") + valid[12:],
        valid[:-1],
        valid[:20] + b"\x00\xd8" + valid[22:],
    ]
    for raw in malformed:
        with pytest.raises(helper.WindowsLocalStoreHealthDenied):
            helper._parse_reparse_target(raw, _SYMLINK_TAG)
    with pytest.raises(helper.WindowsLocalStoreHealthDenied):
        helper._parse_reparse_target(valid, _MOUNT_TAG)


def test_cleanup_failure_changes_success_to_internal_failure_and_attempts_all() -> None:
    api = _plain_api()
    api.close_failures = {30: KeyboardInterrupt("private close detail")}

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\store", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert set(api.close_calls) == {10, 20, 30}


def test_validation_baseexception_is_internal_and_owned_handle_is_closed() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={},
        final_paths={10: "C:\\"},
        attribute_failure=KeyboardInterrupt("private detail"),
    )
    assert (
        helper.inspect_directory_request(
            _request("C:\\", ("C:\\",)),
            api=api,
        )
        == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    )
    assert api.close_calls == [10]


def test_duplicate_live_handle_is_rejected_without_closing_retained_parent() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={(10, "allowed"): 10},
        final_paths={10: "C:\\"},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed", ("C:\\",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_FAILED_EXIT
    assert api.attribute_calls == [10]
    assert api.close_calls == [10]


def test_ownership_bookkeeping_baseexception_closes_new_handle_opaquely() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={},
        final_paths={10: "C:\\"},
    )

    class FailingOwnedHandles(list[int]):
        def append(self, _handle: int, /) -> None:
            raise KeyboardInterrupt("private bookkeeping detail")

    resolver = helper._HandleResolver(
        api,
        owned_handles=FailingOwnedHandles(),
    )

    with pytest.raises(
        helper.WindowsLocalStoreHealthError,
        match=r"^Windows local filesystem verification failed\.$",
    ):
        resolver.inspect(helper._parse_request(_request("C:\\", ("C:\\",))))

    assert api.close_calls == [10]
    assert resolver.close_all()


def test_ownership_bookkeeping_records_failed_best_effort_cleanup() -> None:
    api = ScriptedDirectoryApi(
        roots={"C:\\": 10},
        children={},
        final_paths={10: "C:\\"},
        close_failures={10: KeyboardInterrupt("private close detail")},
    )

    class FailingOwnedHandles(list[int]):
        def append(self, _handle: int, /) -> None:
            raise KeyboardInterrupt("private bookkeeping detail")

    resolver = helper._HandleResolver(
        api,
        owned_handles=FailingOwnedHandles(),
    )

    with pytest.raises(helper.WindowsLocalStoreHealthError):
        resolver.inspect(helper._parse_request(_request("C:\\", ("C:\\",))))

    assert api.close_calls == [10]
    assert not resolver.close_all()


def test_reparse_path_rebind_after_open_uses_retained_exact_handle() -> None:
    original = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\allowed\target",
    )
    rebound = _reparse_buffer(
        tag=_MOUNT_TAG,
        substitute=r"\??\C:\outside",
    )

    class RebindingDirectoryApi(ScriptedDirectoryApi):
        def open_component(self, parent: int, component: str) -> int:
            handle = super().open_component(parent, component)
            if (parent, component) == (20, "link"):
                assert handle == 30
                # Simulate an attacker replacing the path entry immediately
                # after the exact no-follow handle has been returned.
                self.children[(parent, component)] = 31
            return handle

    api = RebindingDirectoryApi(
        roots={"C:\\": 10},
        children={
            (10, "allowed"): 20,
            (20, "link"): 30,
            (20, "target"): 40,
            (40, "nested"): 50,
        },
        final_paths={
            10: "C:\\",
            20: r"C:\allowed",
            40: r"C:\allowed\target",
            50: r"C:\allowed\target\nested",
        },
        attributes={30: _REPARSE_DIRECTORY, 31: _REPARSE_DIRECTORY},
        tags={30: _MOUNT_TAG, 31: _MOUNT_TAG},
        reparses={30: original, 31: rebound},
    )

    result = helper.inspect_directory_request(
        _request(r"C:\allowed\link\nested", (r"C:\allowed",)),
        api=api,
    )

    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.children[(20, "link")] == 31
    assert api.reparse_calls == [30]
    assert 31 not in api.attribute_calls
    assert 31 not in api.close_calls
    assert all(component != "outside" for _, component in api.component_calls)


def test_main_is_output_free_for_accessible_denied_and_malformed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy = _plain_api()
    environment = {
        helper.LOCAL_FILESYSTEM_REQUEST_ENV: _request(
            r"C:\allowed\store",
            (r"C:\allowed",),
        )
    }

    assert helper.main(("helper",), environment, api=healthy) == 0
    assert (
        helper.main(
            ("helper",),
            {helper.LOCAL_FILESYSTEM_REQUEST_ENV: _request(r"C:\other", (r"C:\allowed",))},
            api=healthy,
        )
        == 2
    )
    assert helper.main(("helper",), {}, api=healthy) == 3
    assert helper.main(("helper", "extra"), environment, api=healthy) == 3
    assert capsys.readouterr() == ("", "")


def _run_real_helper(
    candidate: Path,
    roots: Sequence[Path],
    *extra_args: str,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        helper.LOCAL_FILESYSTEM_REQUEST_ENV: _request(
            str(candidate),
            tuple(str(root) for root in roots),
        ),
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and helper path
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(Path(helper.__file__).resolve()),
            *extra_args,
        ],
        check=False,
        capture_output=True,
        env=environment,
    )


def _run_real_raw_request(raw: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        helper.LOCAL_FILESYSTEM_REQUEST_ENV: raw,
    }
    return subprocess.run(  # noqa: S603 - fixed interpreter and helper path
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(Path(helper.__file__).resolve()),
        ],
        check=False,
        capture_output=True,
        env=environment,
    )


def _create_windows_junction(junction: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )


class _PostOpenJunctionRebindApi:
    def __init__(
        self,
        *,
        raced_component: str,
        original: Path,
        moved: Path,
        outside: Path,
    ) -> None:
        self.delegate = helper.CtypesWindowsDirectoryApi()
        self.raced_component = raced_component
        self.original = original
        self.moved = moved
        self.outside = outside
        self.swapped = False
        self.normalized_paths_after_swap: list[str] = []

    def open_root(self, root: str) -> int:
        return self.delegate.open_root(root)

    def open_enumerable_root(self, root: str) -> int:
        return self.delegate.open_enumerable_root(root)

    def open_component(self, parent: int, component: str) -> int:
        handle = self.delegate.open_component(parent, component)
        if component == self.raced_component and not self.swapped:
            self.original.rename(self.moved)
            _create_windows_junction(self.original, self.outside)
            self.swapped = True
        return handle

    def open_enumerable_component(self, parent: int, component: str) -> int:
        handle = self.delegate.open_enumerable_component(parent, component)
        if component == self.raced_component and not self.swapped:
            self.original.rename(self.moved)
            _create_windows_junction(self.original, self.outside)
            self.swapped = True
        return handle

    def open_candidate_component(self, parent: int, component: str) -> int:
        return self.delegate.open_candidate_component(parent, component)

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        return self.delegate.file_attributes_and_reparse_tag(handle)

    def read_reparse_point(self, handle: int) -> bytes:
        return self.delegate.read_reparse_point(handle)

    def normalized_dos_path(self, handle: int) -> str:
        path = self.delegate.normalized_dos_path(handle)
        if self.swapped:
            self.normalized_paths_after_swap.append(path)
        return path

    def directory_identity(self, handle: int) -> tuple[int, bytes]:
        return self.delegate.directory_identity(handle)

    def candidate_file_identity(
        self,
        handle: int,
    ) -> tuple[int, bytes] | None:
        return self.delegate.candidate_file_identity(handle)

    def enumerate_directory(
        self,
        handle: int,
    ) -> Iterator[helper._DirectoryEntry]:
        return self.delegate.enumerate_directory(handle)

    def close(self, handle: int) -> None:
        self.delegate.close(handle)


class _PostValidationParentRebindApi:
    def __init__(
        self,
        *,
        original_parent: Path,
        moved_parent: Path,
        outside_parent: Path,
    ) -> None:
        self.delegate = helper.CtypesWindowsDirectoryApi()
        self.original_parent = original_parent
        self.moved_parent = moved_parent
        self.outside_parent = outside_parent
        self.expected_parent_path = os.path.normcase(os.path.realpath(original_parent))
        self.swapped = False
        self.normalized_paths_after_swap: list[str] = []

    def open_root(self, root: str) -> int:
        return self.delegate.open_root(root)

    def open_component(self, parent: int, component: str) -> int:
        return self.delegate.open_component(parent, component)

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        return self.delegate.file_attributes_and_reparse_tag(handle)

    def read_reparse_point(self, handle: int) -> bytes:
        return self.delegate.read_reparse_point(handle)

    def normalized_dos_path(self, handle: int) -> str:
        path = self.delegate.normalized_dos_path(handle)
        if not self.swapped and os.path.normcase(path) == self.expected_parent_path:
            self.original_parent.rename(self.moved_parent)
            _create_windows_junction(
                self.original_parent,
                self.outside_parent,
            )
            self.swapped = True
        elif self.swapped:
            self.normalized_paths_after_swap.append(path)
        return path

    def close(self, handle: int) -> None:
        self.delegate.close(handle)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")
def test_real_helper_accepts_long_unicode_plain_directory_without_output(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "données-🧪"
    for index in range(30):
        directory /= f"niveau-{index:02d}"
    directory.mkdir(parents=True)

    completed = _run_real_helper(directory, (tmp_path,))

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")
def test_real_helper_direct_read_preserves_opaque_binary_stdout(
    tmp_path: Path,
) -> None:
    data = b"\x00windows\r\nopaque\x1a\xff"
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(data)

    completed = _run_real_raw_request(_read_request(str(artifact), str(tmp_path), len(data)))

    assert completed.returncode == helper.COMPLETE_EXIT
    assert completed.stdout == data
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")
def test_real_helper_registered_nested_read_keeps_binary_stdout_clean(
    tmp_path: Path,
) -> None:
    data = b"\xffregistered\x00\r\n\x1a"
    store = tmp_path / "store"
    nested = store / "nested"
    nested.mkdir(parents=True)
    artifact = nested / "artifact.bin"
    artifact.write_bytes(data)

    completed = _run_real_raw_request(
        _registered_read_request(
            str(store),
            ("nested", "artifact.bin"),
            str(tmp_path),
            len(data),
        )
    )

    assert completed.returncode == helper.COMPLETE_EXIT
    assert completed.stdout == data
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")
def test_real_helper_enumerates_nested_files_with_canonical_json(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "fallback.bin").write_bytes(b"fallback")
    (nested / "artifact.dat").write_bytes(b"preferred")

    completed = _run_real_raw_request(
        _enumerate_request(
            (str(tmp_path),),
            target_name="artifact.dat",
        )
    )

    assert completed.returncode == helper.COMPLETE_EXIT
    assert completed.stderr == b""
    rendered = completed.stdout.decode("ascii")
    payload = cast(dict[str, object], json.loads(rendered))
    assert rendered == json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert payload["status"] == "complete"
    candidates = cast(list[dict[str, object]], payload["candidates"])
    assert candidates[0] == {
        "root_index": 0,
        "locator": ["nested", "artifact.dat"],
    }


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junctions")
def test_native_enumeration_junction_rebind_uses_retained_handle(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    target = allowed / "target"
    target.mkdir(parents=True)
    (target / "inside.bin").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside.bin").write_bytes(b"outside")
    junction = allowed / "junction-raced-after-open"
    moved = tmp_path / "original-enumeration-junction"
    _create_windows_junction(junction, target)
    api = _PostOpenJunctionRebindApi(
        raced_component=junction.name,
        original=junction,
        moved=moved,
        outside=outside,
    )
    output = io.BytesIO()

    result = helper.execute_request(
        _enumerate_request((str(allowed),)),
        api=api,
        output=output,
    )

    payload = _enumeration_payload(output)
    assert result == helper.COMPLETE_EXIT
    assert api.swapped
    assert payload["candidates"] in (
        [
            {
                "root_index": 0,
                "locator": [junction.name, "inside.bin"],
            }
        ],
        [
            {
                "root_index": 0,
                "locator": ["target", "inside.bin"],
            }
        ],
    )
    assert all(
        "outside.bin" not in cast(list[str], candidate["locator"])
        for candidate in cast(list[dict[str, object]], payload["candidates"])
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junctions")
def test_native_post_open_junction_rebind_uses_retained_handle(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    nested = allowed / "target" / "nested"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    junction = allowed / "junction-raced-after-open"
    moved = tmp_path / "original-junction"
    _create_windows_junction(junction, nested.parent)
    api = _PostOpenJunctionRebindApi(
        raced_component=junction.name,
        original=junction,
        moved=moved,
        outside=outside,
    )

    result = helper.inspect_directory_request(
        _request(str(junction / "nested"), (str(allowed),)),
        api=api,
    )

    outside_path = os.path.normcase(os.path.realpath(outside))
    nested_path = os.path.normcase(os.path.realpath(nested))
    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.swapped
    assert api.normalized_paths_after_swap
    assert nested_path in map(os.path.normcase, api.normalized_paths_after_swap)
    assert all(
        os.path.normcase(path) != outside_path
        and not os.path.normcase(path).startswith(outside_path + "\\")
        for path in api.normalized_paths_after_swap
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junctions")
def test_native_replaced_parent_keeps_child_lookup_on_retained_handle(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    parent = allowed / "parent"
    store = parent / "store"
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "store").mkdir(parents=True)
    moved = allowed / "retained-parent"
    api = _PostValidationParentRebindApi(
        original_parent=parent,
        moved_parent=moved,
        outside_parent=outside,
    )

    result = helper.inspect_directory_request(
        _request(str(store), (str(allowed),)),
        api=api,
    )

    retained_store = os.path.normcase(os.path.realpath(moved / "store"))
    outside_store = os.path.normcase(os.path.realpath(outside / "store"))
    normalized_after_swap = tuple(
        os.path.normcase(path) for path in api.normalized_paths_after_swap
    )
    assert result == helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT
    assert api.swapped
    assert retained_store in normalized_after_swap
    assert all(
        path != outside_store and not path.startswith(outside_store + "\\")
        for path in normalized_after_swap
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junctions")
def test_real_helper_accepts_in_root_junction_without_output(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    target = allowed / "target"
    nested = target / "nested"
    nested.mkdir(parents=True)
    junction = allowed / "junction"
    _create_windows_junction(junction, target)

    completed = _run_real_helper(junction / "nested", (allowed,))

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junctions")
def test_real_helper_rejects_escaping_junction_without_output(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    junction = allowed / "junction"
    _create_windows_junction(junction, outside)

    completed = _run_real_helper(junction / "nested", (allowed,))

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows interpreter")
def test_real_helper_protocol_failure_is_output_free(tmp_path: Path) -> None:
    completed = _run_real_helper(tmp_path, (tmp_path,), "unexpected")

    assert completed.returncode == 3
    assert completed.stdout == b""
    assert completed.stderr == b""
