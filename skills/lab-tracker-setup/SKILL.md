---
name: lab-tracker-setup
description: Guide a user through setting up Lab Tracker capture in a consumer repo or on a new machine. Use when the user asks to set up Lab Tracker, connect a repo, configure watch folders, enroll commit hooks, bind a project, capture figures from notebooks, scripts, or R, record command or pipeline runs, capture coding-agent sessions, or asks which capture paths exist, or when `lt setup status` / a session hook reports unconfigured or drifted capture. Covers the consent-gated `lt` setup verbs and their choreography.
allowed-tools: "Read,Bash(lt setup status:*),Bash(lt setup verify-client:*),Bash(lt setup verify-mcp:*),Bash(lt doctor:*)"
version: "0.1.0"
compatible-with: claude-code,codex
tags: [lab-tracker, setup, onboarding, capture]
---

# Lab Tracker Guided Setup (agent-led)

You are the wizard: inventory what exists, narrate what is missing, and walk
the user through the consent-gated commands one approval at a time. You run
only the read-only inventory and `--dry-run` previews yourself; the user
approves every applying command.

The staged script below is generated from the installed package
(`lab_tracker.setup_guide.setup_skill_markdown`) and kept honest by a drift
test; the guide text is also served as the `lab-tracker://setup-guide` MCP
resource.

<!-- BEGIN GENERATED SETUP GUIDE -->
# Lab Tracker Guided Setup

Lab Tracker captures research artifacts (figures, watched folders, git
commits) as staged evidence that a person later reviews. Setup is a
short, consent-gated sequence on the `lt` CLI.

## Consent rules (hard requirements)

- `lt setup status` and `lt setup verify-client` are local read-only
  checks. `lt setup verify-mcp` launches the configured executable but
  makes only health and project-list reads.
- Repository setup writes that support `--dry-run` are previewed
  before applying. Package installs (`uv tool install`, `uv add`) do
  not have a Lab Tracker dry run, so a person reviews and runs each
  exact server-pinned command separately.
- A person approves each applying command. `lt setup connect`,
  `lt project bind`, and `lt hooks install` additionally require an
  explicit `--yes`.
- One command per approval; the diff or preview is shown first.
- Access tokens are minted by a person in the Lab Tracker web app and
  are never relayed through an agent.

## The staged sequence

1. **Matching client** — the web app's Setup page supplies an install
   requirement pinned to the running server's full Git revision. If the
   server cannot report that revision, setup stops instead of falling
   back to a moving branch. `lt setup verify-client
   --expected-revision <revision>` checks the PEP 610 install metadata.
2. **Inventory** — `lt setup status` reports server reachability, the
   connection profile, repo scaffolding, project binding, watch
   folders, and commit-hook enrollment in one JSON payload, with
   suggestions for whatever is missing.
3. **Connectivity** — when no server is reachable, `lab-tracker serve`
   starts a local instance; a lab usually shares one instance and its
   URL comes from whoever operates it.
4. **Connection profile** — `lt setup connect --base-url <url>
   --project <project-id> --yes` persists the server URL and exact
   default project in
   `~/.lab-tracker/config.json` so hooks and schedulers work without
   per-shell environment variables. Token storage is a separate
   consent (`--save-token`). Commit and figure capture need the web
   app's least-privilege **Read + stage evidence** token; read-only
   tokens cannot sync captures.
5. **Project Python dependency** — once the server reports its full
   source revision, the Setup page supplies a pinned `uv add` command
   for each analysis repository. Verify that `uv run
   python` can import `lab_tracker_client` before relying on figure
   capture from that project environment.
6. **Repo scaffolding** — `lt setup init --install-skills` writes the
   integration files
   (MCP config, prompt hooks, `lt_ids.json`). The MCP files use the
   saved/env Lab Tracker URL when one exists, otherwise localhost;
   the setup skill is installed in both Claude and Codex user homes;
   `lt update` refreshes them after a package upgrade. To install or
   refresh only the setup skill (for example when `lt setup status`
   reports it missing or stale), `lt update --skills-only` works
   machine-wide and never touches the current directory or any repo
   (`--dry-run` previews; it cannot be combined with `--yes` or
   `--target`).
7. **Project binding** — `lt project bind --project-id <project-id>
   --yes` verifies the selected project and records its exact id in
   `lt_ids.json`.
8. **Watch folders** — `lt watch add <folder> --include <glob>`
   registers a narrow results folder; broad roots such as `artifacts/`
   are usually skipped or narrowed to a run-specific subfolder. `lt
   watch scan` and `lt watch sync` capture and upload on demand or
   from a scheduler; `lt setup schedule --yes` registers `lt watch
   run` (scan, watch sync, and the repo and HPC outbox drain) with the
   OS scheduler, and `--request-draft` makes that run ask for AI
   drafts too. A folder or file named with a session's `LT-<code>`
   link code attaches its captures to that session; `lt session use
   <code>` checks the session on the server and does the same for
   every capture from the checkout into that session's project for
   the next twelve hours.
