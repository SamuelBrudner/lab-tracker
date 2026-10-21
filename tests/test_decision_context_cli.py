"""``lt decision-context``: the MCP decision-context packet for agents with no MCP server."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from lab_tracker.decision_context_constants import TASK_KIND_VALUES
from lab_tracker.mcp_tools.hints import ORIENTATION_CHECKPOINT_ACTION
from lab_tracker_client import cli as lt_cli
from lab_tracker_client.client import LabTracker, LTValidationError

PROJECT = "123e4567-e89b-12d3-a456-426614174000"
OTHER_PROJECT = "223e4567-e89b-12d3-a456-426614174000"
QUESTION = "323e4567-e89b-12d3-a456-426614174000"
_EMPTY_SECTIONS = (
    "questions",
    "notes",
    "sessions",
    "datasets",
    "analyses",
    "claims",
    "visualizations",
    "exploration_nodes",
)


def _packet(project_id: str = PROJECT, *, empty: bool = False) -> dict[str, Any]:
    sections: dict[str, Any] = {name: [] for name in _EMPTY_SECTIONS}
    if not empty:
        sections["questions"] = [{"question_id": QUESTION, "text": "Does X cause Y?"}]
    return {
        "data": {"task_kind": "plot", "scope": {"project": {"project_id": project_id}}, **sections},
        "meta": {"limit": 20},
    }


def _overview(total: int) -> dict[str, Any]:
    return {
        "data": {
            "project": {"project_id": PROJECT},
            "counts": {"questions": {"total": total}, "notes": {"total": 0}},
            "coverage": {"last_capture_at": None},
        }
    }


class _Server:
    """A MockTransport stand-in for the API that records what the CLI sent."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No connection settings from the developer's machine, and a fresh directory."""

    for name in ("LAB_TRACKER_BASE_URL", "LAB_TRACKER_ACCESS_TOKEN", "LAB_TRACKER_PROJECT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    default_project_id: str | None = None,
) -> _Server:
    server = _Server(handler)
    client = LabTracker(
        base_url="https://lab.example.org",
        access_token="lpat_test",
        default_project_id=default_project_id,
        transport=httpx.MockTransport(server),
    )
    monkeypatch.setattr(lt_cli.LabTracker, "from_env", lambda: client)
    return server


def _packet_handler(packet: dict[str, Any], overview: dict[str, Any] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/assistant/decision-context":
            return httpx.Response(200, json=packet)
        if overview is not None and request.url.path.endswith("/graph/overview"):
            return httpx.Response(200, json=overview)
        return httpx.Response(500, json={"error": {"message": "unexpected"}})

    return handler


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any] | None, str]:
    try:
        lt_cli.main(["decision-context", *argv])
        code = 0
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def test_decision_context_sends_the_request_the_mcp_tool_sends(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _serve(monkeypatch, _packet_handler(_packet()))

    code, payload, _ = _run(
        capsys,
        "--task-kind", "progress_review",
        "--query", "What did Ada do this week?",
        "--project", PROJECT.upper(),
        "--question", QUESTION,
        "--created-by", OTHER_PROJECT,
        "--since", "2026-10-01T00:00:00Z",
        "--until", "2026-10-08T00:00:00-04:00",
        "--limit", "7",
    )  # fmt: skip

    assert code == 0
    assert server.bodies("/assistant/decision-context") == [
        {
            "task_kind": "progress_review",
            "query": "What did Ada do this week?",
            "project_id": PROJECT,
            "question_id": QUESTION,
            "created_by": OTHER_PROJECT,
            "since": "2026-10-01T00:00:00+00:00",
            "until": "2026-10-08T00:00:00-04:00",
            "limit": 7,
        }
    ]
    request = server.requests[0]
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer lpat_test"
    assert request.headers["X-LabTracker-Surface"] == "cli"
    assert payload == {"command": "decision-context", **_packet()}


def test_the_project_comes_from_the_environment_then_the_checkout_then_the_profile(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(connection)], check=True)
    (connection / "lt_ids.json").write_text(json.dumps({"project_id": PROJECT}), encoding="utf-8")
    profile_project = "423e4567-e89b-12d3-a456-426614174000"

    def project_sent(*env: tuple[str, str]) -> str | None:
        for name, value in env:
            monkeypatch.setenv(name, value)
        server = _serve(
            monkeypatch, _packet_handler(_packet()), default_project_id=profile_project
        )
        assert _run(capsys, "--task-kind", "plot", "--query", "q")[0] == 0
        return server.bodies("/assistant/decision-context")[0].get("project_id")

    assert project_sent() == PROJECT
    assert project_sent(("LAB_TRACKER_PROJECT_ID", OTHER_PROJECT)) == OTHER_PROJECT
    monkeypatch.delenv("LAB_TRACKER_PROJECT_ID")
    (connection / "lt_ids.json").unlink()
    assert project_sent() == profile_project


