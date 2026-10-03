"""The capture-setup catalog: server-owned copy, the drafter's schema, trusted candidates."""

from __future__ import annotations

import ast
import base64
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from lab_tracker import capture_setup_catalog
from lab_tracker.capture_setup_catalog import (
    CANDIDATES_PACKET_KEY,
    CAPTURE_SETUP_GUIDES,
    COOLDOWN_DAYS,
    EXPLANATION_MAX_CHARS,
    MAX_CANDIDATES,
    MAX_DEBRIEF_SESSIONS,
    MAX_NOTE_IDS,
    MAX_RECOMMENDATIONS,
    RESPONSE_FIELD,
    RESULT_PACKET_KEY,
    SESSION_SCOPED_GAPS,
    THIN_CAPTURE_MAX_CHARS,
    CaptureSetupGap,
    CaptureSetupKind,
    candidate_id_for,
    capture_setup_items_schema,
    capture_setup_prompt_lines,
    trusted_candidates,
)

# Words that would promise an affordance Lab Tracker does not have: there is
# no print or label feature, and the voice debrief is not a setting.
_FORBIDDEN_COPY = re.compile(r"print|turn on|enable", re.IGNORECASE)


def _guide_copy(gap: CaptureSetupGap) -> list[str]:
    guide = CAPTURE_SETUP_GUIDES[gap]
    return [guide.title, *guide.steps, guide.server_explanation, guide.command or ""]


def _label(session_id: UUID, session_type: str = "operational") -> str:
    code = base64.b32encode(session_id.bytes).decode("ascii").rstrip("=")
    return f"{session_type} session LT-{code}"


def _candidate(**overrides: Any) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "candidate_id": "sessionless_app_captures",
        "kind": "session_capture_link",
        "gap": "sessionless_app_captures",
        "detected": True,
        "note_ids": [str(uuid4()) for _ in range(3)],
        "note_count": 3,
        "session_id": None,
        "session_label": None,
    }
    candidate.update(overrides)
    return candidate


def _debrief_candidate(session_id: UUID, **overrides: Any) -> dict[str, Any]:
    return _candidate(
        candidate_id=f"closed_without_debrief:{session_id}",
        kind="session_debrief",
        gap="closed_without_debrief",
        session_id=str(session_id),
        session_label=_label(session_id),
        **overrides,
    )


