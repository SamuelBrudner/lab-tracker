"""Pure parsing of the operator configuration for server capture channels.

``lab_tracker.config.Settings`` calls these parsers from its validator, so a
malformed channel configuration fails loudly at startup, naming the variable.
Nothing here touches the database, the network, or the filesystem beyond the
existence check of a configured password file; user and project references are
checked for syntax only and resolved against the database at capture time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lab_tracker.local_store_locator import PortableStorePath, is_valid_store_name

MAX_CONFIG_ENTRIES: Final = 200
MAX_STORE_SCAN_PATTERNS: Final = 20
MAX_PATTERN_LENGTH: Final = 200
MAX_USER_REF_LENGTH: Final = 320
MAX_INSTRUMENT_LENGTH: Final = 120
_SLACK_ID_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{1,63}\Z")
_EMAIL_LOCAL_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62})\Z")
_EMAIL_DOMAIN_RE = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,62})(?:\.[a-z0-9-]{1,63})+\Z")
_SIMPLE_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[a-z0-9.-]{1,253}\Z")


class CaptureConfigError(ValueError):
    """An operator capture-channel setting is malformed."""


@dataclass(frozen=True, slots=True)
class CaptureAddress:
    """The base address whose plus-extension carries a capture token."""

    local: str
    domain: str

    def with_token(self, token: str) -> str:
        """Return ``<local>+<token>@<domain>``."""

        return f"{self.local}+{token}@{self.domain}"


@dataclass(frozen=True, slots=True)
class BookingCalendar:
    """One operator-declared instrument calendar feed."""

    project_id: UUID
    url: str
    instrument: str
    timezone: str

    def __repr__(self) -> str:  # feed URLs routinely embed a secret token
        return (
            f"BookingCalendar(project_id={self.project_id!s}, "
            f"instrument={self.instrument!r}, url=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class StoreScan:
    """One operator-declared registered-store prefix to watch for new files."""

    project_id: UUID
    store: str
    prefix: PortableStorePath | None
    patterns: tuple[str, ...]
    include_existing: bool = False


def parse_json_setting(raw: str, *, variable: str) -> object:
    """Parse one JSON-valued setting, naming the variable (never the value)."""

    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise CaptureConfigError(f"{variable} must be valid JSON.") from exc


def parse_user_ref(value: object, *, variable: str, key: str) -> str:
    """Validate a Lab Tracker user reference: a user id, a username, or an email."""

    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CaptureConfigError(
            f"{variable} entry {key!r} must map to a non-empty user id, username, or email."
        )
    if len(value) > MAX_USER_REF_LENGTH:
        raise CaptureConfigError(f"{variable} entry {key!r} names an over-long user reference.")
    return value


def normalize_email(value: object) -> str | None:
    """Return a lower-cased ``local@domain`` address, or ``None`` when malformed."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    if not _SIMPLE_EMAIL_RE.fullmatch(cleaned) or ".." in cleaned:
        return None
    return cleaned


def parse_user_emails(raw: str, *, variable: str) -> dict[str, str]:
    """Parse ``{"email": "<user id|username>"}`` into a lower-cased directory."""

    if not raw.strip():
        return {}
    payload = parse_json_setting(raw, variable=variable)
    if not isinstance(payload, dict):
        raise CaptureConfigError(f"{variable} must be a JSON object of email -> user.")
    if len(payload) > MAX_CONFIG_ENTRIES:
        raise CaptureConfigError(f"{variable} has more than {MAX_CONFIG_ENTRIES} entries.")
    directory: dict[str, str] = {}
    for index, (email, user_ref) in enumerate(payload.items(), start=1):
        normalized = normalize_email(email)
        if normalized is None:
            raise CaptureConfigError(f"{variable} entry {index} is not a valid email address.")
        if normalized in directory:
            raise CaptureConfigError(f"{variable} entry {index} repeats an email address.")
        ref = parse_user_ref(user_ref, variable=variable, key=f"#{index}")
        if "@" in ref:
            raise CaptureConfigError(
                f"{variable} entry {index} must map to a user id or username, not an email."
            )
        directory[normalized] = ref
    return directory


def parse_slack_id_map(raw: str, *, variable: str, kind: str) -> dict[str, str]:
    """Parse a JSON object keyed by Slack ids (``C…`` channels or ``U…`` users)."""

    if not raw.strip():
        return {}
    payload = parse_json_setting(raw, variable=variable)
    if not isinstance(payload, dict):
        raise CaptureConfigError(f"{variable} must be a JSON object keyed by Slack {kind} id.")
    if len(payload) > MAX_CONFIG_ENTRIES:
        raise CaptureConfigError(f"{variable} has more than {MAX_CONFIG_ENTRIES} entries.")
    parsed: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not _SLACK_ID_RE.fullmatch(key):
            raise CaptureConfigError(f"{variable} has a key that is not a Slack {kind} id.")
        parsed[key] = parse_user_ref(value, variable=variable, key=key)
    return parsed


