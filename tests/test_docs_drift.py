"""Drift guards for repository prose that describes checkable facts.

Each test pins one statement in the docs, agent instructions, or skill prose to
the code or repository state it describes, so the prose fails loudly when the
code moves instead of silently going stale.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import shlex
from pathlib import Path
from typing import Any

import httpx
import pytest
from read_opacity_inventory import READ_OPACITY_VARIANTS_BY_SUITE

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib

from lab_tracker import capture_setup_catalog, graph_drafting, mcp_server
from lab_tracker.capture_client_release import (
    EVIDENCE_ADAPTER_KEY,
    CaptureEnvironment,
    capture_environment,
)
from lab_tracker.capture_setup_catalog import (
    CAPTURE_SETUP_GUIDES,
    SESSION_ID_PLACEHOLDER,
    CaptureSetupGap,
    CaptureSetupKind,
)
from lab_tracker.cli import _skills_homes, init_consumer_repo, update_consumer_repo
from lab_tracker.cli import main as lab_tracker_main
from lab_tracker.config import Settings
from lab_tracker.decision_context_constants import AGENT_CONSULTATION_POLICY
from lab_tracker.mcp_tools import READ_TOOLS, WRITE_TOOLS
from lab_tracker.services.graph_draft_capture_setup import (
    DETECTION_THRESHOLDS,
    resolve_capture_setup,
)
from lab_tracker.setup_guide import setup_guide_markdown
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import connection_diagnostics
from lab_tracker_client import setup as setup_helpers
from lab_tracker_client.auth import auth_doctor
from lab_tracker_client.client import LabTracker
from lab_tracker_client.registry import registry_path
from lab_tracker_client.transport import HEALTH_PROBE_DEADLINE_SECONDS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _REPO_ROOT / "docs"
_SKILL_PATH = _REPO_ROOT / "skills" / "lab-tracker" / "SKILL.md"
_CAPTURE_REFERENCE_PATH = _SKILL_PATH.parent / "references" / "capture.md"
_DEVELOPMENT_REFERENCE_PATH = _SKILL_PATH.parent / "references" / "development.md"
_MCP_SKILLS_DOC = _DOCS / "lab-tracker-mcp-skills.md"
_AGENT_SETUP_DOC = _DOCS / "agent-setup.md"
_CLIENT_SETUP_JS = (
    _REPO_ROOT / "src" / "lab_tracker" / "frontend_src" / "features" / "client-setup.js"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"(?ms)^```{language}\n(.*?)^```", text)


def _maintained_docs() -> list[Path]:
    """Current docs; review run reports and the archive quote history verbatim."""

    return [
        path
        for path in sorted(_DOCS.rglob("*.md"))
        if not {"runs", "archive"} & set(path.relative_to(_DOCS).parts)
    ]


@pytest.fixture(autouse=True)
def _clear_appdata(monkeypatch: pytest.MonkeyPatch) -> None:
    # `lt auth doctor` also scans `%APPDATA%\Claude`. Keep that candidate out of the
    # enumeration so the doc-example checks do not depend on the Claude Desktop
    # entries of the machine running them (tests/test_auth_cli.py does the same).
    monkeypatch.delenv("APPDATA", raising=False)


# L11: offline queued capture shipped (IndexedDB upload queue).
def test_configuration_doc_does_not_call_offline_capture_deferred() -> None:
    text = " ".join(_read(_DOCS / "configuration.md").split())
    assert not re.search(r"[Oo]ffline queued capture is [^.]*deferred", text)


# L12: docs must not embed one operator's home directory.
def test_docs_do_not_embed_operator_home_directories() -> None:
    home_path = re.compile(r"/(?:Users|home)/[A-Za-z][\w.-]*/")
    offenders = [
        f"{path.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for path in _maintained_docs()
        for number, line in enumerate(_read(path).splitlines(), start=1)
        if home_path.search(line)
    ]
    assert not offenders, offenders


# L14: the decision-context spec must describe the shipped scope and ranking.
def test_decision_context_spec_matches_single_project_scope_and_merge_order() -> None:
    text = " ".join(_read(_DOCS / "mcp-decision-context-tooling.md").split())
    # build_decision_context always resolves exactly one project.
    assert "cross-project scope" not in text
    # merge_entities orders anchor > search_match > recent_activity only; it
    # does not rank by status, and recency lists every question status.
    assert "Active/staged questions outrank archived" not in text
    assert "Committed datasets and analyses outrank staged" not in text
    assert "recent active or staged questions" not in text
    # The low-level read tools it lists have shipped.
    assert "missing from MCP" not in text
    registered = {tool.__name__ for tool in (*READ_TOOLS, *WRITE_TOOLS)}
    named = set(re.findall(r"`(lab_tracker_(?!client`)[a-z_]+)`", text))
    assert named <= registered, sorted(named - registered)


def _fallback_paragraph(text: str) -> str:
    """The "Prefer `lab_tracker_get_decision_context` ..." paragraph, one line."""

    for paragraph in re.split(r"\n\s*\n", text):
        if paragraph.lstrip().startswith("Prefer `lab_tracker_get_decision_context`"):
            return " ".join(paragraph.split())
    raise AssertionError("no decision-context fallback paragraph found")


# L14: agent-facing copies of the consultation fallback match the served policy.
@pytest.mark.parametrize(
    "path",
    [_REPO_ROOT / "AGENTS.md", _DOCS / "mcp-decision-context-tooling.md"],
    ids=lambda path: path.name,
)
def test_consultation_fallback_matches_the_served_policy(path: Path) -> None:
    section = _read(path).split("## Lab Tracker Knowledge Graph Consultation", 1)[1]
    assert _fallback_paragraph(section) == _fallback_paragraph(AGENT_CONSULTATION_POLICY)


# L15: the authoring spec's status update names every shipped provenance read.
def test_evidence_authoring_current_state_lists_every_provenance_read() -> None:
    text = _read(_DOCS / "mcp-evidence-authoring-spec.md")
    current_state = text.split("## Current State", 1)[1].split("\n## ", 1)[0]
    provenance_tools = sorted(
        tool.__name__
        for tool in READ_TOOLS
        if re.fullmatch(r"lab_tracker_get_\w+_provenance", tool.__name__)
    )
    assert provenance_tools
    missing = [name for name in provenance_tools if f"`{name}`" not in current_state]
    assert not missing, missing


# L16: the read-opacity inventory table matches the test-enforced inventory.
_OPACITY_ROW = re.compile(r"(?m)^\| (?P<slice>[^|*]+?) \| (?P<variants>\d+) \| (?P<ops>\d+) \|")
_OPACITY_TOTAL = re.compile(r"(?m)^\| \*\*Total\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \|")
_OPACITY_SLICES = {
    "Core": "core",
    "Evidence and artifacts": "evidence_artifact",
    "Workflow and registry": "workflow_registry",
    "Acquisition": "acquisition",
}


def test_read_opacity_inventory_doc_counts_match_the_inventory() -> None:
    text = _read(_DOCS / "read-opacity-inventory.md")
    documented = {
        _OPACITY_SLICES[match["slice"]]: (int(match["variants"]), int(match["ops"]))
        for match in _OPACITY_ROW.finditer(text)
    }
    actual = {
        suite: (len(variants), len({variant.operation_id for variant in variants}))
        for suite, variants in READ_OPACITY_VARIANTS_BY_SUITE.items()
    }
    assert documented == actual

    total = _OPACITY_TOTAL.search(text)
    assert total is not None
    all_variants = [v for variants in READ_OPACITY_VARIANTS_BY_SUITE.values() for v in variants]
    assert (int(total[1]), int(total[2])) == (
        len(all_variants),
        len({variant.operation_id for variant in all_variants}),
    )
    for suite in READ_OPACITY_VARIANTS_BY_SUITE:
        suite_test = f"../tests/test_{suite}_read_opacity.py"
        assert suite_test in text, f"no executable-contract link for {suite}"
        assert (_DOCS / suite_test).resolve().is_file()


# L17: Compose prefixes volumes with the project name, so never hard-code it.
def test_self_hosted_runbook_does_not_hard_code_compose_volume_names() -> None:
    text = _read(_DOCS / "self-hosted-operations.md")
    commands = "\n".join(_fenced_blocks(text, "bash"))
    assert not re.search(r"\blab-tracker_(?:app_data|postgres_data)\b", commands)


# L19: `lt update` prose lists every scaffold file the command rewrites.
def _scaffold_files_rewritten_by_update(root: Path) -> list[str]:
    update_consumer_repo(root)
    for path in root.rglob("*"):
        if path.is_file():
            path.write_text("stale\n", encoding="utf-8")
    result = update_consumer_repo(root, dry_run=True)
    return sorted(path.relative_to(root).as_posix() for path in result.backups)


@pytest.mark.parametrize(
    ("doc", "section_start"),
    [
        (_DOCS / "setup.md", "### Update a consumer repo after upgrading"),
        (_CAPTURE_REFERENCE_PATH, "run `lt update` inside a consumer repo"),
    ],
)
def test_lt_update_docs_list_every_rewritten_scaffold_file(
    tmp_path: Path, doc: Path, section_start: str
) -> None:
    rewritten = _scaffold_files_rewritten_by_update(tmp_path)
    assert ".gemini/settings.json" in rewritten
    section = _read(doc).split(section_start, 1)[1].lstrip("\n").split("\n\n", 1)[0]
    missing = [name for name in rewritten if f"`{name}`" not in section]
    assert not missing, f"{doc.name} omits files `lt update` rewrites: {missing}"


# `lt update` flags named in prose must exist, and the machine-wide skills
# refresh must stay documented next to the repo-scoped update.
_LT_UPDATE_INVOCATION = re.compile(r"\b(?:lt|lab-tracker) update\b([^\n`]*)")
_LONG_FLAG = re.compile(r"--[a-z][a-z-]*")


def _lt_update_option_strings() -> set[str]:
    parser = lt_cli._build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return set(subparsers.choices["update"]._option_string_actions)


def test_documented_lt_update_flags_exist() -> None:
    accepted = _lt_update_option_strings()
    assert "--skills-only" in accepted
    unknown = [
        f"{path.relative_to(_REPO_ROOT)}: {match.group(0).strip()}"
        for path in (
            *_maintained_docs(), _SKILL_PATH, *_SKILL_PATH.parent.glob("references/*.md")
        )
        for match in _LT_UPDATE_INVOCATION.finditer(_read(path))
        if set(_LONG_FLAG.findall(match.group(1))) - accepted
    ]
    assert not unknown, f"documented `lt update` flags that the parser rejects: {unknown}"


@pytest.mark.parametrize("doc", [_DOCS / "setup.md", _CAPTURE_REFERENCE_PATH])
def test_lt_update_docs_describe_the_skills_only_refresh(doc: Path) -> None:
    text = " ".join(_read(doc).split())
    assert "`lt update --skills-only`" in text
    assert "machine-wide" in text


# `--skills-only` writes only under the skill homes, and a relative
# LAB_TRACKER_SKILLS_HOME resolves against the current directory (pinned by
# tests/test_lab_tracker_init.py), so every "never touches the current directory"
# claim carries that qualifier.
_RELATIVE_SKILLS_HOME_QUALIFIER = (
    "a relative `LAB_TRACKER_SKILLS_HOME` resolves against the current directory"
)


@pytest.mark.parametrize(
    "text",
    [
        _read(_DOCS / "setup.md"),
        _read(_AGENT_SETUP_DOC),
        _read(_CAPTURE_REFERENCE_PATH),
        _read(_MCP_SKILLS_DOC),
        setup_guide_markdown(),
    ],
    ids=["setup.md", "agent-setup.md", "lab-tracker-skill", "mcp-skills.md", "setup-guide"],
)
def test_skills_only_docs_qualify_the_current_directory_claim(text: str) -> None:
    assert _RELATIVE_SKILLS_HOME_QUALIFIER in " ".join(text.split())


@pytest.mark.parametrize("main", [lt_cli.main, lab_tracker_main], ids=["lt", "lab-tracker"])
def test_skills_only_help_qualifies_the_current_directory_claim(
    main, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["update", "--help"])

    assert excinfo.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "LAB_TRACKER_SKILLS_HOME is a relative path (use an absolute one)" in help_text


def test_setup_doc_defines_the_applied_repos_registry() -> None:
    text = " ".join(_read(_DOCS / "setup.md").split())
    assert f"`~/.lab-tracker/{registry_path().name}`" in text
    assert "`lt doctor --all` sweeps it" in text


def test_setup_guide_does_not_say_a_bare_lt_update_refreshes_the_skill() -> None:
    guide = " ".join(setup_guide_markdown().split())
    assert "`lt update` refreshes them" not in guide
    assert "refreshes everything after upgrades" not in guide
    assert "`lt update --install-skills` refreshes the skill" in guide


@pytest.mark.parametrize(
    "doc", [_DOCS / "setup.md", _CAPTURE_REFERENCE_PATH],
    ids=["setup.md", "lab-tracker-capture-reference"]
)
def test_lt_update_docs_say_only_install_skills_refreshes_the_skill_with_the_repo(
    doc: Path,
) -> None:
    text = " ".join(_read(doc).split())
    assert "Bare `lt update` refreshes the repo's files only" in text
    assert any(
        f"`lt update --install-skills` refreshes {skills} in addition to the repo" in text
        for skills in ("the skill", "both skills")
    )


# The lt-mcp smoke check and the /health probes are bounded, and the prose that
# says so names the bounds the code enforces. Both advisory /health probes share
# the per-phase timeout and the response deadline.
_RELEASE_AWARENESS_DOCS = [_DOCS / "setup.md", _DOCS / "retained-v1-surface.md"]


@pytest.mark.parametrize("doc", _RELEASE_AWARENESS_DOCS, ids=lambda path: path.name)
def test_docs_state_the_lt_mcp_smoke_check_limit(doc: Path) -> None:
    text = " ".join(_read(doc).split())
    assert f"{setup_helpers._MCP_IMPORT_TIMEOUT_SECONDS:g}-second limit" in text


@pytest.mark.parametrize("doc", _RELEASE_AWARENESS_DOCS, ids=lambda path: path.name)
def test_docs_state_the_health_probe_bounds(doc: Path) -> None:
    text = " ".join(_read(doc).split())
    timeout = setup_helpers._HEALTH_PROBE_TIMEOUT_SECONDS
    assert timeout == mcp_server._RELEASE_PROBE_TIMEOUT_SECONDS
    assert f"{timeout:g}-second connect and read timeouts" in text
    # The deadline covers the wait for the headers, not only the body.
    deadline = f"{HEALTH_PROBE_DEADLINE_SECONDS:g}-second deadline on the whole response"
    assert deadline in text
    assert "headers included" in text
    # Neither the deadline nor the connect timeout bounds name resolution, and the
    # connect timeout applies to each resolved address (verified with a stalled
    # getaddrinfo and a host with several unreachable addresses), so the docs
    # must not present the connect timeout as the whole bound on connecting.
    assert "Getting connected is outside that deadline" in text
    assert "each address a name resolves to" in text


def test_agent_setup_doc_states_the_same_health_probe_bounds_as_the_code() -> None:
    # `lt setup status` and `lt setup connect` share the probe; the guide once
    # called its bound "the existing two-second HTTP timeout" and named no deadline.
    text = " ".join(_read(_AGENT_SETUP_DOC).split())
    timeout = setup_helpers._HEALTH_PROBE_TIMEOUT_SECONDS
    assert f"{timeout:g}-second connect and read timeouts" in text
    assert f"{HEALTH_PROBE_DEADLINE_SECONDS:g}-second deadline on the whole response" in text
    assert "two-second HTTP timeout" not in text


def test_setup_doc_describes_the_lt_mcp_startup_safety_probe_as_the_code_behaves() -> None:
    # The behaviour itself is pinned by tests/test_mcp_server.py and
    # tests/test_mcp_hosted_surface.py (loopback skip, hosted fail-closed, stdio
    # fail-soft, an unbuildable client); this pins that the prose says the same.
    text = " ".join(_read(_DOCS / "setup.md").split())
    assert "just before it refuses to start" not in text
    assert "separate startup safety probe (`GET /readiness`) before that check" in text
    assert "Over stdio it probes only a remote API target, not a loopback one" in text
    assert "refuses to start only when the probe confirms that the API has authentication" in text
    assert "still starts when the HTTP client cannot be built" in text
    assert "each probe that needs the client writes its own stderr line" in text
    assert "after one stderr notice" not in text
    assert "A hosted (streamable-http) `lt-mcp` is stricter: it always probes" in text
    assert "refuses to start when it cannot confirm that the API has authentication enabled" in text


def test_retained_surface_names_the_lt_hpc_command_not_its_adapter_id() -> None:
    # `lt hpc` is the command; `lt-hpc` is only the adapter id its notes carry.
    assert "`lt-hpc`" not in _read(_DOCS / "retained-v1-surface.md")


def test_one_click_deploy_reads_the_first_admin_token_from_the_dashboard() -> None:
    # The blueprint keeps the token out of the browser; the runbook must not
    # promise it there or recommend first_run for the public service URL.
    text = " ".join(_read(_DOCS / "one-click-cloud-deploy.md").split())
    assert "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE=never" in text
    assert "copy the value of `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`" in text
    assert "already loaded on that screen" not in text
    assert "opts into first-run browser display" not in text


def test_funnel_runbook_names_what_a_public_local_instance_must_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Authentication being on is not enough in the `local` environment: viewer
    # self-registration defaults to on there, and the first-admin token is shown
    # to any peer address that looks private, which is every client behind a proxy.
    for name in (
        "LAB_TRACKER_AUTH_ENABLED",
        "LAB_TRACKER_AUTH_SECRET_KEY",
        "LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED",
        "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LAB_TRACKER_ENVIRONMENT", "local")
    local = Settings(_env_file=None)
    assert local.is_public_viewer_registration_enabled() is True
    assert local.effective_bootstrap_admin_token_disclosure() == "local"

    text = " ".join(_read(_DOCS / "self-hosted-operations.md").split())
    section = text.split("## Publishing Through Tailscale Funnel", 1)[1]
    section = section.split("### When a client cannot connect", 1)[0]
    assert "LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED=false" in section
    assert "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE=never" in section
    assert "LAB_TRACKER_AUTH_ENABLED=true" in section
    assert "Authentication being on is not enough in the `local` environment" in section


@pytest.mark.parametrize("doc", [_AGENT_SETUP_DOC, _DOCS / "self-hosted-operations.md"])
def test_funnel_docs_stay_within_what_the_recorded_incidents_show(doc: Path) -> None:
    # The evidence is one 502 from a stopped backend and one TLS stall whose cause
    # was not confirmed, so neither doc may present a stalled handshake as usually
    # pointing at one place.
    text = " ".join(_read(doc).split())
    assert "usually pointed" not in text
    assert "usually points" not in text
    assert "maintainer's recorded" not in text
    assert "not a rule" in text


def _emitted_diagnosis_codes() -> set[str]:
    """Every literal ``diagnosis`` value the connection probe can report."""

    codes: set[str] = set()
    for module in (connection_diagnostics, setup_helpers):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            value: ast.expr | None = None
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "diagnosis" for t in node.targets)
            ) or (isinstance(node, ast.keyword) and node.arg == "diagnosis"):
                value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                codes.add(value.value)
    return codes


def test_agent_setup_diagnosis_table_lists_every_diagnosis_the_probe_reports() -> None:
    codes = _emitted_diagnosis_codes()
    assert {"tls_handshake_failed", "proxy_connection_failed", "http_error"} <= codes
    text = _read(_AGENT_SETUP_DOC)
    missing = sorted(code for code in codes if f"| `{code}` |" not in text)
    assert not missing, f"diagnosis codes missing from the agent-setup table: {missing}"


def test_lt_doctor_help_names_every_check_it_runs() -> None:
    help_text = " ".join(lt_cli._build_parser().format_help().split())
    assert "code-facing idiom blocks, that lt-mcp can start" in help_text
    assert "whether this client is behind its server" in help_text
    assert "an unreachable server is a warning, not a failure" in help_text


# Which capture paths DO record the release is pinned by behaviour in
# tests/test_capture_release_stamping.py; this is the other half of that claim.
def test_notes_made_by_hand_or_import_carry_no_release_or_install_id(tmp_path: Path) -> None:
    stamp_keys = ("capture_install_id", "capture_client_version", "capture_client_revision")
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"data": [], "meta": {"total": 0}})
        sent.append(request.content.decode("utf-8", "replace"))
        note = {"note_id": "n-1", "project_id": "p-1", "status": "staged", "metadata": {}}
        return httpx.Response(201, json={"data": note})

    client = LabTracker(base_url="http://127.0.0.1:9", transport=httpx.MockTransport(handler))
    upload = tmp_path / "data.csv"
    upload.write_text("a,b\n", encoding="utf-8")

    client.upsert_note(project_id="p-1", content="typed note")
    client.quick_capture("quick thought", project_id="p-1")
    client.upload_note_file(project_id="p-1", file_path=upload)
    client.import_evidence_file(project_id="p-1", file_path=upload)

    assert len(sent) == 4
    assert not [key for body in sent for key in stamp_keys if key in body]


# L24/L25: examples must use the sanctioned LPAT, never deprecated login.
@pytest.mark.parametrize("doc", [_DEVELOPMENT_REFERENCE_PATH, _MCP_SKILLS_DOC])
def test_mcp_environment_examples_use_an_lpat_not_username_password(doc: Path) -> None:
    blocks = [
        block for block in _fenced_blocks(_read(doc), "bash") if "LAB_TRACKER_BASE_URL=" in block
    ]
    assert blocks
    for block in blocks:
        assert "LAB_TRACKER_MCP_API_KEY=" in block, block
        assert "LAB_TRACKER_MCP_USERNAME" not in block, block
        assert "LAB_TRACKER_MCP_PASSWORD" not in block, block


def test_vscode_mcp_inputs_mark_username_password_as_deprecated() -> None:
    config = json.loads(_read(_REPO_ROOT / ".vscode" / "mcp.json"))
    inputs = {entry["id"]: entry["description"] for entry in config["inputs"]}
    for input_id in ("lt-username", "lt-password"):
        assert "deprecated" in inputs[input_id].lower(), inputs[input_id]
    assert "deprecated" not in inputs["lt-token"].lower(), inputs["lt-token"]


@pytest.mark.parametrize("doc", [_MCP_SKILLS_DOC, _AGENT_SETUP_DOC], ids=lambda path: path.name)
def test_documented_mcp_json_example_passes_lt_auth_doctor(tmp_path: Path, doc: Path) -> None:
    examples = [
        json.loads(block) for block in _fenced_blocks(_read(doc), "json") if '"mcpServers"' in block
    ]
    assert examples
    for index, example in enumerate(examples):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        (repo / ".mcp.json").write_text(json.dumps(example), encoding="utf-8")
        report = auth_doctor(repo, home=tmp_path / "empty-home")
        assert report["deprecated_count"] == 0, report
        assert report["warning_count"] == 0, report


def test_documented_codex_toml_example_passes_lt_auth_doctor(tmp_path: Path) -> None:
    examples = [
        block for block in _fenced_blocks(_read(_AGENT_SETUP_DOC), "toml") if "mcp_servers" in block
    ]
    assert examples
    for index, example in enumerate(examples):
        parsed = tomllib.loads(example)
        entry = parsed["mcp_servers"]["lab-tracker"]
        assert entry["command"] != "lt-mcp", "a desktop entry uses the absolute path"
        assert "env" not in entry, "credentials stay in the saved profile"
        home = tmp_path / f"home-{index}"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text(example, encoding="utf-8")
        report = auth_doctor(tmp_path / "repo", home=home)
        assert [reg["surface"] for reg in report["registrations"]] == ["codex"], report
        assert report["deprecated_count"] == 0, report
        assert report["warning_count"] == 0, report


@pytest.mark.parametrize("desktop_location", ["macos", "windows-appdata"])
def test_auth_doctor_reports_no_auth_for_the_documented_hand_registered_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop_location: str
) -> None:
    # `lt auth doctor` classifies auth only from an entry's own `env`, and never reads
    # the saved profile that supplies the token and URL. The documented entries carry
    # no `env`, so doctor reports `none` and no base URL for them; the Desktop
    # registration check in agent-setup.md must say so (see the test below).
    # The Desktop file goes where agent-setup.md says it lives on macOS or Windows;
    # `auth.py` enumerates both on every platform, so this does not depend on the OS.
    desktop = next(
        json.loads(block)
        for block in _fenced_blocks(_read(_AGENT_SETUP_DOC), "json")
        if '"mcpServers"' in block
    )
    codex = next(
        block for block in _fenced_blocks(_read(_AGENT_SETUP_DOC), "toml") if "mcp_servers" in block
    )
    home = tmp_path / "home"
    if desktop_location == "macos":
        desktop_dir = home / "Library" / "Application Support" / "Claude"
    else:
        appdata = tmp_path / "AppData" / "Roaming"
        monkeypatch.setenv("APPDATA", str(appdata))
        desktop_dir = appdata / "Claude"
    desktop_config = desktop_dir / "claude_desktop_config.json"
    codex_config = home / ".codex" / "config.toml"
    for path in (desktop_config, codex_config):
        path.parent.mkdir(parents=True)
    desktop_config.write_text(json.dumps(desktop), encoding="utf-8")
    codex_config.write_text(codex, encoding="utf-8")

    report = auth_doctor(tmp_path / "repo", home=home)

    observed = {
        reg["surface"]: (reg["auth_mode"], reg["base_url"]) for reg in report["registrations"]
    }
    assert observed == {"codex": ("none", None), "claude-desktop": ("none", None)}, report
    desktop_paths = [
        reg["path"] for reg in report["registrations"] if reg["surface"] == "claude-desktop"
    ]
    assert desktop_paths == [str(desktop_config)], report
    # The placeholder is not an absolute path, so doctor only adds its neutral note; the
    # missing-path warning is reserved for a real absolute path that does not exist.
    assert report["warning_count"] == 0, report


def test_documented_claude_desktop_example_with_a_real_path_passes_lt_auth_doctor(
    tmp_path: Path,
) -> None:
    # Once the reader replaces the placeholder with the path of an installed `lt-mcp`,
    # doctor finds the file and has nothing to say beyond the relaunch reminder.
    desktop = next(
        json.loads(block)
        for block in _fenced_blocks(_read(_AGENT_SETUP_DOC), "json")
        if '"mcpServers"' in block
    )
    executable = tmp_path / "bin" / "lt-mcp"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    desktop["mcpServers"]["lab-tracker"]["command"] = str(executable)
    home = tmp_path / "home"
    desktop_config = home / "Library" / "Application Support" / "Claude"
    desktop_config.mkdir(parents=True)
    (desktop_config / "claude_desktop_config.json").write_text(
        json.dumps(desktop), encoding="utf-8"
    )

    report = auth_doctor(tmp_path / "repo", home=home)

    (registration,) = report["registrations"]
    assert registration["command"] == str(executable), report
    assert registration["command_is_absolute"] is True, report
    assert registration["command_exists"] is True, report
    assert report["warning_count"] == 0, report
    assert len(report["notes"]) == 1, report  # only the quit-and-reopen reminder


_DOCTOR_NO_ENV_SENTENCE = (
    "For an entry without `env`, `lt auth doctor` reports auth mode `none` and no base URL. "
    "That is expected: the token and URL come from the saved profile, "
    "which `lt auth doctor` does not read."
)


def test_claude_desktop_registration_check_explains_the_doctor_none_result() -> None:
    desktop = next(
        body for name, body in _client_sections().items() if name.startswith("Claude Desktop")
    )
    assert _DOCTOR_NO_ENV_SENTENCE in _collapsed_whitespace(desktop)


def test_claude_desktop_example_uses_an_absolute_command_and_no_credentials() -> None:
    blocks = [
        block
        for block in _fenced_blocks(_read(_AGENT_SETUP_DOC), "json")
        if '"mcpServers"' in block
    ]
    assert blocks
    for block in blocks:
        entry = json.loads(block)["mcpServers"]["lab-tracker"]
        assert entry["command"] != "lt-mcp", "a desktop entry uses the absolute path"
        assert "env" not in entry, "credentials stay in the saved profile"
        assert "lpat_" not in block
        assert "LAB_TRACKER_MCP" not in block


def test_docs_state_mcp_tool_counts_that_match_the_registered_tuples() -> None:
    pattern = re.compile(r"(\d+) tools\** \((\d+) read / (\d+) write")
    expected = (len(READ_TOOLS) + len(WRITE_TOOLS), len(READ_TOOLS), len(WRITE_TOOLS))
    stale = [
        f"{path.relative_to(_REPO_ROOT)}: {match.group(0)}"
        for path in _maintained_docs()
        for match in pattern.finditer(_read(path))
        if tuple(int(group) for group in match.groups()) != expected
    ]
    assert not stale, f"stale MCP tool counts (expected {expected}): {stale}"


# L76: the protocol docstring names every implementation in the module.
def test_graph_draft_client_docstring_names_every_implementation() -> None:
    implementations = sorted(
        name
        for name, member in inspect.getmembers(graph_drafting, inspect.isclass)
        if name.endswith("GraphDraftClient")
        and member is not graph_drafting.GraphDraftClient
        and member.__module__ == graph_drafting.__name__
    )
    assert "AnthropicGraphDraftClient" in implementations
    docstring = graph_drafting.GraphDraftClient.__doc__ or ""
    missing = [name for name in implementations if f"``{name}``" not in docstring]
    assert not missing, missing
    assert "tracked as separate beads" not in docstring


_SESSION_LINK_CODE_COMPONENT = (
    _REPO_ROOT / "src/lab_tracker/frontend_src/features/sessions/SessionLinkCode.jsx"
)
_APP_LINK_CODE_SENTENCE = "The app shows and copies each session's link code as `LT-<code>`"


def _collapsed_whitespace(text: str) -> str:
    return " ".join(text.split())


def test_app_session_link_code_matches_the_prefix_the_watcher_claims() -> None:
    from lab_tracker_client.session_context import LINK_CODE_PREFIX

    component = _read(_SESSION_LINK_CODE_COMPONENT)
    assert f'SESSION_LINK_CODE_PREFIX = "{LINK_CODE_PREFIX}"' in component
    for doc in (_DOCS / "watch-folder-capture.md", _DOCS / "retained-v1-surface.md"):
        assert _APP_LINK_CODE_SENTENCE in _collapsed_whitespace(_read(doc)), doc.name


def test_agent_setup_names_every_hook_event_that_carries_lt_context() -> None:
    from lab_tracker_client.hook_context import CONTEXT_EVENTS

    text = _collapsed_whitespace(_read(_DOCS / "agent-setup.md"))
    assert "`hookSpecificOutput.additionalContext`" in text
    missing = [event for event in sorted(CONTEXT_EVENTS) if f"on `{event}`" not in text]
    assert not missing, missing


# Per-client MCP registration: the matrix in docs/agent-setup.md, the generated setup
# guide, and the web Setup and Agents pages (client-setup.js) must describe one thing.
_CLIENT_MATRIX_HEADING = "Choose your client"
_CLIENT_HEADINGS = (
    "Claude Code",
    "Claude Desktop chat",
    "Codex in the ChatGPT desktop app",
    "Codex CLI",
)
_SHARED_GUIDANCE_HEADING = "All clients"
_INIT_USER_LEVEL_SENTENCE = (
    "Any run without `--dry-run` also records the repository in "
    "`~/.lab-tracker/applied-repos.json`, and `--install-skills` additionally writes "
    "both skills and supporting references into the user-level Claude and Codex skill homes "
    "(`~/.claude/skills` and `~/.agents/skills`)."
)
_CLAUDE_DESKTOP_SUPPORT_STATUS = (
    "Claude Desktop chat is supported by manual registration only: "
    "`lt` never writes `claude_desktop_config.json`."
)
_INIT_WRITES_SENTENCE = (
    "The dry run previews the files; running without `--dry-run` writes them, and "
    "`--yes` additionally consents to the managed code-conventions blocks in "
    "`CLAUDE.md`, `AGENTS.md`, and `.cursor/rules/lab-tracker.mdc`."
)


def _client_matrix() -> str:
    """The "Choose your client" section of agent-setup.md, up to the next h2/h3."""

    text = _read(_AGENT_SETUP_DOC)
    section = text.split(f"### {_CLIENT_MATRIX_HEADING}", 1)[1]
    return re.split(r"(?m)^#{2,3} ", section, maxsplit=1)[0]


def _matrix_subsections() -> dict[str, str]:
    """Every `####` subsection of the matrix, keyed by its heading."""

    parts = re.split(r"(?m)^#### (.+)$", _client_matrix())
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def _client_sections() -> dict[str, str]:
    """The per-client subsections of the matrix, without the shared guidance."""

    return {
        heading: body
        for heading, body in _matrix_subsections().items()
        if heading != _SHARED_GUIDANCE_HEADING
    }


def _shared_guidance() -> str:
    """The cross-client paragraphs, under their own heading rather than a client's."""

    return _matrix_subsections()[_SHARED_GUIDANCE_HEADING]


