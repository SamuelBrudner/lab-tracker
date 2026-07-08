# External Harness Graph-Draft Drafting — Design Decision

> **Implementation status (2026-07-08): this is the DESIGN, not the shipped
> state.** The `external_harness` provider ships **default-off as a scaffold**.
> Where this document says a control is "enforced by construction," that is the
> design *intent* — an independent review found several are **operator-managed,
> not code-enforced**: the scoped-executor read chokepoint is **not wired** to
> the subprocess (the child reads a static pre-scoped context), and OS isolation
> / native-tool denial / egress restriction are the operator's sandbox-wrapper
> responsibility. What is genuinely enforced in code, and the remaining gaps, are
> tracked honestly in [external-harness-security-review.md](external-harness-security-review.md)
> and epic `lab-tracker-bhkq`. Do not enable until those gaps close.

## Decision

Incorporate an external agent harness (Claude Code / Codex CLI / Gemini CLI) as the **drafting brain** for daily-review graph proposals by adding a new `HarnessGraphDraftClient` on the existing `GraphDraftClient` protocol. The harness runs only in the SYSTEM background worker as a **sandboxed headless subprocess**, reaches the graph **only** through a per-run **stdio** MCP surface that wraps `ScopedGraphDraftReadToolExecutor`, holds **no Lab Tracker credential**, and returns a structured patch that the **unchanged** `create_batch_graph_draft` pipeline validates and lands as `PROPOSED`.

**One-line rule:** *The harness reasons behind the scoped-executor chokepoint over a credential-less stdio pipe and proposes only; a human still commits — and the enable flag stays off until the sandbox and fail-closed reviewer pass a security review.*

## Why (grounded in the judge findings)

The commit-side invariant is already structural in **every** candidate design: `require_interactive` is a fail-closed allow-list of `{USER, DEVICE}` enforced at accept, bulk-accept, and commit (`project_authorization.py:43`, `graph_draft_service.py:1446/1775`), commit additionally requires `require_owner`, and `AUTO_ACCEPTED` is rejected at both service and repository layers. A harness authenticating as SERVICE/SYSTEM can therefore never accept or commit, **no matter how compromised**. So the real decision is entirely about the **read side** and the harness's **own out-of-band egress**.

On that axis the Lean In-Worker Harness Adapter was top-or-near-top on all four judge lenses (security 7, architecture-fit 8.5, human-gate 8, ops 7.5) — the only design without a weak lens. It reuses `ScopedGraphDraftReadToolExecutor` (verified: 18-tool `AGENTIC_READ_TOOL_ALLOWLIST`, no `resolve_artifact`, self-constructed `AuthContext(VIEWER, USER=target_user)`, foreign-project rejection, sensitivity redaction), keeps **one** authoritative landing in `create_batch_graph_draft`, uses **stdio** (no TCP listener, no nonce lifecycle, no bind-guard failure class), and offers genuine one-flag rollback. Its only real weakness — a soft baseline Windows sandbox — is closed by grafting two cheap, high-value ideas from the max-safety design: a **mandatory egress-controlled sandbox as the flag-flip gate**, and **sensitivity forced to `omit`** for the less-trusted external process. The max-capability design (curated PubMed/ChEMBL/trials tools) is rejected outright: it opens a model-steerable research-query egress channel (stressing invariant 4), carries the largest prompt-injection blast radius, was worst on ops (3.5), and expands the product surface beyond `retained-v1-surface.md`.

## Architecture

**Provider shim.** A new `HarnessGraphDraftClient` implements the `GraphDraftClient` protocol, is selected by `make_graph_draft_client(graph_draft_provider="external_harness")`, sets `requires_background_worker=True` (so `_ensure_draft_client_allowed_here` fences it to the SYSTEM worker), and — critically — sets `_tool_loop_enabled=True` and exposes `configure_live_read_tools(executor)` so that `_configure_agentic_live_read_tools` builds the scoped executor **and trips the fail-closed `review_assignee_user_id is None` raise** (`graph_draft_service.py:509-514`). If that flag is falsy the executor is never built and the reviewer guard silently no-ops; the client must trip the gate exactly as `AgenticGraphDraftClient` does, and a test must pin this.

