from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import lab_tracker._local_store_health_helper as helper

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="requires POSIX descriptor-relative filesystem operations",
)

_ORDINARY_MOUNT_ENV = "LAB_TRACKER_TEST_ORDINARY_MOUNT_ROOT"
_BIND_MOUNT_ENV = "LAB_TRACKER_TEST_BIND_MOUNT_ROOT"


def _payload(candidate: str, roots: list[str]) -> str:
    return json.dumps(
        {
            "v": helper.LOCAL_FILESYSTEM_PROTOCOL_VERSION,
            "op": helper.INSPECT_DIRECTORY_OPERATION,
            "candidate": candidate,
            "roots": roots,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _environment(candidate: str, roots: list[str]) -> dict[str, str]:
    return {helper.LOCAL_FILESYSTEM_REQUEST_ENV: _payload(candidate, roots)}


def _run_helper(
    candidate: Path,
    roots: list[Path],
) -> subprocess.CompletedProcess[bytes]:
    environment = _environment(
        os.fspath(candidate),
        [os.fspath(root) for root in roots],
    )
    return subprocess.run(  # noqa: S603 - fixed interpreter and helper path
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            os.fspath(Path(helper.__file__).resolve()),
        ],
        check=False,
        capture_output=True,
        env=environment,
    )


@pytest.mark.parametrize(
    ("candidate", "roots"),
    (
        ("/grant-sibling/store", ["/grant"]),
        ("/grant/../outside", ["/grant"]),
        ("/anywhere", []),
    ),
)
def test_lexically_denied_candidate_performs_no_filesystem_operation(
    monkeypatch: pytest.MonkeyPatch,
    candidate: str,
    roots: list[str],
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a denied candidate reached the filesystem")

    monkeypatch.setattr(helper, "_inspect_selected_directory", fail)

    assert helper.main(("helper",), _environment(candidate, roots)) == helper.DENIED_EXIT


@pytest.mark.parametrize(
    "raw",
    (
        "",
        "{}",
        '{"candidate":"/grant","op":"inspect-directory","roots":["/grant"],"v":1} ',
        '{"candidate":"/grant","op":"inspect-directory","roots":["/grant"],"v":true}',
        '{"candidate":"relative","op":"inspect-directory","roots":["/grant"],"v":1}',
        '{"candidate":"/grant","extra":0,"op":"inspect-directory",'
        '"roots":["/grant"],"v":1}',
    ),
)
def test_malformed_protocol_fails_before_filesystem_access(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a malformed request reached the filesystem")

    monkeypatch.setattr(helper.os, "open", fail)

    result = helper.main(
        ("helper",),
        {helper.LOCAL_FILESYSTEM_REQUEST_ENV: raw},
    )

    assert result == helper.FAILED_EXIT


def test_safe_relative_and_absolute_aliases_stay_inside_selected_grant(
    tmp_path: Path,
) -> None:
    root = tmp_path / "grant"
    destination = root / "destination"
    leaf = destination / "leaf"
    leaf.mkdir(parents=True)
    relative = root / "relative"
    absolute = root / "absolute"
    relative.symlink_to("destination", target_is_directory=True)
    absolute.symlink_to(destination, target_is_directory=True)

    assert (
        helper.main(("helper",), _environment(str(relative / "leaf"), [str(root)]))
        == helper.ACCESSIBLE_EXIT
    )
    assert (
        helper.main(("helper",), _environment(str(absolute / "leaf"), [str(root)]))
        == helper.ACCESSIBLE_EXIT
    )


def test_link_parent_keeps_native_post_expansion_semantics(tmp_path: Path) -> None:
    root = tmp_path / "grant"
    linked_parent = root / "nested" / "deeper"
    native_target = root / "nested" / "target"
    linked_parent.mkdir(parents=True)
    native_target.mkdir()
    link = root / "link"
    link.symlink_to("nested/deeper", target_is_directory=True)

    result = helper.main(
        ("helper",),
        _environment(f"{link}/../target", [str(root)]),
    )

    assert result == helper.ACCESSIBLE_EXIT


@pytest.mark.parametrize("absolute_target", (False, True))
def test_escaping_alias_uses_only_bootstrap_and_descriptor_relative_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    absolute_target: bool,
) -> None:
    physical_root = tmp_path / "physical-grant"
    physical_root.mkdir()
    configured_root = tmp_path / "configured-grant"
    configured_root.symlink_to(physical_root, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = physical_root / "escape"
    link.symlink_to(
        outside if absolute_target else "../outside",
        target_is_directory=True,
    )

    real_open = helper.os.open
    real_stat = helper.os.stat
    real_readlink = helper.os.readlink
    root_fds: list[int] = []
    calls: list[tuple[str, str, int | None]] = []

    def guarded_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        path_string = os.fspath(path)
        calls.append(("open", path_string, dir_fd))
        if path_string != str(configured_root) or dir_fd is not None:
            raise AssertionError("candidate or alias target reached os.open")
        fd = real_open(path_string, flags, mode, dir_fd=dir_fd)
        root_fds.append(fd)
        return fd

    def guarded_stat(
        path: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        path_string = os.fspath(path)
        calls.append(("stat", path_string, dir_fd))
        is_bound_root_identity = (
            path_string == str(physical_root)
            and dir_fd is None
            and follow_symlinks
        )
        is_alias_lstat = (
            path_string == "escape"
            and dir_fd in root_fds
            and not follow_symlinks
        )
        if not is_bound_root_identity and not is_alias_lstat:
            raise AssertionError("alias target reached os.stat")
        return real_stat(
            path_string,
            dir_fd=dir_fd,
            follow_symlinks=follow_symlinks,
        )

    def guarded_lstat(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> os.stat_result:
        path_string = os.fspath(path)
        calls.append(("lstat", path_string, dir_fd))
        if path_string != "escape" or dir_fd not in root_fds:
            raise AssertionError("alias target reached os.lstat")
        return real_stat(path_string, dir_fd=dir_fd, follow_symlinks=False)

    def guarded_access(
        path: str,
        mode: int,
        *,
        dir_fd: int | None = None,
        effective_ids: bool = False,
        follow_symlinks: bool = True,
    ) -> bool:
        path_string = os.fspath(path)
        calls.append(("access", path_string, dir_fd))
        if (
            path_string != "."
            or mode != os.X_OK
            or dir_fd not in root_fds
            or not effective_ids
            or not follow_symlinks
        ):
            raise AssertionError("alias target reached os.access")
        return True

    def guarded_realpath(
        path: str,
        *,
        strict: bool = False,
    ) -> str:
        path_string = os.fspath(path)
        calls.append(("realpath", path_string, None))
        if path_string != str(configured_root) or strict:
            raise AssertionError("candidate or alias target reached realpath")
        return str(physical_root)

    def guarded_readlink(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> str:
        path_string = os.fspath(path)
        calls.append(("readlink", path_string, dir_fd))
        if path_string != "escape" or dir_fd not in root_fds:
            raise AssertionError("alias target reached readlink")
        return real_readlink(path_string, dir_fd=dir_fd)

    guarded_path_module = SimpleNamespace(**vars(os.path))
    guarded_path_module.realpath = guarded_realpath
    guarded_os_module = SimpleNamespace(**vars(os))
    guarded_os_module.open = guarded_open
    guarded_os_module.stat = guarded_stat
    guarded_os_module.lstat = guarded_lstat
    guarded_os_module.access = guarded_access
    guarded_os_module.readlink = guarded_readlink
    guarded_os_module.path = guarded_path_module
    monkeypatch.setattr(helper, "os", guarded_os_module)

    result = helper.main(
        ("helper",),
        _environment(str(configured_root / "escape"), [str(configured_root)]),
    )

    assert result == helper.DENIED_EXIT
    assert [call[:2] for call in calls] == [
        ("open", str(configured_root)),
        ("access", "."),
        ("realpath", str(configured_root)),
        ("stat", str(physical_root)),
        ("stat", "escape"),
        ("readlink", "escape"),
    ]


def test_absolute_alias_cannot_switch_to_a_broader_overlapping_grant(
    tmp_path: Path,
) -> None:
    narrow = tmp_path / "grant"
    broader_only = tmp_path / "broader-only"
    narrow.mkdir()
    broader_only.mkdir()
    link = narrow / "escape"
    link.symlink_to(broader_only, target_is_directory=True)

    result = helper.main(
        ("helper",),
        _environment(str(link), [str(tmp_path), str(narrow)]),
    )

    assert result == helper.DENIED_EXIT


def test_operator_root_alias_is_a_trusted_bootstrap(tmp_path: Path) -> None:
    physical = tmp_path / "physical"
    child = physical / "child"
    child.mkdir(parents=True)
    configured = tmp_path / "configured"
    configured.symlink_to(physical, target_is_directory=True)

    result = helper.main(
        ("helper",),
        _environment(str(configured / "child"), [str(configured)]),
    )

    assert result == helper.ACCESSIBLE_EXIT


def test_absolute_alias_may_use_the_bound_resolved_root_spelling(
    tmp_path: Path,
) -> None:
    physical = tmp_path / "physical"
    target = physical / "target"
    target.mkdir(parents=True)
    absolute_alias = physical / "absolute-alias"
    absolute_alias.symlink_to(target, target_is_directory=True)
    configured = tmp_path / "configured"
    configured.symlink_to(physical, target_is_directory=True)

    result = helper.main(
        ("helper",),
        _environment(str(configured / "absolute-alias"), [str(configured)]),
    )

    assert result == helper.ACCESSIBLE_EXIT


@pytest.mark.parametrize("component_name", ("parent", "store"))
@pytest.mark.parametrize("swap_before_open", (True, False))
def test_intermediate_component_replacement_never_traverses_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component_name: str,
    swap_before_open: bool,
) -> None:
    root = tmp_path / "grant"
    parent = root / "parent"
    store = parent / "store"
    leaf = store / "leaf"
    leaf.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside_target = outside / f"{component_name}-target"
    if component_name == "parent":
        (outside_target / "store" / "leaf").mkdir(parents=True)
        raced_path = parent
        moved = root / "retained-parent"
    else:
        (outside_target / "leaf").mkdir(parents=True)
        raced_path = store
        moved = parent / "retained-store"

    retained_identities = {
        path.name: (path.stat().st_dev, path.stat().st_ino)
        for path in (parent, store, leaf)
    }
    outside_identities = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (outside_target, *outside_target.rglob("*"))
        if path.is_dir()
    }
    real_open = helper.os.open
    swapped = False
    successful_component_opens: list[tuple[str, tuple[int, int]]] = []

    def racing_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        path_string = os.fspath(path)
        if path_string == component_name and not swapped and swap_before_open:
            swapped = True
            raced_path.rename(moved)
            raced_path.symlink_to(outside_target, target_is_directory=True)
        fd = real_open(path_string, flags, mode, dir_fd=dir_fd)
        metadata = os.fstat(fd)
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in outside_identities:
            os.close(fd)
            raise AssertionError("an outside directory was opened")
        if dir_fd is not None:
            successful_component_opens.append((path_string, identity))
        if path_string == component_name and not swapped:
            swapped = True
            raced_path.rename(moved)
            raced_path.symlink_to(outside_target, target_is_directory=True)
        return fd

    monkeypatch.setattr(helper.os, "open", racing_open)

    result = helper.main(
        ("helper",),
        _environment(str(leaf), [str(root)]),
    )

    expected = helper.DENIED_EXIT if swap_before_open else helper.ACCESSIBLE_EXIT
    assert result == expected
    assert swapped is True
    assert not (
        {identity for _, identity in successful_component_opens}
        & outside_identities
    )
    if swap_before_open:
        assert component_name not in {
            path for path, _identity in successful_component_opens
        }
    else:
        assert successful_component_opens == [
            ("parent", retained_identities["parent"]),
            ("store", retained_identities["store"]),
            ("leaf", retained_identities["leaf"]),
        ]


def test_injected_base_exception_closes_every_owned_descriptor_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = iter((10, 11))
    close_calls: list[int] = []
    access_calls = 0

    monkeypatch.setattr(
        helper.os,
        "open",
        lambda *_args, **_kwargs: next(opened),
    )
    monkeypatch.setattr(
        helper.os,
        "stat",
        lambda *_args, **_kwargs: SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_dev=1,
            st_ino=2,
        ),
    )
    monkeypatch.setattr(
        helper.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_dev=1,
            st_ino=2,
        ),
    )

    def interrupt_second_access(
        _path: str,
        _mode: int,
        **_kwargs: object,
    ) -> bool:
        nonlocal access_calls
        access_calls += 1
        if access_calls == 2:
            raise KeyboardInterrupt("private target diagnostic")
        return True

    monkeypatch.setattr(helper.os, "access", interrupt_second_access)
    monkeypatch.setattr(helper.os, "close", lambda fd: close_calls.append(fd))

    result = helper.main(
        ("helper",),
        _environment("/grant/child", ["/grant"]),
    )

    assert result == helper.FAILED_EXIT
    assert close_calls == [11, 10]


def test_cleanup_failure_still_attempts_all_owned_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = iter((20, 21))
    close_calls: list[int] = []

    monkeypatch.setattr(
        helper.os,
        "open",
        lambda *_args, **_kwargs: next(opened),
    )
    monkeypatch.setattr(
        helper.os,
        "stat",
        lambda *_args, **_kwargs: SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_dev=1,
            st_ino=2,
        ),
    )
    monkeypatch.setattr(
        helper.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_dev=1,
            st_ino=2,
        ),
    )
    monkeypatch.setattr(helper.os, "access", lambda *_args, **_kwargs: True)

    def fail_first_close(fd: int) -> None:
        close_calls.append(fd)
        if fd == 21:
            raise OSError("private close diagnostic")

    monkeypatch.setattr(helper.os, "close", fail_first_close)

    result = helper.main(
        ("helper",),
        _environment("/grant/child", ["/grant"]),
    )

    assert result == helper.FAILED_EXIT
    assert close_calls == [21, 20]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root bypasses native directory search permission checks",
)
@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        (stat.S_IXUSR, helper.ACCESSIBLE_EXIT),
        (0, helper.DENIED_EXIT),
    ),
)
def test_native_directory_search_permission_contract(
    tmp_path: Path,
    mode: int,
    expected: int,
) -> None:
    root = tmp_path / "grant"
    candidate = root / "candidate"
    candidate.mkdir(parents=True)
    candidate.chmod(mode)
    try:
        completed = _run_helper(candidate, [root])
    finally:
        candidate.chmod(stat.S_IRWXU)

    assert completed.returncode == expected
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_helper_is_output_free_in_isolated_mode(tmp_path: Path) -> None:
    root = tmp_path / "grant"
    candidate = root / "candidate"
    candidate.mkdir(parents=True)

    completed = _run_helper(candidate, [root])

    assert completed.returncode == helper.ACCESSIBLE_EXIT
    assert completed.stdout == b""
    assert completed.stderr == b""


@pytest.mark.parametrize(
    "environment_name",
    (_ORDINARY_MOUNT_ENV, _BIND_MOUNT_ENV),
)
def test_native_mount_descendant_is_inside_containing_grant(
    environment_name: str,
) -> None:
    configured = os.environ.get(environment_name)
    if configured is None:
        pytest.skip(f"{environment_name} is not configured")
    mount_root = Path(configured)
    assert mount_root.is_absolute()
    assert mount_root.is_dir()
    grant = mount_root.parent
    candidate = Path(tempfile.mkdtemp(prefix=".lab-tracker-health-", dir=mount_root))
    try:
        completed = _run_helper(candidate, [grant])
    finally:
        candidate.rmdir()

    assert completed.returncode == helper.ACCESSIBLE_EXIT
    assert completed.stdout == b""
    assert completed.stderr == b""
