"""Scaffolded Claude Code hooks: hook payload on stdin, hook output on stdout.

Claude Code parses hook stdout that starts with ``{`` and ends with ``}`` as
structured hook output and drops unknown top-level keys, so context only
reaches the agent through ``hookSpecificOutput.additionalContext`` (or plain
text). These tests feed the commands the payload Claude Code sends and read
their stdout the way Claude Code does.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from lab_tracker.cli import init_consumer_repo
from lab_tracker_client import agent_hooks
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LTValidationError

NEXT_QUESTIONS = {
    "data": [
        {
            "goal": {"goal_id": "g-1", "title": "Map ρ across rigs", "status": "in_progress"},
            "question": {"question_id": "q-1", "text": "Does ρ drift?", "status": "active"},
            "score": 120,
            "rationale": ["question is linked directly to the active goal"],
        }
    ],
    "meta": {"limit": 5, "total_candidates": 1},
    "next_action": {"tool": "lab_tracker_get_decision_context", "arguments": {}},
}


def _payload(event: str, **fields: Any) -> dict[str, Any]:
    # The analysis-flavoured paths are deliberate: classifying the raw
    # payload text would call every prompt in such a checkout research-facing.
    return {
        "session_id": "abc123",
        "transcript_path": "/home/lab/.claude/projects/analysis/abc123.jsonl",
        "cwd": "/home/lab/analysis-results",
        "hook_event_name": event,
        **fields,
    }


def _piped(payload: dict[str, Any] | str, encoding: str = "utf-8") -> io.TextIOWrapper:
    """A piped stdin carrying ``payload`` as UTF-8 bytes, like Claude Code sends."""

    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return io.TextIOWrapper(io.BytesIO(text.encode("utf-8")), encoding=encoding)


class _MustNotRead(io.StringIO):
    """A stdin that fails the test if the command reads it."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty

    def read(self, size: int | None = -1) -> str:
        raise AssertionError("stdin must not be read")


