"""Coverage is derived, never stored: skipped review shows up as numbers, not silence."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, update

from lab_tracker import capture_client_release
from lab_tracker.coverage_query import (
    CAPTURE_SOURCE_LISTING_LIMIT,
    capture_source_is_quiet,
    is_scheduled_capture_adapter,
    unreviewed_capture_counts_by_project,
)
from lab_tracker.db_models import GraphChangeOperationModel, GraphChangeSetModel, NoteModel
from lab_tracker.models import (
    QUIET_CAPTURE_WINDOW_DAYS,
    RECENT_CAPTURE_DAYS,
    ProjectCoverageReport,
    ProjectCoverageSummary,
)


def _project(client: TestClient, headers: dict[str, str], name: str = "Coverage project") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201
    return response.json()["data"]["project_id"]


def _note(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    raw_content: str,
    *,
    metadata: dict[str, str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"project_id": project_id, "raw_content": raw_content}
    if metadata is not None:
        payload["metadata"] = metadata
    response = client.post("/notes", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _timestamp(raw: object) -> datetime:
    assert isinstance(raw, str)
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _change_set(
    project_id: str,
    *,
    status: str,
    source_note_id: str,
    source_note_ids: list[str],
    clarification_requests: list[str] | None = None,
) -> GraphChangeSetModel:
    return GraphChangeSetModel(
        project_id=project_id,
        source_note_id=source_note_id,
        source_note_ids=source_note_ids,
        provider="openai",
        model="fake-gpt",
        prompt_version="coverage-test-v1",
        status=status,
        clarification_requests=clarification_requests or [],
    )


def _operation(
    change_set_id: object,
    *,
    sequence: int,
    status: str,
    source_note_ids: list[str],
) -> GraphChangeOperationModel:
    return GraphChangeOperationModel(
        change_set_id=change_set_id,
        sequence=sequence,
        op="create",
        entity_type="question",
        semantic_type="suggest_new_question",
        payload={"text": f"Operation {sequence}"},
        status=status,
        source_refs=[{"source_note_ids": source_note_ids}],
    )


def test_project_coverage_models_reject_negative_counts() -> None:
    with pytest.raises(ValidationError):
        ProjectCoverageSummary(
            project_id=uuid4(),
            unreviewed_count=-1,
            unplaced_count=0,
            archived_unreviewed_count=0,
            pending_change_sets=0,
            open_clarification_requests=0,
        )

    report = ProjectCoverageReport(
        project_id=uuid4(),
        unreviewed_count=0,
        unplaced_count=0,
        archived_unreviewed_count=0,
        pending_change_sets=0,
        open_clarification_requests=0,
    )
    assert report.capture_sources == []
    assert report.capture_sources_truncated is False
    assert report.server_release.version is None
    assert report.oldest_unreviewed_at is None
    assert report.last_capture_at is None
    assert report.recent_days == RECENT_CAPTURE_DAYS
    assert report.quiet_window_days == QUIET_CAPTURE_WINDOW_DAYS
    assert report.quiet_source_count == 0


def test_capture_source_is_quiet_only_for_scheduled_sources_inside_the_quiet_window() -> None:
    now = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    stalled = now - timedelta(days=RECENT_CAPTURE_DAYS + 1)
    retired = now - timedelta(days=QUIET_CAPTURE_WINDOW_DAYS + 1)
    alive = now - timedelta(days=RECENT_CAPTURE_DAYS - 1)

    assert capture_source_is_quiet(scheduled=True, last_capture_at=stalled, now=now)
    assert not capture_source_is_quiet(scheduled=True, last_capture_at=alive, now=now)
    assert not capture_source_is_quiet(scheduled=True, last_capture_at=retired, now=now)
    assert not capture_source_is_quiet(scheduled=False, last_capture_at=stalled, now=now)


@pytest.mark.parametrize(
    ("adapter", "scheduled"),
    [
        ("lt-watch", True),
        ("lt-watch-files", True),
        ("lt-watch-acquisition", True),
        ("lt-watch-manifest", True),
        ("lt-hpc", True),
        ("lab-tracker-client-figure", False),
        ("lt-import-folder", False),
        ("lt-git-snapshot", False),
        ("lt-repo", False),
        ("mobile_capture", False),
        (None, False),
    ],
)
def test_only_the_watch_family_and_hpc_are_scheduled(adapter: str | None, scheduled: bool) -> None:
    assert is_scheduled_capture_adapter(adapter) is scheduled


def test_project_coverage_derives_unreviewed_unplaced_and_archived_counts(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    notes = [
        _note(client, admin_auth_headers, project_id, f"Capture {index}") for index in range(1, 9)
    ]
    n1, n2, n3, n4, n5, n6, n7, n8 = (str(note["note_id"]) for note in notes)

    with client.app.state.db_session_factory() as session:
        committed = _change_set(
            project_id, status="committed", source_note_id=n1, source_note_ids=[n1, n2]
        )
        session.add(committed)
        session.flush()
        session.add(
            _operation(committed.change_set_id, sequence=1, status="applied", source_note_ids=[n1])
        )
        session.add(
            _operation(committed.change_set_id, sequence=2, status="rejected", source_note_ids=[n2])
        )
        session.add(
            _change_set(project_id, status="rejected", source_note_id=n3, source_note_ids=[n3])
        )
        session.add(
            _change_set(project_id, status="failed", source_note_id=n7, source_note_ids=[])
        )
        session.add(
            _change_set(
                project_id,
                status="submitted",
                source_note_id=n4,
                source_note_ids=[n4],
                clarification_requests=["Which rig?", "Which day?"],
            )
        )
        session.commit()

    assert client.post(f"/notes/{n5}/archive", headers=admin_auth_headers).status_code == 200
    assert (
        client.post(
            f"/notes/{n6}/archive",
            json={"reason": "reviewed_not_relevant"},
            headers=admin_auth_headers,
        ).status_code
        == 200
    )

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["project_id"] == project_id
    # N4 waits on a person, N7's draft failed, N8 was never drafted: none was reviewed.
    assert data["unreviewed_count"] == 3
    assert _timestamp(data["oldest_unreviewed_at"]) == min(
        _timestamp(notes[3]["created_at"]),
        _timestamp(notes[6]["created_at"]),
        _timestamp(notes[7]["created_at"]),
    )
    # N2 was absorbed by the committed draft but no applied operation cites it.
    assert data["unplaced_count"] == 1
    assert data["archived_unreviewed_count"] == 1
    assert data["pending_change_sets"] == 1
    assert data["open_clarification_requests"] == 2
    assert _timestamp(data["last_capture_at"]) == max(
        _timestamp(note["created_at"]) for note in notes
    )
    assert data["capture_sources_truncated"] is False
    assert [source["note_count"] for source in data["capture_sources"]] == [8]


def test_project_coverage_lists_capture_sources_last_seen(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    manual = _note(client, admin_auth_headers, project_id, "Typed by hand")
    provider_only = _note(
        client,
        admin_auth_headers,
        project_id,
        "Provider only",
        metadata={"evidence_source_provider": "git"},
    )
    rig_metadata = {
        "evidence_source_provider": "git",
        "evidence_adapter": "lt-repo",
        "capture_install_id": "A",
        "capture_host_label": "rig-1",
    }
    _note(client, admin_auth_headers, project_id, "Rig capture 1", metadata=rig_metadata)
    rig_latest = _note(
        client, admin_auth_headers, project_id, "Rig capture 2", metadata=rig_metadata
    )

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["capture_sources_truncated"] is False
    sources = data["capture_sources"]
    assert [
        (
            source["evidence_source_provider"],
            source["evidence_adapter"],
            source["capture_install_id"],
            source["capture_host_label"],
            source["note_count"],
        )
        for source in sources
    ] == [
        ("git", "lt-repo", "A", "rig-1", 2),
        ("git", None, None, None, 1),
        (None, None, None, None, 1),
    ]
    # Only the install-stamped source can be judged: it carries an install id
    # but no client release, so its client predates release reporting.
    assert [source["release_status"] for source in sources] == ["behind", "unknown", "unknown"]
    assert sources[0]["update_notice"].startswith("lab-tracker on `rig-1` predates release")
    assert [source["update_notice"] for source in sources[1:]] == [None, None]
    assert _timestamp(sources[0]["last_capture_at"]) == _timestamp(rig_latest["created_at"])
    assert _timestamp(sources[1]["last_capture_at"]) == _timestamp(provider_only["created_at"])
    assert _timestamp(sources[2]["last_capture_at"]) == _timestamp(manual["created_at"])
    assert _timestamp(data["last_capture_at"]) == _timestamp(rig_latest["created_at"])


def _assert_capture_health(client: TestClient, headers: dict[str, str]) -> None:
    """Capture health rides on coverage: a scheduler that stopped shows up as a
    quiet source instead of an emptier review queue, while human-paced
    sources are never flagged however long they are silent."""

    project_id = _project(client, headers)
    rig = {"evidence_adapter": "lt-watch-files", "capture_host_label": "rig-2"}
    figures = {"evidence_adapter": FIGURE_ADAPTER, "capture_host_label": "rig-2"}
    stalled_capture = _note(client, headers, project_id, "Old watch", metadata=rig)
    _backdate(client, str(stalled_capture["note_id"]), days=RECENT_CAPTURE_DAYS + 3)
    reviewed_figure = _note(client, headers, project_id, "Reviewed figure", metadata=figures)
    _note(client, headers, project_id, "Fresh figure", metadata=figures)
    old_typed = _note(client, headers, project_id, "Typed long ago")
    _backdate(client, str(old_typed["note_id"]), days=RECENT_CAPTURE_DAYS + 3)
    for adapter in HUMAN_PACED_ADAPTERS:
        idle = _note(
            client,
            headers,
            project_id,
            f"Idle {adapter}",
            metadata={"evidence_adapter": adapter, "capture_host_label": "laptop"},
        )
        _backdate(client, str(idle["note_id"]), days=RECENT_CAPTURE_DAYS + 3)
    stalled_hpc = _note(
        client,
        headers,
        project_id,
        "Stalled cluster",
        metadata={"evidence_adapter": "lt-hpc", "capture_host_label": "cluster-a"},
    )
    _backdate(client, str(stalled_hpc["note_id"]), days=RECENT_CAPTURE_DAYS + 2)
    retired = _note(
        client,
        headers,
        project_id,
        "Retired rig",
        metadata={"evidence_adapter": "lt-hpc", "capture_host_label": "cluster"},
    )
    _backdate(client, str(retired["note_id"]), days=QUIET_CAPTURE_WINDOW_DAYS + 5)
    with client.app.state.db_session_factory() as session:
        reviewed_id = str(reviewed_figure["note_id"])
        session.add(
            _change_set(
                project_id,
                status="committed",
                source_note_id=reviewed_id,
                source_note_ids=[reviewed_id],
            )
        )
        session.commit()

    response = client.get(f"/projects/{project_id}/coverage", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["recent_days"] == RECENT_CAPTURE_DAYS
    assert data["quiet_window_days"] == QUIET_CAPTURE_WINDOW_DAYS
    assert data["capture_sources_truncated"] is False
    by_key = {
        (source["evidence_adapter"], source["capture_host_label"]): source
        for source in data["capture_sources"]
    }
    assert by_key[(FIGURE_ADAPTER, "rig-2")] == {
        **by_key[(FIGURE_ADAPTER, "rig-2")],
        "note_count": 2,
        "recent_note_count": 2,
        "staged_unreviewed_count": 1,
        "quiet": False,
    }
    assert by_key[("lt-watch-files", "rig-2")] == {
        **by_key[("lt-watch-files", "rig-2")],
        "note_count": 1,
        "recent_note_count": 0,
        "staged_unreviewed_count": 1,
        "quiet": True,
    }
    assert by_key[("lt-hpc", "cluster-a")]["quiet"] is True
    # Silent for longer than the quiet window: retired, not stalled.
    assert by_key[("lt-hpc", "cluster")]["quiet"] is False
    assert by_key[("lt-hpc", "cluster")]["recent_note_count"] == 0
    # Typed notes and human-paced adapters are never flagged, however old.
    assert by_key[(None, None)]["quiet"] is False
    assert by_key[(None, None)]["staged_unreviewed_count"] == 1
    for adapter in HUMAN_PACED_ADAPTERS:
        assert by_key[(adapter, "laptop")]["quiet"] is False, adapter
    assert data["quiet_source_count"] == 2
    # Every staged note nobody reviewed is attributed to exactly one source.
    assert (
        sum(source["staged_unreviewed_count"] for source in data["capture_sources"])
        == data["unreviewed_count"]
    )


def test_project_coverage_flags_quiet_scheduled_sources_and_counts_recent_unreviewed(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _assert_capture_health(client, admin_auth_headers)


@pytest.mark.postgres
def test_project_coverage_flags_quiet_scheduled_sources_on_postgres(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    _assert_capture_health(postgres_client, postgres_admin_auth_headers)


def _assert_quiet_count_covers_sources_past_the_listing_bound(
    client: TestClient, headers: dict[str, str]
) -> None:
    project_id = _project(client, headers)
    stalled = _note(
        client,
        headers,
        project_id,
        "Stalled watch",
        metadata={"evidence_adapter": "lt-watch-files", "capture_install_id": "stalled"},
    )
    _backdate(client, str(stalled["note_id"]), days=RECENT_CAPTURE_DAYS + 3)
    for index in range(CAPTURE_SOURCE_LISTING_LIMIT):
        _note(
            client,
            headers,
            project_id,
            f"Live watch {index}",
            metadata={"evidence_adapter": "lt-watch-files", "capture_install_id": f"i{index:03d}"},
        )

    response = client.get(f"/projects/{project_id}/coverage", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["capture_sources_truncated"] is True
    listed = {source["capture_install_id"] for source in data["capture_sources"]}
    assert "stalled" not in listed
    # The stall the count exists to show is the first source cut from the
    # listing, so the count is taken over every source.
    assert data["quiet_source_count"] == 1


def test_project_coverage_quiet_count_covers_sources_past_the_listing_bound(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    _assert_quiet_count_covers_sources_past_the_listing_bound(client, admin_auth_headers)


@pytest.mark.postgres
def test_project_coverage_quiet_count_covers_sources_past_the_bound_on_postgres(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
) -> None:
    _assert_quiet_count_covers_sources_past_the_listing_bound(
        postgres_client, postgres_admin_auth_headers
    )


def test_quiet_and_behind_are_separate_judgements_on_one_install(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quiet and behind are separate judgements, each made per source.

    A stalled watcher on an old release is quiet and behind, and because it
    captured inside the quiet window it is told to update; the newer figure
    environment on the same install is current and is not. A machine silent
    past the quiet window is retired: neither quiet nor addressed.
    """

    monkeypatch.setattr(capture_client_release, "installed_version", lambda: "0.5.0")
    project_id = _project(client, admin_auth_headers, "Quiet and behind")
    watch = _note(
        client,
        admin_auth_headers,
        project_id,
        "old watch",
        metadata=_watch_metadata(INSTALL_A, "rig-7", "0.3.0"),
    )
    _backdate(client, str(watch["note_id"]), days=RECENT_CAPTURE_DAYS + 3)
    _note(
        client,
        admin_auth_headers,
        project_id,
        "new figure",
        metadata=_host(INSTALL_A, "rig-7", "0.5.0", adapter=FIGURE_ADAPTER),
    )
    retired = _note(
        client,
        admin_auth_headers,
        project_id,
        "retired watch",
        metadata=_watch_metadata(INSTALL_B, "rig-8", "0.3.0"),
    )
    _backdate(client, str(retired["note_id"]), days=QUIET_CAPTURE_WINDOW_DAYS + 1)

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    sources = {
        (source["evidence_adapter"], source["capture_host_label"]): source
        for source in response.json()["data"]["capture_sources"]
    }
    stalled_watch = sources[("lt-watch", "rig-7")]
    figure = sources[(FIGURE_ADAPTER, "rig-7")]
    old_machine = sources[("lt-watch", "rig-8")]
    assert (stalled_watch["quiet"], stalled_watch["release_status"]) == (True, "behind")
    assert stalled_watch["update_notice"].startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind"
    )
    assert (figure["quiet"], figure["release_status"]) == (False, "current")
    assert figure["update_notice"] is None
    # Past the quiet window: retired, so neither quiet nor addressed.
    assert (old_machine["quiet"], old_machine["release_status"]) == (False, "behind")
    assert old_machine["update_notice"] is None


