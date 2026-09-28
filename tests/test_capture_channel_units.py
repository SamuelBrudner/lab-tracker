"""Pure-function tests for the server capture channels (no app, no network)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from lab_tracker.auth import (
    PAT_SCOPE_ALL,
    PAT_SCOPE_BATCH_RUN_DUE,
    PAT_SCOPE_STAGE_EVIDENCE,
    Role,
    service_principal_can_access,
)
from lab_tracker.capture_channels.email_capture import (
    capture_token,
    capture_tokens,
    parse_email,
    strip_quoted_reply,
)
from lab_tracker.capture_channels.ics import (
    parse_duration,
    parse_events,
    resolve_tzid,
    unfold_lines,
)
from lab_tracker.capture_channels.settings import (
    CaptureAddress,
    CaptureConfigError,
    parse_booking_calendars,
    parse_capture_address,
    parse_slack_channel_projects,
    parse_slack_workspace_url,
    parse_store_scans,
    parse_user_emails,
)
from lab_tracker.capture_channels.slack import (
    MAX_SLACK_REQUEST_SKEW_SECONDS,
    SlackReply,
    SlackRequestRejected,
    parse_interactivity,
    parse_slash_command,
    slack_signature,
    verify_slack_request,
)
from lab_tracker.capture_channels.store_scan import parse_rclone_listing
from lab_tracker.config import Settings
from lab_tracker.local_store_locator import PortableStorePath

SECRET = "8f14e45fceea167a5a36dedd4bea2543"
STRONG_AUTH_SECRET = "a-strong-test-secret-that-is-long-enough"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "local",
        "auth_secret_key": STRONG_AUTH_SECRET,
        "database_url": "sqlite+pysqlite:///:memory:",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- config


def test_capture_channels_are_off_by_default() -> None:
    settings = _settings()
    assert settings.slack_signing_secret == ""
    assert settings.email_capture_address == ""
    assert settings.booking_calendars == ""
    assert settings.store_scans == ""
    assert settings.integrations_poller_enabled is False


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"slack_channel_projects": json.dumps({"C123": str(uuid4())})},
            "requires LAB_TRACKER_SLACK_SIGNING_SECRET",
        ),
        ({"slack_signing_secret": "short"}, "too short"),
        (
            {"slack_signing_secret": SECRET, "slack_channel_projects": "{not json"},
            "LAB_TRACKER_SLACK_CHANNEL_PROJECTS must be valid JSON",
        ),
        (
            {"slack_signing_secret": SECRET, "slack_workspace_url": "http://lab.slack.com"},
            "LAB_TRACKER_SLACK_WORKSPACE_URL",
        ),
        ({"email_capture_address": "capture@lab.example.org"}, "IMAP_HOST"),
        ({"email_capture_address": "capture+x@lab.example.org"}, "no '+' extension"),
        (
            {
                "email_capture_address": "capture@lab.example.org",
                "email_capture_imap_host": "imap.example.org",
                "email_capture_imap_username": "capture",
            },
            "exactly one of",
        ),
        (
            {
                "email_capture_address": "capture@lab.example.org",
                "email_capture_imap_host": "imap.example.org",
                "email_capture_imap_username": "capture",
                "email_capture_imap_password_file": "/nonexistent/lab-tracker-imap-password",
            },
            "could not be read",
        ),
        (
            {
                "email_capture_address": "capture@lab.example.org",
                "email_capture_imap_host": "imap.example.org",
                "email_capture_imap_username": "capture",
                "email_capture_imap_password": "hunter2",
                "auth_secret_key": "dev-only-change-me",
                "auth_enabled": False,
            },
            "non-placeholder secret",
        ),
        (
            {"booking_calendars": json.dumps([{"project_id": "x", "url": "https://a/b"}])},
            "LAB_TRACKER_BOOKING_CALENDARS entry 1",
        ),
        (
            {
                "booking_calendars": json.dumps(
                    [{"project_id": str(uuid4()), "url": "http://a/b.ics", "instrument": "Scope"}]
                )
            },
            "https:// URL",
        ),
        (
            {"store_scans": json.dumps([{"project_id": str(uuid4()), "store": "s", "prefix": "../x"}])},
            "portable relative path",
        ),
        ({"integrations_poll_min_interval_seconds": 5}, "POLL_MIN_INTERVAL_SECONDS"),
        ({"integrations_state_path": "relative/state.json"}, "absolute path"),
        ({"store_scan_hash_max_bytes": -1}, "STORE_SCAN_HASH_MAX_BYTES"),
        ({"capture_user_emails": json.dumps({"not-an-email": "alice"})}, "valid email"),
    ],
)
def test_malformed_capture_configuration_fails_loudly_at_startup(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(**overrides)
    assert message in str(excinfo.value)


def test_complete_capture_configuration_is_accepted(tmp_path) -> None:
    password_file = tmp_path / "imap-password"
    password_file.write_text("s3cret\n", encoding="utf-8")
    project_id = str(uuid4())
    settings = _settings(
        slack_signing_secret=SECRET,
        slack_workspace_url="https://mylab.slack.com/",
        slack_channel_projects=json.dumps({"C0123ABC": project_id}),
        slack_users=json.dumps({"U0123ABC": "alice@lab.example.org"}),
        capture_user_emails=json.dumps({"Alice@Lab.Example.org": "alice"}),
        email_capture_address="capture@lab.example.org",
        email_capture_imap_host="imap.example.org",
        email_capture_imap_username="capture",
        email_capture_imap_password_file=str(password_file),
        booking_calendars=json.dumps(
            [
                {
                    "project_id": project_id,
                    "url": "https://calendar.example.org/feed.ics?token=abc",
                    "instrument": "Confocal 1",
                    "timezone": "America/New_York",
                }
            ]
        ),
        store_scans=json.dumps(
            [{"project_id": project_id, "store": "lab-onedrive", "prefix": "flow/", "patterns": ["*.fcs"]}]
        ),
    )
    assert "s3cret" not in repr(settings)
    assert "calendar.example.org" not in repr(settings)
    assert parse_user_emails(settings.capture_user_emails, variable="X") == {
        "alice@lab.example.org": "alice"
    }
    assert parse_slack_workspace_url(settings.slack_workspace_url, variable="X") == (
        "https://mylab.slack.com"
    )
    assert parse_slack_channel_projects(settings.slack_channel_projects, variable="X") == {
        "C0123ABC": UUID(project_id)
    }
    [calendar] = parse_booking_calendars(settings.booking_calendars, variable="X")
    assert calendar.instrument == "Confocal 1"
    assert "token=abc" not in repr(calendar)
    [scan] = parse_store_scans(settings.store_scans, variable="X")
    assert scan.prefix == PortableStorePath(("flow",))
    assert scan.patterns == ("*.fcs",)
    assert scan.include_existing is False


def test_capture_address_parsing() -> None:
    assert parse_capture_address("Capture@Lab.Example.org", variable="X") == CaptureAddress(
        "capture", "lab.example.org"
    )
    assert parse_capture_address("", variable="X") is None
    with pytest.raises(CaptureConfigError):
        parse_capture_address("not-an-address", variable="X")


# --------------------------------------------------------------------------- auth policy


def test_scheduler_tokens_may_trigger_the_capture_pollers_only_as_admins() -> None:
    for scope in (PAT_SCOPE_BATCH_RUN_DUE, PAT_SCOPE_ALL):
        assert service_principal_can_access(
            "POST", "/integrations/run-due", read_only=True, role=Role.ADMIN, scope=scope
        )
        assert not service_principal_can_access(
            "POST", "/integrations/run-due", read_only=True, role=Role.EDITOR, scope=scope
        )
    assert not service_principal_can_access(
        "POST",
        "/integrations/run-due",
        read_only=False,
        role=Role.ADMIN,
        scope=PAT_SCOPE_STAGE_EVIDENCE,
    )
    assert not service_principal_can_access(
        "GET", "/projects", read_only=True, role=Role.ADMIN, scope=PAT_SCOPE_BATCH_RUN_DUE
    )


# --------------------------------------------------------------------------- Slack


def _signed(body: bytes, *, timestamp: int | None = None) -> tuple[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    return stamp, slack_signature(SECRET, stamp, body)


def test_slack_signature_accepts_a_fresh_valid_request() -> None:
    body = b"team_id=T1&channel_id=C1&user_id=U1&text=hello"
    stamp, signature = _signed(body)
    verify_slack_request(
        secret=SECRET, timestamp=stamp, signature=signature, body=body, now=float(stamp)
    )


def test_slack_signature_rejects_stale_and_replayed_requests() -> None:
    body = b"team_id=T1&channel_id=C1&user_id=U1&text=hello"
    stamp, signature = _signed(body, timestamp=1_700_000_000)
    with pytest.raises(SlackRequestRejected, match="replay window"):
        verify_slack_request(
            secret=SECRET,
            timestamp=stamp,
            signature=signature,
            body=body,
            now=1_700_000_000 + MAX_SLACK_REQUEST_SKEW_SECONDS + 1,
        )


@pytest.mark.parametrize(
    "tamper",
    ["body", "signature", "timestamp", "missing_signature", "missing_timestamp", "wrong_secret"],
)
def test_slack_signature_rejects_tampering(tamper: str) -> None:
    body = b"team_id=T1&channel_id=C1&user_id=U1&text=hello"
    stamp, signature = _signed(body)
    kwargs: dict[str, object] = {
        "secret": SECRET,
        "timestamp": stamp,
        "signature": signature,
        "body": body,
        "now": float(stamp),
    }
    if tamper == "body":
        kwargs["body"] = body + b"%21"
    elif tamper == "signature":
        kwargs["signature"] = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    elif tamper == "timestamp":
        kwargs["timestamp"] = str(int(stamp) - 1)
    elif tamper == "missing_signature":
        kwargs["signature"] = None
    elif tamper == "missing_timestamp":
        kwargs["timestamp"] = None
    else:
        kwargs["secret"] = SECRET[::-1]
    with pytest.raises(SlackRequestRejected):
        verify_slack_request(**kwargs)  # type: ignore[arg-type]


def test_slack_payload_parsing() -> None:
    command = parse_slash_command(
        urlencode(
            {
                "team_id": "T0001",
                "channel_id": "C0001",
                "user_id": "U0001",
                "text": "Rig 2 fly 12 &amp; 13",
                "trigger_id": "123.456.abc",
            }
        ).encode()
    )
    assert command is not None
    assert (command.kind, command.channel_id, command.user_id) == ("command", "C0001", "U0001")
    assert parse_slash_command(b"ssl_check=1&token=x") is None

    payload = {
        "type": "message_action",
        "team": {"id": "T0001"},
        "channel": {"id": "C0001"},
        "user": {"id": "U0002"},
        "message": {"ts": "1727712345.000200", "user": "U0003", "text": "flow run done"},
        "trigger_id": "999.1.x",
    }
    shortcut = parse_interactivity(urlencode({"payload": json.dumps(payload)}).encode())
    assert not isinstance(shortcut, SlackReply) and shortcut is not None
    assert shortcut.message_ts == "1727712345.000200"
    assert shortcut.message_user_id == "U0003"
    assert shortcut.user_id == "U0002"
    global_shortcut = parse_interactivity(
        urlencode({"payload": json.dumps({"type": "shortcut"})}).encode()
    )
    assert isinstance(global_shortcut, SlackReply)
    assert parse_interactivity(urlencode({"payload": '{"type":"block_actions"}'}).encode()) is None
    with pytest.raises(SlackRequestRejected):
        parse_interactivity(b"payload=not-json")
    with pytest.raises(SlackRequestRejected):
        parse_slash_command(b"team_id=T1&channel_id=bad%20id&user_id=U1&text=x")


# --------------------------------------------------------------------------- ICS

_NY = ZoneInfo("America/New_York")


def _calendar(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def test_ics_unfolds_continuation_lines() -> None:
    assert unfold_lines("SUMMARY:Confocal booking for a very long\r\n  sample prep run\r\nUID:x") == [
        "SUMMARY:Confocal booking for a very long sample prep run",
        "UID:x",
    ]


def test_ics_parses_utc_tzid_all_day_duration_and_escapes() -> None:
    text = _calendar(
        "BEGIN:VEVENT\r\nUID:utc-1\r\nDTSTART:20260928T140000Z\r\nDTEND:20260928T150000Z\r\n"
        "SUMMARY:Confocal\\, rig 2\\; imaging\\nsecond line\r\n"
        "ORGANIZER;CN=\"Doe, Jane\":mailto:Jane.Doe@Lab.Example.org\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:tz-1\r\nDTSTART;TZID=America/New_York:20260928T100000\r\n"
        "DURATION:PT1H30M\r\nSUMMARY:Local\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:win-1\r\nDTSTART;TZID=\"Eastern Standard Time\":20260929T090000\r\n"
        "DTEND;TZID=\"Eastern Standard Time\":20260929T100000\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:allday-1\r\nDTSTART;VALUE=DATE:20260930\r\nSUMMARY:Service\r\n"
        "END:VEVENT\r\n",
    )
    result = parse_events(text, default_zone=timezone.utc)
    events = {event.uid: event for event in result.events}
    assert result.skipped == 0
    utc = events["utc-1"]
    assert utc.start == datetime(2026, 9, 28, 14, tzinfo=timezone.utc)
    assert utc.summary == "Confocal, rig 2; imaging\nsecond line"
    assert utc.organizer_email == "jane.doe@lab.example.org"
    local = events["tz-1"]
    assert local.start == datetime(2026, 9, 28, 14, tzinfo=timezone.utc)  # EDT is UTC-4
    assert local.end - local.start == timedelta(hours=1, minutes=30)
    assert events["win-1"].start == datetime(2026, 9, 29, 13, tzinfo=timezone.utc)
    all_day = events["allday-1"]
    assert all_day.all_day is True
    assert all_day.start == datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert all_day.end == datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_ics_floating_times_use_the_feed_zone_and_all_day_dates_too() -> None:
    text = _calendar(
        "BEGIN:VEVENT\r\nUID:float\r\nDTSTART:20261201T090000\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:day\r\nDTSTART;VALUE=DATE:20261201\r\n"
        "DTEND;VALUE=DATE:20261203\r\nEND:VEVENT\r\n",
    )
    events = {event.uid: event for event in parse_events(text, default_zone=_NY).events}
    assert events["float"].start == datetime(2026, 12, 1, 14, tzinfo=timezone.utc)  # EST
    assert events["day"].start == datetime(2026, 12, 1, 5, tzinfo=timezone.utc)
    assert events["day"].end == datetime(2026, 12, 3, 5, tzinfo=timezone.utc)


def test_ics_cancelled_overrides_nested_alarms_and_malformed_events() -> None:
    text = _calendar(
        "BEGIN:VEVENT\r\nUID:series\r\nDTSTART:20261005T130000Z\r\nDTEND:20261005T140000Z\r\n"
        "RRULE:FREQ=WEEKLY\r\nSUMMARY:Weekly slot\r\n"
        "BEGIN:VALARM\r\nTRIGGER:-PT15M\r\nSUMMARY:Alarm text\r\nEND:VALARM\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID:20261005T130000Z\r\n"
        "DTSTART:20261005T150000Z\r\nDTEND:20261005T160000Z\r\nSTATUS:CANCELLED\r\n"
        "SUMMARY:Weekly slot (moved)\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nDTSTART:20261005T130000Z\r\nSUMMARY:no uid\r\nEND:VEVENT\r\n",
        "BEGIN:VEVENT\r\nUID:bad-tz\r\nDTSTART;TZID=Mars/Olympus_Mons:20261005T130000\r\n"
        "END:VEVENT\r\n",
    )
    result = parse_events(text, default_zone=timezone.utc)
    assert result.skipped == 2
    [event] = result.events
    assert event.uid == "series"
    assert event.cancelled is True
    assert event.summary == "Weekly slot (moved)"
    assert event.instance_start == datetime(2026, 10, 5, 13, tzinfo=timezone.utc)
    assert event.start == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


def test_ics_helpers() -> None:
    assert parse_duration("P1DT2H") == timedelta(days=1, hours=2)
    assert parse_duration("-PT15M") == -timedelta(minutes=15)
    assert str(resolve_tzid("/mozilla.org/20050126_1/Europe/Berlin")) == "Europe/Berlin"


# --------------------------------------------------------------------------- email


def test_capture_token_is_deterministic_and_scoped() -> None:
    user_id, project_id = uuid4(), uuid4()
    token = capture_token(STRONG_AUTH_SECRET, user_id, project_id)
    assert token == capture_token(STRONG_AUTH_SECRET, user_id, project_id)
    assert len(token) == 20 and token.isalnum() and token == token.lower()
    assert token != capture_token(STRONG_AUTH_SECRET, uuid4(), project_id)
    assert token != capture_token(STRONG_AUTH_SECRET, user_id, uuid4())
    assert token != capture_token("another-secret-entirely-000", user_id, project_id)


def test_capture_tokens_only_match_the_configured_address() -> None:
    address = CaptureAddress("capture", "lab.example.org")
    token = "a" * 20
    assert capture_tokens(
        [
            f"capture+{token}@lab.example.org",
            f"capture+{token}@lab.example.org",
            f"other+{'b' * 20}@lab.example.org",
            f"capture+{'c' * 20}@evil.example.org",
            "capture+short@lab.example.org",
            "capture@lab.example.org",
        ],
        address,
    ) == (token,)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Plate 3 looks good.\n\nOn Mon, Sep 28, 2026 at 9:00 AM Jane <j@x.org> wrote:\n"
            "> earlier\n> text\n",
            "Plate 3 looks good.",
        ),
        (
            "Plate 3 looks good.\nOn Mon, Sep 28, 2026 at 9:00 AM Jane Doe <\njane@x.org> wrote:\n"
            "> earlier\n",
            "Plate 3 looks good.",
        ),
        (
            "New result\n-----Original Message-----\nFrom: someone\nold body",
            "New result",
        ),
        (
            "New result\n________________________________\nFrom: someone\nSent: today\nold",
            "New result",
        ),
        (
            "> quoted question\nMy inline answer\n> another quote\nSecond answer",
            "> quoted question\nMy inline answer\n> another quote\nSecond answer",
        ),
        ("> only quoted text", "> only quoted text"),
        (
            "On Monday we ran it again.\nIt worked.",
            "On Monday we ran it again.\nIt worked.",
        ),
    ],
)
def test_strip_quoted_reply_is_conservative(text: str, expected: str) -> None:
    assert strip_quoted_reply(text) == expected


def test_parse_email_extracts_sender_recipients_body_and_attachments() -> None:
    message = EmailMessage()
    message["From"] = "Alice <Alice@Lab.Example.org>"
    message["To"] = "capture+aaaaaaaaaaaaaaaaaaaa@lab.example.org"
    message["Subject"] = "Rig 2 notes"
    message["Message-ID"] = "<abc@mail.example.org>"
    message["Date"] = "Mon, 28 Sep 2026 10:00:00 -0400"
    message.set_content("Fly 12 dehydrated.\n\nOn Sun, Bob wrote:\n> old")
    message.add_alternative("<p>Fly 12 <b>dehydrated</b>.</p>", subtype="html")
    message.add_attachment(b"a,b\n1,2\n", maintype="text", subtype="csv", filename="plate.csv")
    message.add_attachment(b"\x00" * 10, maintype="application", subtype="zip", filename="raw.zip")

    parsed = parse_email(message.as_bytes())

    assert parsed.sender == "alice@lab.example.org"
    assert parsed.recipients == ("capture+aaaaaaaaaaaaaaaaaaaa@lab.example.org",)
    assert parsed.subject == "Rig 2 notes"
    assert parsed.message_id == "<abc@mail.example.org>"
    assert parsed.sent_at == "2026-09-28T14:00:00+00:00"
    assert parsed.body == "Fly 12 dehydrated."
    assert [(a.filename, a.content_type) for a in parsed.attachments] == [
        ("plate.csv", "text/csv"),
        ("raw.zip", "application/zip"),
    ]


def test_parse_email_refuses_multiple_from_addresses() -> None:
    raw = (
        b"From: a@lab.example.org, b@lab.example.org\r\nTo: x@y.org\r\n"
        b"Subject: s\r\n\r\nbody\r\n"
    )
    assert parse_email(raw).sender is None


# --------------------------------------------------------------------------- rclone


def test_rclone_listing_parse_keeps_files_hashes_and_times() -> None:
    stdout = json.dumps(
        [
            {
                "Path": "run1/sample.fcs",
                "Name": "sample.fcs",
                "Size": 1234,
                "ModTime": "2026-09-27T16:15:57.034468261+01:00",
                "IsDir": False,
                "Hashes": {"MD5": "0cc175b9c0f1b6a831c399e269772661", "bogus name!": "x"},
            },
            {"Path": "run1", "IsDir": True, "Size": -1},
            {"Path": "run1/.DS_Store", "Size": 5, "IsDir": False},
            {"Path": "run1/doc", "Size": -1, "IsDir": False},
        ]
    ).encode()
    listing = parse_rclone_listing(stdout, prefix=PortableStorePath(("flow",)))
    [listed] = listing.files
    assert listed.locator.path == "flow/run1/sample.fcs"
    assert listed.size == 1234
    assert listed.modified_at == datetime(2026, 9, 27, 15, 15, 57, tzinfo=timezone.utc)
    assert listed.provider_hashes == (("md5", "0cc175b9c0f1b6a831c399e269772661"),)