def test_agent_setup_labels_a_section_for_every_client() -> None:
    headings = list(_client_sections())
    for label in _CLIENT_HEADINGS:
        assert any(heading.startswith(label) for heading in headings), (label, headings)


def test_agent_setup_states_the_claude_desktop_support_status() -> None:
    sections = _client_sections()
    desktop = next(body for name, body in sections.items() if name.startswith("Claude Desktop"))
    assert _CLAUDE_DESKTOP_SUPPORT_STATUS in _collapsed_whitespace(desktop)


_SHARED_GUIDANCE_PHRASES = (
    "Official references",
    "The saved connection profile normally supplies the API URL and LPAT.",
    "Server-side AI drafting uses the Lab Tracker operator's configured provider",
    "Every scaffolded instruction file carries the same policy",
)


def test_shared_client_guidance_has_its_own_heading_not_a_clients() -> None:
    # These paragraphs apply to every client. Under the Cursor and GitHub Copilot
    # heading, heading navigation scopes them to those two and `_client_sections()`
    # hands them to the wrong client.
    shared = _collapsed_whitespace(_shared_guidance())
    for phrase in _SHARED_GUIDANCE_PHRASES:
        assert phrase in shared, phrase
    cursor = _collapsed_whitespace(
        next(body for name, body in _client_sections().items() if name.startswith("Cursor"))
    )
    assert "lab-tracker-cursor.md" in cursor
    for phrase in _SHARED_GUIDANCE_PHRASES:
        assert phrase not in cursor, phrase
    # It stays inside the matrix, after the client sections, so the matrix-wide checks
    # below (banned vendor phrases, credential placement) still scan it.
    assert f"#### {_SHARED_GUIDANCE_HEADING}" in _client_matrix()
    assert list(_matrix_subsections())[-1] == _SHARED_GUIDANCE_HEADING