def parse_slack_channel_projects(raw: str, *, variable: str) -> dict[str, UUID]:
    """Parse ``{"C0123": "<project uuid>"}``."""

    return {
        channel: _parse_uuid(value, variable=variable, key=channel)
        for channel, value in parse_slack_id_map(raw, variable=variable, kind="channel").items()
    }


def parse_slack_workspace_url(raw: str, *, variable: str) -> str:
    """Validate ``https://<workspace>.slack.com`` and return it without a slash."""

    cleaned = raw.strip().rstrip("/")
    if not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise CaptureConfigError(
            f"{variable} must be an https://<workspace>.slack.com URL."
        ) from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".slack.com")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise CaptureConfigError(f"{variable} must be an https://<workspace>.slack.com URL.")
    return f"https://{hostname}"


def parse_capture_address(raw: str, *, variable: str) -> CaptureAddress | None:
    """Parse the base capture address ``local@domain`` (no plus-extension)."""

    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    local, separator, domain = cleaned.partition("@")
    if (
        not separator
        or "+" in local
        or not _EMAIL_LOCAL_RE.fullmatch(local)
        or not _EMAIL_DOMAIN_RE.fullmatch(domain)
    ):
        raise CaptureConfigError(
            f"{variable} must be a plain address such as capture@lab.example.org "
            "(no '+' extension; the server appends one per user and project)."
        )
    return CaptureAddress(local=local, domain=domain)


def parse_booking_calendars(raw: str, *, variable: str) -> tuple[BookingCalendar, ...]:
    """Parse ``[{"project_id", "url", "instrument", "timezone"?}]``."""

    if not raw.strip():
        return ()
    payload = parse_json_setting(raw, variable=variable)
    if not isinstance(payload, list):
        raise CaptureConfigError(f"{variable} must be a JSON list of calendar feeds.")
    if len(payload) > MAX_CONFIG_ENTRIES:
        raise CaptureConfigError(f"{variable} has more than {MAX_CONFIG_ENTRIES} entries.")
    calendars: list[BookingCalendar] = []
    for index, entry in enumerate(payload, start=1):
        key = f"#{index}"
        if not isinstance(entry, dict) or not {"project_id", "url", "instrument"} <= set(entry):
            raise CaptureConfigError(
                f"{variable} entry {index} must be an object with project_id, url, instrument."
            )
        unknown = set(entry) - {"project_id", "url", "instrument", "timezone"}
        if unknown:
            raise CaptureConfigError(f"{variable} entry {index} has unknown keys.")
        url = entry["url"]
        if not isinstance(url, str) or not _is_https_url(url):
            raise CaptureConfigError(
                f"{variable} entry {index} url must be an https:// URL without credentials."
            )
        instrument = entry["instrument"]
        if (
            not isinstance(instrument, str)
            or not instrument.strip()
            or len(instrument) > MAX_INSTRUMENT_LENGTH
        ):
            raise CaptureConfigError(
                f"{variable} entry {index} instrument must be a non-empty name of at most "
                f"{MAX_INSTRUMENT_LENGTH} characters."
            )
        timezone_name = entry.get("timezone", "UTC")
        if not isinstance(timezone_name, str) or not _is_zone(timezone_name):
            raise CaptureConfigError(
                f"{variable} entry {index} timezone must be an IANA zone such as America/New_York."
            )
        calendars.append(
            BookingCalendar(
                project_id=_parse_uuid(entry["project_id"], variable=variable, key=key),
                url=url,
                instrument=instrument.strip(),
                timezone=timezone_name,
            )
        )
    return tuple(calendars)


