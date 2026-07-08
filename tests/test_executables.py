"""Unit tests for lt/lt-mcp resolution and install classification.

These exercise the pure logic in ``lab_tracker_client.executables`` by
monkeypatching ``sys.executable`` (the invoking env) and ``shutil.which`` (what
PATH consumers get) and building fake executables/venvs inline, per the repo's
convention of not sharing executable/venv fixtures.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from lab_tracker_client.executables import (
    classify_install,
    installation_report,
    resolve_executable,
)


def _exe(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def _bindir_name() -> str:
    return "Scripts" if sys.platform == "win32" else "bin"


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


# --- classify_install -------------------------------------------------------


def test_classify_project_venv_is_fragile(tmp_path):
    p = tmp_path / "proj" / ".venv" / _bindir_name() / _exe("lt")
    kind, stable, _ = classify_install(p)
    assert kind == "project-venv"
    assert stable is False


def test_classify_uv_tool_store_is_stable():
    # Segment-based and the realpath-trap target: a uv tool env has a pyvenv.cfg
    # in its grandparent, but the un-resolved path is still a stable global tool.
    p = f"/home/x/.local/share/uv/tools/lab-tracker/bin/{_exe('lt')}"
    kind, stable, _ = classify_install(p)
    assert kind == "global-tool"
    assert stable is True


def test_classify_local_bin_is_stable():
    p = Path.home() / ".local" / "bin" / _exe("lt")
    kind, stable, _ = classify_install(p)
    assert kind == "global-tool"
    assert stable is True


def test_classify_generic_virtualenv_via_pyvenv_cfg(tmp_path):
    env = tmp_path / "env"
    _touch(env / "pyvenv.cfg")
    p = env / _bindir_name() / _exe("lt")
    kind, stable, _ = classify_install(p)
    assert kind == "venv"
    assert stable is False


def test_classify_unknown_defaults_stable(tmp_path):
    # An unfamiliar-but-working location must not be nagged about.
    p = tmp_path / "somewhere" / "bin" / _exe("lt")
    kind, stable, _ = classify_install(p)
    assert kind == "unknown"
    assert stable is True


def test_classify_uv_tool_env_with_real_pyvenv_cfg_stays_global_tool(tmp_path):
    # The realpath trap materialized on disk: a uv tool env genuinely has a
    # pyvenv.cfg in the executable's grandparent. The global-tool check must win
    # over the pyvenv.cfg probe, or this ordering could regress unnoticed.
    tool = tmp_path / ".local" / "share" / "uv" / "tools" / "lab-tracker"
    _touch(tool / "pyvenv.cfg")
    p = tool / _bindir_name() / _exe("lt")
    kind, stable, _ = classify_install(p)
    assert kind == "global-tool"
    assert stable is True


def test_classify_never_raises_without_home(tmp_path, monkeypatch):
    # Path.home() raises RuntimeError on a home-less host; classification must
    # degrade instead, honoring the module's never-raises contract.
    def _no_home(*_args, **_kwargs):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    kind, stable, _ = classify_install(tmp_path / "bin" / _exe("lt"))
    assert (kind, stable) == ("unknown", True)


# --- resolve_executable -----------------------------------------------------


def test_resolve_not_found(tmp_path, monkeypatch):
    fake_python = _touch(tmp_path / "emptyenv" / _bindir_name() / _exe("python"))
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    resolved = resolve_executable("lt")

    assert resolved.path is None
    assert resolved.source == "not-found"
    assert resolved.on_path is None
    assert resolved.install_kind == "not-found"
    assert resolved.stable is False


def test_resolve_prefers_sibling_over_path(tmp_path, monkeypatch):
    bindir = tmp_path / "env" / _bindir_name()
    _touch(tmp_path / "env" / "pyvenv.cfg")
    _touch(bindir / _exe("python"))
    sibling = _touch(bindir / _exe("lt"))
    monkeypatch.setattr(sys, "executable", str(bindir / _exe("python")))
    # Even though PATH offers a different lt, the sibling wins.
    other = _touch(tmp_path / "onpath" / _exe("lt"))
    monkeypatch.setattr(shutil, "which", lambda n: str(other) if n == "lt" else None)

    resolved = resolve_executable("lt")

    assert resolved.source == "sibling"
    assert resolved.path == str(sibling)
    assert resolved.install_kind == "venv"  # pyvenv.cfg beside the bin dir
    assert resolved.matches_path is False  # sibling != the PATH copy


def test_resolve_survives_unresolvable_home(tmp_path, monkeypatch):
    # Regression: classify_install used to call Path.home() unguarded, so a
    # home-less host crashed resolve_executable -> hooks/schedule._default_lt_path
    # with a RuntimeError instead of returning the sibling lt.
    def _no_home(*_args, **_kwargs):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", staticmethod(_no_home))
    bindir = tmp_path / "env" / _bindir_name()
    _touch(bindir / _exe("python"))
    sibling = _touch(bindir / _exe("lt"))
    monkeypatch.setattr(sys, "executable", str(bindir / _exe("python")))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    resolved = resolve_executable("lt")  # must not raise
    assert resolved.path == str(sibling)
    installation_report()  # must not raise either


def test_resolve_falls_back_to_path(tmp_path, monkeypatch):
    fake_python = _touch(tmp_path / "emptyenv" / _bindir_name() / _exe("python"))
    monkeypatch.setattr(sys, "executable", str(fake_python))
    on_path = _touch(tmp_path / "onpath" / _exe("lt"))
    monkeypatch.setattr(shutil, "which", lambda n: str(on_path) if n == "lt" else None)

    resolved = resolve_executable("lt")

    assert resolved.source == "path"
    assert resolved.on_path == str(on_path)
    assert resolved.matches_path is True


# --- installation_report ----------------------------------------------------


def test_report_flags_lt_not_on_path(tmp_path, monkeypatch):
    fake_python = _touch(tmp_path / "emptyenv" / _bindir_name() / _exe("python"))
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(shutil, "which", lambda _n: None)

    report = installation_report()

    assert report["ok"] is False
    assert any("`lt` is not on PATH" in problem for problem in report["problems"])


def test_report_flags_lt_mcp_missing_while_lt_present(tmp_path, monkeypatch):
    bindir = tmp_path / "toolenv" / _bindir_name()
    _touch(bindir / _exe("python"))
    lt = _touch(bindir / _exe("lt"))  # lt present, lt-mcp absent
    monkeypatch.setattr(sys, "executable", str(bindir / _exe("python")))
    monkeypatch.setattr(shutil, "which", lambda n: str(lt) if n == "lt" else None)

    report = installation_report()

    assert report["ok"] is False
    assert any(
        "lt-mcp" in problem and "even though" in problem
        for problem in report["problems"]
    )


def test_report_flags_fragile_venv_install(tmp_path, monkeypatch):
    bindir = tmp_path / "proj" / ".venv" / _bindir_name()
    _touch(bindir / _exe("python"))
    lt = _touch(bindir / _exe("lt"))
    lt_mcp = _touch(bindir / _exe("lt-mcp"))
    monkeypatch.setattr(sys, "executable", str(bindir / _exe("python")))

    def _which(name):
        return {"lt": str(lt), "lt-mcp": str(lt_mcp)}.get(name)

    monkeypatch.setattr(shutil, "which", _which)

    report = installation_report()

    assert report["ok"] is False
    assert any("resolves from a project-venv" in problem for problem in report["problems"])


def test_report_ok_when_stable_global_tool(tmp_path, monkeypatch):
    bindir = tmp_path / "share" / "uv" / "tools" / "lab-tracker" / _bindir_name()
    _touch(bindir / _exe("python"))
    lt = _touch(bindir / _exe("lt"))
    lt_mcp = _touch(bindir / _exe("lt-mcp"))
    monkeypatch.setattr(sys, "executable", str(bindir / _exe("python")))

    def _which(name):
        return {"lt": str(lt), "lt-mcp": str(lt_mcp)}.get(name)

    monkeypatch.setattr(shutil, "which", _which)

    report = installation_report()

    assert report["ok"] is True
    assert report["problems"] == []
    assert report["lt"]["install_kind"] == "global-tool"