_CODEX_WINDOWS_PATH = r"C:\Users\someone\bin\lt-mcp.exe"


def test_codex_desktop_toml_note_gives_windows_path_forms_that_parse() -> None:
    # A Windows path pasted into a TOML basic string is a parse error (`\U` opens a
    # unicode escape), and ~/.codex/config.toml is shared by the Codex products.
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(f'command = "{_CODEX_WINDOWS_PATH}"')
    body = _collapsed_whitespace(
        next(
            body
            for name, body in _client_sections().items()
            if name.startswith("Codex in the ChatGPT desktop app")
        )
    )
    assert body.index("[mcp_servers.lab-tracker]") < body.index("single-quoted TOML literal")
    assert body.index("single-quoted TOML literal") < body.index("Verify:")
    assert "double every backslash" in body
    forms = re.findall(r"`(command = [^`]+)`", body)
    assert [form[len("command = ")] for form in forms] == ["'", '"']
    for form in forms:
        parsed = tomllib.loads(form.replace("<user>", "someone"))
        assert parsed == {"command": _CODEX_WINDOWS_PATH}, form


def test_scaffold_files_are_written_without_yes_and_the_docs_say_so(tmp_path: Path) -> None:
    def created(root: Path, **options: bool) -> set[str]:
        result = init_consumer_repo(root, **options)
        return {path.relative_to(root).as_posix() for path in result.created}

    plain = created((tmp_path / "plain").resolve())
    consenting = created((tmp_path / "consenting").resolve(), yes=True)
    # `--yes` is not what writes the scaffold: it only adds the conventions blocks,
    # and `.cursor/rules/lab-tracker.mdc` is the file that only exists because of it.
    assert ".mcp.json" in plain
    assert ".cursor/rules/lab-tracker.mdc" not in plain
    assert ".cursor/rules/lab-tracker.mdc" in consenting
    sections = _client_sections()
    claude_code = next(body for name, body in sections.items() if name.startswith("Claude Code"))
    prose = _collapsed_whitespace(claude_code)
    assert _INIT_WRITES_SENTENCE in prose
    assert "`--yes` writes" not in prose


