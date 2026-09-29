"""Canonical guided-setup text for agents, mirroring the code_facing_idioms
single-source pattern: one generator fans out to the repo-owned setup skill,
the ``lab-tracker://setup-guide`` MCP resource, and the ``--install-skills``
rendered copy, with drift pinned by a version/sha line."""

from __future__ import annotations

import hashlib

from lab_tracker.decision_context_constants import package_version

SETUP_GUIDE_BEGIN_MARKER = "<!-- BEGIN GENERATED SETUP GUIDE -->"
SETUP_GUIDE_END_MARKER = "<!-- END GENERATED SETUP GUIDE -->"
_SETUP_GUIDE_VERSION_PREFIX = "<!-- lab-tracker-setup-guide"


def setup_guide_markdown() -> str:
    """The staged guided-setup script with its consent choreography.

    The text is deliberately non-imperative toward the agent: it names what
    each command does and who approves it. The only commands an agent may run
    unprompted are the read-only inventory and ``--dry-run`` previews.
    """

    return (
        "# Lab Tracker Guided Setup\n"
        "\n"
        "Lab Tracker captures research artifacts (figures, watched folders, git\n"
        "commits) as staged evidence that a person later reviews. Setup is a\n"
        "short, consent-gated sequence on the `lt` CLI.\n"
        "\n"
        "## Consent rules (hard requirements)\n"
        "\n"
        "- `lt setup status` and `lt setup verify-client` are local read-only\n"
        "  checks. `lt setup verify-mcp` launches the configured executable but\n"
        "  makes only health and project-list reads.\n"
        "- Repository setup writes that support `--dry-run` are previewed\n"
        "  before applying. Package installs (`uv tool install`, `uv add`) do\n"
        "  not have a Lab Tracker dry run, so a person reviews and runs each\n"
        "  exact server-pinned command separately.\n"
        "- A person approves each applying command. `lt setup connect`,\n"
        "  `lt project bind`, and `lt hooks install` additionally require an\n"
        "  explicit `--yes`.\n"
        "- One command per approval; the diff or preview is shown first.\n"
        "- Access tokens are minted by a person in the Lab Tracker web app and\n"
        "  are never relayed through an agent.\n"
        "\n"
        "## The staged sequence\n"
        "\n"
        "1. **Matching client** — the web app's Setup page supplies an install\n"
        "   requirement pinned to the running server's full Git revision. If the\n"
        "   server cannot report that revision, setup stops instead of falling\n"
        "   back to a moving branch. `lt setup verify-client\n"
        "   --expected-revision <revision>` checks the PEP 610 install metadata.\n"
        "2. **Inventory** — `lt setup status` reports server reachability, the\n"
        "   connection profile, repo scaffolding, project binding, watch\n"
        "   folders, and commit-hook enrollment in one JSON payload, with\n"
        "   suggestions for whatever is missing.\n"
        "3. **Connectivity** — when no server is reachable, `lab-tracker serve`\n"
        "   starts a local instance; a lab usually shares one instance and its\n"
        "   URL comes from whoever operates it.\n"
        "4. **Connection profile** — `lt setup connect --base-url <url>\n"
        "   --project <project-id> --yes` persists the server URL and exact\n"
        "   default project in\n"
        "   `~/.lab-tracker/config.json` so hooks and schedulers work without\n"
        "   per-shell environment variables. Token storage is a separate\n"
        "   consent (`--save-token`). Commit and figure capture need the web\n"
        "   app's least-privilege **Read + stage evidence** token; read-only\n"
        "   tokens cannot sync captures.\n"
        "5. **Project Python dependency** — once the server reports its full\n"
        "   source revision, the Setup page supplies a pinned `uv add` command\n"
        "   for each analysis repository. Verify that `uv run\n"
        "   python` can import `lab_tracker_client` before relying on figure\n"
        "   capture from that project environment.\n"
        "6. **Repo scaffolding** — `lt setup init --install-skills` writes the\n"
        "   integration files\n"
        "   (MCP config, prompt hooks, `lt_ids.json`). The MCP files use the\n"
        "   saved/env Lab Tracker URL when one exists, otherwise localhost;\n"
        "   the setup skill is installed in both Claude and Codex user homes;\n"
        "   `lt update` refreshes them after a package upgrade. To install or\n"
        "   refresh only the setup skill (for example when `lt setup status`\n"
        "   reports it missing or stale), `lt update --skills-only` works\n"
        "   machine-wide and never touches the current directory or any repo\n"
        "   (`--dry-run` previews; it cannot be combined with `--yes` or\n"
        "   `--target`).\n"
        "7. **Project binding** — `lt project bind --project-id <project-id>\n"
        "   --yes` verifies the selected project and records its exact id in\n"
        "   `lt_ids.json`.\n"
        "8. **Watch folders** — `lt watch add <folder> --include <glob>`\n"
        "   registers a narrow results folder; broad roots such as `artifacts/`\n"
        "   are usually skipped or narrowed to a run-specific subfolder. `lt\n"
        "   watch scan` and `lt watch sync` capture and upload on demand or\n"
        "   from a scheduler; `lt setup schedule --yes` registers `lt watch\n"
        "   run` (scan, watch sync, and the repo and HPC outbox drain) with the\n"
        "   OS scheduler, and `--request-draft` makes that run ask for AI\n"
        "   drafts too. A folder or file named with a session's `LT-<code>`\n"
        "   link code attaches its captures to that session; `lt session use\n"
        "   <code>` checks the session on the server and does the same for\n"
        "   every capture from the checkout into that session's project for\n"
        "   the next twelve hours.\n"
        "9. **Figure autotrack (optional)** — `lt setup autotrack --yes` adds an\n"
        "   IPython startup file so matplotlib figures a notebook or shell saves,\n"
        "   or a notebook displays inline, are captured without code changes\n"
        "   (`--dry-run` previews, `LAB_TRACKER_AUTOTRACK=0` disables).\n"
        "   `--jupyter` instead enables a Jupyter save hook that files each\n"
        "   notebook's day of saves as one staged page (restart Jupyter),\n"
        "   `--scripts` adds a `.pth` file to the Python environment that runs\n"
        "   it so plain scripts capture the figures they save or `plt.show()`,\n"
        "   and `--r` adds the same to `~/.Rprofile` for `ggsave()` and the\n"
        "   png/jpeg/tiff/bmp/pdf devices.\n"
        "   All of it captures only inside a checkout bound with `lt project\n"
        "   bind` (or with `LAB_TRACKER_PROJECT_ID` set) and skips the rest\n"
        "   with a notice. Captures made while the server is unreachable queue\n"
        "   in the checkout's watch outbox and drain with the next sync.\n"
        "10. **Commit hooks** — `lt hooks install --project <project-id> --yes`\n"
        "    enrolls the current repository: each commit queues durable staged\n"
        "    evidence that syncs when the server is reachable. Repos are enrolled\n"
        "    one consented command at a time.\n"
        "11. **MCP launch verification** — after client registration, `lt setup\n"
        "    verify-mcp --expected-revision <revision>` launches `lt-mcp` over\n"
        "    stdio, initializes the protocol, calls health, and performs an\n"
        "    authenticated project read through the saved profile.\n"
        "12. **Agent session capture (optional)** — `lt setup agent-hooks\n"
        "    --dry-run` previews two Claude Code hooks for the person's own\n"
        "    `.claude/settings.local.json`: when a session ends, `lt agent\n"
        "    session-end` stages one bounded, redacted retrospective (prompts,\n"
        "    files edited, commands, test results; never the transcript) that\n"
        "    asks for human-reviewed drafts, and a file the agent writes into a\n"
        "    watch folder is queued at once by `lt watch touch`. It records agent\n"
        "    conversations, so it is offered rather than assumed: the person\n"
        "    decides, and `--yes` applies it for them alone (`--uninstall`\n"
        "    removes it; `LAB_TRACKER_AGENT_HOOKS=0` turns it off). `--shared`\n"
        "    writes the committed `.claude/settings.json` instead, which would\n"
        "    capture the sessions of everyone who clones the repository, so it\n"
        "    is a team decision rather than a setup default.\n"
        "13. **Runs and pipelines (optional, nothing to install)** — once the\n"
        "    project is bound, `lt run --output <dir> -- <command>` records an\n"
        "    analysis command (redacted command line, git and working-copy state,\n"
        "    hashed pointers to the files it wrote under `--output`) without\n"
        "    changing its exit status. `lt pipeline report`, `lt pipeline\n"
        "    nextflow --trace`, `lt pipeline dvc`, and the Snakemake and Kedro\n"
        "    adapters in `lab_tracker_client.integrations` record a pipeline\n"
        "    run's declared inputs and outputs; `lt hpc submit -- sbatch ...`\n"
        "    records Slurm jobs, which a cluster admin's TaskEpilog\n"
        "    (`scripts/slurm-task-epilog.sh`) can finish without job-script\n"
        "    edits; the `lab-tracker-repo-report` GitHub Action records commits\n"
        "    from CI; and `lt capture file <path>` stages one saved file from any\n"
        "    language. Offer the ones that match how the person already works.\n"
        "14. **Beyond this machine** — bench capture lives in the web app: a\n"
        "    session's page offers its capture QR, an NFC tag writer, photo\n"
        "    import, a voice debrief, and the bench kiosk, and the Devices page\n"
        "    offers the kiosk, a hands-free phone shortcut, a desktop bookmarklet,\n"
        "    and the person's email capture address. Slack, email, instrument\n"
        "    calendars, registered-store scans, and photo barcode decoding are\n"
        "    server settings an operator turns on. `docs/capture-guide.md` in the\n"
        "    Lab Tracker repository maps every capture path to its setup.\n"
        "\n"
        "## After setup\n"
        "\n"
        "Captures stage for human review — nothing commits to the research\n"
        "graph automatically. Server-side AI drafting uses the operator's\n"
        "configured provider credential; no local OpenAI key is needed for Lab\n"
        "Tracker. `lt doctor` and `lt setup status` surface drift after package\n"
        "upgrades and confirm that `lt-mcp` can start, and `lt update` is the\n"
        "refresh path for a repo (`lt update --skills-only` is the one for the\n"
        "setup skill alone). When the server moves to a newer MAJOR.MINOR release\n"
        "(docs/versioning.md), `lt setup status`, `lt-mcp` notices, and the\n"
        "Daily review name the clients that should update (each is judged by its\n"
        "captures: `lt watch`, `lt run`, `lt pipeline`, `lt hpc`, the repo hooks,\n"
        "agent sessions, notebook saves, and figure saves record their client's\n"
        "release, notes made by hand or import do not); a PATCH-only gap is\n"
        "reported, never suggested. The `uv tool` install updates with the\n"
        "Setup page's server-pinned install, then `lt update` refreshes each\n"
        "repo; an analysis repo updates by rerunning its pinned `uv add`\n"
        "(step 5), which `lt update` does not change.\n"
    )


