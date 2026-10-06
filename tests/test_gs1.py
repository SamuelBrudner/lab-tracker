"""GS1 element-string parsing: AI lengths, separators, dates, and check digits.

The property-style tests generate element strings from a seeded RNG (no
hypothesis dependency), encode them the way a GS1-128 / DataMatrix / QR
symbol carries them (FNC1 as GS, no separator after a predefined-length AI)
and in the human-readable ``(01)...`` form, and check the parse round-trips.
"""

from __future__ import annotations

import contextlib
import random
import string
from datetime import date

import pytest

from lab_tracker.gs1 import (
    GS,
    Gs1Element,
    Gs1ParseError,
    gs1_date_to_iso,
    gs1_fields,
    gtin_check_digit,
    parse_element_string,
    parse_gs1_fields,
)

TODAY = date(2026, 9, 28)
GTIN = "09506000134352"  # GS1's documentation GTIN, valid check digit


def test_raw_element_string_with_fixed_and_variable_length_ais() -> None:
    text = f"01{GTIN}1726123110ABC123{GS}21SER9{GS}240CAT-42"

    assert parse_element_string(text) == (
        Gs1Element("01", GTIN),
        Gs1Element("17", "261231"),
        Gs1Element("10", "ABC123"),
        Gs1Element("21", "SER9"),
        Gs1Element("240", "CAT-42"),
    )


def test_fixed_length_ai_needs_no_separator_but_tolerates_one() -> None:
    assert parse_element_string(f"01{GTIN}{GS}10LOT") == parse_element_string(f"01{GTIN}10LOT")


def test_variable_length_ai_runs_to_the_separator_not_into_the_next_ai() -> None:
    # Without the GS the lot would swallow "17261231".
    separated = parse_element_string(f"10LOT7{GS}17261231")
    swallowed = parse_element_string("10LOT717261231")

    assert separated == (Gs1Element("10", "LOT7"), Gs1Element("17", "261231"))
    assert swallowed == (Gs1Element("10", "LOT717261231"),)


def test_symbology_identifier_and_leading_fnc1_are_stripped() -> None:
    expected = (Gs1Element("01", GTIN), Gs1Element("10", "A1"))
    for prefix in ("]C1", "]d2", "]Q3", "]e0", "]J1", GS, f"]C1{GS}"):
        assert parse_element_string(f"{prefix}01{GTIN}10A1") == expected, prefix


def test_escaped_and_symbol_group_separators_are_accepted() -> None:
    expected = (Gs1Element("10", "L1"), Gs1Element("21", "S1"))
    assert parse_element_string("10L1<GS>21S1") == expected
    assert parse_element_string("10L1␝21S1") == expected


def test_human_readable_form_with_optional_spaces() -> None:
    expected = (
        Gs1Element("01", GTIN),
        Gs1Element("17", "270100"),
        Gs1Element("10", "LOT 9"),
        Gs1Element("240", "X-1"),
    )
    assert parse_element_string(f"(01){GTIN}(17)270100(10)LOT 9(240)X-1") == expected
    assert parse_element_string(f" (01) {GTIN} (17) 270100 (10) LOT 9 (240) X-1 ") == expected


def test_three_and_four_digit_ais_are_read_by_prefix() -> None:
    # 3103 (net weight, kg, 3 decimals) is a predefined-length 4-digit AI;
    # 400 (order number) is a variable-length 3-digit AI.
    assert parse_element_string(f"3103000750400PO-17{GS}10L") == (
        Gs1Element("3103", "000750"),
        Gs1Element("400", "PO-17"),
        Gs1Element("10", "L"),
    )
    assert parse_element_string("4142345678901234") == (Gs1Element("414", "2345678901234"),)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        GS,
        "ABC",  # not an AI
        "0512345",  # unassigned AI prefix 05
        "0112345",  # GTIN too short for its predefined length
        "17" + "2612",  # date too short
        "10",  # AI without data
        f"10{GS}21X",  # empty variable-length data
        "(01)123",  # HRI fixed length mismatch
        "(1)ABC",  # HRI AI shorter than its prefix says
        "(2400)X",  # HRI AI longer than its prefix says
        "hello (10)ABC",  # text before the first HRI AI
        "(99)",  # HRI AI without data
        f"01{GTIN[:-1]}X",  # non-digit in a numeric predefined field
    ],
)
def test_malformed_element_strings_raise_parse_error(text: str) -> None:
    with pytest.raises(Gs1ParseError):
        parse_element_string(text)