def test_client_matrix_names_the_user_level_files_init_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LAB_TRACKER_SKILLS_HOME", raising=False)
    monkeypatch.delenv("LAB_TRACKER_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _path_cls: tmp_path))
    user_level = [
        f"~/{path.relative_to(tmp_path).as_posix()}"
        for path in [home for _name, home in _skills_homes()] + [registry_path()]
    ]
    assert user_level == [
        "~/.claude/skills",
        "~/.agents/skills",
        "~/.lab-tracker/applied-repos.json",
    ]
    intro = _collapsed_whitespace(_client_matrix().split("\n#### ", 1)[0])
    # `--install-skills` and the applied-repos registry write outside the repository,
    # so the intro must not claim that init writes only the repository files.
    for location in user_level:
        assert location in intro, location
    assert "writes only the repository files" not in intro
    assert "never writes a client's own MCP registration file" in intro


def test_init_records_the_repo_registry_on_every_real_run_and_the_docs_say_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LAB_TRACKER_SKILLS_HOME", raising=False)
    monkeypatch.delenv("LAB_TRACKER_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _path_cls: tmp_path))
    skill_homes = [home for _name, home in _skills_homes()]

    init_consumer_repo((tmp_path / "preview").resolve(), dry_run=True)
    assert not registry_path().exists()

    # The registry is written by a real run whether or not skills are installed; only
    # `--install-skills` touches the skill homes.
    init_consumer_repo((tmp_path / "plain").resolve())
    assert registry_path().is_file()
    assert not any(home.exists() for home in skill_homes)
    init_consumer_repo((tmp_path / "with-skills").resolve(), install_skills=True)
    assert all(home.is_dir() for home in skill_homes)

    intro = _collapsed_whitespace(_client_matrix().split("\n#### ", 1)[0])
    assert _INIT_USER_LEVEL_SENTENCE in intro
    assert "With `--install-skills` it also writes" not in intro


def test_every_client_section_gives_the_three_part_verification() -> None:
    sections = _client_sections()
    for label in _CLIENT_HEADINGS:
        name = next(heading for heading in sections if heading.startswith(label))
        body = sections[name]
        assert "lt setup verify-mcp --expected-revision <full-revision>" in body, name
        assert "lab_tracker_list_projects" in body, name


def test_codex_cli_section_names_the_missing_executable_failure() -> None:
    sections = _client_sections()
    body = next(body for name, body in sections.items() if name.startswith("Codex CLI"))
    assert "command not found: codex" in body
    assert "codex mcp add lab-tracker -- lt-mcp" in body


_CREDENTIALS_RULE_CLIENTS = ("Claude Desktop chat", "Codex")
# Clients whose own pages put credentials in a settings file, so the matrix rule must
# not claim to cover them.
_CREDENTIALS_RULE_EXCLUDED = ("Cursor", ".cursor", "Copilot")


def _credentials_rule() -> str:
    intro = _collapsed_whitespace(_client_matrix().split("\n#### ", 1)[0])
    start = intro.index("**No credentials")
    return intro[start : intro.index("**Verify in three parts", start)]


def test_credentials_rule_is_scoped_to_the_clients_registered_by_hand() -> None:
    intro = _collapsed_whitespace(_client_matrix().split("\n#### ", 1)[0])
    rule = _credentials_rule()
    # docs/lab-tracker-cursor.md sends credentials to ~/.cursor/mcp.json, so a rule
    # stated for "every client" would contradict a page the matrix links to.
    assert "~/.cursor/mcp.json" in _collapsed_whitespace(_read(_DOCS / "lab-tracker-cursor.md"))
    shared = _collapsed_whitespace(_shared_guidance())
    for text in (intro, shared):
        assert "every client" not in text
        assert "another client settings file" not in text
    assert "keep them out of the Claude Desktop and Codex settings files" in shared
    for client in _CREDENTIALS_RULE_CLIENTS:
        assert client in rule, client
    for excluded in _CREDENTIALS_RULE_EXCLUDED:
        assert excluded not in rule, excluded


def test_client_matrix_makes_no_unverified_vendor_claims() -> None:
    # A bare command failing in a GUI, and the deprecation status of
    # `codex mcp add`, are claims the vendors' own documentation does not make.
    lowered = _collapsed_whitespace(_client_matrix()).lower()
    for claim in ("silently", "will fail", "always fails"):
        assert claim not in lowered, claim
    for name, body in _client_sections().items():
        if name.startswith("Codex"):
            assert "deprecat" not in body.lower(), name


def test_documented_verify_mcp_flags_exist() -> None:
    parser = lt_cli._build_parser()
    top = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    setup = top.choices["setup"]
    setup_verbs = next(a for a in setup._actions if isinstance(a, argparse._SubParsersAction))
    accepted = set(setup_verbs.choices["verify-mcp"]._option_string_actions)
    assert {"--command", "--expected-revision"} <= accepted
    invocation = re.compile(r"\blt setup verify-mcp\b([^\n`]*)")
    text = _read(_AGENT_SETUP_DOC)
    flags = {flag for match in invocation.finditer(text) for flag in _LONG_FLAG.findall(match[1])}
    assert "--command" in flags
    assert not flags - accepted, sorted(flags - accepted)


def test_client_setup_js_commands_are_documented_in_agent_setup() -> None:
    literals = re.findall(r'"((?:claude|codex) mcp [^"]+)"', _read(_CLIENT_SETUP_JS))
    assert "claude mcp add --transport stdio --scope user lab-tracker -- lt-mcp" in literals
    assert "codex mcp add lab-tracker -- lt-mcp" in literals
    documented = _read(_AGENT_SETUP_DOC)
    missing = [command for command in literals if command not in documented]
    assert not missing, f"web setup commands absent from docs/agent-setup.md: {missing}"


def _github_anchor(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def test_client_setup_js_docs_link_targets_an_existing_heading() -> None:
    source = _read(_CLIENT_SETUP_JS)
    path = re.search(r'CLIENT_SETUP_DOC_PATH = "([^"]+)"', source)
    anchor = re.search(r'CLIENT_SETUP_DOC_ANCHOR = "([^"]+)"', source)
    assert path and anchor
    assert (_REPO_ROOT / path[1]).is_file()
    headings = re.findall(r"(?m)^#{1,6} (.+)$", _read(_REPO_ROOT / path[1]))
    assert anchor[1] in {_github_anchor(heading) for heading in headings}
    assert anchor[1] == _github_anchor(_CLIENT_MATRIX_HEADING)


def test_verify_mcp_command_help_matches_the_documented_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # agent-setup.md says verify-mcp prefers the lt-mcp next to the lt you ran;
    # `_resolve_mcp_executable` does so, and the flag's own help must agree.
    with pytest.raises(SystemExit) as exit_info:
        lt_cli.main(["setup", "verify-mcp", "--help"])
    assert exit_info.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Defaults to the lt-mcp installed beside this lt, else lt-mcp on PATH" in help_text
    assert "Defaults to lt-mcp on PATH" not in help_text
    assert "prefers the `lt-mcp` next to the `lt` you ran" in " ".join(
        _read(_AGENT_SETUP_DOC).split()
    )


def test_lt_run_counts_as_the_tool_environment_although_it_writes_run_metadata() -> None:
    # The `lt-` adapter is judged before `run_*` metadata, so a stale `lt run`
    # capture gets the tool-install fix; only a non-`lt-` adapter with `run_*`
    # metadata marks an analysis repo. docs/setup.md must say the same.
    lt_run = {EVIDENCE_ADAPTER_KEY: "lt-run", "run_id": "r1", "run_git_commit": "abc1234"}
    in_script = {EVIDENCE_ADAPTER_KEY: "lab-tracker-client-run", "run_id": "r1"}
    assert capture_environment(lt_run) is CaptureEnvironment.TOOL
    assert capture_environment(in_script) is CaptureEnvironment.ANALYSIS_REPO
    setup_doc = " ".join(_read(_DOCS / "setup.md").split())
    assert (
        "since an `lt-` adapter such as `lt run` always counts as the tool environment"
        in setup_doc
    )


def test_no_doc_still_states_the_retired_major_minor_only_update_rule() -> None:
    # A PATCH release now notifies (docs/versioning.md). The dedicated-instance
    # README kept the old rule for a while because it lives outside docs/, so
    # scan every directory that carries maintained prose.
    root = _DOCS.parent
    paths = [
        *_DOCS.glob("*.md"),
        *(root / "deployments").rglob("*.md"),
        *(root / "skills").rglob("*.md"),
    ]
    retired = (
        "only when a client's `MAJOR.MINOR`",
        "a PATCH release is reported",
        "PATCH-only gap",
    )
    stale = [
        f"{path.relative_to(root)}: {phrase}"
        for path in paths
        for phrase in retired
        if phrase in " ".join(path.read_text(encoding="utf-8").split())
    ]
    assert not stale, f"docs still state the retired MAJOR.MINOR-only rule: {stale}"


# Capture-setup tips (src/lab_tracker/capture_setup_catalog.py) quote doc
# anchors, `lt` commands, and UI labels to a person; each must stay real.
_FRONTEND_SRC = _REPO_ROOT / "src" / "lab_tracker" / "frontend_src"
_BACKTICKED_LT_COMMAND = re.compile(r"`(lt [^`]+)`")
_LT_COMMAND_PLACEHOLDERS = (
    ("LT-<code>", "LT-" + "A" * 26),
    ("<folder>", "acq"),
    ("<uuid>", "00000000-0000-4000-8000-000000000000"),
)


def _capture_setup_lt_commands() -> list[tuple[str, str]]:
    """``(gap, command)`` for every backticked `lt` command and every guide command."""

    commands: list[tuple[str, str]] = []
    for gap, guide in CAPTURE_SETUP_GUIDES.items():
        for text in (guide.title, *guide.steps):
            commands.extend((gap.value, found) for found in _BACKTICKED_LT_COMMAND.findall(text))
        if guide.command is not None:
            commands.append((gap.value, guide.command))
    return commands


# A double-quoted or template string (kept, so "https://" or "image/*" is not
# read as a comment), else a block comment, else a line comment (dropped).
# Single quotes are not strings here: JSX text uses them as apostrophes.
_JS_STRING_OR_COMMENT = re.compile(
    r'(?P<string>"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)|/\*.*?\*/|(?<![:\w])//[^\n]*',
    re.DOTALL,
)
# Where a component renders a label: the whole text of a JSX element, a
# branch of a conditional inside a JSX expression ({done ? "Done" : "Skip"}),
# or the label of an app nav link (["home", "/app", "Home"] in shared/ui.jsx).
_RENDERED_LABEL_FORMS = (
    r">\s*{label}\s*<",
    r'\?\s*(?:"[^"\n]*"|\w+)\s*:\s*"{label}"\s*\}}',
    r'\?\s*"{label}"\s*:',
    r'\[\s*"\w+",\s*"/app[^"\n]*",\s*"{label}"\s*\]',
)


def _without_js_comments(source: str) -> str:
    return _JS_STRING_OR_COMMENT.sub(lambda match: match["string"] or "", source)


def _component_shows(label: str, source: str) -> bool:
    """True when ``source`` renders ``label`` itself, not just names it.

    Comments are dropped first, and a string literal counts only in a
    rendered position (:data:`_RENDERED_LABEL_FORMS`), so a doc comment or an
    unrelated constant that quotes the label cannot keep a renamed label green.
    """

    code = _without_js_comments(source)
    escaped = re.escape(label)
    return any(
        re.search(form.format(label=escaped), code) is not None for form in _RENDERED_LABEL_FORMS
    )


def test_capture_setup_guides_link_existing_doc_headings() -> None:
    broken: list[str] = []
    for gap, guide in CAPTURE_SETUP_GUIDES.items():
        path, _, anchor = guide.doc.partition("#")
        doc = _REPO_ROOT / path
        headings = re.findall(r"(?m)^#{1,6} (.+)$", _read(doc)) if doc.is_file() else []
        if anchor not in {_github_anchor(heading) for heading in headings}:
            broken.append(f"{gap.value}: {guide.doc}")
    assert not broken, f"capture-setup guides link missing doc headings: {broken}"


def test_capture_setup_guide_lt_commands_parse() -> None:
    commands = _capture_setup_lt_commands()
    assert commands, "the catalog names no `lt` command, so this check checks nothing"
    parser = lt_cli._build_parser()
    rejected: list[str] = []
    for gap, command in commands:
        concrete = command
        for placeholder, value in _LT_COMMAND_PLACEHOLDERS:
            concrete = concrete.replace(placeholder, value)
        argv = shlex.split(concrete)
        if "<" in concrete or argv[:1] != ["lt"]:
            rejected.append(f"{gap}: {command} (unfilled placeholder or not `lt`)")
            continue
        try:
            parser.parse_args(argv[1:])
        except SystemExit:
            rejected.append(f"{gap}: {command}")
    assert not rejected, f"capture-setup `lt` commands the parser rejects: {rejected}"


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("Capture into this session", '<h3 id="title">Capture into this session</h3>'),
        ("Write NFC tag", "<button>\n  Write NFC tag\n</button>"),
        ("Open bench kiosk", '<a href="https://kiosk.example/s">Open bench kiosk</a>'),
        ("Skip", '<button>{finished ? "Done" : "Skip"}</button>'),
        ("Skip", '<button>{recording ? "Skip" : "Done"}</button>'),
        ("Home", 'const LINKS = [\n  ["home", "/app", "Home"],\n];'),
    ],
)
def test_capture_setup_label_check_accepts_rendered_text(label: str, source: str) -> None:
    assert _component_shows(label, source)


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("Capture into this session", '// "Capture into this session": a QR\n<h3>QR</h3>'),
        ("Write NFC tag", '/**\n * "Write NFC tag": an NFC sticker\n */\n<button>Tag</button>'),
        ("Write NFC tag", "<div>{/* <b>Write NFC tag</b> */}</div>"),
        ("Hands-free shortcut", 'const DEFAULT_LABEL = "Hands-free shortcut";'),
        ("Hands-free shortcut", 'save({ title: "Hands-free shortcut" });'),
        ("Debrief", "<p>Use Debrief after a session.</p>"),
    ],
)
def test_capture_setup_label_check_ignores_comments_and_unrendered_strings(
    label: str, source: str
) -> None:
    assert not _component_shows(label, source)


