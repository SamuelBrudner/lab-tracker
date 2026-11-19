import json
import sys
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from lab_tracker_client import cli
from lab_tracker_client.client import MAX_PAGE_SIZE, LabTracker

PROJECT = str(UUID(int=1))
SESSION = str(UUID(int=2))
QUESTION = str(UUID(int=3))
INSTANCE = "https://lab.example"


def _question(identifier: str, text: str) -> dict[str, str]:
    return {"question_id": identifier, "project_id": PROJECT, "text": text, "status": "active"}


def _session() -> dict[str, str]:
    return {
        "session_id": SESSION,
        "project_id": PROJECT,
        "primary_question_id": QUESTION,
        "started_at": "2026-11-18T14:00:00Z",
        "session_type": "scientific",
        "status": "active",
    }


@pytest.fixture
def local_rig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAB_TRACKER_CONFIG_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("LAB_TRACKER_BASE_URL", INSTANCE)
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", PROJECT)
    monkeypatch.delenv("LAB_TRACKER_SESSION_ID", raising=False)
    monkeypatch.delenv("LAB_TRACKER_WATCH_OUTBOX", raising=False)
    return tmp_path


def _server(monkeypatch, questions, sessions):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        offset, limit = int(params["offset"]), int(params["limit"])
        requests.append((request.url.path, offset))
        assert request.method == "GET"
        assert params["project_id"] == PROJECT and params["status"] == "active"
        rows = questions if request.url.path == "/questions" else sessions
        if rows is None:
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(
            200,
            json={
                "data": rows[offset : offset + limit],
                "meta": {"total": len(rows)},
            },
        )

    monkeypatch.setattr(
        LabTracker,
        "from_env",
        lambda: LabTracker(
            base_url=INSTANCE,
            transport=httpx.MockTransport(handler),
        ),
    )
    return requests


def _disconnect(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("An offline command constructed a network client")

    monkeypatch.setattr(LabTracker, "from_env", forbidden)


def test_pull_then_list_and_record_a_session_linked_run_offline(local_rig, monkeypatch, capsys):
    questions = [_question(str(UUID(int=10 + n)), f"Question {n}") for n in range(MAX_PAGE_SIZE)]
    questions.append(_question(QUESTION, "Does odor order change the response?"))
    requests = _server(monkeypatch, questions, [_session()])

    cli.main(["pull", "--project", PROJECT])
    summary = json.loads(capsys.readouterr().out)
    assert summary["active_questions"] == MAX_PAGE_SIZE + 1
    assert summary["open_sessions"] == 1 and summary["pulled_at"]
    assert requests == [("/questions", 0), ("/questions", MAX_PAGE_SIZE), ("/sessions", 0)]
    assert not (local_rig / ".lab-tracker" / "outbox").exists()

    _disconnect(monkeypatch)
    cli.main(["list-questions", "--project", PROJECT, "--offline", "--search", "ODOR"])
    assert json.loads(capsys.readouterr().out) == [questions[-1]]
    cli.main(["session", "list", "--project", PROJECT])
    [session] = json.loads(capsys.readouterr().out)
    assert session["primary_question_id"] == QUESTION
    cli.main(["session", "use", session["link_code"], "--offline", "--project", PROJECT])
    assert json.loads(capsys.readouterr().out)["session_id"] == SESSION

    with pytest.raises(SystemExit) as completed:
        cli.main(
            [
                "run",
                "--no-drain",
                "--project",
                PROJECT,
                "--question",
                QUESTION,
                "--",
                sys.executable,
                "-c",
                "pass",
            ]
        )
    assert completed.value.code == 0
    [path] = (Path(".lab-tracker") / "outbox" / "watch").glob("*.json")
    event = json.loads(path.read_text())
    assert event["context"]["project_id"] == PROJECT
    assert event["context"]["session_id"] == SESSION
    assert event["context"]["question_id"] == QUESTION
    assert event["sync"]["status"] == "pending"


def test_failed_pull_preserves_snapshot_and_other_instance_cannot_read_it(
    local_rig,
    monkeypatch,
    capsys,
):
    _server(monkeypatch, [_question(QUESTION, "Original question")], [_session()])
    cli.main(["pull"])
    capsys.readouterr()
    _server(monkeypatch, [_question(QUESTION, "Changed question")], None)
    with pytest.raises(SystemExit) as failed:
        cli.main(["pull"])
    assert failed.value.code == 1
    assert "Offline commands can use the last successful pull" in capsys.readouterr().err

    _disconnect(monkeypatch)
    list_questions = ["list-questions", "--offline", "--project", PROJECT]
    cli.main(list_questions)
    assert json.loads(capsys.readouterr().out)[0]["text"] == "Original question"

    monkeypatch.setenv("LAB_TRACKER_BASE_URL", "https://another.example")
    with pytest.raises(SystemExit) as absent:
        cli.main(list_questions)
    assert absent.value.code == 1
    assert "No local pull for this instance and project" in capsys.readouterr().err


def test_successful_pull_replaces_removed_questions_and_sessions(local_rig, monkeypatch, capsys):
    _server(monkeypatch, [_question(QUESTION, "Old question")], [_session()])
    cli.main(["pull"])
    capsys.readouterr()
    _server(monkeypatch, [], [])
    cli.main(["pull"])
    capsys.readouterr()

    _disconnect(monkeypatch)
    cli.main(["list-questions", "--offline", "--project", PROJECT])
    assert json.loads(capsys.readouterr().out) == []
    with pytest.raises(SystemExit) as stale:
        cli.main(["session", "use", SESSION, "--offline"])
    assert stale.value.code == 1
    assert "Session is absent from the local pull" in capsys.readouterr().err
    assert not (local_rig / ".lab-tracker" / "session.json").exists()
