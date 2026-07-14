# External-Harness Sandbox Launcher — Distribution Design

> **Status: DESIGN, not shipped.** This document specifies how Lab Tracker, *as a distributable
> package*, defines and ships the isolation contract for external-harness graph-draft drafting.
> `graph_draft_provider=external_harness` **remains default-OFF**, and the fail-closed gate in
> `_ensure_harness_sandbox_profiles` (`graph_drafting.py:1456`) **stays**. Nothing here changes the
> enablement posture: it adds one enabling code change, ships reference launchers, and defines a
> testable contract. Enabling still requires all of `..._ENABLED=true`,
> `..._SANDBOX_PROFILE=operator_managed`, `..._EGRESS_PROFILE=vendor_api_only`, and a non-empty
> `..._COMMAND` wrapper.

---

## 0. Security review — required corrections (2026-07-09, governs this document)

An adversarial security review of the synthesized design below found issues that **must be resolved before implementation**. Where §3–§6 conflict with the items here, **these corrections win**. Verdict: *architecture sound and the linchpin change correct in principle, but not safe to implement exactly as the body is written.*

### Critical
1. **The bearer token is not a substitute for a narrow bind.** The token reaches the child inside the `--mcp-config` JSON that `_build_harness_launch_argv` appends, so it lands in the host worker's child **argv** — world-readable via `/proc/<pid>/cmdline` / `ps` on a multi-user host — and the MCP channel is **cleartext HTTP**, so the token *and* all project note content cross the wire in the clear. Therefore: bind only the **narrowest point-to-point interface** the one sandbox can reach (a specific bridge/host-only IP), **never `0.0.0.0`**; deliver the token **off argv** (an `--mcp-config` *file* with `0600` perms, or stdin); prefer a **unix-socket / vsock relay** to a TCP bind where the platform allows; and document that a shared L2 segment or a multi-user host **voids** the isolation. *(Supersedes §3.5's "all still need the token" and the Docker `0.0.0.0` default.)*
2. **WSL2 *mirrored* networking is NOT the strong-isolation path.** Mirrored mode shares the host loopback, so a `127.0.0.1` MCP bind lets the child reach **every** host localhost service (the Lab Tracker API, a local DB, other dev servers), below the in-distro nftables the design relies on. Use **WSL2 NAT mode with an explicit egress allowlist**, or require the LT API/DB to be non-loopback / host-firewalled when mirrored mode is used. *(Supersedes §4.3.)*

