---
name: lab-tracker-setup
description: Guide a user through setting up Lab Tracker capture in a consumer repo or on a new machine. Use when the user asks to set up Lab Tracker, connect a repo, switch the Lab Tracker server URL, configure watch folders, enroll commit hooks, bind a project, or when `lt setup status` / a session hook reports unconfigured or drifted capture. Covers the consent-gated `lt` setup verbs and their choreography.
allowed-tools: "Read,Bash(lt setup status:*),Bash(lt doctor:*)"
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

- `lt setup status` is read-only and safe to consult at any time.
- Every write command below takes a `--dry-run` preview; previews are
  safe to show.
- A person approves each applying command. `lt setup connect`,
  `lt setup switch-server`, `lt project bind`, and `lt hooks install`
  additionally require an explicit `--yes`.
- One command per approval; the diff or preview is shown first.
- Access tokens are minted by a person in the Lab Tracker web app and
  are never relayed through an agent.

## The staged sequence

1. **Inventory** — `lt setup status` reports server reachability, the
   connection profile, repo scaffolding, project binding, watch
   folders, and commit-hook enrollment in one JSON payload, with
   suggestions for whatever is missing.
2. **Connectivity** — when no server is reachable, `lab-tracker serve`
   starts a local instance; a lab usually shares one instance and its
   URL comes from whoever operates it.
3. **Connection profile** — `lt setup connect --base-url <url> --yes`
   persists the server URL (and optionally a default project) in
   `~/.lab-tracker/config.json` so hooks and schedulers work without
   per-shell environment variables. Token storage is a separate
   consent (`--save-token`).
4. **Server moves** — when the graph moves to another workstation or
   hosted URL, `lt setup switch-server --base-url <url> --target <repo>`
   updates the profile, repo MCP config, and any existing managed git
   hook in one previewable step. Stored tokens are not carried to the
   new URL unless the user explicitly passes `--save-token` or
   `--keep-token`.
5. **Repo scaffolding** — `lt setup init` writes the integration files
   (MCP config, prompt hooks, `lt_ids.json`); `lt update` refreshes
   them after a package upgrade.
6. **Project binding** — `lt project bind --name <project> --yes`
   resolves or creates the project and records its id in
   `lt_ids.json` (`--create` when it does not exist yet).
7. **Watch folders** — `lt watch add <folder>` registers a results
   folder; `lt watch scan` and `lt watch sync` capture and upload on
   demand or from a scheduler.
8. **Commit hooks** — `lt hooks install --yes` enrolls the current
   repository: each commit queues durable evidence that syncs when
   the server is reachable. Repos are enrolled one consented command
   at a time.

## After setup

Captures stage for human review — nothing commits to the research
graph automatically. `lt doctor` and `lt setup status` surface drift
after package upgrades, and `lt update` is the refresh path.
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

<!-- lab-tracker-setup-guide version=0.1.0 sha256=a78f2ebb6525 -->
