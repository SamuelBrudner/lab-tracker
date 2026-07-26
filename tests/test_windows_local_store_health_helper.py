from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

import lab_tracker._windows_local_store_health_helper as helper

_ROOT_HANDLE = 101
_CHILD_HANDLE = 202
_DIRECTORY_ATTRIBUTES = 0x10
_REPARSE_DIRECTORY_ATTRIBUTES = 0x410
_CLOUD_REPARSE_TAG = 0x9000001A
_MOUNT_POINT_REPARSE_TAG = 0xA0000003
_NON_DIRECTORY_REPARSE_TAG = 0x80000021
_STATIC_ERROR = "Windows local store health verification failed."


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


class FakeCreateFile:
    def __init__(self, handle: int = _ROOT_HANDLE) -> None:
        self.handle = handle
        self.calls: list[tuple[str, int, int, object, int, int, object]] = []

    def __call__(
        self,
        path: object,
        desired_access: object,
        share_mode: object,
        security_attributes: object,
        creation_disposition: object,
        flags_and_attributes: object,
        template_file: object,
    ) -> int:
        assert isinstance(path, str)
        self.calls.append(
            (
                path,
                _as_int(desired_access),
                _as_int(share_mode),
                security_attributes,
                _as_int(creation_disposition),
                _as_int(flags_and_attributes),
                template_file,
            )
        )
        return self.handle


class FakeGetInformationEx:
    def __init__(
        self,
        attributes: Mapping[int, int] | None = None,
        reparse_tags: Mapping[int, int] | None = None,
        *,
        result: int = 1,
    ) -> None:
        self.attributes = dict(
            attributes
            or {
                _ROOT_HANDLE: _DIRECTORY_ATTRIBUTES,
                _CHILD_HANDLE: _DIRECTORY_ATTRIBUTES,
            }
        )
        self.reparse_tags = dict(reparse_tags or {})
        self.result = result
        self.calls: list[tuple[int, int, int]] = []

    def __call__(
        self,
        raw_handle: object,
        raw_information_class: object,
        raw_information: object,
        raw_information_size: object,
    ) -> int:
        handle = _as_int(raw_handle)
        information_class = _as_int(raw_information_class)
        information_size = _as_int(raw_information_size)
        self.calls.append((handle, information_class, information_size))
        if self.result:
            information = ctypes.cast(
                cast(Any, raw_information),
                ctypes.POINTER(helper._FILE_ATTRIBUTE_TAG_INFO),
            )
            information.contents.FileAttributes = self.attributes[handle]
            information.contents.ReparseTag = self.reparse_tags.get(handle, 0)
        return self.result


class FakeGetFinalPath:
    def __init__(self, paths: Mapping[int, str]) -> None:
        self.paths = dict(paths)
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
        path_units = len(path.encode("utf-16-le")) // 2
        required = path_units + 1
        if capacity < required:
            return required
        _write_wide_string(raw_buffer, path)
        return path_units


