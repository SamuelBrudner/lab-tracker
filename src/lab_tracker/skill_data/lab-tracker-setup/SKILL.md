---
name: lab-tracker-setup
description: Guide a user through setting up Lab Tracker capture in a consumer repo or on a new machine. Use when the user asks to set up Lab Tracker, connect a repo, configure watch folders, enroll commit hooks, bind a project, capture figures from notebooks, scripts, or R, record command or pipeline runs, capture coding-agent sessions, or asks which capture paths exist, or when `lt setup status` / a session hook reports unconfigured or drifted capture. Covers the consent-gated `lt` setup verbs and their choreography.
allowed-tools: "Read,Bash(lt setup status:*),Bash(lt setup verify-client:*),Bash(lt setup verify-mcp:*),Bash(lt doctor:*)"
metadata:
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
  before applying. `lt maintain --dry-run` previews package, repo and
  skill maintenance as one job; a person reviews that plan before
  approving the matching `--yes` command. Direct package installs
  (`uv tool install`, `uv add`) have no Lab Tracker dry run, so a
  person reviews each exact server-pinned command separately.
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
   starts a local instance (never in a cloud session's VM; see *Cloud
   sessions* below); a lab usually shares one instance and its URL
   comes from whoever operates it.
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
   `--install-skills` also installs the setup skill in both Claude and
   Codex user homes. After a package upgrade, `lt update` refreshes
   the repo's files only; `lt update --install-skills` refreshes the
   skill as well. To install or refresh only the setup skill (for
   example when `lt setup status` reports it missing or stale),
   `lt update --skills-only` works machine-wide and touches no repo or
   file in the current directory (`--dry-run` previews; it cannot be
   combined with `--yes` or `--target`); a relative
   `LAB_TRACKER_SKILLS_HOME` resolves against the current directory,
   so keep that override absolute. Claude Code reads the scaffolded
   `.mcp.json`, which carries no token, and asks the person to
   approve the server on first run. Claude Desktop chat, Codex in the
   ChatGPT desktop app, and the Codex CLI keep their MCP registration in
   user-level settings that setup never writes; `docs/agent-setup.md`
   in the Lab Tracker repository lists each client's registration.
