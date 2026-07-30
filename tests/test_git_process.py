import os
from types import SimpleNamespace

import pytest

from lab_tracker.bounded_subprocess import ProcessDeadline
from lab_tracker.git_process import (
    GIT_PROCESS_METADATA_LIMIT_BYTES,
    GIT_PROCESS_STDERR_LIMIT_BYTES,
    GitCompleted,
    GitProcessOutputLimitExceeded,
    build_git_environment,
    git_http_config_args,
    git_remote_preflight_matches,
    run_git_command,
)
from lab_tracker.git_remote_policy import parse_git_remote_address


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"bounded\n")


@pytest.mark.parametrize(
    ("stdout", "matches"),
    [
        (b"https://example.com/org/repo.git\n", True),
        (b"https://example.com/org/repo.git\r\n", True),
        (b"https://example.com/org/repo.git", False),
        (b"https://example.com/org/repo.git\r", False),
        (b"https://example.com/org/repo.git\n\n", False),
        (b"https://example.com/org/repo.git\nsecond\n", False),
        (b" https://example.com/org/repo.git\n", False),
        (b"https://example.com/org/repo.git \n", False),
        (b"\thttps://example.com/org/repo.git\n", False),
        (b"https://example.com/org/repo.git\t\n", False),
        (b"https://other.example/org/repo.git\n", False),
        (b"\xff\n", False),
    ],
)
def test_git_remote_preflight_requires_one_exact_terminal_line(
    stdout: bytes,
    matches: bool,
) -> None:
    completed = GitCompleted(0, stdout, b"ignored diagnostic")

    assert (
        git_remote_preflight_matches(
            completed,
            "https://example.com/org/repo.git",
        )
        is matches
    )


def test_git_remote_preflight_rejects_nonzero_exit() -> None:
    completed = GitCompleted(
        1,
        b"https://example.com/org/repo.git\n",
        b"",
    )

    assert (
        git_remote_preflight_matches(
            completed,
            "https://example.com/org/repo.git",
        )
        is False
    )


def test_build_git_environment_sanitizes_repository_selection(
    monkeypatch,
    tmp_path,
) -> None:
    stripped = (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    )
    for variable in stripped:
        monkeypatch.setenv(variable, "ambient-secret")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ext")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/operator/gitconfig")
    cwd = os.fspath(tmp_path / "cache")

    env = build_git_environment("https:ssh:git", cwd=cwd)

    assert all(variable not in env for variable in stripped)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh:git"
    assert env["GIT_CEILING_DIRECTORIES"] == os.path.dirname(os.path.realpath(cwd))
    assert env["GIT_CONFIG_GLOBAL"] == "/operator/gitconfig"


def test_build_git_environment_none_removes_ambient_protocol(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ext")

    env = build_git_environment(None, cwd=os.fspath(tmp_path))

    assert "GIT_ALLOW_PROTOCOL" not in env


def test_git_http_config_denies_redirects_generically_and_for_exact_https_url() -> None:
    approved = parse_git_remote_address("https://example.com/org/repo.git")
    assert approved is not None

    assert git_http_config_args(approved) == [
        "-c",
        "http.followRedirects=false",
        "-c",
        "http.https://example.com/org/repo.git.followRedirects=false",
    ]


def test_git_http_config_uses_only_generic_redirect_denial_for_ssh() -> None:
    approved = parse_git_remote_address("git@example.com:org/repo.git")
    assert approved is not None

    assert git_http_config_args(approved) == [
        "-c",
        "http.followRedirects=false",
    ]


def test_run_git_command_passes_shared_deadline_environment_and_bounds() -> None:
    executor = _RecordingExecutor()
    deadline = ProcessDeadline.after(10.0, clock=lambda: 0.0)
    env = {"GIT_TERMINAL_PROMPT": "0"}
    config_args = ["-c", "http.followRedirects=false"]

    completed = run_git_command(
        runner=None,
        executor=executor,
        binary="git-custom",
        args=["ls-remote", "--", "https://example.com/org/repo.git", "HEAD"],
        cwd="/approved/cache",
        env=env,
        config_args=config_args,
        deadline=deadline,
    )

    assert completed == GitCompleted(0, b"bounded\n", b"")
    assert len(executor.calls) == 1
    command, kwargs = executor.calls[0]
    assert command == [
        "git-custom",
        "-c",
        "http.followRedirects=false",
        "ls-remote",
        "--",
        "https://example.com/org/repo.git",
        "HEAD",
    ]
    assert kwargs["cwd"] == "/approved/cache"
    assert kwargs["deadline"] is deadline
    assert kwargs["env"] is env
    assert kwargs["stdout_limit_bytes"] == GIT_PROCESS_METADATA_LIMIT_BYTES
    assert kwargs["stderr_limit_bytes"] == GIT_PROCESS_STDERR_LIMIT_BYTES


def test_run_git_command_preserves_trusted_runner_contract_and_streams_stdout() -> None:
    calls = []
    consumed = []
    deadline = ProcessDeadline.after(10.0, clock=lambda: 0.0)

    def runner(args):
        calls.append(args)
        return GitCompleted(0, b"blob", b"bounded diagnostic")

    completed = run_git_command(
        runner=runner,
        executor=_RecordingExecutor(),
        binary="ignored",
        args=["cat-file", "blob", "revision:path"],
        cwd="/approved/cache",
        env={},
        config_args=["-c", "http.followRedirects=false"],
        deadline=deadline,
        stdout_limit_bytes=16,
        stdout_consumer=consumed.append,
    )

    assert calls == [
        [
            "-c",
            "http.followRedirects=false",
            "-C",
            "/approved/cache",
            "cat-file",
            "blob",
            "revision:path",
        ]
    ]
    assert consumed == [b"blob"]
    assert completed == GitCompleted(0, b"", b"bounded diagnostic")


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        (b"x" * (GIT_PROCESS_METADATA_LIMIT_BYTES + 1), b""),
        (b"", b"x" * (GIT_PROCESS_STDERR_LIMIT_BYTES + 1)),
    ],
    ids=("stdout-overflow", "stderr-overflow"),
)
def test_run_git_command_bounds_trusted_runner_output(
    stdout: bytes,
    stderr: bytes,
) -> None:
    deadline = ProcessDeadline.after(10.0, clock=lambda: 0.0)

    with pytest.raises(
        GitProcessOutputLimitExceeded,
        match="Git process output limit exceeded",
    ):
        run_git_command(
            runner=lambda _args: GitCompleted(0, stdout, stderr),
            executor=_RecordingExecutor(),
            binary="ignored",
            args=["status"],
            cwd="/approved/cache",
            env={},
            config_args=[],
            deadline=deadline,
        )