### High
3. **DNS/UDP exfiltration must be dropped explicitly.** The nft/netns ruleset must **deny all egress except the proxy TCP endpoint and the MCP endpoint, including UDP/53 and UDP/123**; do not bind a working resolver into the jail — force name resolution through the CONNECT proxy (which resolves on the child's behalf). *(Applies to §4.1 bwrap/firejail; the Docker `--internal` design blocks this by construction and should be the contrast.)*
4. **`HTTPS_PROXY` is a voluntary hint, not enforcement.** The load-bearing control is **nft deny-by-default**; the proxy is only a hostname/SNI filter reachable *through* that deny-all (the proxy `IP:port` must be the sole permitted external TCP destination). The launcher must **fail closed** if the nft table is absent. *(Applies to §4.1.)*
5. **The verification probe is necessary-but-not-sufficient.** It must attempt egress to **multiple** destinations (a random public `IP:443`, UDP/53 to a canary resolver, ICMP) **and from a spawned child process** (real `claude` forks `node` + helpers), treating any unexpected reachability as FAIL — and be labeled, in the doc and in `lt doctor`, as a point-in-time spot-check that does **not** prove ongoing isolation. *(Applies to §6.)*

### Medium
6. Attestation must digest the **sidecar config, effective nft ruleset, and proxy reachability** — not just the `..._COMMAND` hash — and, under `REQUIRE_VERIFICATION`, **re-run the probe immediately before each spawn** rather than trusting a prior file.
7. On timeout, the runner must `docker kill` / `docker rm -f` **by run label**, not rely on killing the `docker` client (`_kill_process_tree` kills the client, not the `--rm` container, which keeps its vendor-API egress after the app considers the run over).
8. Make the specific **docker-bridge gateway IP the documented default**; present `0.0.0.0` only behind `..._MCP_ALLOW_PUBLIC_BIND` with a loud warning, and have config validation **warn** on any public/wildcard bind.

### Verified-true code facts from the review (safe to rely on)
The §6 stale-memo corrections are accurate: the loopback MCP **is** served to the child (`graph_drafting.py:1141`), and `--allowedTools`/`--disallowedTools` **are** appended for `claude_code` (`:1612-1636`). The runtime-trace bug is real (`:1558-1559` hardcode `static_prescoped_context` / `live_scoped_reads=False` with a now-false comment). Dead `_external_harness_prompt_payload` with `transport:stdio` exists (`:1517-1535`). **Open question to resolve during build:** confirm `TransportSecuritySettings.allowed_hosts` actually supports the `host:*` port-wildcard the ephemeral-port path depends on.

---

## 1. Problem & framing

Lab Tracker can draft the daily review by spawning an operator-approved vendor CLI (Claude Code /
Codex / Gemini) as a sandboxed subprocess. The child reads Lab Tracker data and submits its proposed
graph patch **only** through a per-run loopback MCP server hosted in-process by the worker, behind a
single-use bearer token (`HarnessMCPLoopbackServer` + `_BearerTokenASGI`,
`services/graph_draft_harness_mcp.py:331-406`). The child receives only the MCP URL + token — never a
DB DSN or a Lab Tracker credential. This path was live-validated end-to-end against the real Claude
CLI on 2026-07-09.

**Lab Tracker is not a sandboxing product; it is a well-behaved *consumer* of an operator-provided
isolated launcher.** The existing seam is exactly right: `LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_COMMAND`
is shlex-split by `_split_harness_command` into the launch base argv, and `_build_harness_launch_argv`
(`graph_drafting.py:1597`) **appends** the vendor flags after it — so the wrapper is `exec`-style and
must pass every appended argument through unchanged. The fail-closed gate refuses to launch a bare
binary while attesting `operator_managed`, but it **cannot verify** the wrapper actually isolates
(a wrapper that merely `exec`s the binary passes). The package's job is therefore threefold:
**(a) define the isolation contract precisely, (b) ship reference launchers operators can use/adapt,
and (c) make the one enabling code change** so network-isolated launchers can function.

**The central technical wrinkle — loopback-MCP reachability.** `HarnessMCPLoopbackServer` hardcodes
the uvicorn bind interface `host="127.0.0.1"` (`graph_draft_harness_mcp.py:375`) *and* advertises
`url = f"http://127.0.0.1:{port}/mcp"` (`:363-364`). A genuinely network-isolated launcher
(container / WSL2 netns / VM) has its **own** loopback; the host's `127.0.0.1` is unreachable from
inside it. So the moment an operator applies real network isolation, the MCP path breaks and the
propose-only submit channel dies. Until the bind interface **and** the advertised URL are
configurable, the contract clause *"egress = vendor API host(s) + the MCP endpoint only"* is
**unsatisfiable** for a truly isolated launcher. This is the linchpin (§4).

---

## 2. The isolation contract (app-enforced vs operator-owned)

### 2.1 What the app enforces today (verified against source)

| Control | Where | Verified behavior |
| --- | --- | --- |
| Propose-only submit | `graph_draft_harness_mcp.py:103-116` | `submit_graph_patch` shape-checks, stores in-process, returns `{accepted, propose_only, operation_count}`; never persists/commits; raises on a second call. Commit-side gate (`require_interactive`, SYSTEM principal, lands `PROPOSED`) is upstream. |
| No LT secrets in child | `_sanitized_harness_env` (`graph_drafting.py:1483-1514`) | Env rebuilt from an allowlist; **raises** if any `LAB_TRACKER_*`/`DATABASE_URL` is present. |
| Scoped read surface | `HarnessGraphDraftMCPServer.tool_specs` (`:68-75`) | Exactly the 18 allowlisted reads + `submit_graph_patch`; `resolve_artifact` and all writes are structurally absent; `execute_tool` re-checks the allowlist and enforces `max_tool_calls` (`:85-88`). |
| Single-project + omit | `__post_init__` (`:51-52`), `configure_live_read_tools` (`graph_drafting.py:1270-1272`) | Executor bound to one `project_id` server-side (foreign IDs rejected); `sensitivity_policy="omit"` drops `raw_asset` and strips `source_file_*` keys. |
| Native-tool denial (claude) | `_build_harness_launch_argv` (`:1624-1636`) | Appends `--allowedTools mcp__lt__*` + `--disallowedTools Bash,Read,Write,Edit,WebFetch`. **Vendor-CLI-enforced, not OS-enforced.** codex/gemini fail closed (`:1637-1642`). |
| Per-run bearer MCP | `_BearerTokenASGI` (`:298-328`) | `secrets.token_urlsafe(32)`; constant-time `hmac.compare_digest`; ephemeral port; torn down on `__exit__`. |
| Bounded resources | `_run_bounded_harness_subprocess`, `_kill_process_tree` (`:1369-1453`) | On-disk stdio temp files, stdout cap, wall-clock timeout, whole-tree kill, per-tick spawn cap. |
| Fail-closed gate | `_ensure_harness_sandbox_profiles` (`:1456-1480`) | Refuses to launch unless both profiles are "on" **and** a wrapper command is provided. **Cannot verify the wrapper isolates.** |

**The single biggest operator-owned gap:** `_sanitized_harness_env` forwards the operator's real
home (`HOME, USERPROFILE, LOCALAPPDATA, APPDATA`, `:1491-1494`). Absent an OS sandbox the child can
read everything under the real home (SSH keys, other `.env`s, other projects). Neutralizing the
forwarded home is the wrapper's #1 job (or the app's — see §4.4).

### 2.2 Threat model

**Trust assumptions.** The vendor CLI is the vendor's official, signed binary logged into the
operator's own paid account — the same tool the operator already runs interactively. **Untrusted:**
(a) note/graph content (prompt-injectable data; drafting prompts already wrap everything in
`<untrusted_*>` tags) and (b) the model's runtime tool choices.

**In scope:** injected egress of note content to a non-vendor host; injected cross-project/cross-user
reads (contained structurally — scope is server-supplied, not a model parameter); injected use of
native Bash/Read/Write/WebFetch to exfiltrate/tamper; runaway resource use / process-tree leak.