def test_project_coverage_capture_sources_are_bounded(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)
    for index in range(CAPTURE_SOURCE_LISTING_LIMIT + 1):
        _note(
            client,
            admin_auth_headers,
            project_id,
            f"Install {index}",
            metadata={"capture_install_id": f"install-{index:03d}"},
        )

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data["capture_sources"]) == CAPTURE_SOURCE_LISTING_LIMIT
    assert data["capture_sources_truncated"] is True
    assert data["unreviewed_count"] == CAPTURE_SOURCE_LISTING_LIMIT + 1


def test_unreviewed_capture_counts_by_project_batches_projects(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    first = _project(client, admin_auth_headers, "First")
    second = _project(client, admin_auth_headers, "Second")
    empty = _project(client, admin_auth_headers, "Empty")
    for index in range(2):
        _note(client, admin_auth_headers, first, f"First {index}")
    _note(client, admin_auth_headers, second, "Second 0")

    statements = 0

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        nonlocal statements
        statements += 1

    with client.app.state.db_session_factory() as session:
        counts = unreviewed_capture_counts_by_project(session, [first, second, empty])
        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            nothing = unreviewed_capture_counts_by_project(session, [])
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert isinstance(counts, defaultdict)
    assert dict(counts) == {first: 2, second: 1}
    assert counts[empty] == 0
    assert dict(nothing) == {}
    assert statements == 0


def test_project_coverage_requires_project_read_and_records_view_usage(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    scoped_project_member,
) -> None:
    client.app.state.settings.usage_events = True
    visible = scoped_project_member.visible_project_id
    hidden = scoped_project_member.hidden_project_id
    member_headers = scoped_project_member.member_headers

    authorized = client.get(f"/projects/{visible}/coverage", headers=member_headers)
    assert authorized.status_code == 200, authorized.text
    assert authorized.json()["data"]["project_id"] == visible

    denied = client.get(f"/projects/{hidden}/coverage", headers=member_headers)
    missing = client.get(f"/projects/{uuid4()}/coverage", headers=member_headers)
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    assert denied.json()["error"]["message"] == "Project does not exist."

    export = client.get(
        "/usage-events/export",
        params={"format": "jsonl"},
        headers=admin_auth_headers,
    )
    assert export.status_code == 200
    rows = [json.loads(line) for line in export.text.splitlines() if line.strip()]
    views = [row for row in rows if row["verb"] == "view" and row["resource_type"] == "project"]
    assert [row["resource_id"] for row in views] == [visible]


def test_project_coverage_rejects_unauthenticated(
    client: TestClient,
    admin_auth_headers: dict[str, str],
) -> None:
    project_id = _project(client, admin_auth_headers)

    response = client.get(f"/projects/{project_id}/coverage")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_error"


INSTALL_A = "a" * 32
INSTALL_B = "b" * 32
INSTALL_C = "c" * 32
INSTALL_D = "d" * 32
INSTALL_E = "e" * 32
FIGURE_ADAPTER = "lab-tracker-client-figure"
# Sources that deliver when a person acts, so their silence is never a stall.
HUMAN_PACED_ADAPTERS = (FIGURE_ADAPTER, "lt-import-folder", "lt-git-snapshot", "lt-repo")
FLY_URI = "file:///Users/sam/data/fly_walking_data/run1/trace.csv"


def _backdate(client: TestClient, note_id: str, *, days: float) -> None:
    with client.app.state.db_session_factory() as session:
        session.execute(
            update(NoteModel)
            .where(NoteModel.note_id == note_id)
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=days))
        )
        session.commit()