def test_capture_setup_guide_ui_labels_exist_in_their_components() -> None:
    pinned = [
        (gap.value, label, _FRONTEND_SRC / component)
        for gap, guide in CAPTURE_SETUP_GUIDES.items()
        for label, component in guide.ui_labels
    ]
    assert pinned, "the catalog pins no UI label, so this check checks nothing"
    missing = [
        f"{gap}: {label!r} in {path.relative_to(_FRONTEND_SRC)}"
        for gap, label, path in pinned
        if not path.is_file() or not _component_shows(label, _read(path))
    ]
    assert not missing, f"capture-setup UI labels absent from their components: {missing}"


_CAPTURE_SETUP_JS = _FRONTEND_SRC / "features" / "graph-drafts" / "capture-setup.js"
_JS_APP_PATH_LABELS = re.compile(
    r"const APP_PATH_LABELS = new Map\(\[(?P<entries>.*?)\]\);", re.DOTALL
)
_JS_APP_PATH_ENTRY = re.compile(r'\[\s*"(?P<path>[^"]+)",\s*"[^"]+"\s*\]')
_JS_SESSION_PATH_RE = re.compile(r"const SESSION_PATH_RE =\s*/(?P<source>[^\n]+)/i;")
_EXAMPLE_SESSION_ID = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"


