"""Unit tests for the storage-layer TypeDecorators (db_types).

These verify the round-trip contract the domain<->ORM migration relies on:
liberal input (rich type or raw form), typed output, byte-identical storage,
and correct handling of NULL and naive datetimes. Round-trips are exercised
against a real in-memory SQLite table on both bind and result paths.
See docs/domain-orm-mapping-decision.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, select
from sqlalchemy.exc import StatementError

from lab_tracker.db_types import GUID, EnumType, UtcDateTime


class _Color(str, Enum):
    RED = "red"
    BLUE = "blue"


@pytest.fixture()
def engine():
    return create_engine("sqlite+pysqlite:///:memory:", future=True)


def _roundtrip(engine, coltype, value):
    """Insert then read back a single value through ``coltype`` on a fresh table."""
    md = MetaData()
    t = Table("rt", md, Column("id", String, primary_key=True), Column("v", coltype))
    t.drop(engine, checkfirst=True)
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(id="1", v=value))
    with engine.connect() as conn:
        return conn.execute(select(t.c.v)).scalar_one()


# --- GUID ---------------------------------------------------------------

def test_guid_roundtrips_uuid(engine):
    u = uuid4()
    assert _roundtrip(engine, GUID(), u) == u
    assert isinstance(_roundtrip(engine, GUID(), u), UUID)


def test_guid_accepts_str_input_returns_uuid(engine):
    u = uuid4()
    out = _roundtrip(engine, GUID(), str(u))
    assert out == u and isinstance(out, UUID)


def test_guid_stores_canonical_36_char_string(engine):
    u = uuid4()
    md = MetaData()
    t = Table("g", md, Column("v", GUID()))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(v=u))
        raw = conn.exec_driver_sql("SELECT v FROM g").scalar_one()
    assert raw == str(u) and len(raw) == 36


def test_guid_handles_none(engine):
    assert _roundtrip(engine, GUID(), None) is None


# --- UtcDateTime --------------------------------------------------------

def test_utcdatetime_aware_roundtrip(engine):
    dt = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    out = _roundtrip(engine, UtcDateTime(), dt)
    assert out == dt and out.tzinfo is not None


def test_utcdatetime_naive_input_becomes_utc(engine):
    naive = datetime(2024, 1, 2, 3, 4, 5)
    out = _roundtrip(engine, UtcDateTime(), naive)
    assert out.tzinfo is not None
    assert out == naive.replace(tzinfo=timezone.utc)


def test_utcdatetime_handles_none(engine):
    assert _roundtrip(engine, UtcDateTime(), None) is None


# --- EnumType -----------------------------------------------------------

def test_enumtype_roundtrips_member(engine):
    out = _roundtrip(engine, EnumType(_Color), _Color.RED)
    assert out is _Color.RED


def test_enumtype_accepts_raw_value(engine):
    out = _roundtrip(engine, EnumType(_Color), "blue")
    assert out is _Color.BLUE


def test_enumtype_stores_raw_value_string(engine):
    md = MetaData()
    t = Table("e", md, Column("v", EnumType(_Color)))
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(t.insert().values(v=_Color.RED))
        raw = conn.exec_driver_sql("SELECT v FROM e").scalar_one()
    assert raw == "red"


def test_enumtype_rejects_unknown_value(engine):
    # The ValueError from EnumType is raised during flush and wrapped by SQLAlchemy.
    with pytest.raises(StatementError, match="not a valid"):
        _roundtrip(engine, EnumType(_Color), "green")


def test_enumtype_handles_none(engine):
    assert _roundtrip(engine, EnumType(_Color), None) is None


def test_enumtype_distinct_enums_have_distinct_cache_keys():
    class _Other(str, Enum):
        A = "a"

    key_a = EnumType(_Color)._static_cache_key
    key_b = EnumType(_Other)._static_cache_key
    assert key_a != key_b