def _host(install_id: str, label: str, version: str, *, adapter: str) -> dict[str, str]:
    return {
        "evidence_adapter": adapter,
        "capture_install_id": install_id,
        "capture_host_label": label,
        "capture_platform": "Linux",
        "capture_client_version": version,
        "capture_client_revision": "a" * 40,
    }


def _watch_metadata(install_id: str, label: str, version: str) -> dict[str, str]:
    return {
        **_host(install_id, label, version, adapter="lt-watch"),
        "evidence_source_uri": FLY_URI,
        "watch_relative_path": "run1/trace.csv",
    }


def _assert_coverage_names_the_stale_machine(
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_client_release, "installed_version", lambda: "0.5.0")
    project_id = _project(client, headers, "Capture installs")
    watch_metadata = _watch_metadata(INSTALL_A, "rig-7", "0.3.0")
    watch_note = _note(client, headers, project_id, "trace.csv", metadata=watch_metadata)
    _backdate(client, str(watch_note["note_id"]), days=1)
    # The same install id also stamps the analysis repo's own environment,
    # which runs its own pinned release: each source is judged on its own.
    rig_figure_metadata = _host(INSTALL_A, "rig-7", "0.4.0", adapter=FIGURE_ADAPTER)
    _note(client, headers, project_id, "figure", metadata=rig_figure_metadata)
    laptop_metadata = _host(INSTALL_B, "laptop", "0.5.0", adapter=FIGURE_ADAPTER)
    _note(client, headers, project_id, "laptop plot", metadata=laptop_metadata)
    _note(client, headers, project_id, "typed note", metadata={"source": "manual"})
    old_rig_metadata = _host(INSTALL_C, "old-rig", "0.1", adapter=FIGURE_ADAPTER)
    old_note = _note(client, headers, project_id, "old", metadata=old_rig_metadata)
    idle_days = capture_client_release.UPDATE_NOTICE_WINDOW_DAYS + 1
    _backdate(client, str(old_note["note_id"]), days=idle_days)

    response = client.get(f"/projects/{project_id}/coverage", headers=headers)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["server_release"]["version"] == "0.5.0"
    sources = data["capture_sources"]
    assert [
        (source["evidence_adapter"], source["capture_host_label"], source["note_count"])
        for source in sources
    ] == [
        (None, None, 1),
        (FIGURE_ADAPTER, "laptop", 1),
        (FIGURE_ADAPTER, "rig-7", 1),
        ("lt-watch", "rig-7", 1),
        (FIGURE_ADAPTER, "old-rig", 1),
    ]
    manual, laptop, rig_figure, rig_watch, old_rig = sources
    assert manual["release_status"] == "unknown"
    assert laptop["release_status"] == "current"
    assert rig_figure["capture_client_version"] == "0.4.0"
    assert rig_figure["capture_client_revision"] == "a" * 40
    assert rig_figure["release_status"] == "behind"
    assert rig_figure["update_recommended"] is True
    assert rig_figure["watched_folder"] is None
    assert rig_watch["release_status"] == "behind"
    assert rig_watch["watched_folder"] == "fly_walking_data"
    # Each stale environment carries its own notice and its own fix: the
    # figure environment is repinned in its analysis repo, the watcher's tool
    # install is reinstalled; neither borrows the other's folder.
    assert rig_figure["update_notice"].startswith(
        "lab-tracker in an analysis-repo environment on `rig-7` is behind this server: "
        "it captured with release 0.4.0, and the server runs release 0.5.0."
    )
    assert "uv add" in rig_figure["update_notice"]
    assert rig_watch["update_notice"].startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-7) is behind this server: "
        "it captured with release 0.3.0, and the server runs release 0.5.0."
    )
    assert "install command on the server's Agents page" in rig_watch["update_notice"]
    assert "`lt update`" in rig_watch["update_notice"]
    # A machine idle for longer than the window is behind but not addressed.
    assert old_rig["release_status"] == "behind"
    assert [source["update_notice"] for source in (manual, laptop, old_rig)] == [None, None, None]


