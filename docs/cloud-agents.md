# Use Lab Tracker from a cloud agent session

A coding agent does not always run on your computer. Claude Code on the web
([claude.ai/code](https://claude.ai/code)), cloud sessions started from the
Claude desktop or mobile app or with `claude --cloud`, cloud scheduled tasks,
and other vendors' hosted agents run in a **cloud session**: a fresh virtual
machine (VM) with your repository cloned into it. Lab Tracker works there, but
the setup in [agent setup](agent-setup.md) assumes the agent runs where you
installed `lt` and saved your connection profile, and none of that is in the VM.

This page is for the person who sets up the cloud environment. It covers Claude
Code cloud sessions in detail; the [last section](#other-hosted-agents) gives
the same checklist for other hosted agents. Anthropic's
[cloud environment documentation](https://code.claude.com/docs/en/cloud-environments)
is the reference for every environment setting named here, and its screens
change faster than this page, so follow it where the two differ.

**The rules do not change in the cloud.** An agent still proposes and a person
still commits, tokens are still minted by a person in the web app, and a token
is never pasted into an agent's chat.

## What is different in a cloud session

| | On your computer | In a cloud session |
| --- | --- | --- |
| `lt` and `lt-mcp` | `uv tool install` once | Installed by the environment's **setup script**, because each session starts from a fresh VM |
| Server address | Loopback, LAN, or tailnet addresses work | Only a **public HTTPS address** works: the VM's loopback is the VM itself, and the VM is on no lab network or tailnet |
| Token | Saved profile (`lt setup connect --save-token`) | A **network secret**, added outside the VM, or an environment variable |
| Repository files (`.mcp.json`, `.claude/settings.json`, `lt_ids.json`, the managed `CLAUDE.md` block) | Present | Present, because they are committed |
| Skills in `~/.claude/skills` | Installed once | Not carried over; installed by the setup script |
| Saved profile, watch folders, commit hooks, OS schedules, autotrack startup files, personal agent hooks | Persist | Made in the VM, they are lost with it |
| Offline captures | Wait in the checkout's outbox and sync later | Lost with the VM unless synced before the session ends |

`lt setup status` recognizes a Claude Code cloud session from the
`CLAUDE_CODE_REMOTE=true` variable that Anthropic sets in every cloud session
VM. There it reports a `cloud_session` block and replaces the suggestions that
only make sense on a computer (start a local server, save a profile, add watch
folders, install commit hooks or personal agent hooks) with the cloud
environment settings below. Its `--brief` line, which the scaffolded
`SessionStart` hook hands the agent, starts with `lab-tracker (cloud session)`.

## Before you start

- **A server the internet can reach.** The lab's Lab Tracker must answer at a
  public HTTPS address with authentication on: a
  [Render deployment](one-click-cloud-deploy.md), an instance published through
  [Tailscale Funnel](self-hosted-operations.md#publishing-through-tailscale-funnel),
  or one behind your institution's reverse proxy. A tailnet-only address, a LAN
  address, or `127.0.0.1` cannot be reached from a cloud VM. Publishing an
  instance is its operator's decision; if the lab's server is private, use
  [Remote Control](#or-keep-the-agent-on-your-computer) instead.
- **The server-pinned install requirement.** The web app's **Setup** page shows
  the `uv tool install --force "lab-tracker @ git+…@<revision>"` command pinned
  to the server's full revision (see
  [agent setup](agent-setup.md#3-mint-credentials-for-automations)).
- **A token for the cloud environment.** On the **Agents** page, mint a token
  used only by this environment, labeled so you recognize it (for example
  `claude-cloud-<repo>`), at the lowest level the work needs: **Read-only** for
  decision context, **Read + stage evidence** to stage captures and request
  drafts. Its label is recorded on every write it makes, and a separate token
  can be revoked without touching your own.

## Configure a Claude Code cloud environment

Open the environment's settings: the cloud environment menu in the session's
title bar, then **Edit** (or **New environment**). Each step below is one part
of that dialog. An organization-shared environment is edited by an Owner from
the **Cloud environments** page in claude.ai admin settings.

### 1. Setup script: install the client and skills

The setup script runs as root before Claude Code starts, and Anthropic caches
the resulting filesystem for later sessions, so the install does not repeat at
every session start. `uv` is pre-installed.

```bash
#!/bin/bash
set -e
uv tool install --force "lab-tracker @ git+https://github.com/SamuelBrudner/lab-tracker.git@<full-server-revision>"
"$(uv tool dir --bin)/lt" update --skills-only || true
```

Paste the exact command from the Setup page in place of the `uv tool install`
line. The skills step runs `lt update --skills-only` by the full path of `lt`,
because the script's `PATH` may not include the directory `uv tool` installs
into yet, and it is kept non-fatal because a setup script that exits non-zero
stops the session from starting. The cache is rebuilt when you
change the script, so after the server is upgraded, replace the revision here;
`lt setup status` in a cloud session says so when the client is behind its
server. If the repository's own code imports `lab_tracker_client`, its pinned
`uv add` dependency installs with the project's other dependencies, as on your
computer.

### 2. Environment variables: the server address

Environment variables use `.env` format, one `KEY=value` per line:

```text
LAB_TRACKER_BASE_URL=https://lab.example.org
```

Use the origin only, without `/app`. Add `LAB_TRACKER_PROJECT_ID=<project-uuid>`
only if the repository has no committed `lt_ids.json` binding. Do not put the
token here if you can use a network secret (step 4): anyone who uses the
environment can read its variables.

### 3. Network access: allow the server's host

The default **Trusted** level allows package registries and GitHub, which the
setup script needs, but not your server. Either add the server's host (for
example `lab.example.org`) under **Allowed domains**, keeping the default
package-manager list included, or choose a level that allows every domain.
Anthropic's [network access](https://code.claude.com/docs/en/cloud-environments#network-access)
section names the current levels. A host listed on a network secret (step 4)
is reachable whatever the level, so with a network secret this step can be
skipped.

### 4. The token: a network secret

A network secret keeps the token out of the VM: Anthropic's agent proxy adds it
to requests for the hosts you list after they leave the VM, so neither the agent
nor anything it runs can read it. `lt` and `lt-mcp` send no `Authorization`
header when they hold no token, and the Lab Tracker API reads the bearer token
the proxy adds.

In the environment's **Network secrets** section (labeled **API credentials**
in an app that has not updated), select **Add secret** and fill in:

- **Secret type:** Bearer (the default).
- **Name:** a label, such as `Lab Tracker`.
- **Allowed websites:** the server's host, such as `lab.example.org`.
- **Custom headers:** `Authorization` with the prefix `Bearer`, and the
  `lpat_…` token as the value.

Leave `LAB_TRACKER_ACCESS_TOKEN` and `LAB_TRACKER_MCP_API_KEY` unset. Requests
from the setup script never get the secret, which is fine because installing
the client needs no Lab Tracker token. Anthropic's
[network secrets](https://code.claude.com/docs/en/cloud-environments#add-network-secrets)
section lists who can add one: some organization roles, self-hosted
environments, and organizations using customer-managed encryption keys cannot.

**If you cannot add a network secret,** put the token in the environment
variables instead, under both names the two commands read:

```text
LAB_TRACKER_ACCESS_TOKEN=lpat_...
LAB_TRACKER_MCP_API_KEY=lpat_...
```

`lt` reads the first and `lt-mcp` the second. Every person who uses the
environment can read them, so do this only in an environment of your own, with a
token minted for it alone.

### 5. Commit the repository files

A cloud session reads the repository's own `.mcp.json` and `.claude/settings.json`
in a session with one repository, so the files `lt setup init` writes
([agent setup](agent-setup.md#4-connect-coding-agents-over-mcp)) carry over
once they are committed. Check one thing in `.mcp.json`: its `lab-tracker`
entry starts `lt-mcp` with its own `LAB_TRACKER_BASE_URL`, and that value is
used rather than the environment's. If it names `127.0.0.1` or another private
address, run `lt update --dry-run` with `LAB_TRACKER_BASE_URL` set to the public
address (on your computer, or in the cloud session itself), review the rewrite,
then apply it with `lt update` and commit the result. Commit `lt_ids.json` too,
so the project binding reaches every session.

## Verify from a cloud session

Start a new session in the environment and ask the agent to run these:

1. `lt setup status` should show `cloud_session.detected: true`, a reachable
   server, and no suggestions about the address. Its `token_in_vm` shows
   `false` for both commands when a network secret supplies the token; that is
   expected, because the secret never enters the VM.
2. `lt setup verify-mcp --expected-revision <full-revision>` starts `lt-mcp`,
   calls health, and makes an authenticated project read, which is what proves
   the network secret or token works.
3. Ask the agent to call `lab_tracker_list_projects` with `limit` 1. It should
   return a project, or an empty list when the token's user belongs to none,
   rather than an authentication error.

## While a cloud session works

- **Sync before the session ends.** Commit-hook, figure, and watch captures that
  could not reach the server wait in the checkout's outbox, and the outbox lives
  in the VM. Before a session that captured anything ends, the agent runs
  `lt outbox sync`, then `lt outbox status`, and reports anything still pending.
- **Capture that suits a cloud session:** the MCP tools, `lt run`, and
  `lt capture file` send what they capture while the session runs. Commits
  pushed from cloud sessions are recorded in CI by the reusable
  [`lab-tracker-repo-report` GitHub Action](pipeline-capture.md#ci-capture-github-action),
  which needs nothing in the VM.
- **Capture that does not:** watch folders, OS schedules (`lt setup schedule`),
  commit hooks in `.git/hooks`, and the personal agent hooks in
  `.claude/settings.local.json` are made per machine and lost with the VM.
  [Agent session capture](agent-session-capture.md) reaches cloud sessions only
  if the team commits its hooks to `.claude/settings.json` with `--shared`, a
  team decision because it captures everyone's sessions.
- **No local server.** `lab-tracker serve` in the VM would keep the lab's
  captures in a database that is discarded with the VM. When the server cannot
  be reached, the agent says which setting is missing and works without graph
  context.

The managed `CLAUDE.md`, `AGENTS.md`, and `GEMINI.md` block, `AGENTS.lt.md`, and
the MCP server's instructions carry the same rules for the agent, so it follows
them even in a session where `lt` is not installed yet.

## Or keep the agent on your computer

If the lab's server is private, you can drive a session that runs on your own
computer from the Claude app instead of a cloud VM: start `claude remote-control`
in a terminal in the repository, or use the Claude desktop app. That session
uses your installed `lt`, your saved profile, and your network, so the
[agent setup](agent-setup.md) steps apply unchanged.

## Other hosted agents

Any hosted coding agent needs the same four things, under its own names:

1. its environment setup step installs the server-pinned client
   (`uv tool install --force "<server-pinned requirement>"`);
2. `LAB_TRACKER_BASE_URL` holds the server's public HTTPS address, and the
   agent's network policy allows that host;
3. the token comes from the platform's secret store, never from the repository
   or the chat: `LAB_TRACKER_ACCESS_TOKEN` for `lt` and `LAB_TRACKER_MCP_API_KEY`
   for `lt-mcp`;
4. captures are synced (`lt outbox sync`) before the platform discards the
   machine.

Set `LAB_TRACKER_CLOUD_SESSION=1` in that environment so `lt setup status`
gives the cloud-session report there too. On a machine that sets
`CLAUDE_CODE_REMOTE=true` but can reach the lab's network, such as a self-hosted
runner inside it, `LAB_TRACKER_CLOUD_SESSION=0` keeps the ordinary report.
