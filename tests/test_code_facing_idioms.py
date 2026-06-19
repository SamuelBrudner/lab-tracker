from __future__ import annotations

import re
from pathlib import Path

from lab_tracker.decision_context_constants import code_facing_idioms
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
    doc = Path("docs/retained-v1-surface.md").read_text(encoding="utf-8")

    assert "code-facing idiom teaching" in doc
    assert "lab-tracker://code-conventions" in doc
    assert "citation annotation tokens" in doc
    assert "UUID-bearing tokens should be stripped" in doc