9. **Figure autotrack (optional)** — `lt setup autotrack --yes` adds an
   IPython startup file so matplotlib figures a notebook or shell saves,
   or a notebook displays inline, are captured without code changes
   (`--dry-run` previews, `LAB_TRACKER_AUTOTRACK=0` disables).
   `--jupyter` instead enables a Jupyter save hook that files each
   notebook's day of saves as one staged page (restart Jupyter),
   `--scripts` adds a `.pth` file to the Python environment that runs
   it so plain scripts capture the figures they save or `plt.show()`,
   and `--r` adds the same to `~/.Rprofile` for `ggsave()` and the
   png/jpeg/tiff/bmp/pdf devices.
   All of it captures only inside a checkout bound with `lt project
   bind` (or with `LAB_TRACKER_PROJECT_ID` set) and skips the rest
   with a notice. Captures made while the server is unreachable queue
   in the checkout's watch outbox and drain with the next sync.
10. **Commit hooks** — `lt hooks install --project <project-id> --yes`
    enrolls the current repository: each commit queues durable staged
    evidence that syncs when the server is reachable. Repos are enrolled
    one consented command at a time.
11. **MCP launch verification** — after client registration, `lt setup
    verify-mcp --expected-revision <revision>` launches `lt-mcp` over
    stdio, initializes the protocol, calls health, and performs an
    authenticated project read through the saved profile.
12. **Agent session capture (optional)** — `lt setup agent-hooks
    --dry-run` previews two Claude Code hooks for the person's own
    `.claude/settings.local.json`: when a session ends, `lt agent
    session-end` stages one bounded, redacted retrospective (prompts,
    files edited, commands, test results; never the transcript) that
    asks for human-reviewed drafts, and a file the agent writes into a
    watch folder is queued at once by `lt watch touch`. It records agent
    conversations, so it is offered rather than assumed: the person
    decides, and `--yes` applies it for them alone (`--uninstall`
    removes it; `LAB_TRACKER_AGENT_HOOKS=0` turns it off). `--shared`
    writes the committed `.claude/settings.json` instead, which would
    capture the sessions of everyone who clones the repository, so it
    is a team decision rather than a setup default.
13. **Runs and pipelines (optional, nothing to install)** — once the
    project is bound, `lt run --output <dir> -- <command>` records an
    analysis command (redacted command line, git and working-copy state,
    hashed pointers to the files it wrote under `--output`) without
    changing its exit status. `lt pipeline report`, `lt pipeline
    nextflow --trace`, `lt pipeline dvc`, and the Snakemake and Kedro
    adapters in `lab_tracker_client.integrations` record a pipeline
    run's declared inputs and outputs; `lt hpc submit -- sbatch ...`
    records Slurm jobs, which a cluster admin's TaskEpilog
    (`scripts/slurm-task-epilog.sh`) can finish without job-script
    edits; the `lab-tracker-repo-report` GitHub Action records commits
    from CI; and `lt capture file <path>` stages one saved file from any
    language. Offer the ones that match how the person already works.
14. **Beyond this machine** — bench capture lives in the web app: a
    session's page offers its capture QR, an NFC tag writer, photo
    import, a voice debrief, and the bench kiosk, and the Devices page
    offers the kiosk, a hands-free phone shortcut, a desktop bookmarklet,
    and the person's email capture address. Slack, email, instrument
    calendars, registered-store scans, and photo barcode decoding are
    server settings an operator turns on. `docs/capture-guide.md` in the
    Lab Tracker repository maps every capture path to its setup.

## After setup

Captures stage for human review — nothing commits to the research
graph automatically. Server-side AI drafting uses the operator's
configured provider credential; no local OpenAI key is needed for Lab
Tracker. `lt doctor` and `lt setup status` surface drift after package
upgrades and confirm that `lt-mcp` can start, and `lt update` is the
refresh path for a repo (`lt update --skills-only` is the one for the
setup skill alone). When the server moves to a newer MAJOR.MINOR release
(docs/versioning.md), `lt setup status`, `lt-mcp` notices, and the
Daily review name each client that should update; a PATCH-only gap is
reported, never suggested. The `uv tool` install updates with the
Setup page's server-pinned install, then `lt update` refreshes each
repo; an analysis repo updates by rerunning its pinned `uv add`
(step 5), which `lt update` does not change.
<!-- END GENERATED SETUP GUIDE -->

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

<!-- lab-tracker-setup-guide version=0.1.0 sha256=40cf62390b24 -->
