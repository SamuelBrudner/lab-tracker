"""End-to-end tests for the server capture channels against a migrated app."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from api_helpers import TEST_STORE_AUTHORITY_GRANT_ID, install_use_time_store_authority
from fastapi.testclient import TestClient
from sqlalchemy import event
from store_authority_fakes import (
    ExplodingSnapshotProvider,
    RecordingSnapshotProvider,
    bound_data_store,
    empty_registry,
    grant_payload,
    registry_from_grants,
)

from lab_tracker.auth import LOCAL_AUTH_USER_ID, Role
from lab_tracker.bounded_subprocess import BoundedSubprocessExecutor, ProcessResult
from lab_tracker.capture_channels.app_runtime import (
    capture_runtime_from_app,
    start_capture_poller_tasks,
)
from lab_tracker.capture_channels.dispatch import CaptureRuntime, run_due_pollers
from lab_tracker.capture_channels.email_capture import capture_token
from lab_tracker.capture_channels.settings import parse_store_scans
from lab_tracker.capture_channels.slack import slack_signature
from lab_tracker.capture_channels.store_scan import (
    LOCAL_STORE_SCAN_UNSUPPORTED_MESSAGE,
    STORE_SCAN_UNAUTHORIZED_MESSAGE,
    LocalStoreScanAccess,
    StoreScanError,
    StoreScanResult,
    parse_rclone_listing,
    run_store_scan,
    scan_key,
)
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthority
from lab_tracker.local_filesystem_operations import BoundedLocalFilesystemOperations
from lab_tracker.models import DataStore, StoreCapability, StoreKind, utc_now
from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.store_authority_registry import ProjectStoreScope, StoreAuthorityRegistry
from lab_tracker.store_authority_use import (
    FixedStoreAuthoritySnapshotProvider,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)

SLACK_SECRET = "8f14e45fceea167a5a36dedd4bea2543"
AUTH_SECRET = "test-secret"  # what the migrated_sqlite_database_url fixture configures
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- helpers


@dataclasses.dataclass(frozen=True)
class Person:
    headers: dict[str, str]
    user_id: str
    username: str


def _person(client: TestClient, *, role: Role = Role.EDITOR, prefix: str = "user") -> Person:
    username = f"{prefix}-{uuid4().hex[:8]}"
    user = client.app.state.auth_service.register_user(
        username=username, password="secret", role=role
    )
    login = client.post("/auth/login", json={"username": username, "password": "secret"})
    assert login.status_code == 200, login.text
    token = login.json()["data"]["access_token"]
    return Person({"Authorization": f"Bearer {token}"}, str(user.user_id), username)


def _project(client: TestClient, headers: dict[str, str], name: str = "Capture project") -> str:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["project_id"])


def _add_member(
    client: TestClient, admin: dict[str, str], project_id: str, person: Person, role: str
) -> None:
    response = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": person.user_id, "role": role},
        headers=admin,
    )
    assert response.status_code == 201, response.text


def _configure(client: TestClient, **updates: Any) -> None:
    app = client.app
    app.state.settings = app.state.settings.model_copy(update=updates)
    app.state.capture_runtime = None


def _notes(client: TestClient, admin: dict[str, str], project_id: str) -> list[dict[str, Any]]:
    response = client.get("/notes", params={"project_id": project_id, "limit": 200}, headers=admin)
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


def _runtime(client: TestClient, **overrides: Any) -> CaptureRuntime:
    client.app.state.capture_runtime = None
    runtime = dataclasses.replace(capture_runtime_from_app(client.app), **overrides)
    client.app.state.capture_runtime = runtime
    return runtime


# --------------------------------------------------------------------------- Slack


def _slack_post(
    client: TestClient,
    path: str,
    body: bytes,
    *,
    timestamp: int | None = None,
    secret: str = SLACK_SECRET,
) -> Any:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": slack_signature(secret, stamp, body),
        },
    )


@pytest.fixture()
def slack_setup(client: TestClient, admin_auth_headers: dict[str, str]) -> SimpleNamespace:
    alice = _person(client, prefix="alice")
    viewer = _person(client, prefix="viewer")
    project_id = _project(client, admin_auth_headers, "Slack project")
    _add_member(client, admin_auth_headers, project_id, alice, "contributor")
    _add_member(client, admin_auth_headers, project_id, viewer, "viewer")
    _configure(
        client,
        slack_signing_secret=SLACK_SECRET,
        slack_workspace_url="https://mylab.slack.com",
        slack_channel_projects=json.dumps({"C0LAB": project_id}),
        slack_users=json.dumps(
            {"U0ALICE": alice.username, "U0VIEW": viewer.user_id, "U0GHOST": "ghost-user"}
        ),
    )
    return SimpleNamespace(alice=alice, viewer=viewer, project_id=project_id)


def _command(user: str = "U0ALICE", channel: str = "C0LAB", text: str = "Rig 2 fly 12") -> bytes:
    return urlencode(
        {
            "team_id": "T0TEAM",
            "channel_id": channel,
            "user_id": user,
            "text": text,
            "trigger_id": "13345224609.738474920.8088930838d88f008e0",
            "command": "/lt",
        }
    ).encode()


def test_slack_capture_is_not_found_until_configured(client: TestClient) -> None:
    response = _slack_post(client, "/integrations/slack/commands", _command())
    assert response.status_code == 404


def test_slash_command_stages_a_note_authored_by_the_mapped_user(
    client: TestClient, admin_auth_headers: dict[str, str], slack_setup: SimpleNamespace
) -> None:
    response = _slack_post(client, "/integrations/slack/commands", _command())

    assert response.status_code == 200, response.text
    assert response.json()["response_type"] == "ephemeral"
    assert "Saved to Lab Tracker" in response.json()["text"]
    [note] = _notes(client, admin_auth_headers, slack_setup.project_id)
    assert note["raw_content"] == "Rig 2 fly 12"
    assert note["status"] == "staged"
    assert note["created_by"] == slack_setup.alice.user_id
    assert note["origin_provider"] == "slack"
    metadata = note["metadata"]
    assert metadata["capture_channel"] == "slack"
    assert metadata["slack_channel_id"] == "C0LAB"
    assert metadata["slack_user_id"] == "U0ALICE"
    assert note["client_capture_id"].startswith("slack-cmd:")

    replay = _slack_post(client, "/integrations/slack/commands", _command())
    assert "already saved" in replay.json()["text"]
    assert len(_notes(client, admin_auth_headers, slack_setup.project_id)) == 1


def test_message_shortcut_records_permalink_and_is_idempotent_per_saver(
    client: TestClient, admin_auth_headers: dict[str, str], slack_setup: SimpleNamespace
) -> None:
    payload = {
        "type": "message_action",
        "team": {"id": "T0TEAM"},
        "channel": {"id": "C0LAB"},
        "user": {"id": "U0ALICE"},
        "message": {
            "ts": "1727712345.000200",
            "user": "U0BOB",
            "text": "Flow run &amp; plate map attached " + "x" * 9000,
        },
        "trigger_id": "1.2.3",
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()

    first = _slack_post(client, "/integrations/slack/interactivity", body)
    second = _slack_post(client, "/integrations/slack/interactivity", body)

    assert first.status_code == 200 and second.status_code == 200
    [note] = _notes(client, admin_auth_headers, slack_setup.project_id)
    metadata = note["metadata"]
    assert metadata["slack_permalink"] == "https://mylab.slack.com/archives/C0LAB/p1727712345000200"
    assert metadata["slack_message_ts"] == "1727712345.000200"
    assert metadata["slack_message_user_id"] == "U0BOB"
    assert metadata["captured_at"].startswith("2024-09-30T")
    assert metadata["capture_text_truncated"] == "True"
    assert note["raw_content"].startswith("Flow run & plate map")
    assert len(note["raw_content"]) <= 8000
    assert note["created_by"] == slack_setup.alice.user_id


@pytest.mark.parametrize(
    ("user", "channel", "expected"),
    [
        ("U0ALICE", "C0OTHER", "not connected to a Lab Tracker project"),
        ("U0STRANGER", "C0LAB", "not linked to a Lab Tracker user"),
        ("U0GHOST", "C0LAB", "not linked to a Lab Tracker user"),
        ("U0VIEW", "C0LAB", "not a contributor"),
    ],
)
def test_unmapped_or_unauthorized_slack_captures_store_nothing(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    slack_setup: SimpleNamespace,
    user: str,
    channel: str,
    expected: str,
) -> None:
    response = _slack_post(client, "/integrations/slack/commands", _command(user, channel))

    assert response.status_code == 200
    assert expected in response.json()["text"]
    assert _notes(client, admin_auth_headers, slack_setup.project_id) == []


def test_stale_or_tampered_slack_requests_are_rejected(
    client: TestClient, admin_auth_headers: dict[str, str], slack_setup: SimpleNamespace
) -> None:
    stale = _slack_post(
        client, "/integrations/slack/commands", _command(), timestamp=int(time.time()) - 3600
    )
    forged = _slack_post(
        client, "/integrations/slack/commands", _command(), secret="wrong-secret-0123456789"
    )
    body = _command()
    stamp = str(int(time.time()))
    tampered = client.post(
        "/integrations/slack/commands",
        content=body.replace(b"Rig", b"Rug"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": slack_signature(SLACK_SECRET, stamp, body),
        },
    )

    assert [stale.status_code, forged.status_code, tampered.status_code] == [401, 401, 401]
    assert _notes(client, admin_auth_headers, slack_setup.project_id) == []


# --------------------------------------------------------------------------- email


class FakeImap:
    """An in-memory IMAP server object speaking the imaplib subset the poller uses."""

    def __init__(self, messages: dict[str, bytes], *, capabilities: tuple[str, ...] = ()) -> None:
        self.messages = dict(messages)
        self.flags: dict[str, set[str]] = {uid: set() for uid in messages}
        self.moved: dict[str, str] = {}
        self.capabilities = capabilities
        self.login_args: tuple[str, str] | None = None

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.login_args = (user, password)
        return "OK", [b"logged in"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False) -> tuple[str, list[bytes]]:
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        command = command.upper()
        if command == "SEARCH":
            unseen = [uid for uid in self.messages if "\\Seen" not in self.flags[uid]]
            return "OK", [" ".join(unseen).encode()]
        if command == "FETCH":
            uid, what = args
            raw = self.messages[uid]
            if what == "(RFC822.SIZE)":
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            uid, _mode, flags = args
            self.flags[uid].update(flags.strip("()").split())
            return "OK", []
        if command in {"MOVE", "COPY"}:
            uid, folder = args
            self.moved[uid] = folder
            return "OK", []
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", []


def _email(
    *,
    sender: str,
    to: str,
    subject: str = "Rig 2 notes",
    body: str = "Fly 12 looks dehydrated.",
    message_id: str | None = None,
    attachments: tuple[tuple[str, str, bytes], ...] = (),
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = message_id or f"<{uuid4().hex}@mail.example.org>"
    message["Date"] = "Mon, 28 Sep 2026 10:00:00 -0400"
    message.set_content(body)
    for filename, content_type, payload in attachments:
        maintype, subtype = content_type.split("/")
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return message.as_bytes()


@pytest.fixture()
def email_setup(client: TestClient, admin_auth_headers: dict[str, str]) -> SimpleNamespace:
    alice = _person(client, prefix="alice")
    bob = _person(client, prefix="bob")
    project_id = _project(client, admin_auth_headers, "Email project")
    _add_member(client, admin_auth_headers, project_id, alice, "contributor")
    _add_member(client, admin_auth_headers, project_id, bob, "contributor")
    _configure(
        client,
        email_capture_address="capture@lab.example.org",
        email_capture_imap_host="imap.example.org",
        email_capture_imap_username="capture",
        email_capture_imap_password="imap-password",
        email_capture_processed_folder="Captured",
        capture_user_emails=json.dumps(
            {"alice@lab.example.org": alice.username, "bob@lab.example.org": bob.user_id}
        ),
    )
    alice_address = (
        "capture+"
        + capture_token(AUTH_SECRET, UUID(alice.user_id), UUID(project_id))
        + "@lab.example.org"
    )
    return SimpleNamespace(alice=alice, bob=bob, project_id=project_id, address=alice_address)


def _poll_email(client: TestClient, imap: FakeImap) -> dict[str, Any]:
    runtime = _runtime(client, imap_factory=lambda host, port, timeout: imap, clock=lambda: NOW)
    report = run_due_pollers(runtime, trigger="test", only=["email"], force=True)
    [poller] = report.pollers
    return poller.model_dump()


def test_capture_address_endpoint_returns_the_signed_address(
    client: TestClient, email_setup: SimpleNamespace
) -> None:
    response = client.get(
        f"/projects/{email_setup.project_id}/capture-address", headers=email_setup.alice.headers
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["address"] == email_setup.address
    assert data["accepted_senders"] == ["alice@lab.example.org"]


def test_capture_address_needs_configuration_membership_and_a_person(
    client: TestClient, admin_auth_headers: dict[str, str], email_setup: SimpleNamespace
) -> None:
    outsider = _person(client, prefix="outsider")
    denied = client.get(
        f"/projects/{email_setup.project_id}/capture-address", headers=outsider.headers
    )
    token = client.post(
        "/auth/tokens",
        json={
            "label": "agent",
            "role": "editor",
            "read_only": True,
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
        headers=email_setup.alice.headers,
    ).json()["data"]["secret"]
    via_token = client.get(
        f"/projects/{email_setup.project_id}/capture-address",
        headers={"Authorization": f"Bearer {token}"},
    )
    _configure(client, email_capture_address="")
    unconfigured = client.get(
        f"/projects/{email_setup.project_id}/capture-address", headers=email_setup.alice.headers
    )

    assert denied.status_code == 403
    assert via_token.status_code == 403
    assert unconfigured.status_code == 404


def test_email_capture_stages_text_and_attachment_notes_for_the_verified_sender(
    client: TestClient, admin_auth_headers: dict[str, str], email_setup: SimpleNamespace
) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    raw = _email(
        sender="Alice <Alice@Lab.Example.org>",
        to=email_setup.address,
        body="Fly 12 looks dehydrated.\n\nOn Sun, Bob wrote:\n> earlier",
        message_id="<m1@mail.example.org>",
        attachments=(
            ("plate.png", "image/png", png),
            ("raw.zip", "application/zip", b"PK\x03\x04" + b"z" * 32),
        ),
    )
    imap = FakeImap({"7": raw}, capabilities=("IMAP4REV1", "MOVE"))

    report = _poll_email(client, imap)

    assert report["status"] == "ran"
    assert report["counts"]["stored"] == 1
    assert report["counts"]["notes_created"] == 2
    assert imap.login_args == ("capture", "imap-password")
    assert "\\Seen" in imap.flags["7"] and imap.moved == {"7": "Captured"}
    notes = _notes(client, admin_auth_headers, email_setup.project_id)
    text = next(note for note in notes if note["raw_asset"] is None)
    image = next(note for note in notes if note["raw_asset"] is not None)
    assert text["raw_content"].startswith("Rig 2 notes\n\nFly 12 looks dehydrated.")
    assert "earlier" not in text["raw_content"]
    assert "raw.zip (application/zip, 36 bytes, sha256" in text["raw_content"]
    for note in (text, image):
        assert note["created_by"] == email_setup.alice.user_id
        assert note["status"] == "staged"
        assert note["origin_provider"] == "email"
        assert note["metadata"]["capture_channel"] == "email"
        assert note["metadata"]["email_from"] == "alice@lab.example.org"
    assert text["metadata"]["capture_bundle_id"] == image["metadata"]["capture_bundle_id"]
    assert text["metadata"]["captured_at"] == "2026-09-28T14:00:00+00:00"
    assert image["metadata"]["evidence_content_hash"] == hashlib.sha256(png).hexdigest()
    assert image["raw_asset"]["filename"] == "plate.png"

    # A replay (e.g. the flag was lost) stages nothing new.
    imap.flags["7"].clear()
    again = _poll_email(client, imap)
    assert again["counts"]["duplicate"] == 1
    assert len(_notes(client, admin_auth_headers, email_setup.project_id)) == 2


@pytest.mark.parametrize(
    ("sender", "to_kind", "reason"),
    [
        ("mallory@evil.example.org", "alice", "rejected_unknown_sender"),
        ("bob@lab.example.org", "alice", "rejected_token_mismatch"),  # spoofed/crossed From
        ("alice@lab.example.org", "garbage", "rejected_token_mismatch"),
        ("alice@lab.example.org", "plain", "rejected_no_capture_address"),
    ],
)
def test_spoofed_or_misaddressed_email_is_rejected_and_marked(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    email_setup: SimpleNamespace,
    sender: str,
    to_kind: str,
    reason: str,
) -> None:
    to = {
        "alice": email_setup.address,
        "garbage": "capture+" + "a" * 20 + "@lab.example.org",
        "plain": "capture@lab.example.org",
    }[to_kind]
    imap = FakeImap({"3": _email(sender=sender, to=to)})

    report = _poll_email(client, imap)

    assert report["counts"]["rejected"] == 1
    assert report["counts"][reason] == 1
    assert "\\Seen" in imap.flags["3"]
    assert _notes(client, admin_auth_headers, email_setup.project_id) == []


def test_email_to_a_project_the_sender_cannot_write_is_rejected(
    client: TestClient, admin_auth_headers: dict[str, str], email_setup: SimpleNamespace
) -> None:
    other_project = _project(client, admin_auth_headers, "Not Alice's")
    address = (
        "capture+"
        + capture_token(AUTH_SECRET, UUID(email_setup.alice.user_id), UUID(other_project))
        + "@lab.example.org"
    )
    imap = FakeImap({"4": _email(sender="alice@lab.example.org", to=address)})

    report = _poll_email(client, imap)

    assert report["counts"]["rejected"] == 1
    assert _notes(client, admin_auth_headers, other_project) == []


def test_email_storage_failure_leaves_the_message_unseen(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    email_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab_tracker.capture_channels.dispatch as dispatch_module

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is down")

    monkeypatch.setattr(dispatch_module, "stage_email", explode)
    imap = FakeImap({"5": _email(sender="alice@lab.example.org", to=email_setup.address)})

    report = _poll_email(client, imap)

    assert report["counts"]["failed"] == 1
    assert imap.flags["5"] == set()


def test_unverified_mail_is_rejected_before_its_body_is_read(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    email_setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lab_tracker.capture_channels.email_capture as email_module

    read_bodies: list[object] = []
    real_message_text = email_module._message_text

    def recording_message_text(message: Any) -> str:
        read_bodies.append(message)
        return real_message_text(message)

    monkeypatch.setattr(email_module, "_message_text", recording_message_text)
    pathological = "\n" * 2_000_000 + "On Mon, Bob wrote:\n" + "> q\n" * 1000
    imap = FakeImap(
        {
            "8": _email(
                sender="mallory@evil.example.org", to=email_setup.address, body=pathological
            ),
            "9": _email(
                sender="alice@lab.example.org <mallory@evil.example.org>",
                to=email_setup.address,
            ),
            "10": _email(sender="alice@lab.example.org", to=email_setup.address, body=pathological),
        }
    )

    started = time.perf_counter()
    report = _poll_email(client, imap)

    assert time.perf_counter() - started < 60
    assert report["counts"]["rejected_unknown_sender"] == 1
    assert report["counts"]["rejected_sender_missing_or_ambiguous"] == 1
    assert report["counts"]["stored"] == 1
    assert len(read_bodies) == 1  # only the verified message's body was read
    assert all("\\Seen" in imap.flags[uid] for uid in ("8", "9", "10"))
    [note] = _notes(client, admin_auth_headers, email_setup.project_id)
    assert note["created_by"] == email_setup.alice.user_id
    assert len(note["raw_content"]) <= 8000


def test_oversized_slack_timestamp_is_a_401_not_a_500(
    client: TestClient, slack_setup: SimpleNamespace
) -> None:
    body = _command()
    response = client.post(
        "/integrations/slack/commands",
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": "9" * 5000,
            "X-Slack-Signature": "v0=" + "0" * 64,
        },
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------- bookings


def _feed(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def _vevent(uid: str, start: str, end: str, summary: str, extra: str = "") -> str:
    return (
        f"BEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{start}\r\nDTEND:{end}\r\n"
        f"SUMMARY:{summary}\r\nORGANIZER;CN=Alice:mailto:alice@lab.example.org\r\n{extra}"
        "END:VEVENT\r\n"
    )


def test_booking_feed_upserts_system_authored_staged_notes(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Bookings project")
    _configure(
        client,
        booking_calendars=json.dumps(
            [
                {
                    "project_id": project_id,
                    "url": "https://calendar.example.org/confocal.ics?token=secret",
                    "instrument": "Confocal 1",
                }
            ]
        ),
    )
    feed = {
        "body": _feed(
            _vevent("b1", "20260928T140000Z", "20260928T160000Z", "Alice - live imaging"),
            _vevent("far", "20261101T140000Z", "20261101T150000Z", "Too far out"),
            _vevent(
                "gone", "20260929T140000Z", "20260929T150000Z", "Cancelled", "STATUS:CANCELLED\r\n"
            ),
        )
    }
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return feed["body"]

    runtime = _runtime(client, calendar_fetcher=fetch, clock=lambda: NOW)
    first = run_due_pollers(runtime, trigger="test", only=["bookings"]).pollers[0]

    assert first.status == "ran"
    assert first.counts["created"] == 1
    assert first.counts["cancelled_not_captured"] == 1
    [note] = _notes(client, admin_auth_headers, project_id)
    assert note["created_by"] == str(LOCAL_AUTH_USER_ID)  # the SYSTEM principal, never a person
    assert note["created_by_user_id"] is None
    metadata = note["metadata"]
    assert metadata["capture_channel"] == "ics"
    assert metadata["evidence_capture_kind"] == "instrument_booking"
    assert metadata["booking_uid"] == "b1"
    assert metadata["booking_start"] == "2026-09-28T14:00:00+00:00"
    assert metadata["booking_end"] == "2026-09-28T16:00:00+00:00"
    assert metadata["booking_instrument"] == "Confocal 1"
    assert metadata["booking_summary"] == "Alice - live imaging"
    assert metadata["booking_organizer"] == "alice@lab.example.org"
    assert "secret" not in json.dumps(note)

    # The booking moves an hour later and is then cancelled: the staged note follows.
    feed["body"] = _feed(
        _vevent("b1", "20260928T140000Z", "20260928T170000Z", "Alice - live imaging (long)")
    )
    second = run_due_pollers(runtime, trigger="test", only=["bookings"], force=True).pollers[0]
    assert second.counts["updated"] == 1
    feed["body"] = _feed(
        _vevent("b1", "20260928T140000Z", "20260928T170000Z", "x", "STATUS:CANCELLED\r\n")
    )
    run_due_pollers(runtime, trigger="test", only=["bookings"], force=True)
    [note] = _notes(client, admin_auth_headers, project_id)
    assert note["metadata"]["booking_end"] == "2026-09-28T17:00:00+00:00"
    assert note["metadata"]["booking_status"] == "cancelled"
    assert "booking_updated_at" in note["metadata"]

    # Once a person reviews the capture, the feed never rewrites it.
    archived = client.post(
        f"/notes/{note['note_id']}/archive",
        json={"reason": "reviewed_not_relevant"},
        headers=admin_auth_headers,
    )
    assert archived.status_code == 200, archived.text
    feed["body"] = _feed(_vevent("b1", "20260928T140000Z", "20260928T180000Z", "changed"))
    last = run_due_pollers(runtime, trigger="test", only=["bookings"], force=True).pollers[0]
    assert last.counts["left_reviewed"] == 1


def test_one_failing_feed_does_not_stop_the_next(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Two feeds")
    _configure(
        client,
        booking_calendars=json.dumps(
            [
                {
                    "project_id": project_id,
                    "url": "https://down.example.org/a.ics",
                    "instrument": "A",
                },
                {
                    "project_id": project_id,
                    "url": "https://up.example.org/b.ics",
                    "instrument": "B",
                },
            ]
        ),
    )

    def fetch(url: str) -> bytes:
        if "down" in url:
            raise OSError("connection refused")
        return _feed(_vevent("ok", "20260928T140000Z", "20260928T150000Z", "Works"))

    runtime = _runtime(client, calendar_fetcher=fetch, clock=lambda: NOW)
    [report] = run_due_pollers(runtime, trigger="test", only=["bookings"]).pollers

    assert report.status == "ran"
    assert report.counts["feeds_failed"] == 1
    assert report.counts["created"] == 1
    assert report.errors and "down.example.org" not in report.errors[0]


# --------------------------------------------------------------------------- store scans

# What a scan needs from the operator's grant: ``list`` to enumerate and
# ``bytes_by_path`` to stream a file for its SHA-256.
_SCAN_CAPABILITIES = (StoreCapability.LIST, StoreCapability.BYTES_BY_PATH)


def _register_store(client: TestClient, project_id: str, **fields: Any) -> DataStore:
    """Insert a legacy, grantless row (as registered before grant bindings existed)."""

    store = DataStore(store_id=uuid4(), project_id=UUID(project_id), **fields)
    _insert_store(client, store)
    return store


def _insert_store(client: TestClient, store: DataStore) -> None:
    with client.app.state.db_session_factory() as session:
        SQLAlchemyLabTrackerRepository(session).data_stores.insert(store)
        session.commit()


def _register_bound_store(
    client: TestClient,
    *,
    project_id: str | None = None,
    group_id: str | None = None,
    capabilities: tuple[StoreCapability, ...] = _SCAN_CAPABILITIES,
    **fields: Any,
) -> tuple[DataStore, dict[str, object]]:
    """Insert a row bound to an operator grant; return it and that exact grant."""

    store, _registry, scope = bound_data_store(
        project_id=UUID(project_id) if project_id is not None else None,
        group_id=UUID(group_id) if group_id is not None else None,
        capabilities=capabilities,
        **fields,
    )
    _insert_store(client, store)
    grant = grant_payload(
        scope=scope,
        definition=ValidatedDataStoreDefinition.create(
            name=store.name,
            kind=store.kind,
            root=store.root,
            credential_ref=store.credential_ref,
        ),
        capabilities=capabilities,
        grant_id=str(store.authority_grant_id),
    )
    return store, grant


def _touch(path: Path, content: bytes, *, age: timedelta = timedelta(hours=1)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stamp = (NOW - age).timestamp()
    os.utime(path, (stamp, stamp))


def _scan(runtime: CaptureRuntime) -> Any:
    [report] = run_due_pollers(runtime, trigger="test", only=["store_scans"], force=True).pollers
    return report


def _baselines(runtime: CaptureRuntime) -> dict[str, Any]:
    state = json.loads(runtime.state.path.read_text(encoding="utf-8"))
    return dict(state.get("store_scan_baselines", {}))


class _SwitchingProvider:
    """Return one registry per capture, recording each capture in ``events``."""

    def __init__(self, *registries: StoreAuthorityRegistry, events: list[str]) -> None:
        self._registries = list(registries)
        self.events = events
        self.calls = 0

    def __call__(self) -> StoreAuthorityRegistry:
        self.calls += 1
        self.events.append("snapshot")
        return self._registries.pop(0)


# ---------------------------------------------------------------- local_fs scans


@pytest.fixture()
def local_store(
    client: TestClient, admin_auth_headers: dict[str, str], tmp_path: Path
) -> SimpleNamespace:
    allowed = tmp_path / "allowed"
    root = allowed / "onedrive"
    root.mkdir(parents=True)
    project_id = _project(client, admin_auth_headers, "Store scan project")
    store, grant = _register_bound_store(
        client, project_id=project_id, name="lab-disk", kind=StoreKind.LOCAL_FS, root=str(root)
    )
    _configure(
        client,
        store_scans=json.dumps(
            [
                {
                    "project_id": project_id,
                    "store": "lab-disk",
                    "prefix": "flow",
                    "patterns": ["*.fcs"],
                }
            ]
        ),
    )
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([allowed]),
        executor=BoundedSubprocessExecutor(),
    )
    registry = registry_from_grants([grant])
    return SimpleNamespace(
        project_id=project_id,
        root=root,
        allowed=allowed,
        store=store,
        registry=registry,
        access=LocalStoreScanAccess(operations),
        runtime=lambda **overrides: _runtime(
            client,
            **{
                "local_store_access": LocalStoreScanAccess(operations),
                "store_authority_snapshot_provider": FixedStoreAuthoritySnapshotProvider(registry),
                "clock": lambda: NOW,
                **overrides,
            },
        ),
    )


def _scan_with_local_adapter(local_store: SimpleNamespace) -> StoreScanResult:
    """Drive the retained local adapter directly, as dispatch will once .63.5 lands.

    Production refuses ``local_fs`` scans (see the dispatch test below); these
    mechanics tests keep the adapter's listing and hashing contract covered.
    """

    runtime = local_store.runtime()
    [scan] = parse_store_scans(runtime.settings.store_scans, variable="LAB_TRACKER_STORE_SCANS")
    binding = detach_store_authority_binding(local_store.store)
    assert binding is not None
    authority = revalidate_store_authority_binding(local_store.registry, binding)
    assert authority is not None
    with runtime.session_factory() as session:
        return run_store_scan(
            scan,
            authority=authority,
            adapter=local_store.access.adapter(authority.definition.root, deadline_seconds=10.0),
            api=runtime.api_for_session(session),
            actor=runtime.system_actor,
            baselines=runtime.state,
            now=NOW,
            hash_max_bytes=int(runtime.settings.store_scan_hash_max_bytes),
        )


def test_local_store_scans_are_refused_after_revalidation_with_no_host_io(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    local_store: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import lab_tracker.capture_channels.store_scan as store_scan_module

    _touch(local_store.root / "flow" / "present.fcs", b"would be listed")
    events: list[str] = []
    provider = RecordingSnapshotProvider(local_store.registry, events=events)
    scandir_targets: list[object] = []
    real_scandir = os.scandir

    def recording_scandir(target: Any = ".") -> Any:
        scandir_targets.append(target)
        return real_scandir(target)

    def no_local_adapter(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a refused local scan built a local adapter")

    monkeypatch.setattr(os, "scandir", recording_scandir)
    monkeypatch.setattr(store_scan_module.LocalStoreScanAccess, "adapter", no_local_adapter)
    runtime = local_store.runtime(store_authority_snapshot_provider=provider)

    report = _scan(runtime)

    assert report.status == "ran"
    assert report.counts == {"scans_failed": 1}
    assert report.errors == [f"lab-disk/flow: {LOCAL_STORE_SCAN_UNSUPPORTED_MESSAGE}"]
    # The grant was revalidated first, so lifting the refusal after .63.5 is
    # only the adapter dispatch; nothing below the root was enumerated.
    assert provider.calls == 1
    assert scandir_targets == []
    assert _notes(client, admin_auth_headers, local_store.project_id) == []
    assert _baselines(runtime) == {}
    assert str(local_store.root) not in caplog.text
    assert str(local_store.root) not in json.dumps(report.model_dump(mode="json"))


def test_local_store_adapter_baselines_then_stages_new_files_with_sha256(
    client: TestClient, admin_auth_headers: dict[str, str], local_store: SimpleNamespace
) -> None:
    root: Path = local_store.root
    _touch(root / "flow" / "old.fcs", b"already here")

    baseline = _scan_with_local_adapter(local_store)
    assert baseline.baseline_recorded == 1
    assert _notes(client, admin_auth_headers, local_store.project_id) == []

    payload = b"FCS3.1 new acquisition"
    _touch(root / "flow" / "run2" / "new.fcs", payload)
    _touch(root / "flow" / "notes.txt", b"not matched")
    _touch(root / "flow" / "~$lock.fcs", b"office lock file")
    _touch(root / "flow" / "fresh.fcs", b"still being written", age=timedelta(seconds=5))
    outside = local_store.allowed.parent / "outside.fcs"
    outside.write_bytes(b"secret")
    (root / "flow" / "link.fcs").symlink_to(outside)

    second = _scan_with_local_adapter(local_store)

    assert second.created == 1
    assert second.settling == 1
    assert second.hashed == 1
    [note] = _notes(client, admin_auth_headers, local_store.project_id)
    metadata = note["metadata"]
    assert metadata["capture_channel"] == "store_scan"
    assert metadata["evidence_source_uri"] == "store://lab-disk/flow/run2/new.fcs"
    assert metadata["evidence_content_hash"] == hashlib.sha256(payload).hexdigest()
    assert metadata["store_file_size_bytes"] == str(len(payload))
    assert metadata["store_file_modified_at"] == (NOW - timedelta(hours=1)).isoformat()
    # The file's own clock, not the poll time, is the capture clock.
    assert metadata["captured_at"] == metadata["store_file_modified_at"]
    assert "content_hash_pending" not in metadata
    assert note["created_by"] == str(LOCAL_AUTH_USER_ID)
    assert note["created_by_user_id"] is None
    assert note["raw_asset"] is None  # a pointer, never the bytes

    third = _scan_with_local_adapter(local_store)
    assert third.created == 0
    assert third.already_captured == 1


def test_local_store_adapter_marks_large_files_hash_pending(
    client: TestClient, admin_auth_headers: dict[str, str], local_store: SimpleNamespace
) -> None:
    _configure(
        client,
        store_scan_hash_max_bytes=4,
        store_scans=json.dumps(
            [
                {
                    "project_id": local_store.project_id,
                    "store": "lab-disk",
                    "patterns": ["*.fcs"],
                    "include_existing": True,
                }
            ]
        ),
    )
    _touch(local_store.root / "big.fcs", b"more than four bytes")

    result = _scan_with_local_adapter(local_store)

    assert result.hash_pending == 1
    [note] = _notes(client, admin_auth_headers, local_store.project_id)
    assert note["metadata"]["content_hash_pending"] == "True"
    assert "evidence_content_hash" not in note["metadata"]


def test_local_store_adapter_outside_the_operator_roots_is_refused(tmp_path: Path) -> None:
    rogue = tmp_path / "elsewhere"
    rogue.mkdir()
    _touch(rogue / "a.fcs", b"x")
    operations = BoundedLocalFilesystemOperations(
        authority=LocalFilesystemAuthority.from_roots([tmp_path / "allowed"]),
        executor=BoundedSubprocessExecutor(),
    )
    adapter = LocalStoreScanAccess(operations).adapter(str(rogue), deadline_seconds=10.0)

    with pytest.raises(StoreScanError, match="LAB_TRACKER_RESOLVER_ALLOWED_ROOTS"):
        adapter.list(None, include=lambda _locator: True)


class _Listed:
    """A scandir-like context manager over entries captured before a swap."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> _Listed:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(self._entries)