def parse_store_scans(raw: str, *, variable: str) -> tuple[StoreScan, ...]:
    """Parse ``[{"project_id", "store", "prefix"?, "patterns"?}]``."""

    if not raw.strip():
        return ()
    payload = parse_json_setting(raw, variable=variable)
    if not isinstance(payload, list):
        raise CaptureConfigError(f"{variable} must be a JSON list of store scans.")
    if len(payload) > MAX_CONFIG_ENTRIES:
        raise CaptureConfigError(f"{variable} has more than {MAX_CONFIG_ENTRIES} entries.")
    scans: list[StoreScan] = []
    for index, entry in enumerate(payload, start=1):
        if not isinstance(entry, dict) or not {"project_id", "store"} <= set(entry):
            raise CaptureConfigError(
                f"{variable} entry {index} must be an object with project_id and store."
            )
        unknown = set(entry) - {"project_id", "store", "prefix", "patterns", "include_existing"}
        if unknown:
            raise CaptureConfigError(f"{variable} entry {index} has unknown keys.")
        include_existing = entry.get("include_existing", False)
        if type(include_existing) is not bool:
            raise CaptureConfigError(f"{variable} entry {index} include_existing must be a bool.")
        store = entry["store"]
        if not is_valid_store_name(store):
            raise CaptureConfigError(f"{variable} entry {index} store is not a valid store name.")
        raw_prefix = entry.get("prefix", "")
        if not isinstance(raw_prefix, str):
            raise CaptureConfigError(f"{variable} entry {index} prefix must be a string.")
        prefix: PortableStorePath | None = None
        if raw_prefix.strip("/"):
            prefix = PortableStorePath.parse_decoded(raw_prefix.strip("/"))
            if prefix is None:
                raise CaptureConfigError(
                    f"{variable} entry {index} prefix must be a portable relative path "
                    "(no '..', backslashes, or empty components)."
                )
        raw_patterns = entry.get("patterns", ["*"])
        if (
            not isinstance(raw_patterns, list)
            or not raw_patterns
            or len(raw_patterns) > MAX_STORE_SCAN_PATTERNS
            or not all(
                isinstance(pattern, str) and pattern.strip() and len(pattern) <= MAX_PATTERN_LENGTH
                for pattern in raw_patterns
            )
        ):
            raise CaptureConfigError(
                f"{variable} entry {index} patterns must be a list of 1-"
                f"{MAX_STORE_SCAN_PATTERNS} non-empty glob strings."
            )
        scans.append(
            StoreScan(
                project_id=_parse_uuid(entry["project_id"], variable=variable, key=f"#{index}"),
                store=store,
                prefix=prefix,
                patterns=tuple(pattern.strip() for pattern in raw_patterns),
                include_existing=include_existing,
            )
        )
    return tuple(scans)


def _parse_uuid(value: object, *, variable: str, key: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise CaptureConfigError(f"{variable} entry {key!r} must name a project UUID.") from exc


def _is_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not any(character.isspace() for character in value)
    )


def _is_zone(name: str) -> bool:
    if not name or len(name) > 100:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


MIN_POLL_INTERVAL_SECONDS: Final = 60.0
MAX_POLL_INTERVAL_SECONDS: Final = 86_400.0
MAX_STORE_SCAN_HASH_BYTES: Final = 512 * 1024 * 1024


def validate_capture_channel_settings(
    settings: Any,
    *,
    auth_secret_is_placeholder: bool,
) -> None:
    """Fail loudly on any malformed or half-configured capture channel.

    ``settings`` is a ``lab_tracker.config.Settings``; it is typed loosely only
    to keep this module import-free of ``config``. Every error names the
    variable to fix and never echoes a configured value.
    """

    def value(name: str) -> str:
        return str(getattr(settings, name) or "")

    interval = float(settings.integrations_poll_min_interval_seconds)
    if not MIN_POLL_INTERVAL_SECONDS <= interval <= MAX_POLL_INTERVAL_SECONDS:
        raise CaptureConfigError(
            "LAB_TRACKER_INTEGRATIONS_POLL_MIN_INTERVAL_SECONDS must be between "
            f"{MIN_POLL_INTERVAL_SECONDS:g} and {MAX_POLL_INTERVAL_SECONDS:g}."
        )
    state_path = value("integrations_state_path").strip()
    if state_path and not Path(state_path).expanduser().is_absolute():
        raise CaptureConfigError("LAB_TRACKER_INTEGRATIONS_STATE_PATH must be an absolute path.")
    hash_cap = settings.store_scan_hash_max_bytes
    if type(hash_cap) is not int or not 0 <= hash_cap <= MAX_STORE_SCAN_HASH_BYTES:
        raise CaptureConfigError(
            "LAB_TRACKER_STORE_SCAN_HASH_MAX_BYTES must be an integer between 0 and "
            f"{MAX_STORE_SCAN_HASH_BYTES}."
        )

    parse_user_emails(value("capture_user_emails"), variable="LAB_TRACKER_CAPTURE_USER_EMAILS")

    slack_parts = {
        "LAB_TRACKER_SLACK_WORKSPACE_URL": value("slack_workspace_url"),
        "LAB_TRACKER_SLACK_CHANNEL_PROJECTS": value("slack_channel_projects"),
        "LAB_TRACKER_SLACK_USERS": value("slack_users"),
    }
    parse_slack_workspace_url(
        slack_parts["LAB_TRACKER_SLACK_WORKSPACE_URL"],
        variable="LAB_TRACKER_SLACK_WORKSPACE_URL",
    )
    parse_slack_channel_projects(
        slack_parts["LAB_TRACKER_SLACK_CHANNEL_PROJECTS"],
        variable="LAB_TRACKER_SLACK_CHANNEL_PROJECTS",
    )
    parse_slack_id_map(
        slack_parts["LAB_TRACKER_SLACK_USERS"], variable="LAB_TRACKER_SLACK_USERS", kind="user"
    )
    signing_secret = value("slack_signing_secret")
    if signing_secret and len(signing_secret.strip()) < 16:
        raise CaptureConfigError("LAB_TRACKER_SLACK_SIGNING_SECRET is too short to be real.")
    if not signing_secret.strip():
        configured = sorted(name for name, raw in slack_parts.items() if raw.strip())
        if configured:
            raise CaptureConfigError(
                f"{', '.join(configured)} requires LAB_TRACKER_SLACK_SIGNING_SECRET."
            )

    address = parse_capture_address(
        value("email_capture_address"), variable="LAB_TRACKER_EMAIL_CAPTURE_ADDRESS"
    )
    imap_host = value("email_capture_imap_host").strip()
    if address is not None or imap_host:
        _validate_email_capture(settings, address=address, imap_host=imap_host)
        if auth_secret_is_placeholder:
            raise CaptureConfigError(
                "Email capture signs each capture address with LAB_TRACKER_AUTH_SECRET_KEY; "
                "set a strong non-placeholder secret before enabling "
                "LAB_TRACKER_EMAIL_CAPTURE_ADDRESS."
            )

    parse_booking_calendars(value("booking_calendars"), variable="LAB_TRACKER_BOOKING_CALENDARS")
    parse_store_scans(value("store_scans"), variable="LAB_TRACKER_STORE_SCANS")


