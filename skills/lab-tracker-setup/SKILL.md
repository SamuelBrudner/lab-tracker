---
name: lab-tracker-setup
description: Guide a user through setting up Lab Tracker capture in a consumer repo or on a new machine. Use when the user asks to set up Lab Tracker, connect a repo, configure watch folders, enroll commit hooks, bind a project, or when `lt setup status` / a session hook reports unconfigured or drifted capture. Covers the consent-gated `lt` setup verbs and their choreography.
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
5. **Project Python dependency** — the Setup page supplies a pinned
   `uv add` command for each analysis repository. Verify that `uv run
   python` can import `lab_tracker_client` before relying on figure
   capture from that project environment.
6. **Repo scaffolding** — `lt setup init --install-skills` writes the
   integration files
   (MCP config, prompt hooks, `lt_ids.json`). The MCP files use the
   saved/env Lab Tracker URL when one exists, otherwise localhost;
   the setup skill is installed in both Claude and Codex user homes;
   `lt update` refreshes them after a package upgrade.
7. **Project binding** — `lt project bind --project-id <project-id>
   --yes` verifies the selected project and records its exact id in
   `lt_ids.json`.
8. **Watch folders** — `lt watch add <folder> --include <glob>`
   registers a narrow results folder; broad roots such as `artifacts/`
   are usually skipped or narrowed to a run-specific subfolder. `lt
   watch scan` and `lt watch sync` capture and upload on demand or
   from a scheduler.
9. **Commit hooks** — `lt hooks install --project <project-id> --yes`
   enrolls the current repository: each commit queues durable staged
   evidence that syncs when the server is reachable. Repos are enrolled
   one consented command at a time.
10. **MCP launch verification** — after Codex registration, `lt setup
    verify-mcp --expected-revision <revision>` launches `lt-mcp` over
    stdio, initializes the protocol, calls health, and performs an
    authenticated project read through the saved profile.

## After setup

Captures stage for human review — nothing commits to the research
graph automatically. Server-side AI drafting uses the operator's
configured provider credential; no local OpenAI key is needed for Lab
Tracker. `lt doctor` and `lt setup status` surface drift after package
upgrades, and `lt update` is the refresh path.
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

<!-- lab-tracker-setup-guide version=0.1.0 sha256=1a17fae93102 -->
