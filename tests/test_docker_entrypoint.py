from __future__ import annotations

from pathlib import Path


def test_bootstrap_admin_token_is_not_echoed_unconditionally() -> None:
    entrypoint = (
        Path(__file__).resolve().parent.parent
        / "deploy"
        / "docker-entrypoint.sh"
    )
    lines = entrypoint.read_text(encoding="utf-8").splitlines()
    token_echo_lines = [
        line for line in lines if "First admin setup token:" in line and "echo" in line
    ]

    assert token_echo_lines == []
