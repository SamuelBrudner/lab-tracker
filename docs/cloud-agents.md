# Use Lab Tracker from a cloud agent session

A coding agent does not always run on your computer. Claude Code on the web
([claude.ai/code](https://claude.ai/code)), cloud sessions started from the
Claude desktop or mobile app or with `claude --cloud`, cloud scheduled tasks,
Codex cloud tasks, and other vendors' hosted agents run in a **cloud session**:
a fresh virtual machine (VM) with your repository cloned into it. Lab Tracker works there, but
the setup in [agent setup](agent-setup.md) assumes the agent runs where you
installed `lt` and saved your connection profile, and none of that is in the VM.

This page is for the person who sets up the cloud environment. It covers
[Claude Code cloud sessions](#configure-a-claude-code-cloud-environment) and
[Codex cloud tasks](#configure-a-codex-cloud-environment) in detail; the
[last section](#other-hosted-agents) gives the same checklist for other hosted
agents. Anthropic's
[cloud environment documentation](https://code.claude.com/docs/en/cloud-environments)
and OpenAI's
[cloud environments guide](https://learn.chatgpt.com/docs/environments/cloud-environments)
are the references for every environment setting named here, and their screens
change faster than this page, so follow them where they differ.

**The rules do not change in the cloud.** An agent still proposes and a person
still commits, tokens are still minted by a person in the web app, and a token
is never pasted into an agent's chat.

## What is different in a cloud session

| | On your computer | In a cloud session |
| --- | --- | --- |
| `lt` and `lt-mcp` | `uv tool install` once | Installed by the environment's **setup script**, because each session starts from a fresh VM |
| Server address | Loopback, LAN, or tailnet addresses work | An address the VM can reach, normally a **public HTTPS address**: the VM's loopback is the VM itself, and it joins a lab network or tailnet only through a VPN its environment provides (a Codex environment's Tailscale setting; Anthropic-hosted Claude Code environments have none) |
| Token | Saved profile (`lt setup connect --save-token`) | A **network secret**, added outside the VM, or an environment variable |
| Repository files (`.mcp.json`, `.claude/settings.json`, `lt_ids.json`, the managed `CLAUDE.md` block) | Present | Present, because they are committed |
| Skills in `~/.claude/skills` | Installed once | Not carried over; installed by the setup script |
| Saved profile, watch folders, commit hooks, OS schedules, autotrack startup files, personal agent hooks | Persist | Made in the VM, they are lost with it |
| Offline captures | Wait in the checkout's outbox and sync later | Lost with the VM unless synced before the session ends |

`lt setup status` recognizes a Claude Code cloud session from the
`CLAUDE_CODE_REMOTE=true` variable that Anthropic sets in every cloud session
VM. OpenAI documents no such variable for Codex cloud tasks, so a Codex
environment declares itself with `LAB_TRACKER_CLOUD_SESSION=codex`
([below](#2-environment-variables-the-address-and-the-cloud-marker)). In a cloud
session the report's `cloud_session` block and replaces the suggestions that
only make sense on a computer (start a local server, save a profile, add watch
folders, install commit hooks or personal agent hooks) with the cloud
environment settings below. Its `--brief` line, which the scaffolded
`SessionStart` hook hands the agent, starts with `lab-tracker (cloud session)`.

## Before you start

- **A server the internet can reach.** The lab's Lab Tracker must answer at a
  public HTTPS address with authentication on: a
  [Render deployment](one-click-cloud-deploy.md), an instance published through
  [Tailscale Funnel](self-hosted-operations.md#publishing-through-tailscale-funnel),
  or one behind your institution's reverse proxy. A Claude Code cloud VM cannot
  reach a tailnet-only address, a LAN address, or `127.0.0.1`; a Codex cloud
  task reaches a tailnet only through its environment's
  [Tailscale VPN](#5-a-private-server-tailscale). A Codex network secret is
  substituted only for HTTPS on port 443, so with one, serve the API on 443.
  Publishing an instance is its operator's decision; if the lab's server is
  private, use [Remote Control](#or-keep-the-agent-on-your-computer) instead.
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

Open the environment's settings. To create one, select **New environment** at
the bottom of the environment selector under the message box on the
new-session screen; to change the environment of a running session, select the
caret after the session title, then **Edit cloud environment**. Each step below
is one part of that dialog. An organization-shared environment is edited by an
Owner from the **Cloud environments** page in claude.ai admin settings.

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

On a Team plan without an organization admin role, **Add secret** opens a
shorter form instead: enter the server's host under **Host**, choose **Token**,
and paste the `lpat_…` token as the **Value** (Anthropic's
[shorter form](https://code.claude.com/docs/en/cloud-environments#add-a-secret-on-a-team-plan-without-an-organization-admin-role)).
The verification below confirms the server accepts what the proxy sends.

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

The committed address is also what `lt-mcp` uses on every computer that opens
the repository, and `lt-mcp` sends a saved profile's token only to that
profile's own address. Anyone whose profile names another address for the same
server, such as `127.0.0.1` on the server host, then runs
`lt setup connect --base-url <public address> --save-token --yes` once
([agent setup](agent-setup.md#choose-your-client) explains the rule). Later,
`lt update` on any computer rewrites `.mcp.json` from the address it resolves
there, so check that file's change before committing an update.

## Verify from a cloud session

For Claude Code, start a new session in the environment and ask the agent to
run these (Codex has [its own checks](#verify-a-codex-cloud-task)):

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

## Configure a Codex cloud environment

Codex runs cloud tasks from ChatGPT on the web, the desktop and mobile apps,
`codex cloud`, and the IDE extension. OpenAI currently runs two cloud systems
with different settings, so check which one your tasks use:

- **Codex Cloud**, with published, reusable
  [cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments).
  Tasks from ChatGPT on the web, desktop, and mobile, and Slack and Teams
  delegation, use these.
- **[Codex Cloud (Legacy)](https://learn.chatgpt.com/docs/environments/cloud-environment)**,
  which still runs `@codex` requests on GitHub pull requests (other than
  reviews) and Linear tasks, and which OpenAI plans to retire.

Nothing on your computer reaches a Codex cloud task: not your `lt`
installation, saved profile, VPN, or personal skills. OpenAI documents no MCP
server support for Codex cloud tasks, so the agent there has no
`lab_tracker_get_decision_context`. It reads the repository's `AGENTS.md`,
where `lt setup init` writes the managed Lab Tracker block, and it reaches
Lab Tracker through `lt` commands: `lt prime` lists the project's open
questions, and captures (`lt run`, `lt capture file`, commit hooks) work as on
a computer.

### 1. Install script: install the client and skills

In Codex Cloud, an environment records an **Install script** that prepares the
VM, and **Publish** snapshots the prepared filesystem that every new task
starts from; a change reaches new tasks only after you republish. In Codex
Cloud (Legacy) the same commands go in the **Setup script**, whose result is
cached for up to 12 hours. Either way, install the server-pinned client and
the skills:

```bash
uv tool install --force "lab-tracker @ git+https://github.com/SamuelBrudner/lab-tracker.git@<full-server-revision>"
"$(uv tool dir --bin)/lt" update --skills-only || true
```

The install needs `github.com`, which the **Package managers** preset (Legacy:
**Common dependencies**) allows. Skills can also be committed to the
repository's `.agents/skills`, which cloud tasks read directly.

### 2. Environment variables: the address and the cloud marker

```text
LAB_TRACKER_BASE_URL=https://lab.example.org
LAB_TRACKER_CLOUD_SESSION=codex
```

`LAB_TRACKER_CLOUD_SESSION=codex` is what lets `lt setup status` give its
cloud report in a Codex task, worded for Codex. Environment variables reach the
agent and every program in the task, in both systems.

### 3. Internet access: allow the server's host

A Codex agent has no internet access until the environment turns it on. In
Codex Cloud, turn on internet access, choose **Package managers** or **Custom
domains only** under **Allow domains**, and add the server's exact host (for
example `lab.example.org`) under **Additional allowed domains**: subdomains are
not covered by their parent. Saving a network secret for the host (step 4) also
allows it. In Codex Cloud (Legacy), set agent internet access to **On** and add
the host to the allowlist, and leave the allowed HTTP methods unrestricted:
limiting them to `GET`, `HEAD`, and `OPTIONS` blocks every Lab Tracker capture
and draft request.

### 4. The token

**Codex Cloud: a network secret.** Add one network secret per name the two
commands read, each with the `lpat_…` token as its value and the server's host
under **Allowed domains**:

- **Key** `LAB_TRACKER_ACCESS_TOKEN` (read by `lt`);
- **Key** `LAB_TRACKER_MCP_API_KEY` (read by `lt-mcp`, if anything starts it).

Programs see a placeholder under that name, and the proxy puts the real token
in its place on requests to the allowed host. That works only for HTTPS on port
443, so a server on another port needs a direct environment variable instead.
A shared environment can ask each person for their own value through their
**Personal vault**, so no one's token is shared.

**Codex Cloud (Legacy): an environment variable.** Legacy secrets reach only
the setup script and are removed before the agent runs, so the token must be an
environment variable under both names, which the agent and everyone who uses
the environment can read. Mint a token for this environment alone.

### 5. A private server: Tailscale

A Codex Cloud environment can join a tailnet through its VPN setting, with
Tailscale as the supported provider (create the auth key as **Reusable** and
**Ephemeral**). Allow the server in both the tailnet's access rules and the
environment's internet access, and use its fully qualified name, which must
resolve to an IPv4 address. Codex Cloud (Legacy) has no VPN setting.

### Verify a Codex cloud task

Start a new task (after publishing, in Codex Cloud) and ask the agent to run:

1. `lt setup status`, which should show `cloud_session.host` as `codex`, a
   reachable server, and no suggestion about the address;
2. `lt readiness`, an authenticated read that proves the network secret or
   token works;
3. `lt prime`, which should list the bound project's open questions.

## While a cloud session works

- **Sync before the session ends.** Commit-hook, figure, and watch captures that
  could not reach the server wait in the checkout's outbox, and the outbox lives
  in the VM. Before a session that captured anything ends, the agent runs
  `lt outbox sync`, then `lt outbox status`, and reports anything still pending.
- **Capture that suits a cloud session:** the MCP tools, `lt run`, and
  `lt capture file` send what they capture while the session runs. The reusable
  [`lab-tracker-repo-report` GitHub Action](pipeline-capture.md#ci-capture-github-action)
  records, in CI, the head commit of each push or pull request its workflow
  triggers on, pushes from cloud sessions included, and needs nothing in the VM.
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
gives the cloud-session report there too (`codex` for a Codex environment). On a machine that sets
`CLAUDE_CODE_REMOTE=true` but can reach the lab's network, such as a self-hosted
runner inside it, `LAB_TRACKER_CLOUD_SESSION=0` keeps the ordinary report.
