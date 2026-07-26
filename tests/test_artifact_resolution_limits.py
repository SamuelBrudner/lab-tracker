from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.artifact_resolution_limits import (
    DEFAULT_MAX_BYTES,
    MAX_ARTIFACT_BYTE_OFFSET,
    MAX_INLINE_ARTIFACT_BYTES,
    ArtifactContentBounds,
    ArtifactContentBoundsError,
)


def test_artifact_content_bounds_exports_one_eight_mebibyte_default_and_hard_cap():
    assert DEFAULT_MAX_BYTES == 8 * 1024 * 1024
    assert MAX_INLINE_ARTIFACT_BYTES == DEFAULT_MAX_BYTES
    assert MAX_ARTIFACT_BYTE_OFFSET == 2**53 - 1


def test_request_bounds_apply_default_and_calculate_selected_view_allowance():
    unbounded_view = ArtifactContentBounds.for_request(None, None, None)
    ranged_view = ArtifactContentBounds.for_request(4, 7, 20)
    short_view = ArtifactContentBounds.for_request(20, 7, 10)
    empty_view = ArtifactContentBounds.for_request(20, 7, 7)

    assert unbounded_view == ArtifactContentBounds(DEFAULT_MAX_BYTES, None)
    assert unbounded_view.returned_allowance == DEFAULT_MAX_BYTES
    assert ranged_view.byte_range == (7, 20)
    assert ranged_view.returned_allowance == 4
    assert short_view.returned_allowance == 3
    assert empty_view.returned_allowance == 0


def test_resolver_bounds_require_a_two_item_tuple_and_reuse_request_contract():
    bounds = ArtifactContentBounds.from_resolver(
        MAX_INLINE_ARTIFACT_BYTES,
        (0, MAX_ARTIFACT_BYTE_OFFSET),
    )

    assert bounds.max_bytes == MAX_INLINE_ARTIFACT_BYTES
    assert bounds.byte_range == (0, MAX_ARTIFACT_BYTE_OFFSET)

    with pytest.raises(ArtifactContentBoundsError, match="two-item tuple"):
        ArtifactContentBounds.from_resolver(1, [0, 1])  # type: ignore[arg-type]
    with pytest.raises(ArtifactContentBoundsError, match="two-item tuple"):
        ArtifactContentBounds.from_resolver(1, (0, 1, 2))  # type: ignore[arg-type]


def test_resolver_bounds_never_apply_the_request_boundary_default():
    with pytest.raises(ArtifactContentBoundsError, match="must be an integer"):
        ArtifactContentBounds.from_resolver(None, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, False, 1.0, "1", b"1", object()])
def test_max_bytes_requires_an_exact_integer(value):
    with pytest.raises(ArtifactContentBoundsError, match="must be an integer"):
        ArtifactContentBounds.for_request(value, None, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [0, -1, MAX_INLINE_ARTIFACT_BYTES + 1])
def test_max_bytes_must_be_inside_the_global_cap(value):
    with pytest.raises(ArtifactContentBoundsError, match="between 1 and"):
        ArtifactContentBounds.for_request(value, None, None)


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1", b"1", object()])
def test_offsets_require_exact_integers(field, value):
    start, end = (value, 1) if field == "start" else (0, value)

    with pytest.raises(ArtifactContentBoundsError, match="must be an integer"):
        ArtifactContentBounds.for_request(1, start, end)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("value", [-1, MAX_ARTIFACT_BYTE_OFFSET + 1])
def test_offsets_stay_inside_the_portable_inclusive_domain(field, value):
    start, end = (value, value) if field == "start" else (0, value)

    with pytest.raises(ArtifactContentBoundsError, match="between 0 and"):
        ArtifactContentBounds.for_request(1, start, end)


@pytest.mark.parametrize(("start", "end"), [(None, 1), (0, None)])
def test_request_bounds_require_both_range_endpoints_or_neither(start, end):
    with pytest.raises(ArtifactContentBoundsError, match="provided together"):
        ArtifactContentBounds.for_request(1, start, end)


def test_request_bounds_reject_a_reversed_half_open_range():
    with pytest.raises(
        ArtifactContentBoundsError,
        match="greater than or equal to byte_start",
    ):
        ArtifactContentBounds.for_request(1, 2, 1)


def test_direct_construction_cannot_bypass_validation_or_derived_state():
    with pytest.raises(ArtifactContentBoundsError):
        ArtifactContentBounds(MAX_INLINE_ARTIFACT_BYTES + 1, None)
    with pytest.raises(ArtifactContentBoundsError):
        ArtifactContentBounds(1, (2, 1))

    bounds = ArtifactContentBounds(1, None)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        bounds.max_bytes = 2  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        bounds.unvalidated = True  # type: ignore[attr-defined]
