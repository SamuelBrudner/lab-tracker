"""Drift guards for repository prose that describes checkable facts.

Each test pins one statement in the docs, agent instructions, or skill prose to
the code or repository state it describes, so the prose fails loudly when the
code moves instead of silently going stale.
"""

from __future__ import annotations

import argparse
import inspect
import json
import re
from pathlib import Path

import httpx
import pytest
from read_opacity_inventory import READ_OPACITY_VARIANTS_BY_SUITE

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by Python 3.10 CI
    import tomli as tomllib

from lab_tracker import graph_drafting, mcp_server
from lab_tracker.cli import _skills_homes, init_consumer_repo, update_consumer_repo
from lab_tracker.decision_context_constants import AGENT_CONSULTATION_POLICY
from lab_tracker.mcp_tools import READ_TOOLS, WRITE_TOOLS
from lab_tracker_client import cli as lt_cli
from lab_tracker_client import setup as setup_helpers
from lab_tracker_client.auth import auth_doctor
from lab_tracker_client.client import LabTracker
from lab_tracker_client.registry import registry_path
from lab_tracker_client.transport import HEALTH_PROBE_DEADLINE_SECONDS

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _REPO_ROOT / "docs"
_SKILL_PATH = _REPO_ROOT / "skills" / "lab-tracker" / "SKILL.md"
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
        (_SKILL_PATH, "run `lt update` inside a consumer repo"),
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
        for path in (*_maintained_docs(), _SKILL_PATH)
        for match in _LT_UPDATE_INVOCATION.finditer(_read(path))
        if set(_LONG_FLAG.findall(match.group(1))) - accepted
    ]
    assert not unknown, f"documented `lt update` flags that the parser rejects: {unknown}"


@pytest.mark.parametrize("doc", [_DOCS / "setup.md", _SKILL_PATH])
def test_lt_update_docs_describe_the_skills_only_refresh(doc: Path) -> None:
    text = " ".join(_read(doc).split())
    assert "`lt update --skills-only`" in text
    assert "machine-wide" in text


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


def test_lt_doctor_help_names_the_lt_mcp_check() -> None:
    help_text = " ".join(lt_cli._build_parser().format_help().split())
    assert "code-facing idiom blocks and that lt-mcp can start" in help_text


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
@pytest.mark.parametrize("doc", [_SKILL_PATH, _MCP_SKILLS_DOC])
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


# Per-client MCP registration: the matrix in docs/agent-setup.md, the generated setup
# guide, and the web Setup and Agents pages (client-setup.js) must describe one thing.
_CLIENT_MATRIX_HEADING = "Choose your client"
_CLIENT_HEADINGS = (
    "Claude Code",
    "Claude Desktop chat",
    "Codex in the ChatGPT desktop app",
    "Codex CLI",
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


def _client_sections() -> dict[str, str]:
    """Each per-client `####` section of the matrix, keyed by its heading."""

    parts = re.split(r"(?m)^#### (.+)$", _client_matrix())
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_agent_setup_labels_a_section_for_every_client() -> None:
    headings = list(_client_sections())
    for label in _CLIENT_HEADINGS:
        assert any(heading.startswith(label) for heading in headings), (label, headings)


def test_agent_setup_states_the_claude_desktop_support_status() -> None:
    sections = _client_sections()
    desktop = next(body for name, body in sections.items() if name.startswith("Claude Desktop"))
    assert _CLAUDE_DESKTOP_SUPPORT_STATUS in _collapsed_whitespace(desktop)


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