class FakeCloseHandle:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[int] = []

    def __call__(self, raw_handle: object) -> int:
        self.calls.append(_as_int(raw_handle))
        return self.result


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
        object_attributes = ctypes.cast(
            cast(Any, raw_object_attributes),
            ctypes.POINTER(helper._OBJECT_ATTRIBUTES),
        ).contents
        object_name = object_attributes.ObjectName.contents
        component = ctypes.string_at(
            object_name.Buffer,
            object_name.Length,
        ).decode("utf-16-le")
        self.calls.append(
            {
                "desired_access": _as_int(raw_desired_access),
                "object_attributes_length": int(object_attributes.Length),
                "parent": _as_int(object_attributes.RootDirectory),
                "component": component,
                "name_length": int(object_name.Length),
                "name_maximum_length": int(object_name.MaximumLength),
                "attributes": int(object_attributes.Attributes),
                "security_descriptor": object_attributes.SecurityDescriptor,
                "security_qos": object_attributes.SecurityQualityOfService,
                "allocation_size": allocation_size,
                "file_attributes": _as_int(raw_file_attributes),
                "share_access": _as_int(raw_share_access),
                "create_disposition": _as_int(raw_create_disposition),
                "create_options": _as_int(raw_create_options),
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
    close_handle: Callable[..., object] | None = None,
    nt_create_file: Callable[..., object] | None = None,
) -> helper.CtypesWindowsDirectoryApi:
    return helper.CtypesWindowsDirectoryApi(
        create_file=create_file or FakeCreateFile(),
        get_file_information_by_handle_ex=get_information or FakeGetInformationEx(),
        get_final_path_name_by_handle=get_final_path
        or FakeGetFinalPath(
            {
                _ROOT_HANDLE: "\\\\?\\C:\\",
                _CHILD_HANDLE: r"\\?\C:\store",
            }
        ),
        close_handle=close_handle or FakeCloseHandle(),
        nt_create_file=nt_create_file or FakeNtCreateFile(),
    )


def test_create_file_anchors_only_the_drive_root_with_exact_flags() -> None:
    create_file = FakeCreateFile()
    api = _raw_api(create_file=create_file)

    assert api.open_root("C:\\") == _ROOT_HANDLE
    assert create_file.calls == [
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


def test_file_attribute_tag_info_matches_the_public_windows_abi() -> None:
    assert ctypes.sizeof(helper._FILE_ATTRIBUTE_TAG_INFO) == 8
    assert ctypes.alignment(helper._FILE_ATTRIBUTE_TAG_INFO) == 4
    assert helper._FILE_ATTRIBUTE_TAG_INFO.FileAttributes.offset == 0
    assert helper._FILE_ATTRIBUTE_TAG_INFO.ReparseTag.offset == 4


def test_create_file_root_adapter_rejects_a_full_path_before_io() -> None:
    create_file = FakeCreateFile()
    api = _raw_api(create_file=create_file)

    with pytest.raises(helper.WindowsLocalStoreHealthError):
        api.open_root(r"C:\store")

    assert create_file.calls == []


def test_nt_create_file_opens_one_component_with_exact_relative_contract() -> None:
    nt_create_file = FakeNtCreateFile()
    api = _raw_api(nt_create_file=nt_create_file)

    assert api.open_component(_ROOT_HANDLE, "données-🧪") == _CHILD_HANDLE
    assert nt_create_file.calls == [
        {
            "desired_access": 0xA0,
            "object_attributes_length": ctypes.sizeof(helper._OBJECT_ATTRIBUTES),
            "parent": _ROOT_HANDLE,
            "component": "données-🧪",
            "name_length": len("données-🧪".encode("utf-16-le")),
            "name_maximum_length": len("données-🧪".encode("utf-16-le")) + 2,
            "attributes": 0x1000,
            "security_descriptor": None,
            "security_qos": None,
            "allocation_size": None,
            "file_attributes": 0,
            "share_access": 0x7,
            "create_disposition": 1,
            "create_options": 0x00200000,
            "ea_buffer": None,
            "ea_length": 0,
        }
    ]


def test_invalid_parameter_retries_once_without_obj_dont_reparse() -> None:
    nt_create_file = FakeNtCreateFile(
        (
            (0xC000000D, 303),
            (0, _CHILD_HANDLE),
        )
    )
    close_handle = FakeCloseHandle()
    api = _raw_api(
        nt_create_file=nt_create_file,
        close_handle=close_handle,
    )

    assert api.open_component(_ROOT_HANDLE, "store") == _CHILD_HANDLE
    assert [call["attributes"] for call in nt_create_file.calls] == [0x1000, 0]
    assert close_handle.calls == [303]


def test_reparse_encounter_retries_once_for_same_handle_tag_inspection() -> None:
    nt_create_file = FakeNtCreateFile(
        (
            (0xC000050B, 303),
            (0, _CHILD_HANDLE),
        )
    )
    close_handle = FakeCloseHandle()
    api = _raw_api(
        nt_create_file=nt_create_file,
        close_handle=close_handle,
    )

    assert api.open_component(_ROOT_HANDLE, "store") == _CHILD_HANDLE
    assert [call["attributes"] for call in nt_create_file.calls] == [0x1000, 0]
    assert [call["create_options"] for call in nt_create_file.calls] == [
        0x00200000,
        0x00200000,
    ]
    assert close_handle.calls == [303]


def test_nonzero_success_status_fails_closed_and_closes_output() -> None:
    nt_create_file = FakeNtCreateFile(((0x00000103, _CHILD_HANDLE),))
    close_handle = FakeCloseHandle()
    api = _raw_api(
        nt_create_file=nt_create_file,
        close_handle=close_handle,
    )

    with pytest.raises(
        helper.WindowsLocalStoreHealthError,
        match=f"^{_STATIC_ERROR}$",
    ):
        api.open_component(_ROOT_HANDLE, "store")

    assert close_handle.calls == [_CHILD_HANDLE]


@pytest.mark.parametrize("failure_status", [0xC0000022, 0xC0000034, -1])
def test_no_status_except_the_two_safe_cases_uses_the_fallback(
    failure_status: int,
) -> None:
    nt_create_file = FakeNtCreateFile(((failure_status, 303),))
    close_handle = FakeCloseHandle()
    api = _raw_api(
        nt_create_file=nt_create_file,
        close_handle=close_handle,
    )

    with pytest.raises(
        helper.WindowsLocalStoreHealthError,
        match=f"^{_STATIC_ERROR}$",
    ):
        api.open_component(_ROOT_HANDLE, "store")

    assert len(nt_create_file.calls) == 1
    assert nt_create_file.calls[0]["attributes"] == 0x1000
    assert close_handle.calls == [303]


def test_failed_invalid_parameter_fallback_closes_each_stray_output_once() -> None:
    nt_create_file = FakeNtCreateFile(
        (
            (0xC000000D, 303),
            (0xC0000022, 404),
        )
    )
    close_handle = FakeCloseHandle()
    api = _raw_api(
        nt_create_file=nt_create_file,
        close_handle=close_handle,
    )

    with pytest.raises(helper.WindowsLocalStoreHealthError):
        api.open_component(_ROOT_HANDLE, "store")

    assert close_handle.calls == [303, 404]


def test_nt_control_flow_failure_closes_a_written_output_once() -> None:
    nt_create_file = FakeNtCreateFile(
        ((0, _CHILD_HANDLE),),
        after_write=KeyboardInterrupt("private path detail"),
    )
    close_handle = FakeCloseHandle()
    api = _raw_api(
        nt_create_file=nt_create_file,
        close_handle=close_handle,
    )

    with pytest.raises(KeyboardInterrupt, match="private path detail"):
        api.open_component(_ROOT_HANDLE, "store")

    assert close_handle.calls == [_CHILD_HANDLE]


@pytest.mark.parametrize(
    "component",
    [
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
    ],
)
def test_open_component_rejects_noncanonical_names_without_calling_nt(
    component: str,
) -> None:
    nt_create_file = FakeNtCreateFile()
    api = _raw_api(nt_create_file=nt_create_file)

    with pytest.raises(helper.WindowsLocalStoreHealthError):
        api.open_component(_ROOT_HANDLE, component)

    assert nt_create_file.calls == []


def test_same_handle_attributes_and_final_path_are_queried() -> None:
    get_information = FakeGetInformationEx(
        {_CHILD_HANDLE: _REPARSE_DIRECTORY_ATTRIBUTES},
        {_CHILD_HANDLE: _CLOUD_REPARSE_TAG},
    )
    get_final_path = FakeGetFinalPath({_CHILD_HANDLE: r"\\?\C:\store"})
    api = _raw_api(
        get_information=get_information,
        get_final_path=get_final_path,
    )

    assert api.file_attributes_and_reparse_tag(_CHILD_HANDLE) == (
        _REPARSE_DIRECTORY_ATTRIBUTES,
        _CLOUD_REPARSE_TAG,
    )
    assert api.normalized_dos_path(_CHILD_HANDLE) == r"C:\store"
    assert get_information.calls == [
        (
            _CHILD_HANDLE,
            9,
            ctypes.sizeof(helper._FILE_ATTRIBUTE_TAG_INFO),
        )
    ]
    assert get_final_path.calls == [(_CHILD_HANDLE, 260, 0)]


def test_attribute_tag_query_failure_fails_closed() -> None:
    api = _raw_api(get_information=FakeGetInformationEx(result=0))

    with pytest.raises(
        helper.WindowsLocalStoreHealthError,
        match=f"^{_STATIC_ERROR}$",
    ):
        api.file_attributes_and_reparse_tag(_CHILD_HANDLE)


def test_final_path_query_retries_to_support_a_long_unicode_path() -> None:
    final_path = "\\\\?\\D:\\" + ("données\\" * 45) + "résultat"
    query = FakeGetFinalPath({_CHILD_HANDLE: final_path})
    api = _raw_api(get_final_path=query)

    assert api.normalized_dos_path(_CHILD_HANDLE) == final_path[4:]
    assert query.calls == [
        (_CHILD_HANDLE, 260, 0),
        (_CHILD_HANDLE, len(final_path) + 1, 0),
    ]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        r"\\?\UNC\server\share\store",
        r"\\?\Volume{01234567-89ab-cdef-0123-456789abcdef}\store",
        r"\\?\GLOBALROOT\Device\HarddiskVolume1\store",
        r"\\.\C:\store",
        r"\??\C:\store",
        r"C:\store",
        r"\\?\1:\store",
        "\\\\?\\é:\\store",
        r"\\?\C:relative",
        r"\\?\C:/store",
        r"\\?\C:\allowed\..\store",
        r"\\?\C:\allowed\\store",
        r"\\?\C:\allowed\store.",
        "\\\\?\\C:\\allowed\\line\nbreak",
        "\\\\?\\C:\\allowed\\store\x00suffix",
    ],
)
def test_final_path_rejects_noncanonical_or_non_dos_namespaces(
    unsafe_path: str,
) -> None:
    api = _raw_api(get_final_path=FakeGetFinalPath({_CHILD_HANDLE: unsafe_path}))

    with pytest.raises(
        helper.WindowsLocalStoreHealthError,
        match=f"^{_STATIC_ERROR}$",
    ):
        api.normalized_dos_path(_CHILD_HANDLE)