def setup_guide_version_line(body: str | None = None) -> str:
    resolved = body if body is not None else setup_guide_markdown()
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"{_SETUP_GUIDE_VERSION_PREFIX} version={package_version()} sha256={digest} -->"


def skill_content_without_version_line(text: str) -> str:
    """Drop the trailing version/sha line so staleness stays a CONTENT verdict.

    Mirrors the doctor's content-only drift rule: a package bump that leaves
    the skill text unchanged must not flag the installed copy as stale.
    """

    return "\n".join(
        line for line in text.splitlines() if not line.startswith(_SETUP_GUIDE_VERSION_PREFIX)
    )


_SKILL_DESCRIPTION = (
    "Guide a user through setting up Lab Tracker capture in a consumer repo "
    "or on a new machine. Use when the user asks to set up Lab Tracker, "
    "connect a repo, configure watch folders, enroll commit hooks, bind a "
    "project, capture figures from notebooks, scripts, or R, record command "
    "or pipeline runs, capture coding-agent sessions, or asks which capture "
    "paths exist, or when `lt setup status` / a session hook reports "
    "unconfigured or drifted capture. Covers the consent-gated `lt` setup "
    "verbs and their choreography."
)

_SKILL_FRONTMATTER = (
    "---\n"
    "name: lab-tracker-setup\n"
    f"description: {_SKILL_DESCRIPTION}\n"
    # Read-only surface only: applying commands must still hit the agent
    # harness's own permission prompt (the second consent gate).
    'allowed-tools: "Read,Bash(lt setup status:*),Bash(lt setup verify-client:*),'
    'Bash(lt setup verify-mcp:*),Bash(lt doctor:*)"\n'
    'version: "0.1.0"\n'
    "compatible-with: claude-code,codex\n"
    "tags: [lab-tracker, setup, onboarding, capture]\n"
    "---\n"
)

