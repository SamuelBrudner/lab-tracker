"""`lt agent session-end`: the coding-agent session retrospective harvester."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import httpx
import pytest

import lab_tracker_client.agent_session as agent_session
from lab_tracker_client import LabTracker
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import watch as watch_capture

FIXTURE = Path(__file__).parent / "fixtures" / "agent_sessions" / "claude_code_session.jsonl"
SESSION_ID = "5f0c7c1e-9d2a-4e55-bb1a-3c1e8f7a0001"
# Built at runtime so no credential-shaped literal is committed.
FAKE_GH_TOKEN = "ghp_" + "Zq7" * 12
PROJECT_ID = "project-agent-1"


@pytest.fixture
def agent_env(monkeypatch, tmp_path: Path) -> Path:
    for key in (
        "LAB_TRACKER_PROJECT_ID",
        "LAB_TRACKER_WATCH_CONFIG",
        "LAB_TRACKER_WATCH_OUTBOX",
        "LAB_TRACKER_AGENT_HOOKS",
        "LAB_TRACKER_SESSION_CONTEXT",
        "LAB_TRACKER_BASE_URL",
        "LAB_TRACKER_ACCESS_TOKEN",
        "CLAUDE_PROJECT_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603, S607
    return path.resolve()


def _bind(repo: Path, project_id: str = PROJECT_ID) -> None:
    (repo / "lt_ids.json").write_text(
        json.dumps({"project_id": project_id, "project_name": "Agent project"}) + "\n",
        encoding="utf-8",
    )


def _fixture_transcript(tmp_path: Path, checkout: Path) -> Path:
    text = (
        FIXTURE.read_text(encoding="utf-8")
        .replace("__CHECKOUT__", json.dumps(str(checkout))[1:-1])
        .replace("__FAKE_GH_TOKEN__", FAKE_GH_TOKEN)
    )
    path = tmp_path / "transcripts" / f"{SESSION_ID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_jsonl(path: Path, records: list[object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join((item if isinstance(item, str) else json.dumps(item)) + "\n" for item in records),
        encoding="utf-8",
    )
    return path


def _prompt(text: str) -> dict[str, object]:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _edit(tool_id: str, file_path: str) -> list[dict[str, object]]:
    return [
        {
            "type": "assistant",
            "message": {
                "id": f"msg-{tool_id}",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Write",
                        "input": {"file_path": file_path, "content": "x"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
            },
            "toolUseResult": {"filePath": file_path},
        },
    ]


def _hook(repo: Path, transcript: Path, **extra: object) -> dict[str, object]:
    return {
        "session_id": SESSION_ID,
        "transcript_path": str(transcript),
        "cwd": str(repo),
        "hook_event_name": "SessionEnd",
        "reason": "prompt_input_exit",
        **extra,
    }


def _outbox_events(repo: Path) -> list[Path]:
    return sorted((repo / ".lab-tracker" / "outbox" / "watch").glob("*.json"))


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def test_digest_reads_prompts_files_commands_checks_and_final_message(
    agent_env: Path,
) -> None:
    checkout = _git_repo(agent_env / "analysis")
    transcript = _fixture_transcript(agent_env, checkout)

    digest = agent_session.digest_transcript(transcript, checkout=checkout, cwd=checkout)

    # Person prompts only: meta records, bare slash commands, local command
    # output, sidechain (subagent) prompts, and interrupt markers are not asks.
    assert digest.prompt_count == 3
    assert digest.prompts[0].startswith("Fix the flaky dose-response fit")
    assert FAKE_GH_TOKEN not in digest.prompts[0]
    assert "hunter22" not in digest.prompts[0]
    assert digest.prompts[1] == "/review the Hill-fit change"
    assert digest.prompts[2] == (
        "Here is the residual plot; does the fit look right now? [+1 image(s)]"
    )
    # Repo-relative paths only; an edit the tool rejected changed nothing; a
    # write outside the checkout is counted, never named.
    assert list(digest.edited_files) == ["analysis/fit.py", "results/summary.md"]
    assert digest.outside_edit_count == 1
    assert digest.edit_count == 3
    # The agent's own description beats the raw command; raw commands are redacted.
    assert digest.commands["Run the fit tests"] == 2
    assert digest.commands["Lint the repo"] == 1
    raw = next(label for label in digest.commands if label.startswith("$ "))
    assert "supersecretvalue123" not in raw
    assert "hunter2" not in raw
    assert "bob:pw" not in raw
    assert "abc123" not in raw
    assert "curl -u alice:[REDACTED]" in raw
    assert digest.command_count == 5
    # A quoted "pytest" inside a commit message is not a test run.
    assert [(check.kind, check.outcome) for check in digest.checks] == [
        ("test", "failed"),
        ("test", "passed"),
        ("lint", "passed"),
    ]
    assert digest.checks[0].summary == "1 failed, 3 passed in 0.52s"
    assert digest.checks[1].summary == "4 passed in 0.40s"
    assert digest.checks[2].summary == "All checks passed!"
    assert digest.final_message == (
        "Fixed the Hill fit: the initial guess now comes from the data range.\n\n"
        "All 4 tests pass and ruff is clean."
    )
    assert digest.malformed_lines == 1
    assert digest.model == "claude-opus-5-5"
    assert digest.client_version == "2.1.0"
    assert digest.complete is True
    assert digest.sha256 == hashlib.sha256(transcript.read_bytes()).hexdigest()


def test_digest_survives_huge_lines_malformed_lines_and_missing_fields(
    agent_env: Path,
) -> None:
    checkout = _git_repo(agent_env / "repo")
    huge_prompt = _prompt("x" * 5000)
    transcript = _write_jsonl(
        agent_env / "odd.jsonl",
        [
            huge_prompt,
            "",
            "   ",
            "{truncated",
            "[1, 2, 3]",
            "null",
            {"type": "user"},
            {"type": "user", "message": None},
            {"type": "user", "message": {"content": None}},
            {"type": "user", "message": {"content": [None, 7, {"type": "text"}]}},
            {"type": "assistant", "message": {"content": "plain string content"}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": "Edit", "id": "e1", "input": {}}]
                },
            },
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "unknown"}]},
            },
            {"type": "mystery", "message": {"content": "ignored"}},
            _prompt("the one real prompt"),
        ],
    )

    digest = agent_session.digest_transcript(
        transcript, checkout=checkout, cwd=None, max_line_bytes=1024
    )

    assert digest.oversized_lines == 1
    assert digest.malformed_lines == 3
    assert digest.prompt_count == 1
    assert digest.prompts == ["the one real prompt"]
    assert digest.edit_count == 0
    assert digest.command_count == 0
    assert digest.tool_call_count == 3
    assert digest.sha256 == hashlib.sha256(transcript.read_bytes()).hexdigest()
    assert digest.complete is True


def test_digest_stops_at_its_byte_and_time_bounds(agent_env: Path) -> None:
    transcript = _write_jsonl(
        agent_env / "long.jsonl", [_prompt(f"prompt {index}") for index in range(50)]
    )

    by_bytes = agent_session.digest_transcript(transcript, checkout=None, max_bytes=200)
    assert by_bytes.complete is False
    assert 0 < by_bytes.prompt_count < 50

    ticks = iter(range(100))
    by_time = agent_session.digest_transcript(
        transcript, checkout=None, deadline_seconds=3, clock=lambda: float(next(ticks))
    )
    assert by_time.complete is False
    assert by_time.prompt_count <= 4

    whole = agent_session.digest_transcript(transcript, checkout=None)
    assert whole.complete is True
    assert whole.prompt_count == 50


def test_prompt_final_message_and_body_bounds(agent_env: Path) -> None:
    long_prompt = "y" * (agent_session.MAX_PROMPT_CHARS - 10) + " password=abcdefghijklmnop"
    transcript = _write_jsonl(
        agent_env / "big.jsonl",
        [
            *[_prompt(long_prompt) for _ in range(60)],
            {
                "type": "assistant",
                "message": {"id": "final", "content": [{"type": "text", "text": "z" * 20000}]},
            },
        ],
    )

    digest = agent_session.digest_transcript(transcript, checkout=None)

    assert digest.prompt_count == 60
    assert all(len(prompt) <= agent_session.MAX_PROMPT_CHARS for prompt in digest.prompts)
    assert sum(map(len, digest.prompts)) <= agent_session.MAX_PROMPTS_TOTAL_CHARS
    assert len(digest.prompts) <= agent_session.MAX_PROMPTS
    # Redaction runs before the cap, so a secret straddling it never survives.
    assert not any("abcdefgh" in prompt for prompt in digest.prompts)
    assert len(digest.final_message) <= agent_session.MAX_FINAL_MESSAGE_CHARS

    facts = agent_session.SessionFacts(
        agent="claude-code",
        session_id=SESSION_ID,
        reason="other",
        repo_name="repo",
        branch="main",
        head="a" * 40,
        transcript_uri="file:///tmp/t.jsonl",
    )
    body, truncated = agent_session.render_retrospective(facts, digest)
    assert len(body) <= agent_session.MAX_BODY_CHARS
    assert body.startswith("# Agent session retrospective\n")
    assert "later prompt(s) not listed" in body
    assert truncated is False


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("curl -H 'Authorization: Bearer abcdefghijklmnop' x", "abcdefghijklmnop"),
        ("git clone https://user:pa55word@github.com/lab/repo.git", "pa55word"),
        ("git clone https://x-access-token-value@github.com/lab/repo.git", "x-access-token"),
        ("mysql --password s3cretvalue db", "s3cretvalue"),
        ("tool --api-key=xyz123secret run", "xyz123secret"),
        ("export OPENAI_API_KEY=sk-proj-abcdefghijklmnop", "abcdefghijklmnop"),
        ('{"access_token": "tok-123456"}', "tok-123456"),
        ("GITHUB_TOKEN=" + FAKE_GH_TOKEN, FAKE_GH_TOKEN),
        ("key id " + "AKIA" + "ABCDEFGHIJKLMNOP", "AKIA" + "ABCDEFGHIJKLMNOP"),
        ("lt setup connect --token lpat_abc.def123", "lpat_abc.def123"),
        ("GET /x?api_key=abc123secret&page=2", "abc123secret"),
        ("curl -u alice:hunter2 https://example.org", "hunter2"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\n-----END RSA PRIVATE KEY-----",
            "MIIEsecretbody",
        ),
        ("db password: correct-horse", "correct-horse"),
    ],
)
def test_redact_secrets_removes_credential_shapes(text: str, secret: str) -> None:
    redacted = agent_session.redact_secrets(text)
    assert secret not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "client.messages.create(max_tokens=100)",
        "token_count: 5",
        "edit src/token_store.py and tests/test_passwords.py",
        "see https://example.org/docs/page",
        "ssh://git@github.com/lab/repo.git",
    ],
)
def test_redact_secrets_leaves_ordinary_text_alone(text: str) -> None:
    assert agent_session.redact_secrets(text) == text


# ---------------------------------------------------------------------------
# Session-end capture
# ---------------------------------------------------------------------------


def test_session_end_queues_one_staged_retrospective_that_requests_a_draft(
    agent_env: Path,
) -> None:
    repo = _git_repo(agent_env / "analysis")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)

    payload = agent_session.capture_session_end(
        _hook(repo, transcript), agent_session.SessionEndOptions(sync=False)
    )

    assert payload["action"] == "queued"
    assert payload["project_id"] == PROJECT_ID
    assert payload["project_source"] == "checkout"
    events = _outbox_events(repo)
    assert [Path(payload["event_path"])] == events
    event = watch_capture.read_event(events[0])
    assert event["capture_kind"] == "agent_session_retrospective"
    assert event["adapter"] == "lt-agent-session"
    assert event["sink"] == watch_capture.SINK_STAGED_NOTE
    assert event["context"]["project_id"] == PROJECT_ID
    assert event["payload"]["request_draft"] is True
    assert event["payload"]["status"] == "staged"
    # The transcript is a pointer (hash + URI), never an uploaded file.
    assert "path" not in event["source"]
    assert event["source"]["external_id"] == f"agent-session:claude-code:{SESSION_ID}"
    body = event["payload"]["body"]
    assert body.startswith("# Agent session retrospective\n")
    for section in (
        "## What the person asked",
        "## Files the agent wrote or edited",
        "## Commands the agent ran",
        "## Tests and linters",
        "## Final agent message",
    ):
        assert section in body
    assert "`analysis/fit.py` (Edit)" in body
    assert "test: Run the fit tests — **failed**" in body
    assert "1 file(s) outside this checkout" in body
    for leaked in (FAKE_GH_TOKEN, "hunter2", "supersecretvalue123", "import numpy", "iVBORw0"):
        assert leaked not in body
    metadata = event["payload"]["metadata"]
    assert metadata["agent_name"] == "claude-code"
    assert metadata["agent_session_id"] == SESSION_ID
    assert (
        metadata["agent_transcript_sha256"] == hashlib.sha256(transcript.read_bytes()).hexdigest()
    )
    assert metadata["agent_prompt_count"] == 3
    assert metadata["agent_files_edited_count"] == 3
    assert metadata["agent_check_count"] == 3
    assert metadata["agent_checks_failed_count"] == 1
    assert metadata["agent_session_end_reason"] == "prompt_input_exit"
    assert all(isinstance(value, (str, int, float, bool)) for value in metadata.values())

    # Same session, same transcript: nothing new is queued.
    again = agent_session.capture_session_end(
        _hook(repo, transcript), agent_session.SessionEndOptions(sync=False)
    )
    assert again["action"] == "already-queued"
    assert _outbox_events(repo) == events


def test_session_end_skips_an_unbound_checkout_without_queueing(agent_env: Path, capsys) -> None:
    repo = _git_repo(agent_env / "unbound")
    transcript = _fixture_transcript(agent_env, repo)
    # A watch-config project alone is not a binding for an agent hook.
    (repo / ".lab-tracker").mkdir()
    (repo / ".lab-tracker" / "watch.json").write_text(
        json.dumps({"version": 1, "outbox": ".lab-tracker/outbox/watch", "project_id": "p"}),
        encoding="utf-8",
    )

    payload = agent_session.capture_session_end(
        _hook(repo, transcript), agent_session.SessionEndOptions(sync=False)
    )

    assert payload["action"] == "skipped"
    assert payload["reason"] == "unbound"
    assert _outbox_events(repo) == []
    err = capsys.readouterr().err
    assert err.count("agent session retrospective skipped") == 1
    assert "lt project bind" in err
    assert "Nothing was sent or queued" in err


def test_session_end_binds_through_env_or_explicit_project(agent_env: Path, monkeypatch) -> None:
    repo = _git_repo(agent_env / "env-bound")
    transcript = _fixture_transcript(agent_env, repo)
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "project-from-env")

    payload = agent_session.capture_session_end(
        _hook(repo, transcript), agent_session.SessionEndOptions(sync=False)
    )
    assert payload["project_id"] == "project-from-env"

    explicit = agent_session.capture_session_end(
        _hook(repo, transcript),
        agent_session.SessionEndOptions(sync=False, project_id="project-explicit", agent="x"),
    )
    assert explicit["project_id"] == "project-explicit"


def test_session_end_skips_trivially_short_sessions(agent_env: Path) -> None:
    repo = _git_repo(agent_env / "short")
    _bind(repo)
    chat = _write_jsonl(agent_env / "chat.jsonl", [_prompt("what is a Hill fit?"), _prompt("ok")])
    hook = _hook(repo, chat)

    skipped = agent_session.capture_session_end(hook, agent_session.SessionEndOptions(sync=False))
    assert skipped["reason"] == "trivial_session"
    assert skipped["counts"]["prompts"] == 2
    assert _outbox_events(repo) == []

    lowered = agent_session.capture_session_end(
        hook, agent_session.SessionEndOptions(sync=False, min_prompts=2)
    )
    assert lowered["action"] == "queued"

    one_edit = _write_jsonl(
        agent_env / "edit.jsonl",
        [_prompt("write the summary"), *_edit("w1", str(repo / "notes.md"))],
    )
    edited = agent_session.capture_session_end(
        _hook(repo, one_edit), agent_session.SessionEndOptions(sync=False)
    )
    assert edited["action"] == "queued"


def test_session_end_skip_reasons(agent_env: Path, monkeypatch) -> None:
    repo = _git_repo(agent_env / "reasons")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)
    options = agent_session.SessionEndOptions(sync=False)

    stop = agent_session.capture_session_end(
        _hook(repo, transcript, hook_event_name="Stop"), options
    )
    assert stop["reason"] == "not_session_end"
    missing = agent_session.capture_session_end(_hook(repo, agent_env / "gone.jsonl"), options)
    assert missing["reason"] == "no_transcript"
    no_session = agent_session.capture_session_end({"hook_event_name": "SessionEnd"}, options)
    assert no_session["reason"] == "no_session"
    loose = agent_env / "loose"
    loose.mkdir()
    outside = agent_session.capture_session_end(_hook(loose, transcript), options)
    assert outside["reason"] == "no_checkout"
    monkeypatch.setenv("LAB_TRACKER_AGENT_HOOKS", "0")
    disabled = agent_session.capture_session_end(_hook(repo, transcript), options)
    assert disabled["reason"] == "disabled"
    assert _outbox_events(repo) == []


def test_session_end_prefers_the_agent_project_dir_over_a_worktree_cwd(
    agent_env: Path, monkeypatch
) -> None:
    repo = _git_repo(agent_env / "project")
    _bind(repo)
    worktree = _git_repo(agent_env / "elsewhere")
    transcript = _fixture_transcript(agent_env, repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))

    payload = agent_session.capture_session_end(
        _hook(worktree, transcript), agent_session.SessionEndOptions(sync=False)
    )

    assert payload["action"] == "queued"
    assert payload["repo"] == str(repo)


def test_session_end_uses_the_hooks_last_assistant_message(agent_env: Path) -> None:
    repo = _git_repo(agent_env / "last")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)

    payload = agent_session.capture_session_end(
        _hook(repo, transcript, last_assistant_message="Done. token=abcdef123456"),
        agent_session.SessionEndOptions(dry_run=True),
    )

    assert payload["action"] == "would-queue"
    assert "> Done. token=[REDACTED]" in payload["body"]
    assert "abcdef123456" not in payload["body"]
    assert _outbox_events(repo) == []


def _recording_client(requests: list[httpx.Request], *, fail_health: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/health":
            if fail_health:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "GET" and path == "/notes":
            return httpx.Response(
                200, json={"data": [], "meta": {"limit": 200, "offset": 0, "total": 0}}
            )
        if request.method == "POST" and path == "/notes/upload-file":
            return httpx.Response(201, json={"data": {"note_id": "note-retro", "metadata": {}}})
        if request.method == "POST" and path == "/notes/note-retro/analysis-graph-drafts":
            return httpx.Response(201, json={"data": {"change_set_id": "draft-retro"}})
        return httpx.Response(500, json={"error": {"message": f"unexpected {path}"}})

    return lambda: LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler))


def test_session_end_drains_the_outbox_and_requests_the_draft(agent_env: Path) -> None:
    repo = _git_repo(agent_env / "synced")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)
    requests: list[httpx.Request] = []

    payload = agent_session.capture_session_end(
        _hook(repo, transcript),
        agent_session.SessionEndOptions(),
        client_factory=_recording_client(requests),
    )

    assert payload["sync"]["errors"] == []
    assert [request.url.path for request in requests] == [
        "/health",
        "/notes",
        "/notes/upload-file",
        "/notes/note-retro/analysis-graph-drafts",
    ]
    upload = requests[2].content
    assert b"Agent session retrospective" in upload
    assert b"agent_session_id" in upload
    assert b"import numpy" not in upload
    synced = watch_capture.read_event(_outbox_events(repo)[0])
    assert synced["sync"]["status"] == "synced"
    assert synced["sync"]["change_set_id"] == "draft-retro"


def test_session_end_keeps_the_event_queued_when_the_server_is_unreachable(
    agent_env: Path, capsys
) -> None:
    repo = _git_repo(agent_env / "offline")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)
    requests: list[httpx.Request] = []

    payload = agent_session.capture_session_end(
        _hook(repo, transcript),
        agent_session.SessionEndOptions(),
        client_factory=_recording_client(requests, fail_health=True),
    )

    assert payload["action"] == "queued"
    assert "sync_error" in payload
    # One probe, not one timeout per queued event.
    assert [request.url.path for request in requests] == ["/health"]
    event = watch_capture.read_event(_outbox_events(repo)[0])
    assert event["sync"]["status"] == "pending"
    assert capsys.readouterr().err.count("`lt outbox sync` retries") == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_lt_agent_session_end_reads_hook_stdin_and_keeps_stdout_empty(
    agent_env: Path, monkeypatch, capsys
) -> None:
    repo = _git_repo(agent_env / "cli")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_hook(repo, transcript))))

    lt_cli.main(["agent", "session-end", "--fail-silent", "--no-sync"])

    # Claude Code parses a hook's JSON stdout as hook control output.
    assert capsys.readouterr().out == ""
    assert len(_outbox_events(repo)) == 1


def test_lt_agent_session_end_dry_run_prints_the_packet_and_writes_nothing(
    agent_env: Path, monkeypatch, capsys
) -> None:
    repo = _git_repo(agent_env / "preview")
    _bind(repo)
    transcript = _fixture_transcript(agent_env, repo)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_hook(repo, transcript))))

    lt_cli.main(["agent", "session-end", "--dry-run"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "agent-session-end"
    assert payload["action"] == "would-queue"
    assert payload["body"].startswith("# Agent session retrospective")
    assert _outbox_events(repo) == []


def test_lt_agent_session_end_fail_silent_swallows_everything(
    agent_env: Path, monkeypatch, capsys
) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_session, "capture_session_end", explode)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

    lt_cli.main(["agent", "session-end", "--fail-silent"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "boom" not in captured.err


def test_read_hook_input_salvages_fields_from_oversized_payloads() -> None:
    head = json.dumps(
        {
            "session_id": "s-1",
            "cwd": "/work/repo",
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": "/work/repo/results/a.csv", "content": "x" * 5000},
        }
    )

    parsed = agent_session.read_hook_input(io.StringIO(head), max_chars=200)

    assert parsed["session_id"] == "s-1"
    assert parsed["hook_event_name"] == "PostToolUse"
    assert parsed["tool_input"] == {"file_path": "/work/repo/results/a.csv"}
    assert agent_session.read_hook_input(io.StringIO("not json")) == {}
    assert agent_session.read_hook_input(io.StringIO("[1]")) == {}
    assert agent_session.read_hook_input(io.StringIO("")) == {}
