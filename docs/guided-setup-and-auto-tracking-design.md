# Guided Setup and Automatic Tracking — Design

_Status: Phase 1 implemented (2026-07-01, epic `lab-tracker-stkn`, children
.1–.5); Phases 2–4 designed, not built. Produced from a five-way code
exploration plus a three-design / three-judge review; the Phase 1
implementation then passed an 18-finding adversarial review (all findings
fixed). The retained surface
([`retained-v1-surface.md`](retained-v1-surface.md)) wins any scope
disagreement._

## Product goal

When a user installs lab-tracker, their coding agents should become aware of
the `lt` tools for saving files; they should be guided to set up watch folders
and should be able to select repos to have lt watch commit hooks. Easy setup
first, followed by as-automatic-as-possible tracking.

## What exists today, and where it hurts

The capture machinery is shipped; the *setup path to it* is not. A new
consumer today crosses roughly nine manual steps spanning **two different
CLIs and two hand-edited JSON files**:

1. Install the full distribution (no client-only package) and start the API.
2. Export `LAB_TRACKER_BASE_URL` / `LAB_TRACKER_ACCESS_TOKEN` env vars in
   *every* shell, hook, and scheduler job — `LabTracker.from_env`
   (`src/lab_tracker_client/client.py:258`) is env-var-only, with no persisted
   profile and no `lt login`.
3. Run `lab_tracker init` (the *server*-package CLI, not `lt`) to scaffold
   `.mcp.json`, `.cursor/mcp.json`, `.claude/settings.json` (a
   `lt prime` prompt hook), `scripts/lt.py`, `AGENTS.lt.md`, `lt_ids.json`,
   and a managed CLAUDE.md activation block; `--yes` adds the code-conventions
   blocks.
4. Hand-edit `lt_ids.json` — init writes `project_id` as an **empty string**
   and nothing fills it.
5. `lt watch init` — which writes an **empty** `watches` list; registering an
   actual folder means hand-editing `.lab-tracker/watch.json`, because the CLI
   never exposes the `watches` parameter `watch.py:init_config` already
   accepts.
6. Run `lt watch scan` / `lt watch sync` manually, forever — no scheduler
   integration.
7. Enroll each repo's commit hook with
   `scripts/install-git-graph-draft-hook.ps1` — PowerShell-only, ships only in
   the source tree, and the installed hook hard-depends on a source checkout
   path (`LAB_TRACKER_ROOT` + `.venv`), so a pip-installed client cannot serve
   it and a moved checkout silently breaks every enrolled repo.
8. The hook is fire-and-forget: commits made while the server is unreachable
   are **lost** (one stderr warning, no queue) — unlike `lt watch`/`lt hpc`,
   which already have a durable offline outbox.
9. Nothing re-runs init on upgrade; `lt doctor` is manual, checks only the
   three conventions blocks, and reports drift on every version bump even when
   the text is unchanged (exact version-line equality), training users to
   ignore it.

Agent awareness has the same gap: the always-written CLAUDE.md block is
consultation-policy only — every idiom about *saving* files rides the
`--yes`-gated conventions blocks or the sidecar `AGENTS.lt.md` that no agent
auto-loads.

## Decision

Build **atomic, non-interactive, consent-gated `lt` verbs** as the spine, and
put the "wizard" in agent-readable prose (a skill + managed-block pointers +
an MCP resource), not in an interactive TTY state machine. Each verb is
flag-driven (`--dry-run` / `--yes` / `--force` / `--uninstall`), preserves the
JSON-stdout/exit-code contract, and is a thin composition over seams that
already exist and are already tested.

Judged alternatives, for the record:

- **Interactive `lt setup` wizard** — rejected as the spine: the client
  codebase has zero interactive code today, the prompts-to-stderr /
  JSON-to-stdout dual contract is fragile, and agents cannot sit at a consent
  prompt — hostile to the very discovery channel the goal names. Three of its
  components are grafted in (connection profile, outbox-backed commit
  capture, sha-only drift).
- **Committed `.lab-tracker.toml` + `lt apply` reconciler** — deferred, not
  rejected: best teammate/clone story, but it demotes the documented
  hand-editable `watch.json` to a derived artifact (permanent two-sources-of-
  truth tax) and its machine-wide standing consent is **explicitly rejected**
  — a committed config plus a pre-granted machine consent is a drive-by
  enrollment channel (clone → habitual `lt apply` → someone else's committed
  intent installs hooks on your machine with no per-repo review).
- **MCP setup tools** — rejected by all three designs independently: setup
  actions stay on the CLI, where the agent harness's own command-approval
  prompt is the second consent gate and no new server-side permission
  carve-outs are needed.