_SKILL_PREAMBLE = """\
# Lab Tracker Guided Setup (agent-led)

You are the wizard: inventory what exists, narrate what is missing, and walk
the user through the consent-gated commands one approval at a time. You run
only the read-only inventory and `--dry-run` previews yourself; the user
approves every applying command.

The staged script below is generated from the installed package
(`lab_tracker.setup_guide.setup_skill_markdown`) and kept honest by a drift
test; the guide text is also served as the `lab-tracker://setup-guide` MCP
resource.
"""

_SKILL_CONVERSATION = """\
## Conversation shape

1. Start from `lt setup status` (safe, read-only) and summarize the gaps in
   plain language — which capture surfaces are configured, which are not.
2. For each gap the user wants closed, show the `--dry-run` preview, then let
   the user run (or approve) the applying command. Do not batch approvals.
3. Watch folders deserve a real elicitation: ask which folders actually
   accumulate results worth capturing rather than guessing.
4. Commit hooks are per-repo consent: name the repo, show the preview, and
   let the user apply `lt hooks install --yes` themselves when in doubt.
5. Close by re-running `lt setup status` and reflecting the healthy state
   back; mention that `lt update` refreshes everything after upgrades.

If Lab Tracker is unreachable and the user does not operate a server, point
them at whoever runs their lab's instance instead of standing one up ad hoc.
"""


def setup_skill_markdown() -> str:
    """The complete lab-tracker-setup SKILL.md content.

    The repo-owned copy and any ``--install-skills`` rendered copy are exact
    outputs of this function, pinned by the trailing version/sha line.
    """

    guide = setup_guide_markdown()
    return (
        _SKILL_FRONTMATTER
        + "\n"
        + _SKILL_PREAMBLE
        + "\n"
        + SETUP_GUIDE_BEGIN_MARKER
        + "\n"
        + guide.rstrip()
        + "\n"
        + SETUP_GUIDE_END_MARKER
        + "\n\n"
        + _SKILL_CONVERSATION
        + "\n"
        + setup_guide_version_line(guide)
        + "\n"
    )
