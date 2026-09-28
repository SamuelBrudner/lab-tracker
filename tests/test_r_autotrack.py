"""R autotrack: the shipped R source, the managed .Rprofile block, and R itself.

The .Rprofile tests run everywhere. The integration tests start real R and
run only where ``Rscript`` is installed (CI runners without R skip them); the
last one runs the R package's own testthat suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lab_tracker_client import cli as lt_cli
from lab_tracker_client import r_autotrack
from lab_tracker_client.client import LTValidationError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
R_PACKAGE = ROOT / "r" / "labtracker"
RSCRIPT = shutil.which("Rscript")
needs_r = pytest.mark.skipif(RSCRIPT is None, reason="Rscript is not installed")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stand-in")


@pytest.fixture
def rprofile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "home" / ".Rprofile"
    monkeypatch.setenv("R_PROFILE_USER", str(path))
    monkeypatch.delenv("LAB_TRACKER_AUTOTRACK", raising=False)
    return path


def _setup(capsys: pytest.CaptureFixture[str], *flags: str) -> dict:
    lt_cli.main(["setup", "autotrack", "--r", *flags])
    return json.loads(capsys.readouterr().out)


def test_shipped_r_source_is_the_r_package_source() -> None:
    """`lt` ships the R package's implementation byte for byte, so the
    .Rprofile block and `R CMD INSTALL r/labtracker` run the same code."""

    shipped = r_autotrack.shipped_r_source()
    assert shipped.is_file()
    assert shipped.read_bytes() == (R_PACKAGE / "R" / "labtracker.R").read_bytes()


def test_the_shipped_r_source_is_wheel_package_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["lab_tracker_client"]
    relative = r_autotrack.shipped_r_source().relative_to(ROOT / "src" / "lab_tracker_client")
    assert any(relative.match(pattern) for pattern in package_data)


def test_r_package_exports_what_the_block_attaches() -> None:
    namespace = (R_PACKAGE / "NAMESPACE").read_text(encoding="utf-8")
    exported = {
        line.removeprefix("export(").removesuffix(")")
        for line in namespace.splitlines()
        if line.startswith("export(")
    }
    assert exported == set(r_autotrack.R_PUBLIC_FUNCTIONS)


def test_setup_autotrack_r_requires_consent(rprofile: Path) -> None:
    with pytest.raises(SystemExit, match="--yes"):
        lt_cli.main(["setup", "autotrack", "--r"])
    assert not rprofile.exists()


def test_dry_run_shows_the_block_and_writes_nothing(
    rprofile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    preview = _setup(capsys, "--dry-run")

    assert preview["action"] == "would-install"
    assert preview["language"] == "r"
    assert Path(preview["rprofile"]) == rprofile
    assert f"+{r_autotrack.RPROFILE_BEGIN}" in preview["diff"]
    assert not rprofile.exists()


def test_install_update_and_uninstall_preserve_the_rest_of_the_profile(
    rprofile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = 'options(repos = c(CRAN = "https://cloud.r-project.org"))\n'
    rprofile.parent.mkdir(parents=True)
    rprofile.write_text(original, encoding="utf-8")

    installed = _setup(capsys, "--yes")
    assert installed["action"] == "installed"
    content = rprofile.read_text(encoding="utf-8")
    block = r_autotrack.rprofile_block()
    assert content == f"{original}\n{block}"
    # The block loads the installed package, else the copy shipped with lt.
    assert 'requireNamespace("labtracker"' in block
    assert r_autotrack.shipped_r_source().as_posix() in block
    assert r_autotrack.recorded_lt_path().replace("\\", "/") in block
    assert "LAB_TRACKER_AUTOTRACK" in block
    assert r_autotrack.rprofile_status()["up_to_date"] is True

    assert _setup(capsys, "--yes")["action"] == "current"

    # An older block (say, from before an upgrade moved the package) with the
    # person's own lines after it is refreshed in place.
    stale = content.replace("lt_source <- ", "lt_source <- 'old' # ")
    rprofile.write_text(stale + "library(ggplot2)\n", encoding="utf-8")
    assert r_autotrack.rprofile_status()["up_to_date"] is False
    preview = _setup(capsys, "--dry-run")
    assert preview["action"] == "would-update"
    assert rprofile.read_text(encoding="utf-8") == stale + "library(ggplot2)\n"
    assert _setup(capsys, "--yes")["action"] == "updated"
    assert rprofile.read_text(encoding="utf-8") == f"{original}\n{block}library(ggplot2)\n"

    removal = _setup(capsys, "--uninstall", "--dry-run")
    assert removal["action"] == "would-remove"
    assert f"-{r_autotrack.RPROFILE_END}" in removal["diff"]
    assert r_autotrack.RPROFILE_BEGIN in rprofile.read_text(encoding="utf-8")

    assert _setup(capsys, "--uninstall", "--yes")["action"] == "removed"
    assert rprofile.read_text(encoding="utf-8") == f"{original}\nlibrary(ggplot2)\n"
    assert _setup(capsys, "--uninstall", "--yes")["action"] == "absent"


def test_uninstall_round_trips_an_appended_block_exactly(
    rprofile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = "# my profile\noptions(digits = 4)\n"
    rprofile.parent.mkdir(parents=True)
    rprofile.write_text(original, encoding="utf-8")
    rprofile.chmod(0o600)

    _setup(capsys, "--yes")
    if sys.platform != "win32":
        assert rprofile.stat().st_mode & 0o777 == 0o600
    _setup(capsys, "--uninstall", "--yes")

    assert rprofile.read_text(encoding="utf-8") == original


def test_uninstall_removes_a_profile_that_held_only_the_block(
    rprofile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _setup(capsys, "--yes")["action"] == "installed"
    assert rprofile.read_text(encoding="utf-8") == r_autotrack.rprofile_block()

    removed = _setup(capsys, "--uninstall", "--yes")

    assert removed["file_removed"] is True
    assert not rprofile.exists()


def test_unpaired_markers_are_refused_untouched(
    rprofile: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rprofile.parent.mkdir(parents=True)
    damaged = f"x <- 1\n{r_autotrack.RPROFILE_BEGIN}\nprint('half a block')\n"
    rprofile.write_text(damaged, encoding="utf-8")

    with pytest.raises(LTValidationError, match="unpaired or repeated"):
        lt_cli.main(["setup", "autotrack", "--r", "--yes"])
    assert rprofile.read_text(encoding="utf-8") == damaged
    assert "error" in r_autotrack.rprofile_status()


def test_default_profile_is_the_home_rprofile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("R_PROFILE_USER", raising=False)
    monkeypatch.setattr(r_autotrack, "_r_home", lambda: tmp_path)
    assert r_autotrack.rprofile_path() == tmp_path / ".Rprofile"
    monkeypatch.setenv("R_PROFILE_USER", "  ")
    assert r_autotrack.rprofile_path() == tmp_path / ".Rprofile"


def test_the_r_flag_leaves_the_ipython_startup_file_alone(
    rprofile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    import lab_tracker_client.figure_autotrack as autotrack_module

    monkeypatch.setenv("IPYTHONDIR", str(tmp_path / "ipython"))
    _setup(capsys, "--yes")
    assert not autotrack_module.ipython_startup_path().exists()


def test_setup_status_reports_the_r_block(
    rprofile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    from lab_tracker_client import setup as setup_helpers

    _setup(capsys, "--yes")
    monkeypatch.setattr(setup_helpers, "_autotrack_status", lambda: {"installed": False})
    status = setup_helpers._autotrack_r_status()
    assert status["installed"] is True
    assert status["up_to_date"] is True
    assert status["rprofile"] == str(rprofile)


def _symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without rights
        pytest.skip(f"cannot create symlinks here: {exc}")


def test_a_symlinked_profile_keeps_its_link_and_edits_the_dotfiles_copy(
    rprofile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dotfiles-managed ~/.Rprofile is a symlink: install, update and
    uninstall edit the file it points at, never replace or delete the link."""

    original = "options(digits = 4)\n"
    target = tmp_path / "dotfiles" / "Rprofile"
    target.parent.mkdir()
    target.write_text(original, encoding="utf-8")
    _symlink(rprofile, Path("..") / "dotfiles" / "Rprofile")
    link_text = os.readlink(rprofile)

    preview = _setup(capsys, "--dry-run")
    assert preview["action"] == "would-install"
    assert Path(preview["rprofile"]) == rprofile
    assert Path(preview["rprofile_target"]) == target.resolve()
    assert target.read_text(encoding="utf-8") == original

    installed = _setup(capsys, "--yes")
    assert installed["action"] == "installed"
    assert Path(installed["rprofile_target"]) == target.resolve()
    assert rprofile.is_symlink() and os.readlink(rprofile) == link_text
    block = r_autotrack.rprofile_block()
    assert target.read_text(encoding="utf-8") == f"{original}\n{block}"
    assert [path.name for path in target.parent.iterdir()] == ["Rprofile"]
    status = r_autotrack.rprofile_status()
    assert status["up_to_date"] is True
    assert Path(status["rprofile_target"]) == target.resolve()

    target.write_text(f"{original}\n{block.replace('lt_path <- ', 'lt_path <- 1 # ')}")
    assert _setup(capsys, "--yes")["action"] == "updated"
    assert rprofile.is_symlink() and os.readlink(rprofile) == link_text
    assert target.read_text(encoding="utf-8") == f"{original}\n{block}"

    assert _setup(capsys, "--uninstall", "--yes")["action"] == "removed"
    assert rprofile.is_symlink() and os.readlink(rprofile) == link_text
    assert target.read_text(encoding="utf-8") == original