class RecordingDirectoryApi:
    def __init__(
        self,
        *,
        open_handles: Sequence[int],
        paths: Mapping[int, str],
        attributes: Mapping[int, int] | None = None,
        reparse_tags: Mapping[int, int] | None = None,
        close_failures: Mapping[int, BaseException] | None = None,
        attribute_failure: BaseException | None = None,
    ) -> None:
        self.open_handles = list(open_handles)
        self.paths = dict(paths)
        self.attributes = dict(attributes or {})
        self.reparse_tags = dict(reparse_tags or {})
        self.close_failures = dict(close_failures or {})
        self.attribute_failure = attribute_failure
        self.root_calls: list[str] = []
        self.component_calls: list[tuple[int, str]] = []
        self.attribute_calls: list[int] = []
        self.path_calls: list[int] = []
        self.close_calls: list[int] = []

    def open_root(self, root: str) -> int:
        self.root_calls.append(root)
        return self.open_handles.pop(0)

    def open_component(self, parent: int, component: str) -> int:
        self.component_calls.append((parent, component))
        return self.open_handles.pop(0)

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        self.attribute_calls.append(handle)
        if self.attribute_failure is not None:
            raise self.attribute_failure
        return (
            self.attributes.get(handle, _DIRECTORY_ATTRIBUTES),
            self.reparse_tags.get(handle, 0),
        )

    def normalized_dos_path(self, handle: int) -> str:
        self.path_calls.append(handle)
        return self.paths[handle]

    def close(self, handle: int) -> None:
        self.close_calls.append(handle)
        failure = self.close_failures.get(handle)
        if failure is not None:
            raise failure