def _hook_output(stdout: str) -> dict[str, Any]:
    """Parse stdout as Claude Code does and require hook-only top-level keys."""

    text = stdout.strip()
    assert text.startswith("{") and text.endswith("}")
    output = json.loads(text)
    assert set(output) == {"hookSpecificOutput"}
    return output["hookSpecificOutput"]


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def next_questions(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return NEXT_QUESTIONS

    def close(self) -> None:
        pass


@pytest.fixture
def stub_client(monkeypatch) -> _StubClient:
    client = _StubClient()
    monkeypatch.setattr(lt_cli.LabTracker, "from_env", lambda: client)
    return client


@pytest.fixture
def isolated_homes(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "lt-home"))
    monkeypatch.setenv("LAB_TRACKER_SKILLS_HOME", str(tmp_path / "skills-home"))
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "http://127.0.0.1:9")
    for name in ("LAB_TRACKER_MCP_BASE_URL", "LAB_TRACKER_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


PRIME_HOOK = ["prime", "--if-research-facing", "--fail-silent", "--limit", "5"]


def test_prime_hook_classifies_the_payload_prompt_and_emits_context(
    stub_client, monkeypatch, capsys
) -> None:
    payload = _payload("UserPromptSubmit", prompt="Plot the dataset for the figure")
    monkeypatch.setattr(sys, "stdin", _piped(payload))

    lt_cli.main(PRIME_HOOK)

    hook = _hook_output(capsys.readouterr().out)
    assert hook["hookEventName"] == "UserPromptSubmit"
    header, body = hook["additionalContext"].split("\n", 1)
    assert header == agent_hooks.PRIME_CONTEXT_HEADER
    assert json.loads(body) == NEXT_QUESTIONS
    assert stub_client.calls == [{"project_id": None, "limit": 5}]


def test_prime_hook_ignores_trigger_words_outside_the_prompt(
    stub_client, monkeypatch, capsys
) -> None:
    # cwd and transcript_path both contain "analysis"; the prompt does not.
    payload = _payload("UserPromptSubmit", prompt="please fix the import ordering")
    monkeypatch.setattr(sys, "stdin", _piped(payload))

    lt_cli.main(PRIME_HOOK)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert stub_client.calls == []


def test_prime_hook_decodes_a_utf8_payload_whatever_the_console_code_page(
    stub_client, monkeypatch, capsys
) -> None:
    # "ρ" is CF 81 in UTF-8, and 0x81 is undefined in cp1252: decoding the
    # payload with a Windows console code page would fail.
    payload = _payload("UserPromptSubmit", prompt="Does ρ drift across the dataset?")
    monkeypatch.setattr(sys, "stdin", _piped(payload, encoding="cp1252"))

    lt_cli.main(PRIME_HOOK)

    hook = _hook_output(capsys.readouterr().out)
    assert "Does ρ drift?" in hook["additionalContext"]


def test_prime_rejects_a_payload_without_a_prompt(stub_client, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", _piped(_payload("SessionStart", source="startup")))
    with pytest.raises(LTValidationError, match="UserPromptSubmit"):
        lt_cli.main(["prime", "--if-research-facing"])

    monkeypatch.setattr(sys, "stdin", _piped(_payload("SessionStart", source="startup")))
    lt_cli.main(PRIME_HOOK)
    assert capsys.readouterr().out == ""
    assert stub_client.calls == []


@pytest.mark.parametrize(
    "stdin_text",
    ["Summarize the analysis results", '{"prompt": "Summarize the analysis results"}'],
)
def test_prime_direct_stdin_keeps_its_json_output(
    stdin_text, stub_client, monkeypatch, capsys
) -> None:
    # Piped text that is not a hook payload is classified whole, as before.
    monkeypatch.setattr(sys, "stdin", _piped(stdin_text))

    lt_cli.main(["prime", "--if-research-facing"])

    assert json.loads(capsys.readouterr().out) == NEXT_QUESTIONS


def test_status_brief_hook_emits_every_suggestion_as_context(
    isolated_homes, monkeypatch, capsys
) -> None:
    repo = isolated_homes / "consumer"
    repo.mkdir()
    monkeypatch.chdir(repo)
    lt_cli.main(["setup", "status", "--target", str(repo), "--brief"])
    brief = json.loads(capsys.readouterr().out)
    assert len(brief["suggestions"]) > 1

    payload = _payload("SessionStart", source="startup")
    monkeypatch.setattr(sys, "stdin", _piped(payload))
    lt_cli.main(["setup", "status", "--target", str(repo), "--brief", "--fail-silent"])

    hook = _hook_output(capsys.readouterr().out)
    assert hook["hookEventName"] == "SessionStart"
    lines = hook["additionalContext"].splitlines()
    assert lines[0] == brief["brief"]
    assert lines[1:] == [f"- {item}" for item in brief["suggestions"][1:]]


@pytest.mark.parametrize(
    "make_stdin",
    [lambda: _MustNotRead(tty=True), lambda: io.StringIO("not a hook payload")],
    ids=["terminal", "piped-text"],
)
def test_status_brief_without_a_hook_payload_keeps_its_json_output(
    make_stdin, isolated_homes, monkeypatch, capsys
) -> None:
    repo = isolated_homes / "consumer"
    repo.mkdir()
    monkeypatch.setattr(sys, "stdin", make_stdin())

    lt_cli.main(["setup", "status", "--target", str(repo), "--brief"])

    brief = json.loads(capsys.readouterr().out)
    assert set(brief) == {"command", "brief", "suggestions"}


def test_full_status_never_reads_stdin(isolated_homes, monkeypatch, capsys) -> None:
    repo = isolated_homes / "consumer"
    repo.mkdir()
    monkeypatch.setattr(sys, "stdin", _MustNotRead(tty=False))

    lt_cli.main(["setup", "status", "--target", str(repo)])

    assert json.loads(capsys.readouterr().out)["command"] == "setup-status"


def test_status_brief_refuses_an_event_that_takes_no_context(
    isolated_homes, monkeypatch, capsys
) -> None:
    repo = isolated_homes / "consumer"
    repo.mkdir()
    argv = ["setup", "status", "--target", str(repo), "--brief"]

    monkeypatch.setattr(sys, "stdin", _piped(_payload("Stop")))
    with pytest.raises(LTValidationError, match="Stop payload"):
        lt_cli.main(argv)

    monkeypatch.setattr(sys, "stdin", _piped(_payload("Stop")))
    lt_cli.main([*argv, "--fail-silent"])
    assert capsys.readouterr().out == ""


def test_scaffolded_hooks_answer_their_own_event(
    isolated_homes, stub_client, monkeypatch, capsys
) -> None:
    """Every scaffolded hook command, fed its event's payload, adds context."""

    repo = isolated_homes / "repo"
    init_consumer_repo(repo)
    monkeypatch.chdir(repo)
    settings = json.loads((repo / ".claude" / "settings.json").read_text(encoding="utf-8"))
    event_fields = {
        "SessionStart": {"source": "startup", "model": "claude-test"},
        "UserPromptSubmit": {"prompt": "Draft the results summary", "permission_mode": "default"},
    }
    assert set(settings["hooks"]) == set(event_fields)
    for event, groups in settings["hooks"].items():
        for group in groups:
            for entry in group["hooks"]:
                program, *argv = shlex.split(entry["command"])
                assert program == "lt"
                payload = _payload(event, **event_fields[event])
                monkeypatch.setattr(sys, "stdin", _piped(payload))
                lt_cli.main(argv)
                hook = _hook_output(capsys.readouterr().out)
                assert hook["hookEventName"] == event
                assert hook["additionalContext"].startswith("lab-tracker")


def test_status_brief_hook_over_a_real_stdin_pipe(isolated_homes) -> None:
    """The SessionStart command as Claude Code runs it: a separate process."""

    repo = isolated_homes / "consumer"
    repo.mkdir()
    env = {**os.environ, "HOME": str(isolated_homes), "USERPROFILE": str(isolated_homes)}
    payload = json.dumps(_payload("SessionStart", source="startup"))
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and argv.
        [
            sys.executable,
            "-c",
            "from lab_tracker_client.cli import main; main()",
            "setup",
            "status",
            "--brief",
            "--fail-silent",
        ],
        input=payload,
        capture_output=True,
        cwd=repo,
        env=env,
        text=True,
        timeout=60,
        check=True,
    )
    hook = _hook_output(completed.stdout)
    assert hook["hookEventName"] == "SessionStart"
    assert hook["additionalContext"].startswith("lab-tracker:")