**Credential model.** The harness receives **zero** Lab Tracker credential — no LPAT, no bearer, no session. A project-scoped LPAT does not exist today (no project dimension in the token stack; an admin LPAT collapses to the `accessible_project_ids=None` all-projects wildcard) and would be a bearer secret that becomes an exfiltration target. Instead scope is bound **server-side at spawn** to `(run.project_id, run.review_assignee_user_id)`; the executor mints its own VIEWER/USER `AuthContext` internally and raises `AuthError` if that user cannot read the project. The harness's only secret is its **own** model-vendor API key, injected into the sandbox environment alone — never the server's `LAB_TRACKER_*` keys, DB DSN, or repo.

**Transport & tool surface.** A **new**, minimal, per-run **stdio** MCP server hosted by the worker — never the existing `lt-mcp` server (26 tools incl. `resolve_artifact`, single env identity, no REST-side sensitivity scrub). It registers exactly: (a) the 18 allowlisted read tools, each dispatching to `executor.execute(tool_name, arguments)` via a **neutral MCP serialization added beside `anthropic_tool_specs()`** (MCP derives its own schema), so single-project scope, `_scoped_project_id` foreign-id rejection, and `_apply_sensitivity` run server-side; and (b) **one** propose-only tool, `submit_graph_patch`. `resolve_artifact` and all `create_*`/write tools are **structurally absent**. Sensitivity policy for this path defaults to the stricter `omit` (note id/metadata only), not `redact`.

**Output contract.** `submit_graph_patch(graph_patch)` is a **termination signal + shape-check**, not a persistence path. The adapter captures the patch matching `graph_patch_response_schema()` and returns `(patch, trace)` from `draft_from_batch`; the **unchanged** `create_batch_graph_draft` runs `validate_top_level` then `operations_from_graph_patch` (per-op Pydantic Create/Update validation, project-scoped reference-existence, semantic-target legality) and lands `GraphChangeSet(status=READY)` with every op `PROPOSED`, `acceptance_mode=None`, `created_by=SYSTEM`, `review_assignee_user_id=` the run reviewer. Caller-supplied `created_by`/`status`/`acceptance_mode`/`project_id` are ignored (server-derived). Malformed/partial output → `FAILED` with bounded `error_metadata`. Only the final patch + a bounded trace digest (names/truncated-args/result-ids) is persisted; **raw harness stdout/stderr is never stored** (new admin-visible leak channel).

**Sandbox mechanism.** Single Windows box; isolation is net-new (`.codex-runtime` is only logs). Layered, deny-by-default: (1) per-vendor **native-tool denial** — Claude Code `--disallowedTools Bash/Read/Write/Edit/WebFetch` + `allowedTools mcp__lt__*`; Codex sandbox/approval; Gemini equivalents — so the only tools the model sees are the 18 reads + `submit_graph_patch`; (2) run under a **dedicated low-privilege local account** whose NTFS ACLs deny the repo, `.env`, `lab_tracker.db`, and server keys, in an **ephemeral empty cwd**; (3) **egress allowlist** — outbound restricted to exactly the model-vendor API host(s) (stdio needs no network), via the guest's firewall/proxy; prefer a real isolation primitive (Windows Sandbox / WSL2 network namespace) over ACL+firewall alone; (4) **bounded budget** — wall-clock timeout + max turns (reuse `graph_draft_agentic_max_tool_calls`) + Job-Object kill-on-overrun. If the sandbox or egress control cannot be established, the run **fails closed** and does not draft.

**Unattended vs human-in-loop.** Drafting is fully **unattended** and reuses the existing cadence verbatim (Task Scheduler → `/batches/run-due` → background worker → `create_batch_graph_draft`), gated by `graph_draft_background_enabled`/`graph_draft_scheduler_enabled`. No new scheduler, no new endpoint. The human enters **only** at the existing review queue, where accept authority is re-keyed to `review_assignee_user_id` and commit requires the project owner.