7. **Project binding** — `lt project bind --project-id <project-id>
   --yes` verifies the selected project and records its exact id in
   `lt_ids.json`. Work already underway in a Lab Tracker project binds
   that project rather than a new one (see *Projects already
   underway* below).
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
    authenticated project read through the saved profile. It uses the
    terminal's environment, so for a desktop app it takes `--command
    <absolute path>` naming the `lt-mcp` that app registered, and the
    in-client check is asking the assistant to call
    `lab_tracker_list_projects` with limit 1.
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
    `lt doctor` and `lt setup status` explain missing or partial hooks
    and show the preview and personal installation commands. These
    are optional suggestions, never automatic installation or doctor
    failures. Delivery needs a bound project and a server connection
    with a Read + stage evidence token.
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

## Projects already underway

Setup configures capture going forward, with one exception. Commit
hooks record commits made after `lt hooks install`, not earlier ones,
but a watch folder's first scan stages files already in it. A watch
in the default `files` mode stages every file its `--include` and
`--exclude` globs match. The globs filter by path and name, not age,
so a fresh or run-specific folder (or an exclude for older paths)
keeps old material out. A `manifest`-mode watch ignores the globs and
stages every manifest below its root that its filename `pattern`
matches (`lab-tracker-evidence.json` by default), so a fresh root
keeps old manifests out. `lt watch scan --dry-run` previews a scan.
`lt setup status` reports configuration, not content, so a healthy
checkout can still belong to a project with no records.

For such a project, "Orient me to this project" in the web app
(`/app/projects/<project-id>/onboarding`) records one attributed
checkpoint of where the work stands, its live questions, and the next
move, and capture continues from there. Contributors and owners can
record it; a viewer sees "View project orientation" and needs edit
access first. Earlier history is not imported;
`docs/member-onboarding.md` in the Lab Tracker repository describes
the checkpoint. Agents connected over MCP suggest it once per session
when the graph overview shows no records, and orienting stays the
person's step.

A folder that is not a git repository takes the same connection,
binding, and watch steps without commit hooks, and `lt import-folder`
stages the files a synced folder holds now, skipping duplicates on
later runs (`docs/configuration.md`, *Local evidence inbox imports*).
`lt repo install-hook` runs the same installer as `lt hooks install`
but writes at once, with no `--dry-run` preview or `--yes` gate, so
guided setup uses `lt hooks install` (`docs/repo-report-capture.md`).

## Cloud sessions

A cloud session (Claude Code on the web, `claude --cloud`, a cloud
scheduled task, a Codex cloud task, or another vendor's hosted agent)
runs in a fresh VM cloned from the repository and discarded later, so
nothing the steps above wrote to the person's computer is there. `lt
setup status` recognizes one from `CLAUDE_CODE_REMOTE=true` or an
explicit `LAB_TRACKER_CLOUD_SESSION` (OpenAI documents no marker for a
Codex cloud task, so its environment sets
`LAB_TRACKER_CLOUD_SESSION=codex`) and suggests cloud environment
settings instead, which a person changes in the environment's settings:

- the setup script (a Codex environment's install script) runs the
  server-pinned `uv tool install` (step 1) and `lt update
  --skills-only`, so `lt`, `lt-mcp`, and both skills are in every
  session's VM;
- `LAB_TRACKER_BASE_URL` holds an address the VM can reach, normally
  the lab server's public HTTPS address: the VM's loopback is the VM
  itself, and only a VPN the environment joins (a Codex environment's
  Tailscale setting) puts it on a lab network or tailnet; for Claude
  Code, the committed `.mcp.json` carries the same address, since its
  entry overrides the environment's value;
- the network or internet access setting allows that host (a Codex
  agent has none until it is turned on);
- the token is a network secret for that host or an environment
  variable (`LAB_TRACKER_ACCESS_TOKEN` and `LAB_TRACKER_MCP_API_KEY`),
  which anyone who uses the environment can read. A Claude Code network
  secret never enters the VM; a Codex one reaches programs as a
  placeholder under the variable's name, replaced only on HTTPS port
  443; a Codex Cloud (Legacy) secret reaches only the setup script, so
  there the token is an environment variable. It is never pasted into
  the chat.

OpenAI documents no MCP server for Codex cloud tasks, so a Codex agent
there reads AGENTS.md and reaches Lab Tracker through `lt`:
`lt decision-context --task-kind <kind> --query <text>` returns
the decision context the MCP tool returns, for the project in
`lt_ids.json`, and `lt prime --project <project_id from lt_ids.json>`
ranks the project's open questions under its planned or in-progress
goals. Codex Cloud (Legacy), which
still runs @codex requests on GitHub and Linear tasks, can limit the
agent to GET, HEAD, and OPTIONS requests, which blocks every capture
and `lt decision-context`, a POST.

Committed files (`.mcp.json`, `.claude/settings.json`, `lt_ids.json`,
the managed blocks) reach every cloud session, so `lt setup init`,
`lt update`, and `lt project bind` still apply; the `.mcp.json`
server and the `.claude/settings.json` hooks load only in a session
with one repository. A saved profile, watch folders, commit hooks, OS
schedules, autotrack startup files, and personal agent hooks made in
the VM do not outlive it, and `lab-tracker serve` there would keep
captures in a VM that is discarded. A client update is a new revision
in the setup script: `lt maintain` in the VM changes only that
session. The `lab-tracker-repo-report` GitHub Action records, in CI,
the head commit of each push or pull request its workflow triggers
on, pushes from cloud sessions included. Before a cloud session that
captured anything ends, `lt outbox sync` and then `lt outbox status`
confirm nothing is left in the VM. `docs/cloud-agents.md` in the Lab
Tracker repository walks through each setting. In a Claude Code VM,
`lt setup verify-mcp --expected-revision <revision>` checks the
installed `lt-mcp` and the token with the environment's variables,
`lt setup status` checks the committed `.mcp.json` launch, and asking
the agent to call `lab_tracker_list_projects` checks the server Claude
Code started. In a Codex cloud task, which starts no `lt-mcp`, `lt
setup status` shows `cloud_session.host` as `codex` and `lt readiness`
checks the token.

## After setup

Captures stage for human review — nothing commits to the research
graph automatically. Server-side AI drafting uses the operator's
configured provider credential; no local OpenAI key is needed for Lab
Tracker. `lt doctor` and `lt setup status` surface drift after package
upgrades and check the MCP import in this CLI's Python environment;
they do not verify the app's registered executable. Both compare this
client's release with the server's (`lt doctor` only warns when the
server cannot be reached). `lt update` is the refresh path for a repo
(`lt update --skills-only` is the one for the setup skill alone). When
the server runs a newer release, a PATCH release included
(docs/versioning.md), `lt setup status`, `lt doctor`, `lt-mcp` notices,
and the Daily review name the clients that should update (each is
judged by its captures: `lt watch`, `lt run`, `lt pipeline`, `lt hpc`,
the repo hooks, agent sessions, notebook saves, and figure saves record
their client's release, notes made by hand or import do not); the same
release at a different commit is reported, never suggested.

For an update recommended by doctor or MCP, use the server's full
revision in `lt maintain --all --upgrade-client --revision <revision>
--refresh-skills --dry-run`. Review the package, repo and skill plan,
then the same command with `--yes` applies it. `--repo <path>` selects
individual repos, including ones not registered yet. An MCP-only
machine with no registered repos can still update its package and
skills; repo options are optional for those requested steps. `--lt-path
<absolute path to lt>` selects the MCP host's installation. Omitting
`--revision` deliberately targets GitHub main, so keep it for
server-directed updates. If the server cannot report its full
revision, ask its operator for a pinned release before upgrading.

Both `lt` and `lt-mcp` come from that package. Restart the MCP host
after a package upgrade, then `lt setup verify-mcp --expected-revision
<revision> --command <absolute path to the registered lt-mcp>` checks
the real launch and authenticated reads. Confirm in the app by asking
the assistant to list one Lab Tracker project.

If the installed CLI lacks `maintain`, run the same arguments through
`python3 scripts/maintain_lab_tracker.py` from a current Lab Tracker
checkout; it needs only the standard library. Without a checkout,
the Setup page's exact server-pinned `uv tool install` updates both
commands, then `lt update` refreshes each repo and `lt update
--skills-only` refreshes both skills. An analysis repo updates by
rerunning its pinned `uv add` (step 5), which neither `lt update` nor
`lt maintain` changes.
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
   back. For package upkeep, offer the server-pinned `lt maintain` preview
   above; after upgrading, restart and verify the app's registered `lt-mcp`.
   `lt update` refreshes repo files and `lt update --skills-only` refreshes
   both skills without upgrading the package.

If Lab Tracker is unreachable and the user does not operate a server, point
them at whoever runs their lab's instance instead of standing one up ad hoc.

<!-- lab-tracker-setup-guide version=0.3.0 sha256=7f10c8107e4d -->