@pytest.mark.parametrize("descriptor_walk", [True, False])
def test_directory_swapped_for_a_symlink_mid_walk_is_not_followed(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    local_store: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_walk: bool,
) -> None:
    import lab_tracker.capture_channels.store_scan as store_scan_module

    if descriptor_walk and not store_scan_module._FD_WALK_SUPPORTED:
        pytest.skip("descriptor walking is unavailable on this platform")
    monkeypatch.setattr(store_scan_module, "_FD_WALK_SUPPORTED", descriptor_walk)
    _configure(
        client,
        store_scans=json.dumps(
            [
                {
                    "project_id": local_store.project_id,
                    "store": "lab-disk",
                    "prefix": "flow",
                    "include_existing": True,
                }
            ]
        ),
    )
    flow = local_store.root / "flow"
    _touch(flow / "ok.fcs", b"inside")
    _touch(flow / "sub" / "inside.fcs", b"inside too")
    secrets_dir = local_store.allowed.parent / "secrets"
    _touch(secrets_dir / "private-key.pem", b"-----BEGIN PRIVATE KEY-----")
    flow_identity = os.stat(flow)
    real_scandir = os.scandir
    swapped: list[bool] = []

    def swapping_scandir(target: Any) -> Any:
        iterator = real_scandir(target)
        listing_flow = os.path.samestat(
            os.fstat(target) if isinstance(target, int) else os.stat(target), flow_identity
        )
        if swapped or not listing_flow:
            return iterator
        with iterator:
            entries = list(iterator)
        # The race: after "flow" is listed, "sub" becomes a symlink leading outside.
        (flow / "sub").rename(flow / "sub-moved")
        (flow / "sub").symlink_to(secrets_dir, target_is_directory=True)
        swapped.append(True)
        return _Listed(entries)

    monkeypatch.setattr(os, "scandir", swapping_scandir)

    _scan_with_local_adapter(local_store)

    assert swapped == [True]
    paths = sorted(
        note["metadata"]["store_file_path"]
        for note in _notes(client, admin_auth_headers, local_store.project_id)
    )
    assert paths == ["flow/ok.fcs"]