def test_handle_walk_retains_each_parent_until_its_child_is_validated() -> None:
    api = RecordingDirectoryApi(
        open_handles=(10, 20, 30),
        paths={
            10: "C:\\",
            20: r"C:\allowed",
            30: r"C:\allowed\store",
        },
    )

    assert helper.is_handle_bound_traversable_directory(
        r"c:\allowed\store",
        api=api,
    )
    assert api.root_calls == ["C:\\"]
    assert api.component_calls == [(10, "allowed"), (20, "store")]
    assert api.attribute_calls == [10, 20, 30]
    assert api.path_calls == [10, 20, 30]
    assert api.close_calls == [10, 20, 30]


@pytest.mark.parametrize(
    ("paths", "attributes", "reparse_tags"),
    [
        (
            {10: "C:\\", 20: r"C:\outside"},
            {},
            {},
        ),
        (
            {10: "C:\\", 20: r"C:\store"},
            {20: _REPARSE_DIRECTORY_ATTRIBUTES},
            {},
        ),
        (
            {10: "C:\\", 20: r"C:\store"},
            {20: _REPARSE_DIRECTORY_ATTRIBUTES},
            {20: _MOUNT_POINT_REPARSE_TAG},
        ),
        (
            {10: "C:\\", 20: r"C:\store"},
            {20: _REPARSE_DIRECTORY_ATTRIBUTES},
            {20: _NON_DIRECTORY_REPARSE_TAG},
        ),
        (
            {10: "C:\\", 20: r"C:\store"},
            {20: 0x80},
            {},
        ),
    ],
)
def test_mismatched_final_identity_or_attributes_fail_closed(
    paths: Mapping[int, str],
    attributes: Mapping[int, int],
    reparse_tags: Mapping[int, int],
) -> None:
    api = RecordingDirectoryApi(
        open_handles=(10, 20),
        paths=paths,
        attributes=attributes,
        reparse_tags=reparse_tags,
    )

    assert not helper.is_handle_bound_traversable_directory(
        r"C:\store",
        api=api,
    )
    assert api.close_calls == [20, 10]