def test_without_any_project_the_server_resolves_one(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _serve(monkeypatch, _packet_handler(_packet()))

    assert _run(capsys, "--task-kind", "summary", "--query", "q")[0] == 0

    assert "project_id" not in server.bodies("/assistant/decision-context")[0]


def test_a_malformed_bound_project_is_named_instead_of_sent(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LAB_TRACKER_PROJECT_ID", "not-a-uuid")
    server = _serve(monkeypatch, _packet_handler(_packet()))

    code, payload, err = _run(capsys, "--task-kind", "plot", "--query", "q")

    assert (code, payload) == (1, None)
    assert "'not-a-uuid' (from the environment) is not a Lab Tracker UUID" in err
    assert server.requests == []


def test_a_refusal_in_the_body_prints_the_packet_and_exits_1(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    refusal = {
        "error": {
            "code": "ambiguous_project",
            "message": "Several projects match.",
            "candidate_projects": [{"project_id": PROJECT}, {"project_id": OTHER_PROJECT}],
        }
    }
    _serve(monkeypatch, _packet_handler(refusal))

    code, payload, _ = _run(capsys, "--task-kind", "plot", "--query", "q")

    assert code == 1
    assert payload == {"command": "decision-context", **refusal}


def test_an_inaccessible_project_is_a_one_line_error(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(
        monkeypatch,
        lambda _request: httpx.Response(
            403, json={"error": {"code": "forbidden", "message": "Project access required."}}
        ),
    )

    code, payload, err = _run(capsys, "--task-kind", "plot", "--query", "q", "--project", PROJECT)

    assert (code, payload) == (1, None)
    assert err.startswith("error: ") and "Project access required." in err


@pytest.mark.parametrize(
    ("overview", "hinted"),
    [(_overview(0), True), (_overview(3), False)],
)
def test_an_empty_project_gets_the_orientation_next_action(
    connection: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overview: dict[str, Any],
    hinted: bool,
) -> None:
    server = _serve(monkeypatch, _packet_handler(_packet(empty=True), overview))

    code, payload, _ = _run(capsys, "--task-kind", "plot", "--query", "q")

    assert code == 0
    assert payload is not None
    assert [r.url.path for r in server.requests][-1] == f"/projects/{PROJECT}/graph/overview"
    action = payload.get("next_action", {}).get("action")
    assert (action == ORIENTATION_CHECKPOINT_ACTION) is hinted


def test_a_project_with_records_reads_no_overview(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _serve(monkeypatch, _packet_handler(_packet(), _overview(0)))

    code, payload, _ = _run(capsys, "--task-kind", "plot", "--query", "q")

    assert code == 0 and payload is not None and "next_action" not in payload
    assert [r.url.path for r in server.requests] == ["/assistant/decision-context"]


def test_a_failed_overview_leaves_the_packet_as_sent(
    connection: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve(monkeypatch, _packet_handler(_packet(empty=True)))

    code, payload, _ = _run(capsys, "--task-kind", "plot", "--query", "q")

    assert code == 0
    assert payload == {"command": "decision-context", **_packet(empty=True)}


@pytest.mark.parametrize(
    "argv",
    [
        ("--task-kind", "poster", "--query", "q"),
        ("--task-kind", "plot", "--query", "   "),
        ("--task-kind", "plot"),
        ("--task-kind", "plot", "--query", "q", "--project", "p-1"),
        ("--task-kind", "plot", "--query", "q", "--dataset", "../projects"),
        ("--task-kind", "plot", "--query", "q", "--since", "2026-10-01T00:00:00"),
        ("--task-kind", "plot", "--query", "q", "--limit", "0"),
        ("--task-kind", "plot", "--query", "q", "--limit", "101"),
    ],
)
def test_bad_arguments_fail_before_any_request(
    connection: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    server = _serve(monkeypatch, _packet_handler(_packet()))

    code, payload, err = _run(capsys, *argv)

    assert (code, payload) == (2, None)
    assert "lt decision-context: error:" in err
    assert server.requests == []


def test_task_kinds_are_the_servers() -> None:
    parser = lt_cli._build_parser()
    subparsers = lt_cli.argparse._SubParsersAction
    subcommands = next(action for action in parser._actions if isinstance(action, subparsers))
    command = subcommands.choices["decision-context"]
    task_kind = next(action for action in command._actions if action.dest == "task_kind")
    assert tuple(task_kind.choices) == TASK_KIND_VALUES


def test_the_client_method_validates_like_the_mcp_client() -> None:
    unreachable = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = LabTracker(base_url="https://lab.example.org", transport=unreachable)
    try:
        for kwargs, message in (
            ({"since": "2026-10-08T00:00:00Z", "until": "2026-10-01T00:00:00Z"}, "later"),
            ({"since": "yesterday"}, "timezone offset"),
            ({"question_id": "../x"}, "question_id must be a Lab Tracker UUID"),
            ({"limit": 0}, "between 1 and 100"),
            ({"query": " "}, "query must not be empty"),
        ):
            arguments = {"task_kind": "plot", "query": "q", **kwargs}
            with pytest.raises(LTValidationError, match=message):
                client.decision_context(**arguments)
    finally:
        client.close()


def test_decision_context_against_the_real_api(
    connection: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    client: Any,
    admin_auth_headers: dict[str, str],
) -> None:
    created = client.post("/projects", json={"name": "Decoding"}, headers=admin_auth_headers)
    assert created.status_code in (200, 201), created.text
    project_id = created.json()["data"]["project_id"]
    question = client.post(
        "/questions",
        json={
            "project_id": project_id,
            "text": "Does decoding track attention?",
            "question_type": "hypothesis_driven",
        },
        headers=admin_auth_headers,
    )
    assert question.status_code in (200, 201), question.text
    token = admin_auth_headers["Authorization"].split(" ", 1)[1]
    lt = LabTracker(base_url="http://testserver", access_token=token, transport=client._transport)
    monkeypatch.setattr(lt_cli.LabTracker, "from_env", lambda: lt)

    code, payload, _ = _run(
        capsys, "--task-kind", "plot", "--query", "decoding attention", "--project", project_id
    )

    assert code == 0 and payload is not None
    data = payload["data"]
    assert data["scope"]["project"]["project_id"] == project_id
    assert [item["question_id"] for item in data["questions"]] == [
        question.json()["data"]["question_id"]
    ]