**Out of scope (state plainly):** a deliberately malicious/backdoored signed vendor binary (it runs
as the operator daily with full privileges — the host is already compromised); the vendor API itself
as a data recipient. The disclosed data-flow the contract commits to: *note content (minus
`omit`-stripped sensitive bodies) may reach the configured vendor API under the operator's account,
and must reach nothing else.*

### 2.3 Paste-ready contract (MUST / MUST NOT)

> **External-Harness Isolation Contract.** Enabling `external_harness` is a deployment promise Lab
> Tracker cannot verify in code. Before setting the enable + profile variables, the operator MUST
> supply `..._COMMAND` as a launcher that satisfies **all** of the following.
>
> **The launcher MUST:**
> - Run the vendor CLI under a real OS isolation primitive (low-privilege account + host firewall,
>   Windows Sandbox, WSL2 network namespace, or a container/`firejail`/`bwrap`), not merely `exec`
>   the binary.
> - Deny the child read access to the repository, `.env`, `lab_tracker.db`, server key material, and
>   every other project's data, in a throwaway working directory.
> - Present a minimal home directory exposing only the vendor CLI's own credential/config directory
>   (the app forwards `HOME/USERPROFILE/APPDATA/LOCALAPPDATA`).
> - Restrict outbound network to exactly the vendor API host(s) in the vendor launch entry **plus**
>   the Lab Tracker MCP endpoint the app advertises for the run — and nothing else.
> - Pass through **unchanged** every argument the app appends after the wrapper (`-p`,
>   `--output-format json`, `--mcp-config`, `--allowedTools mcp__lt__*`, `--disallowedTools …`).
> - Preserve the vendor CLI's own authentication (the injected `*_API_KEY` env var and/or the
>   exposed vendor credential directory).
>
> **The launcher MUST NOT:**
> - Grant the child any Lab Tracker credential, DSN, or `LAB_TRACKER_*`/`DATABASE_URL` value.
> - Allow egress to any host other than the vendor API and the run's MCP endpoint.
> - Re-enable the vendor's native Bash/Read/Write/Edit/WebFetch tools, or strip the
>   `--disallowedTools`/`--allowedTools`/`--mcp-config` flags.
> - Grant filesystem write outside the throwaway cwd, or read access to the real home tree.
> - Persist or forward the child's stdout/stderr anywhere the app does not.
>
> **What the app guarantees in return:** propose-only submission, a scrubbed env with no server
> secrets, a scoped 18-tool read surface with writes/`resolve_artifact` structurally absent,
> single-project + `omit`-sensitivity enforcement server-side, a per-run bearer-gated loopback MCP,
> bounded budgets with process-tree kill, and a fail-closed gate that refuses to launch without the
> wrapper.

The trace's `sandbox.app_code_enforced: false` / `egress.app_code_enforced: false`
(`graph_drafting.py:1567,1572`) remain **correct** — the app still cannot verify the wrapper
isolates. Verification (§6) is the only way to gain any signal.

---

## 3. The enabling code change — configurable MCP bind + advertised URL (the linchpin)

This is the one change that lets a network-isolated launcher reach the loopback MCP. **Defaults
preserve today's behavior exactly** (bind `127.0.0.1`, advertised = bind host, ephemeral port).

### 3.1 The non-obvious blocker: FastMCP's DNS-rebinding guard 421s a non-loopback Host

Advertising a non-loopback URL is necessary **but not sufficient**. `build_fastmcp_server()`
constructs `FastMCP(...)` with FastMCP's default `host="127.0.0.1"`, which auto-enables
`TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:*",
"localhost:*", "[::1]:*"], …)`. `TransportSecurityMiddleware._validate_host` then rejects any request
whose `Host` header is not in that list with **HTTP 421 "Invalid Host header"** — *before* the bearer
guard. A child dialing `http://host.docker.internal:{port}/mcp` sends `Host: host.docker.internal:{port}`
and is 421'd. So the enabling change has **three coupled parts**: (1) bind a reachable interface,
(2) advertise a reachable host, **(3) widen the DNS-rebinding allow-list to include that host** while
keeping protection ON.

### 3.2 New Settings fields (`config.py`, beside lines 49-56; `env_prefix=LAB_TRACKER_`)

```python
graph_draft_external_harness_mcp_bind_host: str = "127.0.0.1"
graph_draft_external_harness_mcp_advertised_host: str = ""   # "" -> fall back to bind host
graph_draft_external_harness_mcp_port: int = 0               # 0 -> ephemeral (today's behavior)
graph_draft_external_harness_mcp_allow_public_bind: bool = False
```

Env: `..._MCP_BIND_HOST`, `..._MCP_ADVERTISED_HOST`, `..._MCP_PORT`, `..._MCP_ALLOW_PUBLIC_BIND`.
(Canonical spelling is **`advertised`**, not `advertise`, to avoid drift.) Bind vs advertised differ
per isolation tech:

| Launcher | `..._MCP_BIND_HOST` (worker binds) | `..._MCP_ADVERTISED_HOST` (child dials) |
| --- | --- | --- |
| default (today) | `127.0.0.1` | `""` → `127.0.0.1` |
| Docker (bridge, Scenario A) | bridge gateway IP (e.g. `172.30.0.1`) or `0.0.0.0` | `host.docker.internal` |
| WSL2 (mirrored) | `127.0.0.1` | `""` → `127.0.0.1` (loopback mirrored) |
| WSL2 (NAT) | `vEthernet (WSL)` host IP | that IP |
| VM (host-only net) | host-only adapter IP | that adapter IP |