def _review_page_tip_paths() -> tuple[set[str], re.Pattern[str]]:
    """The fixed app paths capture-setup.js opens, and its session-page pattern."""

    source = _without_js_comments(_read(_CAPTURE_SETUP_JS))
    labels = _JS_APP_PATH_LABELS.search(source)
    session = _JS_SESSION_PATH_RE.search(source)
    assert labels is not None and session is not None, (
        "capture-setup.js no longer declares APP_PATH_LABELS and SESSION_PATH_RE"
    )
    fixed = {entry["path"] for entry in _JS_APP_PATH_ENTRY.finditer(labels["entries"])}
    return fixed, re.compile(session["source"], re.IGNORECASE)


def test_capture_setup_guide_app_paths_are_the_pages_the_review_page_opens() -> None:
    # The review page shows an "Open" button only for a path it allowlists, so a
    # guide path missing from that list would quietly lose its button, and a
    # listed path no guide uses is dead code.
    fixed, session_page = _review_page_tip_paths()
    paths = {guide.app_path for guide in CAPTURE_SETUP_GUIDES.values() if guide.app_path}
    session_paths = {path for path in paths if SESSION_ID_PLACEHOLDER in path}
    assert session_paths, "no guide opens a session page, so the session check checks nothing"
    unopened = [
        path
        for path in sorted(session_paths)
        if not session_page.fullmatch(path.replace(SESSION_ID_PLACEHOLDER, _EXAMPLE_SESSION_ID))
    ]
    assert not unopened, f"the review page opens no button for these guide paths: {unopened}"
    assert paths - session_paths == fixed, (
        "capture-setup.js APP_PATH_LABELS must list exactly the catalog's fixed app paths"
    )