def test_gtin_check_digit() -> None:
    assert gtin_check_digit(GTIN[:-1]) == GTIN[-1]
    assert gtin_check_digit("629104150021") == "3"  # GTIN-13 6291041500213
    assert gtin_check_digit("0001234560001") == "2"


@pytest.mark.parametrize(
    ("yymmdd", "iso"),
    [
        ("261231", "2026-12-31"),
        ("260200", "2026-02-28"),  # DD=00 means the last day of the month
        ("240200", "2024-02-29"),  # ...including leap years
        ("261100", "2026-11-30"),
        ("760101", "2076-01-01"),  # +50 years stays in this century
        ("770101", "1977-01-01"),  # +51 years goes back a century
        ("000101", "2000-01-01"),
        ("990101", "1999-01-01"),
    ],
)
def test_gs1_dates_use_the_dd00_and_century_rules(yymmdd: str, iso: str) -> None:
    assert gs1_date_to_iso(yymmdd, today=TODAY) == iso


def test_century_rule_moves_forward_near_the_end_of_a_century() -> None:
    # In 2090, YY=10 is a -80 difference: the next century.
    assert gs1_date_to_iso("100101", today=date(2090, 6, 1)) == "2110-01-01"
    # -50 is still "next century"; -49 stays in this one.
    assert gs1_date_to_iso("400101", today=date(2090, 6, 1)) == "2140-01-01"
    assert gs1_date_to_iso("410101", today=date(2090, 6, 1)) == "2041-01-01"


@pytest.mark.parametrize("yymmdd", ["261301", "260001", "260230", "260431", "26010", "2601AB"])
def test_invalid_gs1_dates_raise(yymmdd: str) -> None:
    with pytest.raises(Gs1ParseError):
        gs1_date_to_iso(yymmdd, today=TODAY)


def test_fields_map_the_retained_ais_to_metadata_keys() -> None:
    elements = parse_element_string(f"01{GTIN}1726020010LOT-1{GS}21S/N 9{GS}240CAT{GS}9112")

    assert gs1_fields(elements, today=TODAY) == {
        "barcode_gs1_gtin": GTIN,
        "barcode_gs1_expiry": "2026-02-28",
        "barcode_gs1_lot": "LOT-1",
        "barcode_gs1_serial": "S/N 9",
        "barcode_gs1_catalog": "CAT",
    }


def test_fields_reject_a_bad_gtin_check_digit_and_invalid_expiry() -> None:
    bad_gtin = GTIN[:-1] + ("3" if GTIN[-1] != "3" else "4")
    with pytest.raises(Gs1ParseError):
        gs1_fields(parse_element_string(f"01{bad_gtin}"), today=TODAY)
    with pytest.raises(Gs1ParseError):
        gs1_fields(parse_element_string("17261340"), today=TODAY)


def test_fields_keep_the_first_value_of_a_repeated_ai() -> None:
    elements = (Gs1Element("10", "FIRST"), Gs1Element("10", "SECOND"))
    assert gs1_fields(elements, today=TODAY) == {"barcode_gs1_lot": "FIRST"}


def test_parse_gs1_fields_returns_none_for_non_gs1_text() -> None:
    assert parse_gs1_fields("https://example.org/x", today=TODAY) is None
    assert parse_gs1_fields("LT-ABC", today=TODAY) is None
    assert parse_gs1_fields(f"(01){GTIN}(10)L", today=TODAY) == {
        "barcode_gs1_gtin": GTIN,
        "barcode_gs1_lot": "L",
    }


def test_parse_gs1_fields_requires_the_gs1_flag_for_unparenthesized_digits() -> None:
    raw = f"01{GTIN}10L"
    assert parse_gs1_fields(raw, today=TODAY) is None
    assert parse_gs1_fields(raw, today=TODAY, is_gs1=True) == {
        "barcode_gs1_gtin": GTIN,
        "barcode_gs1_lot": "L",
    }
    # A GS1 identifier prefix is itself the flag.
    assert parse_gs1_fields(f"]C1{raw}", today=TODAY) == {
        "barcode_gs1_gtin": GTIN,
        "barcode_gs1_lot": "L",
    }