def test_drive_root_reparse_point_is_rejected_even_when_not_a_name_surrogate() -> None:
    api = RecordingDirectoryApi(
        open_handles=(10,),
        paths={10: "C:\\"},
        attributes={10: _REPARSE_DIRECTORY_ATTRIBUTES},
        reparse_tags={10: _CLOUD_REPARSE_TAG},
    )

    assert not helper.is_handle_bound_traversable_directory("C:\\", api=api)
    assert api.component_calls == []
    assert api.close_calls == [10]


def test_walk_can_open_a_child_relative_to_a_cloud_directory_handle() -> None:
    api = RecordingDirectoryApi(
        open_handles=(10, 20, 30),
        paths={
            10: "C:\\",
            20: r"C:\OneDrive",
            30: r"C:\OneDrive\store",
        },
        attributes={20: _REPARSE_DIRECTORY_ATTRIBUTES},
        reparse_tags={20: _CLOUD_REPARSE_TAG},
    )

    assert helper.is_handle_bound_traversable_directory(
        r"C:\OneDrive\store",
        api=api,
    )
    assert api.component_calls == [(10, "OneDrive"), (20, "store")]
    assert api.path_calls == [10, 20, 30]
    assert api.close_calls == [10, 20, 30]


def test_unused_tag_is_ignored_for_a_plain_directory() -> None:
    api = RecordingDirectoryApi(
        open_handles=(10,),
        paths={10: "C:\\"},
        attributes={10: _DIRECTORY_ATTRIBUTES},
        reparse_tags={10: _MOUNT_POINT_REPARSE_TAG},
    )

    assert helper.is_handle_bound_traversable_directory("C:\\", api=api)
    assert api.close_calls == [10]


def test_injected_base_exception_attempts_each_tracked_handle_close() -> None:
    api = RecordingDirectoryApi(
        open_handles=(10, 20),
        paths={10: "C:\\", 20: r"C:\store"},
        close_failures={10: KeyboardInterrupt("private close detail")},
    )

    assert not helper.is_handle_bound_traversable_directory(
        r"C:\store",
        api=api,
    )
    assert api.close_calls == [10, 20]


def test_injected_validation_base_exception_attempts_tracked_handle_close() -> None:
    api = RecordingDirectoryApi(
        open_handles=(10,),
        paths={10: "C:\\"},
        attribute_failure=KeyboardInterrupt("private attribute detail"),
    )

    assert not helper.is_handle_bound_traversable_directory("C:\\", api=api)
    assert api.close_calls == [10]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "",
        "C:",
        r"C:relative",
        r"\rooted",
        r"\\server\share\store",
        r"\\?\C:\store",
        r"C:/store",
        r"C:\allowed\\store",
        r"C:\allowed\.",
        r"C:\allowed\..",
        r"C:\allowed\NUL.txt",
        r"C:\allowed\store ",
        "C:\\allowed\\line\nbreak",
        "C:\\allowed\\\ud800",
    ],
)
def test_strict_native_path_parser_rejects_aliasing_syntax_before_io(
    unsafe_path: str,
) -> None:
    api = RecordingDirectoryApi(open_handles=(), paths={})

    assert not helper.is_handle_bound_traversable_directory(
        unsafe_path,
        api=api,
    )
    assert api.root_calls == []
    assert api.component_calls == []
    assert api.close_calls == []