**Vendor-neutrality.** The entire safety boundary (allowlist, per-(project,user) scope, omit-sensitivity, no artifact-deref, propose-only submit) lives in the Lab Tracker executor + MCP server and is identical across vendors. The only per-vendor artifact is a thin declarative launch table `{executable, headless flag, MCP-attach flag/format, native-tool deny config, output convention}`, selected by `graph_draft_external_harness = claude_code | codex | gemini`. Because the harness owns its own model loop, this also sidesteps the current gap where only `AnthropicGraphDraftClient` implements a real tool loop.

## How each HARD INVARIANT is enforced by construction

1. **Propose-only / human commits.** Harness runs as SYSTEM with no interactive credential; `require_interactive` fail-closes accept/bulk-accept/commit; `AUTO_ACCEPTED` rejected at two layers; no new write/commit code path exists.
2. **No cross-project leakage.** Reads go only through the executor bound to a single `project_id` + concrete non-admin `target_user_id`; foreign `project_id` rejected by `_scoped_project_id`; reviewer-None fails closed (never admin/owner fallback, which would yield the all-projects wildcard).
3. **Sensitivity honored on every model-facing read.** Every payload passes `_apply_sensitivity` with the external path forced to `omit`; the batch context builder applies the same policy; the persisted trace is a bounded digest, never note bodies.
4. **No uncontrolled egress.** `resolve_artifact` and all write tools structurally absent; the harness's own bash/web/fs tools are denied and network egress is restricted to the vendor API + the stdio pipe; no research tools.
5. **Retained-v1 / build-vs-buy.** Buy the reasoner; keep the semantic spine + human review gate in-house. Drafting-only preserves the literal "v1 does not delegate graph commits to autonomous agents"; the surface doc is amended and a security review passes before the flag flips.

## Rejected alternatives

- **Curated external research tools (max-capability).** Opens a model-steerable exfiltration channel, largest injection blast radius, off-server processing, and scope creep beyond retained-v1. Rejected.
- **Loopback-HTTP + per-run nonce (ops-neutral transport).** Adds a local network listener, a bearer secret reachable by other local processes, a bind-guard, and the FastMCP lifespan pitfall — for no benefit over stdio.
- **Per-run project-scoped LPAT.** Requires a new token column + admin-wildcard override + mint/revoke lifecycle, and is a bearer secret in an untrusted process. Unnecessary given the in-process executor.
- **Public proposal-upload endpoint.** Proposal-injection surface; gives up server-stamped attribution. Replaced by in-process stdout capture.
- **Pointing the harness at the existing `lt-mcp` server.** Over-broad (resolve_artifact, writes, single env identity, no REST-side sensitivity scrub) — violates invariants 2/3/4.

## Open product decisions

- **Sensitivity default:** confirm `omit` (recommended) vs `redact` for the external path.
- **Reviewer-ambiguity:** confirm refuse-to-draft (recommended) vs any owner fallback for scheduled batches with no assignee.
- **Cost ceilings:** the `create_batch_graph_draft` 3× retry and per-reviewer fan-out multiply full harness spawns; decide a global per-tick spend/turn ceiling and whether harness-call failures should retry at all.
- **Provenance granularity:** whether to record the harness identity/version/model per-operation (`origin_agent_tools`) now or later; `created_by` stays SYSTEM.
- **Opt-in scope:** per-project/per-batch enablement + A/B measurement before it is anything but experimental (it is strictly costlier and less deterministic than the in-house loop).

## Phased build order

1. Neutral MCP serialization + per-run stdio server wrapping the executor (18 tools + `submit_graph_patch`).
2. Credential-less per-run binding + fail-closed reviewer re-enforcement.
3. `HarnessGraphDraftClient` + per-vendor launch table + stdout capture into the existing landing.
4. Windows sandbox profile (isolation, egress allowlist, no-secrets cwd, tool-deny).
5. Cost/turn/timeout bounds, retry & fan-out ceilings, kill-on-overrun.
6. Deterministic tests (scope isolation, fail-closed reviewer, propose-only, omit-sensitivity, malformed output).
7. Security review gating the default-off enable flag.
8. Docs: amend `retained-v1-surface.md`, update `agent-setup.md`, ship this design doc.