def test_project_coverage_names_the_machine_whose_client_is_behind(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_coverage_names_the_stale_machine(client, admin_auth_headers, monkeypatch)


@pytest.mark.postgres
def test_project_coverage_names_the_stale_machine_on_postgres(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_coverage_names_the_stale_machine(
        postgres_client, postgres_admin_auth_headers, monkeypatch
    )


def test_project_coverage_reads_a_note_whose_client_version_is_oversized(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One hostile stored version must not take the whole coverage read down.

    ``POST /notes`` stores any metadata string, so a capture source's version
    is untrusted; a digit run past CPython's integer-string limit made
    ``int()`` raise inside the release comparison and the read returned 500.
    """

    monkeypatch.setattr(capture_client_release, "installed_version", lambda: "0.5.0")
    project_id = _project(client, admin_auth_headers, "Oversized version")
    hostile_metadata = _host(INSTALL_A, "rig-7", "9" * 5000, adapter=FIGURE_ADAPTER)
    _note(client, admin_auth_headers, project_id, "hostile", metadata=hostile_metadata)
    stale_metadata = _watch_metadata(INSTALL_B, "rig-8", "0.3.0")
    stale = _note(client, admin_auth_headers, project_id, "stale", metadata=stale_metadata)
    _backdate(client, str(stale["note_id"]), days=1)

    response = client.get(f"/projects/{project_id}/coverage", headers=admin_auth_headers)

    assert response.status_code == 200, response.text
    sources = {
        (source["evidence_adapter"], source["capture_host_label"]): source
        for source in response.json()["data"]["capture_sources"]
    }
    hostile = sources[(FIGURE_ADAPTER, "rig-7")]
    assert hostile["release_status"] == "unknown"
    assert hostile["update_recommended"] is False
    assert hostile["update_notice"] is None
    # The other sources are still judged on their own.
    assert sources[("lt-watch", "rig-8")]["release_status"] == "behind"
    assert sources[("lt-watch", "rig-8")]["update_notice"] is not None


def _assert_source_row_is_its_newest_capture(
    client: TestClient,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_client_release, "installed_version", lambda: "0.5.0")
    project_id = _project(client, headers, "Upgrades in place")
    # rig-9 upgraded in place; rig-10 went back to an older release.
    for label, install_id, versions in (
        ("rig-9", INSTALL_D, ("0.3.0", "0.3.0", "0.5.0")),
        ("rig-10", INSTALL_E, ("0.5.0", "0.3.0")),
    ):
        for age_days, version in zip(range(len(versions) - 1, -1, -1), versions, strict=True):
            metadata = _watch_metadata(install_id, label, version)
            note = _note(client, headers, project_id, f"{label} {version}", metadata=metadata)
            if age_days:
                _backdate(client, str(note["note_id"]), days=age_days)
    predating = {
        "evidence_adapter": "lt-hpc",
        "capture_install_id": INSTALL_A,
        "capture_host_label": "bench",
    }
    _note(client, headers, project_id, "old client", metadata=predating)

    response = client.get(f"/projects/{project_id}/coverage", headers=headers)

    assert response.status_code == 200, response.text
    listed = response.json()["data"]["capture_sources"]
    sources = {source["capture_host_label"]: source for source in listed}
    upgraded, downgraded, bench = sources["rig-9"], sources["rig-10"], sources["bench"]
    assert (upgraded["note_count"], upgraded["capture_client_version"]) == (3, "0.5.0")
    assert upgraded["release_status"] == "current"
    assert upgraded["update_notice"] is None
    assert (downgraded["note_count"], downgraded["capture_client_version"]) == (2, "0.3.0")
    assert downgraded["release_status"] == "behind"
    assert downgraded["update_notice"].startswith(
        "lab-tracker on the machine watching `fly_walking_data` (rig-10) is behind"
    )
    # A capture with an install id but no release predates release reporting.
    assert bench["capture_client_version"] is None
    assert bench["release_status"] == "behind"
    assert bench["update_notice"].startswith("lab-tracker on `bench` predates release reporting")


def test_project_coverage_source_row_is_its_newest_capture(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_source_row_is_its_newest_capture(client, admin_auth_headers, monkeypatch)


@pytest.mark.postgres
def test_project_coverage_source_row_is_its_newest_capture_on_postgres(
    postgres_client: TestClient,
    postgres_admin_auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_source_row_is_its_newest_capture(
        postgres_client, postgres_admin_auth_headers, monkeypatch
    )
