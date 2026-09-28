"""Helpers shared by the server capture channels."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from lab_tracker.auth import AuthContext, PrincipalType, User

CAPTURE_CHANNEL_KEY: Final = "capture_channel"
CAPTURE_BUNDLE_ID_KEY: Final = "capture_bundle_id"
CAPTURED_AT_KEY: Final = "captured_at"
EVIDENCE_SOURCE_PROVIDER_KEY: Final = "evidence_source_provider"
EVIDENCE_SOURCE_URI_KEY: Final = "evidence_source_uri"
EVIDENCE_SOURCE_EXTERNAL_ID_KEY: Final = "evidence_source_external_id"
EVIDENCE_SOURCE_OBSERVED_AT_KEY: Final = "evidence_source_observed_at"
EVIDENCE_CAPTURE_KIND_KEY: Final = "evidence_capture_kind"
EVIDENCE_CONTENT_HASH_KEY: Final = "evidence_content_hash"
EVIDENCE_ADAPTER_KEY: Final = "evidence_adapter"
EVIDENCE_TITLE_KEY: Final = "evidence_title"
CAPTURE_TEXT_TRUNCATED_KEY: Final = "capture_text_truncated"
CAPTURE_TEXT_ORIGINAL_CHARS_KEY: Final = "capture_text_original_chars"

# Bounded body stored for any person-authored text capture (Slack, email).
MAX_CAPTURE_TEXT_CHARS: Final = 8_000
MAX_TITLE_CHARS: Final = 200
MAX_METADATA_VALUE_CHARS: Final = 500
_TRUNCATION_MARKER: Final = "\n[… truncated by Lab Tracker]"


@dataclass(frozen=True, slots=True)
class BoundedText:
    """A text body cut to a stated cap, remembering whether it was cut."""

    text: str
    truncated: bool
    original_chars: int

    def metadata(self) -> dict[str, str | int | bool]:
        """Metadata recording a truncation (empty when nothing was cut)."""

        if not self.truncated:
            return {}
        return {
            CAPTURE_TEXT_TRUNCATED_KEY: True,
            CAPTURE_TEXT_ORIGINAL_CHARS_KEY: self.original_chars,
        }


def bound_text(text: str, *, limit: int = MAX_CAPTURE_TEXT_CHARS) -> BoundedText:
    """Cut ``text`` to at most ``limit`` characters, marking the cut."""

    cleaned = text.strip()
    if len(cleaned) <= limit:
        return BoundedText(cleaned, False, len(cleaned))
    kept = cleaned[: max(0, limit - len(_TRUNCATION_MARKER))].rstrip()
    return BoundedText(f"{kept}{_TRUNCATION_MARKER}", True, len(cleaned))


def bound_value(value: str, *, limit: int = MAX_METADATA_VALUE_CHARS) -> str:
    """Cut a single-line metadata value to ``limit`` characters."""

    collapsed = " ".join(value.split())
    return collapsed[:limit]


def sha256_hex(payload: bytes) -> str:
    """Bare lower-case SHA-256 hex digest (the ``evidence_content_hash`` form)."""

    return hashlib.sha256(payload).hexdigest()


def stable_key(prefix: str, *parts: str, length: int = 40) -> str:
    """A bounded, deterministic ``client_capture_id`` from identity parts."""

    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:length]}"


def channel_principal(user: User, *, label: str) -> AuthContext:
    """The principal a channel acts as when a verified person captured through it.

    The note is authored by that person (``created_by`` is their user id) and
    ``label`` becomes the note's ``origin_provider``. The principal type is
    ``SERVICE``: the person is not operating a Lab Tracker session, so the
    structural human-commit gate can never admit this principal.
    """

    return AuthContext(
        user_id=user.user_id,
        role=user.role,
        principal_type=PrincipalType.SERVICE,
        principal_label=label,
    )


class UserDirectory(Protocol):
    """The two user lookups channel mapping needs (``AuthService`` satisfies it)."""

    def get_user(self, username: str) -> User | None: ...

    def get_user_by_id(self, user_id: UUID) -> User | None: ...


def resolve_user_ref(
    users: UserDirectory,
    ref: str,
    *,
    email_directory: dict[str, str] | None = None,
) -> User | None:
    """Resolve an operator user reference: a user id, a username, or a mapped email."""

    cleaned = ref.strip()
    if "@" in cleaned:
        mapped = (email_directory or {}).get(cleaned.lower())
        if mapped is None:
            return None
        cleaned = mapped
    try:
        user_id = UUID(cleaned)
    except ValueError:
        return users.get_user(cleaned)
    return users.get_user_by_id(user_id)


def emails_for_user(user: User, email_directory: dict[str, str]) -> tuple[str, ...]:
    """Every configured sender address that names ``user`` (by id or username)."""

    names = {str(user.user_id), user.username.lower()}
    return tuple(
        sorted(email for email, ref in email_directory.items() if ref.strip().lower() in names)
    )