_STATIC_DEMO_API = _FRONTEND_SRC / "shared" / "static-demo-api.js"
_STATIC_DEMO_CAPTURE_SETUP = _FRONTEND_SRC / "shared" / "static-demo-capture-setup.json"
_DEMO_CAPTURE_SETUP_IMPORT = re.compile(
    r'^import (?P<name>\w+) from "\./static-demo-capture-setup\.json" with \{ type: "json" \};$',
    re.MULTILINE,
)
_DEMO_CAPTURE_SETUP_VALUE = re.compile(r"\bcapture_setup:\s*(?P<value>[^\s,]+)")
_CANDIDATE_FIELDS = (
    "kind",
    "gap",
    "detected",
    "note_ids",
    "note_count",
    "session_id",
    "session_label",
)


def _server_recorded(packet: dict[str, Any]) -> dict[str, Any] | None:
    """What the server records when the drafter picks exactly the demo's tips.

    Each demo tip is replayed as an offered candidate plus the drafter's pick
    through :func:`resolve_capture_setup`, so the server, not this test,
    supplies every guide field, the kind for the gap, and the explanation source.
    """

    tips = [tip for tip in packet.get("recommendations") or [] if isinstance(tip, dict)]
    candidates = [
        {"candidate_id": tip.get("recommendation_id")}
        | {field: tip.get(field) for field in _CANDIDATE_FIELDS}
        for tip in tips
    ]
    picks = [
        {
            "candidate_id": tip.get("recommendation_id"),
            "note_ids": tip.get("note_ids"),
            "explanation": tip.get("explanation"),
        }
        for tip in tips
    ]
    return resolve_capture_setup(candidates, picks)