def _validate_email_capture(
    settings: Any,
    *,
    address: CaptureAddress | None,
    imap_host: str,
) -> None:
    if address is None:
        raise CaptureConfigError(
            "LAB_TRACKER_EMAIL_CAPTURE_IMAP_HOST requires LAB_TRACKER_EMAIL_CAPTURE_ADDRESS."
        )
    if not imap_host:
        raise CaptureConfigError(
            "LAB_TRACKER_EMAIL_CAPTURE_ADDRESS requires LAB_TRACKER_EMAIL_CAPTURE_IMAP_HOST."
        )
    if any(character.isspace() or character in "/@" for character in imap_host):
        raise CaptureConfigError("LAB_TRACKER_EMAIL_CAPTURE_IMAP_HOST must be a bare hostname.")
    port = settings.email_capture_imap_port
    if type(port) is not int or not 1 <= port <= 65535:
        raise CaptureConfigError("LAB_TRACKER_EMAIL_CAPTURE_IMAP_PORT must be 1-65535.")
    if not str(settings.email_capture_imap_username or "").strip():
        raise CaptureConfigError("Email capture requires LAB_TRACKER_EMAIL_CAPTURE_IMAP_USERNAME.")
    password = str(settings.email_capture_imap_password or "")
    password_file = str(settings.email_capture_imap_password_file or "").strip()
    if bool(password) == bool(password_file):
        raise CaptureConfigError(
            "Set exactly one of LAB_TRACKER_EMAIL_CAPTURE_IMAP_PASSWORD and "
            "LAB_TRACKER_EMAIL_CAPTURE_IMAP_PASSWORD_FILE."
        )
    if password_file:
        read_password_file(password_file)
    folder = str(settings.email_capture_imap_folder or "")
    processed = str(settings.email_capture_processed_folder or "")
    for variable, folder_name, required in (
        ("LAB_TRACKER_EMAIL_CAPTURE_IMAP_FOLDER", folder, True),
        ("LAB_TRACKER_EMAIL_CAPTURE_PROCESSED_FOLDER", processed, False),
    ):
        if (required and not folder_name.strip()) or not _is_safe_mailbox_name(folder_name):
            raise CaptureConfigError(f"{variable} must be a plain IMAP mailbox name.")
    if processed and processed == folder:
        raise CaptureConfigError(
            "LAB_TRACKER_EMAIL_CAPTURE_PROCESSED_FOLDER must differ from the polled folder."
        )


def read_password_file(path: str) -> str:
    """Read a password file (trailing newline stripped), failing with a static message."""

    try:
        secret = Path(path).expanduser().read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise CaptureConfigError(
            "LAB_TRACKER_EMAIL_CAPTURE_IMAP_PASSWORD_FILE could not be read."
        ) from exc
    if not secret:
        raise CaptureConfigError("LAB_TRACKER_EMAIL_CAPTURE_IMAP_PASSWORD_FILE is empty.")
    return secret


def _is_safe_mailbox_name(name: str) -> bool:
    if len(name) > 200:
        return False
    return all(character.isprintable() and character not in '"\\*%\r\n' for character in name)