### Consent stance (the invariant, restated for this feature)

- Agents may run **read-only** setup commands unprompted: `lt setup status`
  and any `--dry-run`.
- Every mutating verb is consent-gated with the existing
  `--dry-run`/`--yes`/`--uninstall` pattern; the skill and all pointer text
  instruct the agent to *show the diff and let the human approve the
  command* — the agent never self-initiates `--yes`.
- Non-interactive invocation of a mutating verb without `--yes` or
  `--dry-run` hard-fails (a tested invariant, so agent- or CI-invoked runs
  can never hang or half-apply).
- PAT minting stays human-in-browser: service principals are blocked from
  `/auth/*`, and no flow asks an agent to relay a credential.
- No daemon, no server background machinery: recurrence is git hooks, agent
  session hooks, and OS schedulers, per the deployment philosophy.

## Components

### Phase 1 — setup verbs (easy setup: ~9 hand steps → ~3 approved commands)

1. **`lt setup status`** (S) — read-only machine/repo inventory,
   `needs_client=False`, network limited to one swallowed `GET /health`,
   `--fail-silent` supported. One JSON payload: server
   `{reachable, base_url, auth_enabled}`; repo scaffold presence and
   managed-block drift (via the existing `_doctor`); `lt_ids.json` binding;
   watch config + outbox pending/failed; hook-block presence; skill
   copies + versions. This is the substrate agents narrate from, the
   SessionStart drift sensor (phase 3), and the only command agents run
   unprompted.
2. **`lt setup init`** (S) — delegates to `lab_tracker.cli.init_consumer_repo`
   unchanged (same flags), exactly as `lt doctor` already delegates to
   `_doctor` (`src/lab_tracker_client/cli.py:742`). Consumers stop needing to
   discover a second CLI.
3. **`lt project bind`** (S) — `--project-id UUID | --name NAME [--create]`:
   resolve or upsert the project (existing `upsert_project`), write the id
   into `lt_ids.json` with a diff preview. Kills the hand-edited-UUID step.
   `InitResult.offers` gains a pointer to it.
4. **`lt watch add / list / remove`** (S) — read-modify-write over the
   `watches` parameter `watch.py:init_config` already accepts, atomic write,
   entry printed before writing. Kills the hand-edited `watch.json` step.
   Also: register `--fail-silent` on `watch scan`/`sync` so hook- and
   scheduler-fired invocations are safe.
5. **Persisted connection profile** (S) — `~/.lab-tracker/config.json`
   (honoring `LAB_TRACKER_CONFIG_DIR`, next to the existing `install-id`),
   read by `LabTracker.from_env` with **env-vars-first precedence** so
   nothing existing changes. Stores `base_url` and default project by
   default; **token persistence is opt-in behind its own explicit consent
   line** (`lt setup connect --save-token`), hardened with `icacls` on
   Windows (chmod is a no-op there) and documented short-TTL PATs. This is
   the single highest-leverage automaticity fix: hook-, scheduler-, and
   GUI-git-fired commands stop silently dying for lack of per-shell env vars.
   Two safeguards from the adversarial review are load-bearing: the profile
   token is ignored whenever the environment supplies its own credentials
   (`LAB_TRACKER_USERNAME`) **or** an env base URL pointing at a different
   server than the token was saved for, and both sides of every profile diff
   are redacted so a stored token can never surface on the `-` line of a
   later `connect` run.

### Phase 2 — repo enrollment + durable commit capture

6. **`lt git snapshot`** (M) — moves the transport of
   `scripts/create-analysis-graph-draft.py` into `lab_tracker_client` and
   makes commit capture **outbox-backed**: render the same git evidence
   (metadata, `--stat`, capped diff), write a deterministic outbox event
   (`capture_id.git.event_id.json`, same `(provider, external_id,
   content_hash)` dedupe), then best-effort sync; `--request-draft` flows
   through the existing draft endpoint on sync. Commits made while the server
   is down **queue instead of vanish** — the largest fail-soft win of the
   whole design, at zero new persistence machinery.
7. **`lt hooks install | uninstall | status`** (M) — pure-Python,
   cross-platform port of the PS1 installer: hook path via
   `git rev-parse --git-path`, the **same BEGIN/END managed-block markers**
   (existing enrolled repos upgrade in place), POSIX-sh body invoking
   `lt git snapshot --fail-silent` and exiting 0 unconditionally, absolute
   `lt` path recorded at install time (GUI git clients without PATH),
   refuses pre-existing unmanaged hooks without `--force`. Reuses
   `_upsert_managed_block`/`_strip_managed_block`. The PS1 script is
   deprecated-but-kept.