Validation inside the existing `@model_validator(mode="after")` (`config.py:93`): strip/default bind
host; `0 <= port <= 65535`; and **fail closed** if `bind_host in {"0.0.0.0","::","*"}` unless
`allow_public_bind` is set (acknowledge the exposure or bind the specific bridge/host-only IP).

### 3.3 Touch points in `graph_draft_harness_mcp.py`

`HarnessMCPLoopbackServer.__init__` gains keyword-only `bind_host="127.0.0.1"`,
`advertised_host: str | None = None`, `port=0` (positional callers keep working). Then:

- **`url` property (`:362-364`):** `f"http://{_format_host_for_url(self._advertised_host or self._bind_host)}:{self.port}{HARNESS_MCP_HTTP_PATH}"` (bracket bare IPv6 literals).
- **`__enter__` (`:366-380`):** two edits — (a) when advertising beyond loopback, set
  `fastmcp.settings.transport_security` to a widened `TransportSecuritySettings`
  (`enable_dns_rebinding_protection=True`, `allowed_hosts`/`allowed_origins` = the localhost triple
  **plus** `f"{advertised_host}:*"` and `f"{bind_host}:*"`) **before** `streamable_http_app()`
  snapshots it; (b) pass `host=self._bind_host, port=self._configured_port` to `uvicorn.Config`.
  Port-wildcard (`host:*`) is required because the ephemeral port is unknown at snapshot time.

Two module helpers: `_is_loopback_host(host)` and `_format_host_for_url(host)`.

### 3.4 Threading (add fields to existing carriers — no new plumbing path)

`HarnessGraphDraftClient.__init__`/`from_settings` (`graph_drafting.py:1216-1267`) store the three
settings → `HarnessDraftRequest` (frozen dataclass, `:1073-1086`) gains `mcp_bind_host`,
`mcp_advertised_host`, `mcp_port`, populated in `draft_from_batch` (`:1316-1332`) →
`SubprocessHarnessDraftRunner.run` (`:1141`) changes its one construction line to
`HarnessMCPLoopbackServer(mcp_server, bind_host=request.mcp_bind_host,
advertised_host=(request.mcp_advertised_host or None), port=request.mcp_port)`.
**`_build_harness_launch_argv` needs no edit** — it already receives `mcp_url=loopback.url` (`:1144`),
so the advertised URL flows into `--mcp-config` automatically.

### 3.5 Security argument

The **bearer token is the authN, independent of the bind interface** — 256-bit, per-run, constant-time
compared, torn down at run end. Broadening the bind only widens who may *attempt* a connection; all
still need the token. Keep DNS-rebinding protection **ON** scoped to the advertised host (strictly
better than passing a non-loopback `host` to `FastMCP`, which silently disables Host validation).
Bind the **narrowest** interface the sandbox can reach; never `0.0.0.0` unless
`allow_public_bind=true`. Ephemeral port stays default (no squatting surface); a fixed port is
offered only because container `-p` publishing, firewall rules, and WSL port-forwarding need a known
port pre-wired — a hostile local pre-bind makes the worker fail closed at the 20s startup poll, not
silently. Residual, operator-owned: on a shared bridge/host-only segment, co-resident hosts can reach
the port (still need the token) — keep only the intended sandbox on that segment.

---

## 4. Reference launchers

Each honors one contract clause that makes it **verifiable**: the launcher must invoke the leaf model
binary via a substitutable token (`${LT_HARNESS_BIN:-claude}` or a `{vendor}` argv placeholder) so
`verify-sandbox` (§6) can swap in `lt-harness-probe` without changing any isolation rule.

**Env-scrub constraint (all launchers):** `_sanitized_harness_env` drops anything the operator
exports before startup, so **the wrapper cannot be configured via arbitrary environment variables** —
its config must live in script constants or a sidecar file (e.g. `/etc/lab-tracker/harness-sandbox.conf`).
Only `PATH`, `HOME`, and the vendor `*_API_KEY` are reliably present.

### 4.1 Linux — bubblewrap (recommended) / firejail

`bwrap` rootlessly enforces the filesystem/process half of the contract; it **cannot filter egress**
(its only net primitive is share-vs-unshare of the netns). Reference wrapper
`scripts/harness-sandbox-bwrap.sh`:

```bash
#!/usr/bin/env bash
# Lab Tracker external-harness Linux reference launcher (FS/process jail via bwrap).
# Egress is enforced by a companion mechanism (nftables + loopback CONNECT proxy).
# NOTE: Lab Tracker SCRUBS the child env — configure via constants / the sidecar file, NOT env.
set -euo pipefail
CONFIG_FILE="/etc/lab-tracker/harness-sandbox.conf"; [ -r "$CONFIG_FILE" ] && . "$CONFIG_FILE"
VENDOR_BIN="${VENDOR_BIN:-claude}"; NET_MODE="${NET_MODE:-pasta}"   # pasta=own netns (recommended) | share=host netns
EGRESS_PROXY="${EGRESS_PROXY:-http://127.0.0.1:38001}"; MCP_HOST="${MCP_HOST:-127.0.0.1}"
VENDOR_PATH="$(command -v "$VENDOR_BIN")" || { echo "vendor CLI not on PATH" >&2; exit 127; }
# Fail-closed: refuse to run if the chosen egress control is absent.
case "$NET_MODE" in
  share) nft list table inet lt_harness >/dev/null 2>&1 || { echo "egress allowlist missing" >&2; exit 3; };;
  pasta) command -v pasta >/dev/null 2>&1 || command -v slirp4netns >/dev/null 2>&1 || { echo "need pasta/slirp4netns" >&2; exit 3; };;
  *) echo "unknown NET_MODE=$NET_MODE" >&2; exit 2;; esac
BWRAP_FLAGS=( --die-with-parent --new-session --unshare-user --unshare-pid --unshare-ipc --unshare-uts --unshare-cgroup --cap-drop ALL
  --ro-bind /usr /usr --ro-bind-try /bin /bin --ro-bind-try /lib /lib --ro-bind-try /lib64 /lib64
  --ro-bind-try /etc/ssl /etc/ssl --ro-bind-try /etc/ca-certificates /etc/ca-certificates --ro-bind-try /etc/resolv.conf /etc/resolv.conf
  --proc /proc --dev /dev --tmpfs /tmp
  --tmpfs "$HOME"                                   # fresh home: repo/.env/DB in the real home are invisible
  --ro-bind "$VENDOR_PATH" "$VENDOR_PATH"
  --ro-bind-try "$HOME/.claude" "$HOME/.claude" --ro-bind-try "$HOME/.claude.json" "$HOME/.claude.json"
  --tmpfs /work --chdir /work
  --setenv HTTPS_PROXY "$EGRESS_PROXY" --setenv HTTP_PROXY "$EGRESS_PROXY" --setenv NO_PROXY "$MCP_HOST,127.0.0.1,localhost" )
if [ "$NET_MODE" = "share" ]; then exec bwrap "${BWRAP_FLAGS[@]}" -- "$VENDOR_PATH" "$@"       # host netns; MCP on 127.0.0.1 works
else exec pasta --config-net -- bwrap "${BWRAP_FLAGS[@]}" -- "$VENDOR_PATH" "$@"; fi           # own netns; advertise MCP at host-loopback gw
```

- **`"$@"` verbatim passthrough** satisfies the append contract; the fresh `--tmpfs "$HOME"` +
  RO-binding only `~/.claude*` back on top gives "cred dir yes, everything else no." `/mnt` is never
  bound, so on WSL the Windows filesystem is excluded by the same profile.
- **Process-tree reaping composes:** the runner starts the child with `start_new_session=True` and
  `os.killpg` on timeout; `bwrap --die-with-parent` (PID 1 of its netns) tears down the tree.