def test_the_catalog_imports_only_the_standard_library() -> None:
    # The provider clients and the services both import it, so it must stay a leaf.
    tree = ast.parse(Path(capture_setup_catalog.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name if isinstance(node, ast.Import) else node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    roots = {name.split(".")[0] for name in imported}
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, sorted(roots)


def test_the_packet_keys_and_bounds_are_the_documented_constants() -> None:
    assert CANDIDATES_PACKET_KEY == "capture_setup_candidates"
    assert RESULT_PACKET_KEY == "capture_setup"
    assert RESPONSE_FIELD == "capture_setup_recommendations"
    assert (MAX_CANDIDATES, MAX_DEBRIEF_SESSIONS, MAX_NOTE_IDS, MAX_RECOMMENDATIONS) == (
        8,
        3,
        20,
        6,
    )
    assert (EXPLANATION_MAX_CHARS, THIN_CAPTURE_MAX_CHARS, COOLDOWN_DAYS) == (280, 40, 7)


def test_every_gap_has_a_guide_and_every_kind_is_covered() -> None:
    assert set(CAPTURE_SETUP_GUIDES) == set(CaptureSetupGap)
    assert {guide.kind for guide in CAPTURE_SETUP_GUIDES.values()} == set(CaptureSetupKind)
    assert [kind.value for kind in CaptureSetupKind] == [
        "session_capture_link",
        "shortcut_session",
        "watch_folder_link_code",
        "session_debrief",
        "nwb_h5py",
    ]
    assert [gap.value for gap in CaptureSetupGap] == [
        "sessionless_app_captures",
        "shortcut_no_active_session",
        "shortcut_without_session",
        "sessionless_watch_files",
        "watch_session_from_checkout",
        "closed_without_debrief",
        "nwb_headers_unread",
    ]


def test_guides_are_frozen() -> None:
    guide = CAPTURE_SETUP_GUIDES[CaptureSetupGap.NWB_HEADERS_UNREAD]
    with pytest.raises(AttributeError):
        guide.title = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        CAPTURE_SETUP_GUIDES[CaptureSetupGap.NWB_HEADERS_UNREAD] = guide  # type: ignore[index]


@pytest.mark.parametrize("gap", list(CaptureSetupGap), ids=lambda gap: gap.value)
def test_guides_have_titles_steps_and_app_paths_under_app(gap: CaptureSetupGap) -> None:
    guide = CAPTURE_SETUP_GUIDES[gap]
    assert guide.title.strip() and guide.steps
    assert all(step.strip() for step in guide.steps)
    assert guide.app_path is None or guide.app_path.startswith("/app")
    session_scoped = guide.app_path is not None and "{session_id}" in guide.app_path
    assert session_scoped == (gap in SESSION_SCOPED_GAPS)
    assert re.fullmatch(r"docs/[a-z0-9-]+\.md#[a-z0-9-]+", guide.doc), guide.doc
    assert guide.command is None or guide.command.startswith("lt ")
    assert "{count}" in guide.server_explanation
    assert guide.server_explanation.format(count=3).count("3") >= 1


def test_only_the_debrief_gap_is_scoped_to_one_session() -> None:
    assert frozenset({CaptureSetupGap.CLOSED_WITHOUT_DEBRIEF}) == SESSION_SCOPED_GAPS
    session_id = uuid4()
    assert candidate_id_for(CaptureSetupGap.NWB_HEADERS_UNREAD, None) == "nwb_headers_unread"
    assert (
        candidate_id_for(CaptureSetupGap.CLOSED_WITHOUT_DEBRIEF, session_id)
        == f"closed_without_debrief:{session_id}"
    )


@pytest.mark.parametrize("gap", list(CaptureSetupGap), ids=lambda gap: gap.value)
def test_guide_copy_never_promises_print_or_a_debrief_toggle(gap: CaptureSetupGap) -> None:
    offending = [text for text in _guide_copy(gap) if _FORBIDDEN_COPY.search(text)]
    assert not offending, offending


@pytest.mark.parametrize("gap", list(CaptureSetupGap), ids=lambda gap: gap.value)
def test_every_quoted_ui_label_is_pinned_and_every_pinned_label_is_quoted(
    gap: CaptureSetupGap,
) -> None:
    # The drift test checks each pinned label against its component, so a
    # label the copy quotes must be pinned, and a pinned label must be used.
    guide = CAPTURE_SETUP_GUIDES[gap]
    quoted = {
        match for text in (guide.title, *guide.steps) for match in re.findall(r'"([^"]+)"', text)
    }
    assert quoted == {label for label, _component in guide.ui_labels}


def test_items_schema_is_strict() -> None:
    schema = capture_setup_items_schema()
    assert schema["type"] == "array"
    item = schema["items"]
    assert item["type"] == "object"
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert set(item["properties"]) == {"candidate_id", "note_ids", "explanation"}
    assert item["properties"]["candidate_id"] == {"type": "string"}
    assert item["properties"]["note_ids"] == {"type": "array", "items": {"type": "string"}}
    assert item["properties"]["explanation"] == {"type": "string"}
    # Each call is a fresh copy, so a caller extending it cannot leak.
    schema["items"]["properties"]["extra"] = {}
    assert "extra" not in capture_setup_items_schema()["items"]["properties"]


def test_prompt_lines_describe_each_kind_and_its_gaps_without_steps() -> None:
    lines = capture_setup_prompt_lines()
    assert len(lines) == len(CaptureSetupKind)
    for line, kind in zip(lines, CaptureSetupKind, strict=True):
        assert line.startswith(f"- {kind.value}: ")
        gaps = [gap for gap, guide in CAPTURE_SETUP_GUIDES.items() if guide.kind is kind]
        assert all(gap.value in line for gap in gaps), line
        assert "`" not in line and "://" not in line


def test_the_debrief_prompt_line_claims_thin_captures_only_for_detected_candidates() -> None:
    # Every closed session with bench captures and no debrief in the batch is
    # offered; only detected=true says Lab Tracker found its captures thin.
    lines = dict(zip(CaptureSetupKind, capture_setup_prompt_lines(), strict=True))
    line = lines[CaptureSetupKind.SESSION_DEBRIEF]
    assert "little context" not in line
    claim = f"detected=true means several of those captures carry at most {THIN_CAPTURE_MAX_CHARS}"
    assert claim in line


def test_trusted_candidates_keep_server_values_in_a_fixed_shape() -> None:
    session_id = uuid4()
    app = _candidate(extra="dropped", note_text="never trusted")
    debrief = _debrief_candidate(session_id, detected=False)
    assert trusted_candidates([app, debrief]) == [
        {key: value for key, value in app.items() if key not in {"extra", "note_text"}},
        debrief,
    ]
    assert list(trusted_candidates([app])[0]) == [
        "candidate_id",
        "kind",
        "gap",
        "detected",
        "note_ids",
        "note_count",
        "session_id",
        "session_label",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"kind": "print_qr"}, id="unknown kind"),
        pytest.param({"gap": "sessionless_everything"}, id="unknown gap"),
        pytest.param({"gap": ["sessionless_app_captures"]}, id="unhashable gap"),
        pytest.param({"kind": "nwb_h5py"}, id="kind of another gap"),
        pytest.param({"candidate_id": "sessionless_app_captures:extra"}, id="foreign id"),
        pytest.param({"detected": "true"}, id="string detected"),
        pytest.param({"note_ids": "not-a-list"}, id="non-list note ids"),
        pytest.param({"note_ids": []}, id="no note ids"),
        pytest.param({"note_ids": ["not-a-uuid"]}, id="non-uuid note id"),
        pytest.param({"note_ids": [7]}, id="non-string note id"),
        pytest.param(
            {"note_ids": [str(uuid4()) for _ in range(MAX_NOTE_IDS + 1)], "note_count": 30},
            id="too many note ids",
        ),
        pytest.param({"note_count": "3"}, id="string count"),
        pytest.param({"note_count": True}, id="bool count"),
        pytest.param({"note_count": 2}, id="count below listed ids"),
        pytest.param({"session_id": str(uuid4())}, id="session on an unscoped gap"),
        pytest.param({"session_label": "operational session LT-AAAA"}, id="label without session"),
    ],
)
def test_trusted_candidates_keeps_only_whitelisted_server_values(
    overrides: dict[str, Any],
) -> None:
    assert trusted_candidates([_candidate(**overrides)]) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"session_id": None, "session_label": None},
        {"session_id": "not-a-uuid"},
        {"session_label": "operational session LT-AAAA"},
        {"session_label": "<b>injected</b>"},
        {"candidate_id": "closed_without_debrief"},
    ],
    ids=["no session", "bad session id", "label for another session", "free text", "bare id"],
)
def test_trusted_debrief_candidates_need_their_own_session_and_label(
    overrides: dict[str, Any],
) -> None:
    candidate = _debrief_candidate(uuid4())
    candidate.update(overrides)
    assert trusted_candidates([candidate]) == []


@pytest.mark.parametrize("value", [None, "candidates", {"candidate_id": "x"}, 3, [None, "x", 1]])
def test_trusted_candidates_of_a_malformed_block_is_empty(value: object) -> None:
    assert trusted_candidates(value) == []


def test_trusted_candidates_drop_repeats_and_cap_the_block() -> None:
    first = _candidate()
    repeated = _candidate()
    sessions = [_debrief_candidate(uuid4()) for _ in range(MAX_CANDIDATES + 2)]
    trusted = trusted_candidates([first, repeated, *sessions])
    assert len(trusted) == MAX_CANDIDATES
    assert trusted[0] == first
    assert [item["candidate_id"] for item in trusted].count("sessionless_app_captures") == 1


def test_trusted_candidates_canonicalize_note_and_session_ids() -> None:
    note_id = uuid4()
    session_id = uuid4()
    candidate = _debrief_candidate(session_id, note_ids=[note_id.hex.upper()], note_count=1)
    candidate["session_id"] = str(session_id).upper()
    candidate["candidate_id"] = f"closed_without_debrief:{session_id}"
    [trusted] = trusted_candidates([candidate])
    assert trusted["note_ids"] == [str(note_id)]
    assert trusted["session_id"] == str(session_id)