def test_a_poller_out_of_budget_leaves_work_for_its_next_run(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Budget project")
    _configure(
        client,
        booking_calendars=json.dumps(
            [
                {"project_id": project_id, "url": "https://a.example.org/a.ics", "instrument": "A"},
                {"project_id": project_id, "url": "https://b.example.org/b.ics", "instrument": "B"},
            ]
        ),
    )
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return _feed()

    runtime = _runtime(client, calendar_fetcher=fetch, clock=lambda: NOW, poller_budget_seconds=0.0)
    [report] = run_due_pollers(runtime, trigger="test", only=["bookings"]).pollers

    assert report.status == "ran"
    assert report.counts == {"feeds_left_for_next_poll": 2}
    assert fetched == []


# ---------------------------------------------------------------- rclone scans


class FakeRclone:
    """A ProcessExecutor fake answering ``rclone lsjson`` and ``rclone cat``."""

    def __init__(
        self,
        listing: list[dict[str, Any]],
        files: dict[str, bytes],
        *,
        events: list[str] | None = None,
    ) -> None:
        self.listing = listing
        self.files = files
        self.calls: list[list[str]] = []
        self.events = events if events is not None else []

    def run(
        self,
        command: Any,
        *,
        deadline: Any,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        stdout_consumer: Callable[[bytes], None] | None = None,
        cwd: Any = None,
        env: Any = None,
    ) -> ProcessResult:
        argv = list(command)
        self.calls.append(argv)
        self.events.append(f"rclone {argv[1]}")
        if argv[1] == "lsjson":
            stdout = json.dumps(self.listing).encode()
            return ProcessResult(0, stdout, len(stdout), 0)
        if argv[1] == "cat":
            payload = self.files[argv[2]]
            assert stdout_consumer is not None and len(payload) <= stdout_limit_bytes
            stdout_consumer(payload)
            return ProcessResult(0, b"", len(payload), 0)
        raise AssertionError(argv)


def _listed(path: str, size: int = 8) -> dict[str, Any]:
    return {"Path": path, "Size": size, "ModTime": "2026-09-27T10:00:00Z", "IsDir": False}


def _rclone_runtime(
    client: TestClient,
    fake: FakeRclone,
    provider: Any,
    *,
    remotes: str = "lab-s3",
    **overrides: Any,
) -> CaptureRuntime:
    return _runtime(
        client,
        process_executor=fake,
        rclone_remote_policy=RcloneRemotePolicy.from_config(remotes),
        store_authority_snapshot_provider=provider,
        clock=lambda: NOW,
        **overrides,
    )


def test_rclone_store_scan_revalidates_then_uses_the_bounded_executor_and_remote_policy(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "S3 project")
    _store, grant = _register_bound_store(
        client, project_id=project_id, name="lab-s3", kind=StoreKind.S3, root="bucket/data"
    )
    _configure(
        client,
        store_scans=json.dumps(
            [
                {
                    "project_id": project_id,
                    "store": "lab-s3",
                    "prefix": "flow",
                    "include_existing": True,
                }
            ]
        ),
    )
    payload = b"s3 object bytes"
    events: list[str] = []
    fake = FakeRclone(
        [
            {
                "Path": "run1/sample.fcs",
                "Size": len(payload),
                "ModTime": "2026-09-27T10:00:00.123456789Z",
                "IsDir": False,
                "Hashes": {"md5": "9e107d9d372bb6826bd81d3542a419d6"},
            }
        ],
        {"lab-s3:bucket/data/flow/run1/sample.fcs": payload},
        events=events,
    )
    base_factory = client.app.state.db_session_factory

    def recording_session_factory() -> Any:
        session = base_factory()
        event.listen(session, "after_rollback", lambda _session: events.append("release"))
        return session

    provider = RecordingSnapshotProvider(
        registry_from_grants([grant]), events=events, marker="snapshot"
    )
    runtime = _rclone_runtime(client, fake, provider, session_factory=recording_session_factory)

    report = _scan(runtime)

    assert report.status == "ran", report.errors
    assert report.counts["created"] == 1
    # The read scope is released, then exactly one snapshot is captured,
    # before the first subprocess.
    assert events[:3] == ["release", "snapshot", "rclone lsjson"]
    assert "rclone cat" in events
    assert provider.calls == 1
    assert fake.calls[0][:2] == ["rclone", "lsjson"]
    assert "--hash" in fake.calls[0] and fake.calls[0][-1] == "lab-s3:bucket/data/flow"
    [note] = _notes(client, admin_auth_headers, project_id)
    metadata = note["metadata"]
    assert metadata["evidence_source_uri"] == "store://lab-s3/flow/run1/sample.fcs"
    assert metadata["store_file_provider_hash_md5"] == "9e107d9d372bb6826bd81d3542a419d6"
    assert metadata["evidence_content_hash"] == hashlib.sha256(payload).hexdigest()


def test_rclone_store_scan_uses_the_apps_startup_snapshot_provider(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Registered S3 project")
    registered = client.post(
        "/data-stores",
        json={
            "project_id": project_id,
            "name": "lab-s3",
            "kind": "s3",
            "root": "bucket",
            "authority_grant_id": TEST_STORE_AUTHORITY_GRANT_ID,
        },
        headers=admin_auth_headers,
    )
    assert registered.status_code == 201, registered.text
    _configure(
        client,
        store_scans=json.dumps(
            [{"project_id": project_id, "store": "lab-s3", "include_existing": True}]
        ),
    )
    fake = FakeRclone([_listed("a.fcs")], {"lab-s3:bucket/a.fcs": b"12345678"})

    def runtime() -> CaptureRuntime:
        # No provider override: the runtime takes the app's startup provider.
        return _runtime(
            client,
            process_executor=fake,
            rclone_remote_policy=RcloneRemotePolicy.from_config("lab-s3"),
            clock=lambda: NOW,
        )

    granted = _scan(runtime())
    install_use_time_store_authority(client.app, empty_registry())
    revoked = _scan(runtime())

    assert granted.counts["created"] == 1, granted.errors
    assert revoked.counts == {"scans_failed": 1}
    assert revoked.errors == [f"lab-s3/: {STORE_SCAN_UNAUTHORIZED_MESSAGE}"]
    assert [argv[1] for argv in fake.calls] == ["lsjson", "cat"]


def test_rclone_remote_outside_the_allowlist_is_refused(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Unlisted remote")
    _store, grant = _register_bound_store(
        client, project_id=project_id, name="lab-s3", kind=StoreKind.S3, root="bucket"
    )
    _configure(client, store_scans=json.dumps([{"project_id": project_id, "store": "lab-s3"}]))
    fake = FakeRclone([], {})
    runtime = _runtime(
        client,
        process_executor=fake,
        rclone_remote_policy=RcloneRemotePolicy.deny_all(),
        store_authority_snapshot_provider=FixedStoreAuthoritySnapshotProvider(
            registry_from_grants([grant])
        ),
        clock=lambda: NOW,
    )

    report = _scan(runtime)

    # The global allowlist stays a conjunctive ceiling on a valid grant.
    assert report.counts["scans_failed"] == 1
    assert fake.calls == []


def test_rclone_grant_without_bytes_by_path_lists_but_never_streams(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "List-only grant")
    _store, grant = _register_bound_store(
        client,
        project_id=project_id,
        name="lab-s3",
        kind=StoreKind.S3,
        root="bucket",
        capabilities=(StoreCapability.LIST,),
    )
    _configure(
        client,
        store_scans=json.dumps(
            [{"project_id": project_id, "store": "lab-s3", "include_existing": True}]
        ),
    )
    fake = FakeRclone([_listed("a.fcs")], {"lab-s3:bucket/a.fcs": b"12345678"})
    runtime = _rclone_runtime(
        client, fake, FixedStoreAuthoritySnapshotProvider(registry_from_grants([grant]))
    )

    report = _scan(runtime)

    assert report.counts["created"] == 1, report.errors
    assert report.counts["hash_pending"] == 1
    assert [argv[1] for argv in fake.calls] == ["lsjson"]
    [note] = _notes(client, admin_auth_headers, project_id)
    assert note["metadata"]["content_hash_pending"] == "True"


@pytest.mark.parametrize(
    "denial",
    ["legacy", "revoked", "changed_fingerprint", "missing_list", "other_scope"],
)
def test_store_scan_denials_are_opaque_and_perform_no_io(
    client: TestClient,
    admin_auth_headers: dict[str, str],
    caplog: pytest.LogCaptureFixture,
    denial: str,
) -> None:
    project_id = _project(client, admin_auth_headers, f"Denied scan {denial}")
    capabilities = _SCAN_CAPABILITIES
    if denial == "missing_list":
        capabilities = (StoreCapability.BYTES_BY_PATH, StoreCapability.BYTE_RANGE)
    if denial == "legacy":
        _register_store(client, project_id, name="lab-s3", kind=StoreKind.S3, root="bucket/data")
        store_id = None
        grant: dict[str, object] = {}
    else:
        store, grant = _register_bound_store(
            client,
            project_id=project_id,
            name="lab-s3",
            kind=StoreKind.S3,
            root="bucket/data",
            capabilities=capabilities,
        )
        store_id = store.store_id
    definition = ValidatedDataStoreDefinition.create(
        name="lab-s3", kind=StoreKind.S3, root="bucket/data"
    )
    events: list[str] = []
    provider: Any
    if denial in {"legacy", "missing_list"}:
        # Pure checks on the detached binding deny before any snapshot.
        provider = ExplodingSnapshotProvider()
    elif denial == "revoked":
        provider = RecordingSnapshotProvider(empty_registry(), events=events)
    elif denial == "changed_fingerprint":
        # Same grant ID, widened semantics: the persisted fingerprint is stale.
        provider = RecordingSnapshotProvider(
            registry_from_grants(
                [
                    grant_payload(
                        scope=ProjectStoreScope(UUID(project_id)),
                        definition=definition,
                        capabilities=(*_SCAN_CAPABILITIES, StoreCapability.BYTE_RANGE),
                        grant_id=str(grant["grant_id"]),
                    )
                ]
            ),
            events=events,
        )
    else:
        # The same grant ID now names another project's boundary.
        provider = RecordingSnapshotProvider(
            registry_from_grants(
                [
                    grant_payload(
                        scope=ProjectStoreScope(uuid4()),
                        definition=definition,
                        capabilities=_SCAN_CAPABILITIES,
                        grant_id=str(grant["grant_id"]),
                    )
                ]
            ),
            events=events,
        )
    _configure(
        client,
        store_scans=json.dumps(
            [{"project_id": project_id, "store": "lab-s3", "include_existing": True}]
        ),
    )
    fake = FakeRclone([_listed("a.fcs")], {"lab-s3:bucket/data/a.fcs": b"12345678"})
    runtime = _rclone_runtime(client, fake, provider)

    report = _scan(runtime)

    assert report.status == "ran"
    assert report.counts == {"scans_failed": 1}
    assert report.errors == [f"lab-s3/: {STORE_SCAN_UNAUTHORIZED_MESSAGE}"]
    assert fake.calls == []
    assert events == ([] if denial in {"legacy", "missing_list"} else ["authority"])
    assert _notes(client, admin_auth_headers, project_id) == []
    assert _baselines(runtime) == {}
    if store_id is not None:
        assert str(store_id) not in json.dumps(report.model_dump(mode="json"))
    assert "bucket" not in caplog.text
    assert "bucket" not in json.dumps(report.model_dump(mode="json"))


def _grouped_project(client: TestClient, headers: dict[str, str], name: str) -> tuple[str, str]:
    group = client.post("/groups", json={"name": f"{name} lab"}, headers=headers)
    assert group.status_code == 201, group.text
    group_id = str(group.json()["data"]["group_id"])
    project = client.post("/projects", json={"name": name, "group_id": group_id}, headers=headers)
    assert project.status_code == 201, project.text
    return group_id, str(project.json()["data"]["project_id"])


def test_a_shadowing_project_store_without_a_grant_never_falls_through_to_the_group_store(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    group_id, project_id = _grouped_project(client, admin_auth_headers, "Shadowed scan")
    _group_store, group_grant = _register_bound_store(
        client, group_id=group_id, name="lab-s3", kind=StoreKind.S3, root="group-bucket"
    )
    _configure(
        client,
        store_scans=json.dumps(
            [{"project_id": project_id, "store": "lab-s3", "include_existing": True}]
        ),
    )
    fake = FakeRclone([_listed("a.fcs")], {"lab-s3:group-bucket/a.fcs": b"12345678"})
    registry = registry_from_grants([group_grant])

    inherited = _scan(_rclone_runtime(client, fake, FixedStoreAuthoritySnapshotProvider(registry)))
    assert inherited.counts["created"] == 1, inherited.errors
    assert fake.calls[0][-1] == "lab-s3:group-bucket"

    fake.calls.clear()
    _register_store(client, project_id, name="lab-s3", kind=StoreKind.S3, root="elsewhere")
    shadowed = _scan(_rclone_runtime(client, fake, ExplodingSnapshotProvider()))

    assert shadowed.counts == {"scans_failed": 1}
    assert shadowed.errors == [f"lab-s3/: {STORE_SCAN_UNAUTHORIZED_MESSAGE}"]
    assert fake.calls == []


@pytest.mark.parametrize(
    ("kind", "root"),
    [
        (StoreKind.OBJECT_TABLE, "lab.recordings"),
        (StoreKind.DATABASE, "lab_db"),
        (StoreKind.HTTP, "https://data.example.org/files"),
        (StoreKind.GIT, "https://git.example.org/lab/data.git"),
    ],
)
def test_an_unlistable_store_kind_reports_unsupported_listing_before_any_authority_check(
    client: TestClient, admin_auth_headers: dict[str, str], kind: StoreKind, root: str
) -> None:
    project_id = _project(client, admin_auth_headers, f"Unlistable {kind.value}")
    # Registered without a grant binding; for ``object_table`` and ``database``
    # no binding can be detached at all, yet the kind is still the reason.
    _register_store(client, project_id, name="lab-table", kind=kind, root=root)
    _configure(
        client,
        store_scans=json.dumps(
            [{"project_id": project_id, "store": "lab-table", "include_existing": True}]
        ),
    )
    fake = FakeRclone([], {})

    report = _scan(_rclone_runtime(client, fake, ExplodingSnapshotProvider()))

    assert report.counts == {"scans_failed": 1}
    assert report.errors == [f"lab-table/: Listing is not supported for {kind.value} stores."]
    assert fake.calls == []
    assert _baselines(runtime=client.app.state.capture_runtime) == {}


def _seed_legacy_baseline(runtime: CaptureRuntime, *paths: str) -> str:
    """Record a baseline the way a build before store-ID keys did; return its key."""

    import lab_tracker.capture_channels.store_scan as store_scan_module

    [scan] = parse_store_scans(runtime.settings.store_scans, variable="LAB_TRACKER_STORE_SCANS")
    listing = parse_rclone_listing(
        json.dumps([_listed(path) for path in paths]).encode(), prefix=scan.prefix
    )
    legacy_key = store_scan_module._legacy_scan_key(scan)
    runtime.state.set_store_scan_baseline(
        legacy_key, frozenset(listed.capture_key(scan.store) for listed in listing.files)
    )
    return legacy_key


def test_a_legacy_name_keyed_baseline_is_adopted_once_without_a_capture_gap(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Upgraded scan")
    store, grant = _register_bound_store(
        client, project_id=project_id, name="lab-s3", kind=StoreKind.S3, root="bucket"
    )
    _configure(client, store_scans=json.dumps([{"project_id": project_id, "store": "lab-s3"}]))
    fake = FakeRclone(
        [_listed("seen.fcs"), _listed("arrived-meanwhile.fcs")],
        {"lab-s3:bucket/arrived-meanwhile.fcs": b"12345678"},
    )
    runtime = _rclone_runtime(
        client, fake, FixedStoreAuthoritySnapshotProvider(registry_from_grants([grant]))
    )
    legacy_key = _seed_legacy_baseline(runtime, "seen.fcs")

    report = _scan(runtime)

    # The pre-upgrade baseline still holds: only the file that arrived since
    # the last pre-upgrade poll is staged, and nothing is silently absorbed.
    assert report.counts["baseline_recorded"] == 0, report.errors
    assert report.counts["created"] == 1
    [note] = _notes(client, admin_auth_headers, project_id)
    assert note["metadata"]["store_file_path"] == "arrived-meanwhile.fcs"
    baselines = _baselines(runtime)
    assert legacy_key not in baselines
    [adopted_key] = baselines
    assert adopted_key == scan_key(
        parse_store_scans(runtime.settings.store_scans, variable="X")[0], store_id=store.store_id
    )


def test_a_name_resolving_to_a_new_registration_records_a_fresh_baseline(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    group_id, project_id = _grouped_project(client, admin_auth_headers, "Rebound scan")
    group_store, group_grant = _register_bound_store(
        client,
        group_id=group_id,
        name="lab-s3",
        kind=StoreKind.S3,
        root="group-bucket",
        grant_id="group-scan",
    )
    _configure(client, store_scans=json.dumps([{"project_id": project_id, "store": "lab-s3"}]))
    fake = FakeRclone([_listed("group-file.fcs")], {})
    first_runtime = _rclone_runtime(
        client, fake, FixedStoreAuthoritySnapshotProvider(registry_from_grants([group_grant]))
    )
    legacy_key = _seed_legacy_baseline(first_runtime, "group-file.fcs")

    first = _scan(first_runtime)
    # The group store adopts the pre-upgrade baseline, and the legacy entry goes.
    assert first.counts["baseline_recorded"] == 0, first.errors
    assert first.counts["created"] == 0
    assert legacy_key not in _baselines(first_runtime)

    # The project now registers its own "lab-s3" under a different grant; the
    # name resolves to that registration from here on.
    project_store, project_grant = _register_bound_store(
        client,
        project_id=project_id,
        name="lab-s3",
        kind=StoreKind.S3,
        root="project-bucket",
        grant_id="project-scan",
    )
    fake.listing = [_listed("existing-1.fcs"), _listed("existing-2.fcs")]
    runtime = _rclone_runtime(
        client,
        fake,
        FixedStoreAuthoritySnapshotProvider(registry_from_grants([group_grant, project_grant])),
    )

    second = _scan(runtime)

    # Files already in the newly resolved store are baselined, not staged as
    # new under the previous registration's (or the adopted legacy) baseline.
    assert second.counts["baseline_recorded"] == 2, second.errors
    assert second.counts.get("created", 0) == 0
    assert fake.calls[-1][-1] == "lab-s3:project-bucket"
    assert _notes(client, admin_auth_headers, project_id) == []
    assert legacy_key not in _baselines(runtime)
    assert len(_baselines(runtime)) == 2
    assert group_store.store_id != project_store.store_id


def test_each_scan_captures_one_point_in_time_snapshot(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Point-in-time scans")
    _store, grant = _register_bound_store(
        client, project_id=project_id, name="lab-s3", kind=StoreKind.S3, root="bucket"
    )
    _configure(
        client,
        store_scans=json.dumps(
            [
                {"project_id": project_id, "store": "lab-s3", "prefix": "first"},
                {"project_id": project_id, "store": "lab-s3", "prefix": "second"},
            ]
        ),
    )
    events: list[str] = []
    fake = FakeRclone([_listed("a.fcs")], {}, events=events)
    # Revocation becomes visible between the two scans of one run.
    provider = _SwitchingProvider(registry_from_grants([grant]), empty_registry(), events=events)

    report = _scan(_rclone_runtime(client, fake, provider))

    assert provider.calls == 2
    assert events == ["snapshot", "rclone lsjson", "snapshot"]
    assert fake.calls[0][-1] == "lab-s3:bucket/first"
    assert report.counts["scans_run"] == 1
    assert report.counts["scans_failed"] == 1
    assert report.errors == [f"lab-s3/second: {STORE_SCAN_UNAUTHORIZED_MESSAGE}"]


# --------------------------------------------------------------------------- dispatch


def test_pollers_are_isolated_rate_limited_and_skip_when_unconfigured(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    project_id = _project(client, admin_auth_headers, "Dispatch project")
    _configure(
        client,
        email_capture_address="capture@lab.example.org",
        email_capture_imap_host="imap.example.org",
        email_capture_imap_username="capture",
        email_capture_imap_password="pw",
        booking_calendars=json.dumps(
            [{"project_id": project_id, "url": "https://c.example.org/x.ics", "instrument": "X"}]
        ),
    )

    def broken_imap(host: str, port: int, timeout: float) -> Any:
        raise OSError("imap is down")

    clock = {"now": NOW}
    runtime = _runtime(
        client,
        imap_factory=broken_imap,
        calendar_fetcher=lambda url: _feed(
            _vevent("d1", "20260928T140000Z", "20260928T150000Z", "Ran anyway")
        ),
        clock=lambda: clock["now"],
    )

    first = {report.poller: report for report in run_due_pollers(runtime, trigger="t").pollers}
    assert first["email"].status == "failed"
    assert first["email"].errors == ["Could not connect to the capture mailbox."]
    assert first["bookings"].status == "ran"
    assert first["bookings"].counts["created"] == 1
    assert first["store_scans"].status == "not_configured"

    second = {report.poller: report for report in run_due_pollers(runtime, trigger="t").pollers}
    assert second["email"].status == "skipped_interval"
    assert second["bookings"].status == "skipped_interval"
    assert second["bookings"].next_eligible_at == NOW + timedelta(seconds=300)

    clock["now"] = NOW + timedelta(seconds=301)
    third = {report.poller: report for report in run_due_pollers(runtime, trigger="t").pollers}
    assert third["bookings"].status == "ran"
    assert third["bookings"].counts["unchanged"] == 1


def test_run_due_route_is_admin_only_and_accepts_the_scheduler_token(
    client: TestClient, admin_auth_headers: dict[str, str]
) -> None:
    editor = _person(client, prefix="editor")
    token = client.post(
        "/auth/tokens",
        json={
            "label": "scheduler",
            "role": "admin",
            "read_only": True,
            "scope": "batch_run_due",
            "expires_at": (utc_now() + timedelta(days=1)).isoformat(),
        },
        headers=admin_auth_headers,
    ).json()["data"]["secret"]

    as_admin = client.post("/integrations/run-due", headers=admin_auth_headers)
    as_editor = client.post("/integrations/run-due", headers=editor.headers)
    as_scheduler = client.post(
        "/integrations/run-due", headers={"Authorization": f"Bearer {token}"}
    )

    assert as_admin.status_code == 200, as_admin.text
    assert {poller["status"] for poller in as_admin.json()["data"]["pollers"]} == {"not_configured"}
    assert as_editor.status_code == 403
    assert as_scheduler.status_code == 200, as_scheduler.text


def test_ticker_starts_only_when_enabled_and_configured(client: TestClient) -> None:
    async def started(**updates: Any) -> int:
        _configure(client, **updates)
        tasks = start_capture_poller_tasks(client.app)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    feeds = json.dumps(
        [{"project_id": str(uuid4()), "url": "https://c.example.org/x.ics", "instrument": "X"}]
    )
    assert asyncio.run(started(integrations_poller_enabled=False, booking_calendars=feeds)) == 0
    assert asyncio.run(started(integrations_poller_enabled=True, booking_calendars="")) == 0
    assert asyncio.run(started(integrations_poller_enabled=True, booking_calendars=feeds)) == 1


def test_integrations_poll_cli_reports_json(
    migrated_sqlite_database_url: str, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from lab_tracker.cli import main

    main(["integrations", "poll", "--only", "bookings"])

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "trigger": "cli",
        "pollers": [
            {
                "poller": "bookings",
                "status": "not_configured",
                "started_at": None,
                "finished_at": None,
                "next_eligible_at": None,
                "counts": {},
                "errors": [],
            }
        ],
    }