8. **Doctor extension + sha-only drift** (S/M) — `version_in_sync` compares
   the content sha only, so package bumps stop crying wolf; `_doctor` also
   verifies profile presence, project binding, `.mcp.json`, settings hooks,
   watch config, and hook-block health (including a stale-`lt`-path check);
   payload gains a non-imperative `suggestions[]` naming the exact repair
   verb. Doctor never writes.

### Phase 3 — the agent-awareness loop

9. **`skills/lab-tracker-setup/SKILL.md`** (S) — the wizard as prose. Staged
   script: inventory (`lt setup status`) → connectivity (suggest
   `lab-tracker serve`, never auto-start) → credentials (human mints PAT in
   the browser) → scaffold (`lt setup init`) → bind (`lt project bind`) →
   watch folders (`lt watch add`, eliciting *which* folders are worth
   watching) → repo hooks (`lt hooks install`, per repo the user selects) →
   optional scheduler enrollment. Consent choreography embedded as hard
   rules: always `--dry-run` first, show the diff, one command per human
   approval, non-imperative phrasing. Kept mechanically honest by the
   existing skill-reference drift-test pattern.
10. **Skill distribution: `lab_tracker init --install-skills`** (S) —
    consent-gated file copy (no symlinks; Windows) of the packaged skills
    into `~/.claude/skills/` with a version stamp; `--dry-run`/`--uninstall`
    supported; `lt setup status` reports copy version so upgrades surface as
    drift.
11. **MCP + cold-start discovery** (S) — a `lab-tracker://setup-guide`
    resource rendering the same staged script from one canonical generator
    (the `code_facing_idioms` single-source pattern); a setup hint in the
    `lab_tracker_unavailable` envelope's `next_action`; one non-imperative
    pointer sentence in the always-written managed CLAUDE.md block, the
    AGENTS fragment, and the `--yes` conventions block; and a copy-paste
    bootstrap line in README and `/app` — the only channel that works on a
    machine with zero prior scaffolding.
12. **SessionStart drift hook** (S) — `_claude_settings_json` scaffold gains a
    SessionStart hook running `lt setup status --fail-silent --brief`
    (one line when healthy, short advisory when drifted). Closes the
    "nothing re-runs init on upgrade" gap inside the sessions where agents
    actually work; the agent then *offers* the repair command.

### Phase 4 — automatic sustain (deferred)

13. **Scheduler enrollment** — `lt setup schedule` emitting a Task
    Scheduler / cron / launchd entry for `lt watch scan && lt watch sync
    --fail-silent`, mirroring the daily-review installer pattern. External
    scheduler, zero server machinery.
14. **`~/.lab-tracker/applied-repos.json` registry + `lt doctor --all`** —
    recorded at init/hooks-install time; answers "which repos are enrolled on
    this machine" and enables a post-upgrade fleet sweep. Client-side only.
15. **Declarative `.lab-tracker.toml` + `lt apply`** (for-eval) — only if the
    committed file is treated as *untrusted input* with per-repo
    diff-and-confirm on first apply; machine-wide standing consent stays
    rejected.

## The new-user journey (target state)

Agent-led path: a coding agent in a repo notices (bootstrap line, skill,
`setup status` in a SessionStart hook, or an MCP `next_action` hint) that
Lab Tracker capture is unconfigured. It runs `lt setup status`, narrates what
is missing, and walks the user through `lt setup init` → `lt project bind`
→ `lt watch add <results-folder>` → `lt hooks install`, showing each
`--dry-run` diff; the human approves each command. From then on: figure saves
capture via the fail-soft client idioms, watched folders queue offline events,
every commit in enrolled repos queues a snapshot event, and syncs drain
whenever the server is reachable. Drift after a package upgrade surfaces as
one advisory line at the next agent session, with the repair verb named.

Terminal-first humans run the same verbs in sequence; a future interactive
wizard, if ever wanted, is a thin front over the same verbs.

## Testing notes

- All new verbs are flag-driven and JSON-out — testable exactly like
  `test_watch_cli.py` / `test_lab_tracker_init.py`, no new infrastructure.
- Must-keep regression: `test_doctor_treats_safe_default_absent_blocks_as_
  not_installed` (absent blocks are *not installed*, not drift).
- New tested invariants: mutating verbs hard-fail non-interactively without
  `--yes`/`--dry-run`; `--dry-run` writes nothing while returning full diffs;
  hook install preserves the PS1 markers byte-for-byte; `from_env` precedence
  is env-first; outbox re-scan/re-snapshot of the same commit is a no-op.
