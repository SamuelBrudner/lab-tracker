from __future__ import annotations

import re
from pathlib import Path

import lab_tracker_client
from lab_tracker.decision_context_constants import (
    CODE_CONVENTIONS_BLOCK_BEGIN,
    CODE_CONVENTIONS_BLOCK_END,
    code_conventions_version_line,
    code_facing_idioms,
    cursor_rules_mdc,
    managed_code_conventions_block,
)
from lab_tracker_client import __all__ as client_symbols


def test_code_facing_idioms_cover_shipped_idioms_and_citation_caveat() -> None:
    body = code_facing_idioms(symbols=["first_line_marker", "upsert_note"])

    assert "first_line_marker()" in body
    assert "upsert_note()" in body
    assert "EntityRef" in body
    assert "ids()" in body
    assert "lt_ids.json" in body
    assert "import_evidence_file()" in body
    assert "<!-- lt-cite:" in body
    assert "% lt-cite:" in body
    assert "Additional authoring convention (no tooling)" in body
    assert "Strip UUID-bearing citation tokens before external sharing" in body
    assert "savefig()" not in body


def test_code_facing_idioms_figure_section_is_symbol_gated() -> None:
    assert "savefig()" not in code_facing_idioms(symbols=["capture_figures"])

    gated = code_facing_idioms(symbols=["savefig", "capture_figures"])
    assert "savefig()" in gated
    assert "capture_figures()" in gated
    assert "run_context()" in gated

    default_body = code_facing_idioms()
    assert "savefig()" in default_body
    assert "capture_figures()" in default_body


def test_code_facing_idioms_named_client_symbols_are_exported() -> None:
    body = code_facing_idioms(symbols=client_symbols)
    names = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)\(", body))

    assert names <= set(client_symbols)


def test_code_facing_idioms_avoid_shell_or_install_directives() -> None:
    body = code_facing_idioms(symbols=client_symbols).lower()

    for forbidden in ("pip install", "shell", "execute", "subprocess", "curl "):
        assert forbidden not in body


def test_retained_surface_records_code_idiom_and_citation_decisions() -> None:
    doc = " ".join(Path("docs/retained-v1-surface.md").read_text(encoding="utf-8").split())

    assert "code-facing idiom teaching" in doc
    assert "lab-tracker://code-conventions" in doc
    assert "citation annotation tokens" in doc
    assert "authoring convention with no runtime tooling" in doc
    assert "UUID-bearing tokens should be stripped" in doc


def test_managed_code_conventions_block_has_stable_markers_and_version_line() -> None:
    body = code_facing_idioms()
    block = managed_code_conventions_block()

    assert "version=" not in CODE_CONVENTIONS_BLOCK_BEGIN
    assert "sha256=" not in CODE_CONVENTIONS_BLOCK_BEGIN
    assert "version=" not in CODE_CONVENTIONS_BLOCK_END
    assert "sha256=" not in CODE_CONVENTIONS_BLOCK_END
    assert block.split(CODE_CONVENTIONS_BLOCK_BEGIN, 1)[1].split(
        CODE_CONVENTIONS_BLOCK_END,
        1,
    )[0].strip() == body.strip()
    assert code_conventions_version_line(body) in block

    cursor = cursor_rules_mdc()
    assert cursor.startswith("---\n")
    assert body.strip() in cursor


def test_package_docstring_names_first_wave_idioms() -> None:
    doc = " ".join((lab_tracker_client.__doc__ or "").split())

    assert "first-line note markers" in doc
    assert "EntityRef note targets" in doc
    assert "lt_ids.json" in doc
    assert "content-hash evidence imports" in doc
    assert "fail-soft figure capture" in doc