def test_values_longer_than_their_ai_maximum_are_not_gs1() -> None:
    assert parse_gs1_fields("(10)" + "L" * 20, today=TODAY) == {"barcode_gs1_lot": "L" * 20}
    assert parse_gs1_fields("(10)" + "L" * 21, today=TODAY) is None
    assert parse_gs1_fields("(240)" + "C" * 31, today=TODAY) is None


def test_valid_gs1_without_a_retained_ai_yields_none() -> None:
    # An SSCC alone is valid GS1 but maps to no barcode_gs1_* key.
    assert parse_gs1_fields("(00)012345678901234560", today=TODAY) is None


# --- property-style round trips -------------------------------------------------

_CHARSET_82 = string.ascii_letters + string.digits + "!\"%&'*+,-./:;<=>?_"
# (ai, fixed data length or None for variable, max variable length)
_AI_SAMPLES: tuple[tuple[str, int | None, int], ...] = (
    ("00", 18, 18),
    ("01", 14, 14),
    ("02", 14, 14),
    ("10", None, 20),
    ("11", 6, 6),
    ("17", 6, 6),
    ("20", 2, 2),
    ("21", None, 20),
    ("22", None, 20),
    ("240", None, 30),
    ("241", None, 30),
    ("30", None, 8),
    ("3103", 6, 6),
    ("3922", None, 15),
    ("400", None, 30),
    ("410", 13, 13),
    ("422", None, 3),
    ("4300", None, 35),
    ("7003", None, 10),
    ("8004", None, 30),
    ("90", None, 30),
)


def _random_elements(rng: random.Random) -> list[Gs1Element]:
    elements = []
    for _ in range(rng.randint(1, 6)):
        ai, fixed, maximum = rng.choice(_AI_SAMPLES)
        if fixed is not None:
            data = "".join(rng.choice(string.digits) for _ in range(fixed))
        else:
            # Parentheses are legal GS1 data but ambiguous in HRI; skip them.
            data = "".join(rng.choice(_CHARSET_82) for _ in range(rng.randint(1, maximum)))
        elements.append(Gs1Element(ai, data))
    return elements


def _encode_raw(elements: list[Gs1Element], rng: random.Random) -> str:
    parts: list[str] = []
    for index, element in enumerate(elements):
        parts.append(element.ai + element.data)
        last = index == len(elements) - 1
        fixed = next(sample[1] for sample in _AI_SAMPLES if sample[0] == element.ai)
        if not last and (fixed is None or rng.random() < 0.2):
            parts.append(GS)
    return "".join(parts)


def test_random_raw_element_strings_round_trip() -> None:
    rng = random.Random(20260928)
    for _ in range(600):
        elements = _random_elements(rng)
        encoded = _encode_raw(elements, rng)
        assert parse_element_string(encoded) == tuple(elements), repr(encoded)
        assert parse_element_string("]d2" + encoded) == tuple(elements)


def test_random_human_readable_strings_round_trip() -> None:
    rng = random.Random(4242)
    for _ in range(600):
        elements = [
            Gs1Element(element.ai, element.data.strip() or "X") for element in _random_elements(rng)
        ]
        encoded = "".join(f"({element.ai}){element.data}" for element in elements)
        assert parse_element_string(encoded) == tuple(elements), repr(encoded)


def test_random_truncations_and_garbage_never_raise_anything_but_parse_errors() -> None:
    rng = random.Random(7)
    alphabet = string.printable + GS + "()"
    for _ in range(1500):
        elements = _random_elements(rng)
        encoded = _encode_raw(elements, rng)
        candidates = [
            encoded[: rng.randint(0, len(encoded))],
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))),
            "(" + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))),
        ]
        for candidate in candidates:
            try:
                parsed = parse_element_string(candidate)
            except Gs1ParseError:
                continue
            assert all(element.data for element in parsed)
            with contextlib.suppress(Gs1ParseError):
                gs1_fields(parsed, today=TODAY)
