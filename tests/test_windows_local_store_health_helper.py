from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
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
        result: int = 1,
    ) -> None:
        self.attributes = dict(attributes or {})
        self.tags = dict(tags or {})
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
        self.calls.append((handle, _as_int(raw_class), _as_int(raw_size)))
        if self.result:
            information = ctypes.cast(
                cast(Any, raw_information),
                ctypes.POINTER(helper._FILE_ATTRIBUTE_TAG_INFO),
            )
            information.contents.FileAttributes = self.attributes.get(handle, _DIRECTORY)
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
    )


def test_public_protocol_constants_are_fixed() -> None:
    assert helper.LOCAL_FILESYSTEM_REQUEST_ENV == "LAB_TRACKER_INTERNAL_LOCAL_FILESYSTEM_REQUEST"
    assert helper.LOCAL_FILESYSTEM_PROTOCOL_VERSION == 1
    assert helper.LOCAL_FILESYSTEM_REQUEST_VERSION == 1
    assert helper.LOCAL_FILESYSTEM_INSPECT_DIRECTORY_OP == "inspect-directory"
    assert helper.LOCAL_FILESYSTEM_READ_FILE_OP == "read-file"
    assert helper.LOCAL_FILESYSTEM_READ_REGISTERED_FILE_OP == "read-registered-file"
    assert helper.LOCAL_FILESYSTEM_COMPLETE_EXIT == 0
    assert helper.LOCAL_FILESYSTEM_ACCESSIBLE_EXIT == 0
    assert helper.LOCAL_FILESYSTEM_DENIED_EXIT == 2
    assert helper.LOCAL_FILESYSTEM_FAILED_EXIT == 3
    assert helper.LOCAL_FILESYSTEM_MISSING_EXIT == 4
    assert helper.INSPECT_DIRECTORY_OPERATION == "inspect-directory"
    assert helper.READ_FILE_OPERATION == "read-file"
    assert helper.READ_REGISTERED_FILE_OPERATION == "read-registered-file"
    assert helper.COMPLETE_EXIT == 0
    assert helper.ACCESSIBLE_EXIT == 0
    assert helper.DENIED_EXIT == 2
    assert helper.FAILED_EXIT == 3
    assert helper.MISSING_EXIT == 4
    assert helper.MAX_LOCAL_FILESYSTEM_REQUEST_BYTES == 24 * 1024
    assert helper.MAX_LOCAL_FILESYSTEM_ROOTS == 64
    assert helper.MAX_LOCAL_FILESYSTEM_SELECTED_ROOTS == 64
    assert helper.MAX_LOCAL_FILESYSTEM_READ_BYTES == 512 * 1024 * 1024


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

    def open_component(self, parent: int, component: str) -> int:
        handle = self.delegate.open_component(parent, component)
        if component == self.raced_component and not self.swapped:
            self.original.rename(self.moved)
            _create_windows_junction(self.original, self.outside)
            self.swapped = True
        return handle

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        return self.delegate.file_attributes_and_reparse_tag(handle)

    def read_reparse_point(self, handle: int) -> bytes:
        return self.delegate.read_reparse_point(handle)

    def normalized_dos_path(self, handle: int) -> str:
        path = self.delegate.normalized_dos_path(handle)
        if self.swapped:
            self.normalized_paths_after_swap.append(path)
        return path

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