- **Egress (bwrap can't):** ship a **loopback CONNECT allowlisting proxy** as the sole gateway
  (`scripts/harness-egress-proxy.py`, ~40-line stdlib, or `tinyproxy` with `ConnectPort 443`) that
  permits only `api.anthropic.com:443` (hostname → CDN IP rotation is a non-issue), plus an
  `scripts/harness-egress-nft.sh` nftables allowlist (uid-match in `share` mode; netns/uplink filter
  in `pasta` mode) that DROPs everything except loopback→{proxy, MCP}. `NET_MODE=pasta` (empty guest
  loopback, so the host DB simply isn't there) is preferred over `share` (which shares the host
  loopback — pin the MCP port and restrict the loopback allow to `{proxy_port, mcp_port}` so the
  child can't poke the DB port).
- **OAuth caveat:** RO-binding `~/.claude` blocks token refresh — prefer API-key auth
  (`ANTHROPIC_API_KEY`), or bind a **writable throwaway copy** of the cred dir.
- **firejail alternative:** bundles `--net`/`--netfilter` (turnkey egress bwrap lacks) but is
  setuid-root (larger, historically CVE-prone surface); recommend only where already trusted, and
  still pair `--netfilter` with the loopback proxy for the CDN-rotation caveat.

### 4.2 Container — Docker / Podman (the gold standard)

Reproducible, cross-platform (Linux/macOS/Windows via Desktop's WSL2 backend), with a real
deny-by-default egress boundary. `docker run` alone has no hostname allowlist, so the design uses a
**dual-homed egress sidecar** as the vendor container's *only* neighbor:

```
 [ vendor CLI container ]                 [ egress sidecar ]              host / internet
   net: lt-harness-internal (--internal)  ── squid :3128 (CONNECT allow api.anthropic.com:443)
   NO direct internet          │           socat :8765 ─► host.docker.internal:8765 (MCP relay)
                               └── also on lt-harness-external ─► internet + host
```

- **`lt-harness-internal`** is `--internal` (no NAT); the vendor container attaches only here →
  zero direct egress. The **squid** conduit CONNECT-allowlists the vendor host; the **socat** conduit
  relays MCP directly (sidesteps HTTP-proxy uncertainty and SSE buffering — MCP keeps the bearer token
  and stays a direct TCP path via `NO_PROXY=host.docker.internal`). Net result: a precise two-entry
  egress allowlist enforced by topology.
- **Files:** `docker/harness/claude/{Dockerfile,entrypoint.sh}` (pinned `@anthropic-ai/claude-code`,
  non-root uid 10001, `DISABLE_AUTOUPDATER=1`), `docker/harness/egress/{Dockerfile,squid.conf,entrypoint.sh}`,
  `scripts/harness-net-{up,down}.{sh,ps1}` (one-time: create nets + start the long-lived sidecar),
  `scripts/harness-docker-run.{sh,ps1}` (**this is the `..._COMMAND`**), `scripts/harness-docker-reap.{sh,ps1}`.
- **Per-run wrapper (`..._COMMAND`):** `docker run --rm --network lt-harness-internal
  --add-host host.docker.internal:172.30.0.2 --read-only --tmpfs /work --tmpfs /home/harness/.claude
  --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 1g
  -e ANTHROPIC_API_KEY -e HTTPS_PROXY=http://172.30.0.2:3128 -e NO_PROXY=host.docker.internal
  lab-tracker-harness-claude:<ver> claude "$@"` — `"$@"` forwarded unchanged. The Windows twin is a
  `.ps1` (use `pwsh -NoProfile -File …` in `..._COMMAND`; a `.cmd` `%*` mangles the `--mcp-config`
  JSON quotes, whereas PowerShell `@args` and Python's `list2cmdline` preserve them).
- **Config (Scenario A, worker native on host):** `..._MCP_BIND_HOST=0.0.0.0` (or the bridge IP),
  `..._MCP_ADVERTISED_HOST=host.docker.internal`, `..._MCP_PORT=8765`. Creds default to
  `-e ANTHROPIC_API_KEY` (zero host mounts); OAuth adds one RO `-v "$HOME/.claude:/run/harness-cred:ro"`
  that the entrypoint copies into tmpfs.
- **Kill semantics (real tradeoff):** the runner's `_kill_process_tree` kills the `docker` *client*,
  not the container. Mitigations, all shipped: `timeout -s KILL` in the entrypoint, `--rm`, and a
  scheduled `harness-docker-reap.*` (`--filter label=lt.harness.run`). App timeout is primary;
  `LT_HARNESS_HARD_TIMEOUT` slightly larger.
- **Linux-Engine optimization:** drop the socat relay and bind the MCP to the internal bridge gateway
  (`--gateway`); host-INPUT delivery bypasses `--internal` isolation. On Docker Desktop the host is
  outside the VM, so the relay is required — the relay design is the single portable default.
- **Podman:** same design; `host.containers.internal` alias or `--add-host` to the relay IP; rootless
  Podman is a further win.

### 4.3 Windows — WSL2 (recommended) + Windows Sandbox (fallback)

Native in-host Windows isolation is weak for this workload, so **do not build a second isolation
implementation** — run the §4.1 Linux launcher inside WSL2:

- **WSL2 (recommended):** a **dedicated distro** `lab-tracker-harness` (`wsl --import`, separate from
  the operator's main distro) with `/etc/wsl.conf` `[automount] enabled=false` so `/mnt/c` doesn't
  exist, plus the bwrap profile (which never binds `/mnt`). `..._COMMAND =
  wsl.exe -d lab-tracker-harness -u ltharness -- /opt/lab-tracker/launch-claude-sandbox.sh` (the
  `-- claude "$@"` passthrough is load-bearing; `_split_harness_command` on Windows uses
  `posix=False`, so keep unquoted tokens). **MCP:** with **mirrored networking** (`.wslconfig`
  `[wsl2] networkingMode=mirrored`, available on build 26200) keep `..._MCP_BIND_HOST=127.0.0.1` and
  empty advertised host — **zero bind widening**; NAT fallback uses the `vEthernet (WSL)` host IP.
  Egress is real Linux nftables/proxy inside the distro. **Credentials best-in-class:** with a
  persistent logged-in distro the worker crosses **zero** secrets — the child gets only the MCP
  URL+token via the forwarded `--mcp-config` argv (env vars don't propagate into WSL). Ship
  `scripts/harness-launchers/provision-wsl-sandbox.ps1` (dedicated distro, automount off, unprivileged
  uid, isolation asserts incl. `test ! -e /mnt/c`, prints the exact env) and
  `/opt/lab-tracker/launch-claude-sandbox.sh`. **Op note:** WSL distros are per-Windows-user —
  provision under the same account Task Scheduler runs the daily review as. **Not isolated:** VM/kernel
  escape (one shared per-user VM), and a compromised child persists in the distro (mitigate: the
  distro does only this job).
- **Windows Sandbox (`.wsb`, fallback):** strongest disposability (fresh throwaway Hyper-V VM,
  nothing persists, map no sensitive folders) but **weak egress** (inside you're back on Windows —
  same node-shim/WFP limitation as §4.4), per-run provisioning latency (map a portable Node+CLI,
  `LogonCommand` each spawn), credential crosses every run, and needs Pro/Enterprise + the feature.
- **AppContainer / restricted-token (document as a trap, not recommended):** its one draw is
  preserving `127.0.0.1` MCP reachability with no bind change, but per-app firewall egress is
  defeated because the socket-opening process is `node.exe`, not `claude.exe`; WFP filters by IP not
  SNI (CDN rotation); FS isolation is deny-listing on a shared profile (one miss leaks); and there is
  no VM boundary. Ship this in docs as "why we don't do host-native isolation."

### 4.4 macOS

`bwrap`/`firejail`/nftables are Linux-only and `sandbox-exec`/Seatbelt has no clean egress allowlist
— **recommend the container launcher** (§4.2, VM-backed netns + published-port MCP). Same seam, same
§3 change, same proxy pattern.

### 4.5 Package hardening (candidate app change, reduces operator burden)

Point the child's `HOME`/`USERPROFILE` at an app-built **empty throwaway dir** and rely on env-var
vendor auth (`ANTHROPIC_API_KEY`) so the **app** denies real-home access without the wrapper having
to re-home the process — shrinking the wrapper's job to "provide the OS boundary." Requires
per-vendor confirmation that claude/codex/gemini run fully headless from `*_API_KEY` alone.

---

## 5. Verification

Verification closes the "a wrapper that just `exec`s the binary passes the gate" hole with an active
red-team probe + attestation + CI. It depends on §3 (the probe's MCP-reachability check cannot
exercise a network-isolated sandbox until bind/advertised are configurable).

- **`lt-harness-probe`** (new `[project.scripts]` console script, productionized from
  `tests/_fake_harness_cli.py`) **replaces the leaf vendor binary** so it runs in the *identical*
  namespace/mount/egress context. It impersonates the vendor argv surface (accept-and-ignore `-p`,
  `--output-format`, `--allowedTools`, `--disallowedTools`; parse `--mcp-config`), reads a
  verify-only manifest via **argv** (`--lt-probe <b64>`, never env — so `_sanitized_harness_env` stays
  byte-identical between verify and production), performs four attempts, and emits one
  `LT_PROBE_VERDICT {json}` line on stdout: **FS-DENY** (open forbidden paths + planted canary),
  **NET-DENY** (connect a non-allowlisted host), **NET-VENDOR** (TCP+TLS ClientHello to the vendor
  host — no HTTP, no key, zero spend), **MCP-ALLOW** (call `probe_ping(nonce)` through the boundary).
- **`lab-tracker graph-draft verify-sandbox`** (new server-CLI subcommand): plants a `.lt-verify-canary`,
  starts a **forbidden-endpoint observer** (ground truth for NET-DENY), stands up the **real**
  `HarnessMCPLoopbackServer` wrapping a `probe_ping` stub (ground truth for MCP-ALLOW), and launches
  the operator wrapper via a `run_harness_argv(...)` helper factored out of
  `SubprocessHarnessDraftRunner.run` so **verify == production path** (same env scrub, temp cwd,
  `creationflags`, bounds), substituting `lt-harness-probe` for the leaf via the `{vendor}` seam.
  **PASS requires ALL:** every FS path unreadable + canary absent from stdout; NET-DENY self-report
  false **and** observer logged zero connections; MCP-ALLOW reachable+nonce-echoed **and** loopback
  logged the ping. NET-VENDOR unreachable → WARN (drafting fails closed). **No parseable verdict /
  timeout / launch failure → verification FAILS** ("treating as NOT isolated"). No vendor API cost —
  the probe *is* the stub.
- **Attestation + doctor:** on PASS, write `~/.lab-tracker/harness-verification.json`
  (`{wrapper_command_sha256, vendor, mcp_advertised_host, probe_verdict_digest, package_version,
  host_fingerprint, verified_at}`). `lt doctor` (`cli.py:992`) gains a **cheap static** external_harness
  section (enabled? profiles? wrapper? advertised host non-loopback? does a passing attestation match
  the current command hash + package version?) — **informational only**, must not feed
  `_doctor_exit_code` (mirroring `installation_report`). Optional
  `LAB_TRACKER_..._REQUIRE_VERIFICATION=true` extends `_ensure_harness_sandbox_profiles` to require a
  fresh matching attestation before spawning (proves verification ran against this wrapper hash, not
  that isolation is currently intact).
- **CI strategy.** *Unit (always-on, cross-platform):* default construction → `url` starts
  `http://127.0.0.1:`; advertised host → url reflects it; a client sending `Host:
  host.docker.internal:{port}` (dialing `127.0.0.1:{port}`) is **accepted**; an unlisted `Host`
  (`evil.example`) is **421'd**; a regression proving the *default* loopback server 421s a
  non-loopback Host today; fixed port → `loopback.port==configured`; bearer guard still 401s on a
  `0.0.0.0` bind; config port-range + wildcard-bind guards; probe argv-compatibility. *Integration
  (Linux, gated `@pytest.mark.linux_sandbox` + `LT_RUN_SANDBOX_TESTS=1`, `bwrap`/`unshare` present):*
  **positive** — a shipped reference launcher PASSes verify-sandbox (observer saw zero connections,
  MCP logged the ping, canary not echoed); **negative (anti-theater)** — a passthrough wrapper
  `sh -c 'exec "$@"' --` **FAILS**, naming FS-DENY + NET-DENY. Without the negative test the verifier
  could rubber-stamp everything.
- **Go-live checklist.** (1) Install/adapt a reference launcher for your OS; set only the vendor API
  key inside it. (2) Point `..._COMMAND` at it (keep the `{vendor}`/`$LT_HARNESS_BIN` seam). (3) If
  network-isolated, set `..._MCP_ADVERTISED_HOST` (+ `..._MCP_BIND_HOST`/`..._MCP_PORT`) to the
  address the sandbox can reach. (4) `lab-tracker graph-draft verify-sandbox` → must print **PASS**;
  a FAIL means it doesn't isolate — stop. (5) Confirm `lt doctor` shows a matching passing
  attestation. (6) Set the two profiles, then `..._ENABLED=true` (optionally `..._REQUIRE_VERIFICATION`).
  (7) Re-run verify after any wrapper/host/vendor-CLI change. (8) Confirm the §6 doc corrections are
  merged so what you trust matches what the code does. Default stays **OFF**; the fail-closed gate
  stays.

---

## 6. Doc & code corrections (stale, must fix)

`docs/external-harness-security-review.md` is materially stale after the loopback-MCP wiring (commit
`5152b41`) and must be corrected before an operator relies on it:

- **Row at line 47** — *"the per-run MCP server is not served to the subprocess … no live scoped
  reads."* **Now FALSE.** `SubprocessHarnessDraftRunner.run` (`graph_drafting.py:1141`) hosts
  `HarnessMCPLoopbackServer` and the child performs **live scoped reads** over loopback
  streamable-HTTP behind the bearer token (`services/graph_draft_harness_mcp.py`;
  `tests/test_graph_draft_harness_mcp.py`; `tests/_fake_harness_cli.py`). Cross-project isolation is
  now enforced by the **live executor chokepoint**, not merely incidentally. Corrected text should
  say so and add a row for the new bind/advertised/port config + the retained (widened)
  DNS-rebinding guard.
- **Row at line 49** — *"there is no `--disallowedTools`."* **Now FALSE.** `_build_harness_launch_argv`
  passes `--allowedTools mcp__lt__*` + `--disallowedTools <native_tool_denies>` for `claude_code`
  (`graph_drafting.py:1624-1636`). Native-tool denial is now argv-enforced for Claude Code (still
  advisory for the unwired codex/gemini). Note it is **vendor-CLI-enforced, not OS-enforced** — the
  wrapper remains the real egress backstop.
- **The `sandbox`/`egress` `app_code_enforced: false` fields remain CORRECT** — the app still cannot
  verify the wrapper isolates. Keep that honesty.

**Runtime trace bug (fix alongside):** `_external_harness_trace` (`graph_drafting.py:1553-1559`)
hardcodes `"read_path": "static_prescoped_context"` and `"live_scoped_reads": False` with a comment
asserting the MCP surface is not served. For the wired claude_code loopback path this **mislabels the
persisted trace** and is internally inconsistent — the same run sets `subprocess.mcp_transport =
"loopback_streamable_http"` (`:1149`) and `tool_call_count` is > 0 from live loopback reads. Set
`read_path="loopback_scoped_mcp"` / `live_scoped_reads=True` when the loopback server is used, and
update the comment. (Confirm no downstream consumer/test asserts the current false values before
flipping.)

**Dead code:** `_external_harness_prompt_payload` (`graph_drafting.py:1517-1535`) advertises
`"transport": "stdio"` and has **no call site** (verified). Remove it so no stale "stdio" contract
survives.

**Design doc:** `docs/external-harness-drafting-design.md` still describes **stdio** transport and an
unwired chokepoint; update its banner and transport/tool-surface sections to describe the shipped
loopback streamable-HTTP path plus the new bind/advertised/port config and the DNS-rebinding
allow-list.

---

## 7. Work breakdown (maps to beads, ordered)

1. **[linchpin]** Make the harness loopback MCP bind host, advertised URL, and port configurable
   (§3) — Settings fields + validation, `HarnessMCPLoopbackServer` params + DNS-rebinding widen,
   threading through the request/runner. Defaults preserve today.
2. Tests for §3 — advertised-host reachability, DNS-rebinding accept/reject, fixed port, config
   guards, bearer-still-401s.
3. Correct the stale security memo, drafting-design doc, run trace, and remove dead stdio payload
   (§6). Independent; do early so trust matches code.
4. Ship the isolation-contract doc (MUST/MUST-NOT + threat model, §2.3).
5. Ship the Linux reference launcher (§4.1) — bwrap wrapper + egress proxy + nftables + fail-closed
   probe.
6. Ship the container reference launcher (§4.2, gold standard) — vendor image + egress sidecar +
   net-up/run/reap scripts + Scenario A wiring.
7. Ship the Windows reference launcher (§4.3) — WSL2 provisioning + shim (recommended); Windows
   Sandbox `.wsb` fallback; document the AppContainer trap.
8. Ship `lt-harness-probe` (§5) — vendor-CLI stand-in red-team probe.
9. Build `lab-tracker graph-draft verify-sandbox` orchestrator + attestation (§5) — `run_harness_argv`
   refactor, ground-truth observers.
10. Surface the attestation in `lt doctor` (+ optional `REQUIRE_VERIFICATION` gate, §5).
11. CI: Linux sandbox integration tests — positive + broken-wrapper negative (§5).
12. Scenario B: worker-in-Docker container-to-container compose profile (§4.2).
13. Package hardening: env-var vendor auth + empty child HOME (§4.5) — shrinks the wrapper's job.
14. Multi-arch pinned harness image build + validation in CI (§4.2).