"""Tests for the note capture clock (services/note_observed_at)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from lab_tracker.models import Note
from lab_tracker.services.note_observed_at import (
    ADAPTER_OBSERVED_AT_KEY,
    CLIENT_CAPTURED_AT_KEY,
    ObservedAtSource,
    note_observed_at,
)

_CREATED_AT = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)
_CLIENT_AT = datetime(2026, 6, 25, 10, 30, tzinfo=timezone.utc)
_ADAPTER_AT = datetime(2026, 6, 25, 11, 15, tzinfo=timezone.utc)


def _note(metadata: dict[str, str] | None = None, *, created_at: datetime = _CREATED_AT) -> Note:
    return Note(
        note_id=uuid4(),
        project_id=uuid4(),
        raw_content="Rig 2 Fly 12",
        created_at=created_at,
        metadata=dict(metadata or {}),
    )


def test_prefers_client_captured_at() -> None:
    observed_at, source = note_observed_at(_note({CLIENT_CAPTURED_AT_KEY: _CLIENT_AT.isoformat()}))

    assert observed_at == _CLIENT_AT
    assert source is ObservedAtSource.CLIENT


def test_falls_back_to_adapter_observed_at() -> None:
    observed_at, source = note_observed_at(
        _note({ADAPTER_OBSERVED_AT_KEY: _ADAPTER_AT.isoformat()})
    )

    assert observed_at == _ADAPTER_AT
    assert source is ObservedAtSource.ADAPTER


def test_client_wins_over_adapter() -> None:
    observed_at, source = note_observed_at(
        _note(
            {
                CLIENT_CAPTURED_AT_KEY: _CLIENT_AT.isoformat(),
                ADAPTER_OBSERVED_AT_KEY: _ADAPTER_AT.isoformat(),
            }
        )
    )

    assert observed_at == _CLIENT_AT
    assert source is ObservedAtSource.CLIENT


def test_server_when_no_metadata() -> None:
    observed_at, source = note_observed_at(_note())

    assert observed_at == _CREATED_AT
    assert source is ObservedAtSource.SERVER


def test_ignores_unparsable_and_naive_values() -> None:
    # An unparsable client clock falls through to the adapter clock.
    observed_at, source = note_observed_at(
        _note(
            {
                CLIENT_CAPTURED_AT_KEY: "not-a-date",
                ADAPTER_OBSERVED_AT_KEY: _ADAPTER_AT.isoformat(),
            }
        )
    )
    assert observed_at == _ADAPTER_AT
    assert source is ObservedAtSource.ADAPTER

    # A naive clock (no offset) is ambiguous, so it counts as absent too.
    naive_only, naive_source = note_observed_at(
        _note(
            {
                CLIENT_CAPTURED_AT_KEY: "2026-06-25T10:30:00",
                ADAPTER_OBSERVED_AT_KEY: "2026-06-25T11:15:00",
            }
        )
    )
    assert naive_only == _CREATED_AT
    assert naive_source is ObservedAtSource.SERVER


def test_clamps_future_client_clock_to_created_at() -> None:
    skewed = (_CREATED_AT + timedelta(hours=3)).isoformat()

    observed_at, source = note_observed_at(_note({CLIENT_CAPTURED_AT_KEY: skewed}))

    # Clamped to the server receipt, but still labelled as the client's clock.
    assert observed_at == _CREATED_AT
    assert source is ObservedAtSource.CLIENT


def test_accepts_zulu_suffix() -> None:
    observed_at, source = note_observed_at(_note({CLIENT_CAPTURED_AT_KEY: "2026-06-25T10:30:00Z"}))

    assert observed_at == _CLIENT_AT
    assert observed_at.tzinfo is not None
    assert source is ObservedAtSource.CLIENT


def test_naive_created_at_is_treated_as_utc() -> None:
    naive_created_at = datetime(2026, 6, 25, 18, 0)

    observed_at, source = note_observed_at(_note(created_at=naive_created_at))

    assert observed_at == _CREATED_AT
    assert observed_at.tzinfo is not None
    assert source is ObservedAtSource.SERVER


def test_normalizes_offset_clocks_to_utc() -> None:
    observed_at, _source = note_observed_at(
        _note({CLIENT_CAPTURED_AT_KEY: "2026-06-25T12:30:00+02:00"})
    )

    assert observed_at == _CLIENT_AT
    assert observed_at.utcoffset() == timedelta(0)