def test_main_is_output_free_for_success_and_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    healthy_api = RecordingDirectoryApi(
        open_handles=(10, 20),
        paths={10: "C:\\", 20: r"C:\store"},
    )
    unhealthy_api = RecordingDirectoryApi(open_handles=(), paths={})

    assert (
        helper.main(
            ("helper",),
            {helper.LOCAL_STORE_HEALTH_ROOT_ENV: r"C:\store"},
            api=healthy_api,
        )
        == 0
    )
    assert (
        helper.main(
            ("helper",),
            {helper.LOCAL_STORE_HEALTH_ROOT_ENV: r"C:\allowed\..\store"},
            api=unhealthy_api,
        )
        != 0
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    ("argv", "environment"),
    [
        ((), {}),
        (("helper", "extra"), {}),
        (("helper",), {}),
        (("helper",), {helper.LOCAL_STORE_HEALTH_ROOT_ENV: ""}),
        (("helper",), {helper.LOCAL_STORE_HEALTH_ROOT_ENV: "bad\0path"}),
    ],
)
def test_main_rejects_malformed_protocol_without_constructing_the_api(
    argv: Sequence[str],
    environment: Mapping[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    api = RecordingDirectoryApi(open_handles=(), paths={})

    assert helper.main(argv, environment, api=api) != 0
    assert api.root_calls == []
    assert capsys.readouterr() == ("", "")


def _run_real_helper(root: Path, *extra_args: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows"),
        helper.LOCAL_STORE_HEALTH_ROOT_ENV: str(root),
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


class _NativeDirectoryApi:
    def __init__(self) -> None:
        self.delegate = helper.CtypesWindowsDirectoryApi()

    def open_root(self, root: str) -> int:
        return self.delegate.open_root(root)

    def open_component(self, parent: int, component: str) -> int:
        return self.delegate.open_component(parent, component)

    def file_attributes_and_reparse_tag(self, handle: int) -> tuple[int, int]:
        return self.delegate.file_attributes_and_reparse_tag(handle)

    def normalized_dos_path(self, handle: int) -> str:
        return self.delegate.normalized_dos_path(handle)

    def close(self, handle: int) -> None:
        self.delegate.close(handle)


class _OpenComponentJunctionRaceApi(_NativeDirectoryApi):
    def __init__(
        self,
        *,
        raced_component: str,
        original: Path,
        moved: Path,
        outside: Path,
        swap_before_open: bool,
    ) -> None:
        super().__init__()
        self.raced_component = raced_component
        self.original = original
        self.moved = moved
        self.outside = outside
        self.swap_before_open = swap_before_open
        self.swapped = False
        self.open_returned = False
        self.opened_path_after_swap: str | None = None

    def _swap(self) -> None:
        self.original.rename(self.moved)
        _create_windows_junction(self.original, self.outside)
        self.swapped = True

    def open_component(self, parent: int, component: str) -> int:
        if component == self.raced_component and not self.swapped:
            if self.swap_before_open:
                self._swap()
            handle = self.delegate.open_component(parent, component)
            self.open_returned = True
            if not self.swap_before_open:
                self._swap()
            self.opened_path_after_swap = self.delegate.normalized_dos_path(handle)
            return handle
        return self.delegate.open_component(parent, component)


class _PostValidationParentJunctionRaceApi(_NativeDirectoryApi):
    def __init__(
        self,
        *,
        original_parent: Path,
        moved_parent: Path,
        outside_parent: Path,
    ) -> None:
        super().__init__()
        self.original_parent = original_parent
        self.moved_parent = moved_parent
        self.outside_parent = outside_parent
        self.expected_parent_path = os.path.realpath(original_parent)
        self.swapped = False
        self.opened_child_path: str | None = None

    def normalized_dos_path(self, handle: int) -> str:
        actual_path = self.delegate.normalized_dos_path(handle)
        if actual_path == self.expected_parent_path and not self.swapped:
            self.original_parent.rename(self.moved_parent)
            _create_windows_junction(
                self.original_parent,
                self.outside_parent,
            )
            self.swapped = True
        elif self.swapped and actual_path.startswith(os.path.realpath(self.moved_parent) + "\\"):
            self.opened_child_path = actual_path
        return actual_path


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handles")
def test_real_helper_accepts_a_long_unicode_plain_directory_without_output(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "données-🧪"
    for index in range(30):
        directory /= f"niveau-{index:02d}"
    directory.mkdir(parents=True)
    assert len(str(directory)) > 260

    completed = _run_real_helper(directory)

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
@pytest.mark.parametrize("race_final_component", (False, True))
def test_native_windows_pre_open_junction_substitution_is_rejected(
    tmp_path: Path,
    race_final_component: bool,
) -> None:
    root = tmp_path / "pre-open-race"
    raced = root / (
        "raced-final-component" if race_final_component else "raced-intermediate-component"
    )
    target = raced if race_final_component else raced / "store"
    target.mkdir(parents=True)
    outside = tmp_path / "pre-open-outside"
    if race_final_component:
        outside.mkdir()
    else:
        (outside / target.name).mkdir(parents=True)
    moved = tmp_path / "pre-open-moved"
    canonical_target = os.path.realpath(target)
    api = _OpenComponentJunctionRaceApi(
        raced_component=raced.name,
        original=raced,
        moved=moved,
        outside=outside,
        swap_before_open=True,
    )

    assert not helper.is_handle_bound_traversable_directory(
        canonical_target,
        api=api,
    )
    assert api.swapped
    if api.open_returned:
        assert api.opened_path_after_swap is not None
        outside_path = os.path.realpath(outside)
        assert api.opened_path_after_swap != outside_path
        assert not api.opened_path_after_swap.startswith(outside_path + "\\")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_native_windows_post_open_replacement_inspects_retained_final_handle(
    tmp_path: Path,
) -> None:
    target = tmp_path / "raced-final-after-open"
    target.mkdir()
    outside = tmp_path / "post-open-outside"
    outside.mkdir()
    moved = tmp_path / "post-open-moved"
    canonical_target = os.path.realpath(target)
    api = _OpenComponentJunctionRaceApi(
        raced_component=target.name,
        original=target,
        moved=moved,
        outside=outside,
        swap_before_open=False,
    )

    assert not helper.is_handle_bound_traversable_directory(
        canonical_target,
        api=api,
    )
    assert api.swapped
    assert api.opened_path_after_swap is not None
    assert api.opened_path_after_swap != os.path.realpath(outside)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_native_windows_post_validation_replacement_keeps_relative_handle_walk(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "raced-parent-after-validation"
    target = parent / "store"
    target.mkdir(parents=True)
    outside = tmp_path / "post-validation-outside"
    outside.mkdir()
    moved = tmp_path / "post-validation-moved"
    canonical_target = os.path.realpath(target)
    api = _PostValidationParentJunctionRaceApi(
        original_parent=parent,
        moved_parent=moved,
        outside_parent=outside,
    )

    assert not helper.is_handle_bound_traversable_directory(
        canonical_target,
        api=api,
    )
    assert api.swapped
    assert api.opened_child_path is not None
    assert api.opened_child_path != os.path.realpath(outside / target.name)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_real_helper_rejects_a_final_junction_without_output(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    _create_windows_junction(junction, target)

    completed = _run_real_helper(junction)

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_real_helper_rejects_an_intermediate_junction_without_output(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    junction = tmp_path / "junction"
    _create_windows_junction(junction, outside)

    completed = _run_real_helper(junction / nested.name)

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows interpreter")
def test_real_helper_protocol_failure_is_output_free(tmp_path: Path) -> None:
    completed = _run_real_helper(tmp_path, "unexpected")

    assert completed.returncode != 0
    assert completed.stdout == b""
    assert completed.stderr == b""