def test_static_demo_capture_setup_is_what_the_server_records() -> None:
    # The Pages demo shows a sample tip without a server. Its whole packet must
    # equal the server's own result for the same picks, so the demo never shows
    # a step, command, link, or guide Lab Tracker would not record.
    packet = json.loads(_read(_STATIC_DEMO_CAPTURE_SETUP))
    assert packet.get("recommendations"), (
        "the static demo carries no capture-setup tip, so this check checks nothing"
    )
    assert packet == _server_recorded(packet)


def test_static_demo_draft_takes_its_capture_setup_from_the_checked_fixture() -> None:
    source = _read(_STATIC_DEMO_API)
    imported = _DEMO_CAPTURE_SETUP_IMPORT.search(source)
    assert imported is not None, "static-demo-api.js no longer imports the capture-setup fixture"
    values = [match["value"] for match in _DEMO_CAPTURE_SETUP_VALUE.finditer(source)]
    assert values == [imported["name"]], (
        f"the demo's capture_setup must be the checked fixture alone, found: {values}"
    )


_SESSION_SUGGESTIONS_DOC = _DOCS / "session-suggestions.md"
_CAPTURE_SETUP_DOC_CONSTANTS = (
    "MAX_CANDIDATES",
    "MAX_DEBRIEF_SESSIONS",
    "MAX_NOTE_IDS",
    "MAX_RECOMMENDATIONS",
    "EXPLANATION_MAX_CHARS",
    "THIN_CAPTURE_MAX_CHARS",
    "COOLDOWN_DAYS",
)


def test_capture_setup_doc_names_every_kind_and_gap() -> None:
    text = _read(_SESSION_SUGGESTIONS_DOC)
    missing = [
        member.value
        for member in (*CaptureSetupKind, *CaptureSetupGap)
        if f"`{member.value}`" not in text
    ]
    assert not missing, f"docs/session-suggestions.md omits capture-setup kinds or gaps: {missing}"


def test_capture_setup_doc_states_the_catalog_bounds() -> None:
    # Each bound is written "<value> ... (`NAME`)", e.g. "at most 6 tips
    # (`MAX_RECOMMENDATIONS`)", and every mention must carry the current value,
    # so a page that states a bound twice cannot keep one stale copy.
    text = " ".join(_read(_SESSION_SUGGESTIONS_DOC).split())
    stale: list[str] = []
    for name in _CAPTURE_SETUP_DOC_CONSTANTS:
        value = str(getattr(capture_setup_catalog, name))
        mentions = text.count(f"(`{name}`)")
        stated = re.findall(rf"(?<!\d)(\d+)[^().`\d]*\(`{name}`\)", text)
        if mentions == 0 or len(stated) != mentions or set(stated) != {value}:
            stale.append(f"{name}={value} (doc states {stated} in {mentions} mentions)")
    assert not stale, f"docs/session-suggestions.md states other capture-setup bounds: {stale}"


_GAP_TABLE_ROW = re.compile(
    r"^\| `(?P<kind>[a-z0-9_]+)` \| `(?P<gap>[a-z0-9_]+)` \|.*\| (?P<threshold>\d+)[^|]* \|$",
    re.MULTILINE,
)


def test_capture_setup_doc_gap_table_matches_the_catalog_and_thresholds() -> None:
    # One row per gap: its kind, and the "Detected at" count Lab Tracker's own
    # check uses (DETECTION_THRESHOLDS), so the table cannot drift from either.
    rows = {
        row["gap"]: (row["kind"], int(row["threshold"]))
        for row in _GAP_TABLE_ROW.finditer(_read(_SESSION_SUGGESTIONS_DOC))
    }
    expected = {
        gap.value: (guide.kind.value, DETECTION_THRESHOLDS[gap])
        for gap, guide in CAPTURE_SETUP_GUIDES.items()
    }
    assert rows == expected