def test_uninstall_empties_a_symlinked_block_only_profile_but_keeps_the_link(
    rprofile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "dotfiles" / "Rprofile"
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")
    _symlink(rprofile, target)
    _setup(capsys, "--yes")

    removed = _setup(capsys, "--uninstall", "--yes")

    assert removed["action"] == "removed"
    assert "file_removed" not in removed
    assert rprofile.is_symlink()
    assert target.exists() and target.read_text(encoding="utf-8") == ""


def test_a_dangling_profile_symlink_is_refused_clearly(
    rprofile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "dotfiles" / "Rprofile"
    _symlink(rprofile, missing)

    for flags in (["--dry-run"], ["--yes"], ["--uninstall", "--yes"]):
        with pytest.raises(LTValidationError, match="symlink to .* which does not exist"):
            lt_cli.main(["setup", "autotrack", "--r", *flags])
    assert rprofile.is_symlink()
    assert not missing.exists()
    assert "does not exist" in r_autotrack.rprofile_status()["error"]


def test_r_string_literals_escape_quotes_and_backslashes() -> None:
    assert r_autotrack._r_string('C:\\Users\\a "b"') == '"C:\\\\Users\\\\a \\"b\\""'


# --- real R ----------------------------------------------------------------------


def _r_env(tmp_path: Path, rprofile: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAB_TRACKER_")}
    env.update(
        {
            "R_PROFILE_USER": str(rprofile),
            "R_LIBS_USER": str(tmp_path / "no-user-lib"),
            "LAB_TRACKER_CONFIG_DIR": str(tmp_path / "lt-config"),
            "LAB_TRACKER_WATCH_OUTBOX": str(tmp_path / "outbox"),
        }
    )
    env.update(extra)
    return env


def _write_fake_lt(directory: Path) -> Path:
    fake = directory / "fake-lt"
    fake.write_text(
        "#!/bin/sh\n"
        '{ for a in "$@"; do printf \'%s\\n\' "$a"; done; echo --end--; } >> "$FAKE_LT_LOG"\n'
        "out=''; prev=''\n"
        'for a in "$@"; do if [ "$prev" = --output ]; then out="$a"; fi; prev="$a"; done\n'
        'printf \'{\\n  "action": "imported",\\n  "notices": []\\n}\\n\' > "$out"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_r(code: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    assert RSCRIPT is not None
    return subprocess.run(  # noqa: S603 - fixed Rscript, test-authored code.
        [RSCRIPT, "-e", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )


@needs_r
@posix_only
def test_the_installed_block_autotracks_saves_in_a_fresh_r_session(
    rprofile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rscript reads the profile, the block sources the shipped copy once the
    default packages attach, and a plain png()/dev.off() reaches `lt`."""

    _setup(capsys, "--yes")
    log = tmp_path / "lt.log"
    env = _r_env(
        tmp_path, rprofile, LAB_TRACKER_LT=str(_write_fake_lt(tmp_path)), FAKE_LT_LOG=str(log)
    )
    figure = tmp_path / "fig.png"
    code = (
        "options(labtracker.autotrack.wait = TRUE); "
        'stopifnot("tools:labtracker" %in% search(), is_autotracking()); '
        f'png("{figure.as_posix()}"); plot(1:3); invisible(dev.off())'
    )

    completed = _run_r(code, env, tmp_path)

    assert completed.returncode == 0, completed.stderr
    args = log.read_text(encoding="utf-8").split("\n")
    assert args[:3] == ["capture", "file", str(figure)]
    assert "--require-bound" in args
    assert "figure_autotracked=true" in args and "capture_language=R" in args


@needs_r
@posix_only
def test_the_kill_switch_skips_the_block_entirely(
    rprofile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup(capsys, "--yes")
    env = _r_env(tmp_path, rprofile, LAB_TRACKER_AUTOTRACK="0")

    completed = _run_r('stopifnot(!("tools:labtracker" %in% search()))', env, tmp_path)

    assert completed.returncode == 0, completed.stderr


@needs_r
@posix_only
def test_real_lt_reports_an_unbound_checkout_once_and_r_keeps_running(
    rprofile: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R -> `lt capture file --require-bound` -> JSON -> one R message: two
    saves in an unbound checkout name it once, send nothing, and the script
    finishes normally."""

    lt_path = r_autotrack.recorded_lt_path()
    if not lt_path:
        pytest.skip("the lt entry point is not installed next to this interpreter")
    _setup(capsys, "--yes")
    checkout = tmp_path / "analysis"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)  # noqa: S603, S607
    env = _r_env(tmp_path, rprofile, LAB_TRACKER_LT=lt_path)
    code = (
        "options(labtracker.autotrack.wait = TRUE); "
        'for (name in c("a.png", "b.png")) { png(name); plot(1); invisible(dev.off()) }; '
        'cat("script finished\\n")'
    )

    completed = _run_r(code, env, checkout)

    assert completed.returncode == 0, completed.stderr
    assert "script finished" in completed.stdout
    assert completed.stderr.count("autotrack is not capturing saves") == 1
    assert "lt project bind" in completed.stderr
    assert not (tmp_path / "outbox").exists()


def _testthat_available() -> bool:
    if RSCRIPT is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed Rscript, fixed code.
        [
            RSCRIPT,
            "-e",
            'quit(status = !all(vapply(c("testthat", "withr"), requireNamespace, '
            "logical(1), quietly = TRUE)))",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


@needs_r
@posix_only
def test_r_package_testthat_suite_passes(tmp_path: Path) -> None:
    if not _testthat_available():
        pytest.skip("the testthat and withr R packages are not installed")
    env = {key: value for key, value in os.environ.items() if not key.startswith("LAB_TRACKER_")}
    env["R_PROFILE_USER"] = str(tmp_path / "empty.Rprofile")
    code = (
        'results <- testthat::test_dir("tests/testthat", reporter = "summary", '
        "stop_on_failure = FALSE); "
        "failed <- as.data.frame(results); "
        "quit(status = as.integer(sum(failed$failed) + sum(failed$error) > 0))"
    )
    completed = _run_r(code, env, R_PACKAGE)
    assert completed.returncode == 0, completed.stdout + completed.stderr
