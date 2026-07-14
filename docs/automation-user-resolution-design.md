# Automation User Resolution Design Considerations

_Status: design note, 2026-07-08. Tracked by `lab-tracker-f5u4`._

Lab Tracker now has several consumer-side automation paths that can stage evidence
or request graph-draft proposals: git hooks, `lt watch`, `lt repo`, `lt hpc`,
figure capture, phone/device capture, and scheduled daily-review runs. They all
share one product promise:

> Automation may capture evidence and propose graph changes; only a person
> accepts and commits graph meaning.

That promise depends on user resolution being unsurprising. A proposal needs an
honest audit actor, a human review owner, and a read scope for any agentic tools.
Those are related but not identical.

## Terms

- **Actor**: the authenticated Lab Tracker principal on the API request
  (`AuthContext`). This is what authorizes the operation.
- **Attribution**: `created_by` / `created_by_user_id` on notes, graph change
  sets, batch runs, and committed entities. Today this is derived from the actor.
- **Review assignee**: `review_assignee` / `review_assignee_user_id` on batch
  graph drafts. This is the human whose review queue and read scope the draft is
  for, and it is also an exclusive review-authority transfer: non-admin creators
  who are not the assignee cannot edit, submit, accept, or bulk-accept that
  change set.
- **Principal type**: `USER`, `DEVICE`, `SERVICE`, or `SYSTEM`.
- **Source identity**: external facts such as git author, OS user, hostname,
  device label, Slurm user, repository remote, or commit SHA. These are evidence
  metadata, not Lab Tracker authority.

## Current Behavior

### Credential Resolution

Consumer clients resolve the Lab Tracker identity from environment variables
first, then the persisted machine profile:

1. `LAB_TRACKER_BASE_URL` / `LAB_TRACKER_MCP_BASE_URL`
2. `LAB_TRACKER_ACCESS_TOKEN`, `LAB_TRACKER_MCP_API_KEY`, or
   `LAB_TRACKER_MCP_TOKEN`
3. `LAB_TRACKER_USERNAME` + `LAB_TRACKER_PASSWORD` or MCP equivalents, used
   when no token is present or when a supplied token is rejected and login
   credentials are available
4. saved profile values in `~/.lab-tracker/config.json`

A saved profile token is ignored when the environment supplies username/password
credentials, or when an environment base URL points at a different server than
the saved profile URL. This prevents a token saved for one Lab Tracker instance
from being silently sent to another.

The installed hook usually pins the `lt` path, base URL, and project ID, but it
does not pin a user. The user is whichever credential the hook process can use
when it runs. GUI git clients, OS schedulers, shells, and login nodes may inherit
different environments.

Important implication: an outbox event does not currently carry a trusted
"intended Lab Tracker user." If Alice scans a folder and Bob later syncs the
outbox with Bob's token, the created note is attributed to Bob.

### Principal Capabilities

The coarse route allowlists are intentionally asymmetric:

- `DEVICE` can read and can write capture endpoints (`/notes`,
  `/notes/upload-file`, `/notes/quick-capture`, note transcript). Device
  principals can read graph-draft summaries, but cannot create, edit, submit,
  accept, bulk-accept, or commit graph drafts.
- `SERVICE` (`lpat_...`) can read and may call `GET /auth/me` to introspect
  its resolved user and token metadata. A read-only token cannot write except
  for the deliberately narrow admin scheduler carve-out,
  `POST /batches/run-due`; a writable editor/admin token can stage notes,
  request drafts, and use other role-permitted write APIs. LPATs have a
  90-day maximum TTL, and status/introspection surfaces expose token label,
  read-only state, and expiration without exposing the secret.
- `SYSTEM` is used by in-process background work. It can draft when running as
  an admin-like worker, but accept/commit gates reject it.
- `USER` browser/session requests are interactive.

Acceptance and commit require an interactive `USER`. `SERVICE`, `SYSTEM`, and
`DEVICE` can produce proposals or captures through their allowed endpoints, but
cannot accept, bulk-accept, or commit them.

The MCP write tools are an explicit supervised-use exception to the "automation
stages/proposes" framing: an MCP client holding a writable editor/admin LPAT can
call ordinary write surfaces when a person directs it, just as the same token can
call the REST API. We do not gate those MCP tools behind an interactive `USER`
principal because MCP clients normally use LPATs. Unattended MCP workflows should
still stage evidence and request proposals rather than accept or commit graph
meaning.

### Note-Scoped Drafts

`POST /notes/{note_id}/analysis-graph-drafts` resolves exactly like any other
authenticated request:

1. The note must be in a project the actor can contribute to.
2. The resulting `GraphChangeSet.created_by` and `created_by_user_id` come from
   the actor.
3. There is no `review_assignee` today; edit/author checks fall back to
   `created_by`.

For a writable personal access token owned by Sam, the proposal is created under
Sam's user id even though the request principal type is `SERVICE`. Sam can later
open the app as an interactive `USER` with the same user id and review/commit,
assuming project owner requirements are satisfied.

### Batch/Scheduled Drafts

Batch runs have two identities:

- `created_by`: the actor that triggered/enqueued/executed the run, often an
  admin token or `SYSTEM`.
- `review_assignee_user_id`: the human reviewer and read-scope key.

For due scheduled runs, reviewer fan-out is derived from batch settings:

- A per-user batch setting drafts for that explicit `user_id`.
- A project-default setting derives reviewers from staged notes'
  `created_by_user_id`, falling back to legacy `created_by`.
- The batch key includes the reviewer so each reviewer gets an idempotent window.

Agentic/live-read and external-harness drafting require a concrete
`review_assignee_user_id`; the scoped read executor reads as that target user
within the batch project, not as the scheduler/admin actor. Per-user batch
settings are validated at save time so the assignee must exist and be able to
read the project. Due scheduled runs also fail closed at dispatch time: if a
previously valid reviewer has disappeared or lost access, the scheduler records a
`skipped` run with `reviewer_unavailable` metadata and advances the window
instead of repeatedly drafting under the wrong identity.

### Auth-Disabled Development

When authentication is disabled, requests use the local pseudo-admin
`local-tester`. `created_by_user_id` is deliberately null for the local auth
user. This mode is convenient for development but cannot distinguish real humans
for attribution or per-user review queues. It also cannot provide a structural
human gate for accept-adjacent scripted calls, because every local request is the
same pseudo-admin `USER`. Keep auth-disabled mode on loopback/local development
only; `lt setup status` and the status subcommands warn when the connected
server reports `auth_enabled: false`.

## Invariants To Preserve

1. **Human gate stays structural in auth-enabled deployments.** Any automation
   identity can at most stage evidence or create proposed graph changes. It must
   not accept or commit. Auth-disabled local development is explicitly weaker,
   and status surfaces must say so.
2. **Read scope follows the reviewer, not the scheduler.** Background drafting
   may be started by `SYSTEM` or an admin service token, but model/tool reads
   must be scoped to a concrete human reviewer.
3. **Authority comes from Lab Tracker credentials.** Git author, OS account,
   hostname, Slurm user, and file owner are evidence facts, not authorization.
4. **Attribution must be honest.** Do not silently launder machine work as a
   person unless the credential or an explicit, server-validated delegation says
   so.
5. **Offline capture remains durable.** Hooks and watchers must keep queuing
   locally when the API is unreachable.
6. **Setup must stay consent-gated.** Agents can inspect and dry-run setup, but
   mutating enrollment and token persistence remain human-approved.

## Design Tensions

### Sync-Time Attribution vs Capture-Time Intent

The current outbox model attributes notes to the syncing credential. This is
simple and secure, but it can surprise shared-machine workflows:

- a commit hook queues under Alice's OS session, then a scheduler syncs with a
  lab admin token;
- an HPC login node syncs with a service token owned by the PI, not the trainee
  who ran the job;
- a synced cloud folder is scanned on one workstation and drained on another.

We should avoid treating OS/git facts as authority, but the product may still
need a first-class "intended reviewer" on queued events.

### Service Tokens: Owner Or Delegate?

An LPAT is owned by a human user, but its principal type is `SERVICE` because it
can run unattended. Today the token owner's user id becomes `created_by`; the
principal type controls what the token cannot do. This is workable for personal
automation, but weaker for lab-owned scheduled jobs:

- it does not say which token or machine performed the write;
- a shared admin token can make many users' proposals look admin-created unless
  `review_assignee_user_id` is set by batch logic;
- note-scoped drafts have no review-assignee field to separate request actor
  from review owner.

### Direct Drafts vs Daily Review Batches

`--request-draft` on `lt watch`, `lt repo`, and `lt hpc` immediately asks for a
note-scoped analysis draft. This is convenient but loses the richer reviewer
resolution that scheduled batches now have. The daily-review batch path is a
better fit for unattended, multi-user automation because it can derive or store
the reviewer explicitly.

### Auth-Disabled Mode

Auth-disabled local development cannot solve human attribution. It should remain
supported, but docs and setup status should be clear that per-user resolution
and structural human gating require auth-enabled users/tokens.

## Options

### Option A: Keep Credential-Owner Attribution

Document the current rule: automation resolves to the Lab Tracker credential it
uses at sync/draft time. Improve `lt setup status` and hook/watch docs so users
can see "this repo will sync as `<username>`."

Pros:

- no schema or API change;
- secure by default because the server trusts only authenticated identity;
- good enough for single-user local automation.

Cons:

- shared machines and scheduler accounts remain confusing;
- source user and sync user can diverge silently;
- note-scoped drafts still lack explicit review ownership.

### Option B: Add Local Profile Owner Checks

Persist the last verified `/auth/me` user id/username in the local connection
profile, then have hooks, `lt watch status`, and `lt setup status` warn when the
current token owner differs from the configured/bound owner.

Pros:

- catches accidental environment/profile drift;
- no server trust decision is delegated to local files;
- useful immediately for setup UX.

Cons:

- advisory only;
- does not support legitimate shared scheduler delegation by itself.

### Option C: Event-Level Intended Reviewer

Allow outbox events to carry an `intended_review_assignee_user_id` or `"me"` at
capture time. Sync may pass that to draft creation or leave it as note metadata.
The server only honors it when the actor is the same user, or when an owner/admin
has explicit authority to route reviews for that project.

Pros:

- makes capture-time intent durable across offline sync;
- lets shared schedulers route proposals to the scientist who produced the
  evidence;
- aligns note-scoped drafts with batch drafts.

Cons:

- needs API design and validation rules;
- a raw user id in a local file is spoofable unless server-side checks are
  strict;
- may need UI affordances for "drafted by automation for Alice."

### Option D: First-Class Automation Principal Audit

Keep `created_by_user_id` as the human owner, but add explicit audit fields for
the automation principal: token id/label, principal type, device id, hostname or
capture host, and adapter. This could live first in metadata, then graduate to
schema if it becomes load-bearing.

Pros:

- preserves human grouping while exposing the machine path honestly;
- helps incident/debug investigations;
- complements existing `origin_provider` / adapter metadata.

Cons:

- migration if done as first-class columns;
- token labels can leak operational details if overexposed;
- does not by itself choose the review owner.

### Option E: Reviewer-Scoped Tokens

Mint tokens that are explicitly scoped to one project and one review assignee.
Automation using the token can stage evidence and create proposals only for that
reviewer/project pair.

Pros:

- strongest unattended semantics;
- reduces blast radius versus a writable admin token;
- clean fit for shared HPC or lab scheduler jobs.

Cons:

- new token model and UI;
- token lifecycle/rotation burden;
- still a bearer secret in unattended environments.

### Option F: Prefer Batch Drafting For Background Automation

Keep direct `--request-draft` available for user-run commands, but steer hooks,
schedulers, and shared machines toward staging notes only. Daily-review batches
then resolve reviewers from per-user settings or note creators.

Pros:

- reuses the best reviewer-resolution path already present;
- reduces per-event model calls;
- keeps background proposal generation in one human-gated queue.

Cons:

- less immediate feedback after a commit/run;
- project-default reviewer derivation still depends on note attribution;
- does not solve capture-time intent unless paired with Option C or D.

## Recommended Direction

Adopt a layered model:

1. **Short term: make the current rule visible.**
   `lt setup status`, `lt hooks status`, `lt watch status`, `lt repo status`,
   and `lt hpc status` should report the resolved Lab Tracker user when a token
   is available, and warn when auth is disabled or no credential is configured.
2. **Short term: record automation path as metadata.**
   Staged notes may carry adapter, host label, repo/outbox path, and
   token-principal hints for audit/debug. Stored note metadata is still visible
   to project readers today, so do not record secrets. Model-bound graph draft
   context and read-tool packets strip operational metadata such as host/install
   IDs, token labels, service-token hints, source URIs, and local paths while
   preserving scientific context such as `capture_source`, `capture_hint`, and
   `source_file_name`.
3. **Near term: add explicit, owner-controlled reviewer routing for note-scoped drafts.**
   Add an optional `review_assignee_user_id` to note-scoped graph draft creation,
   with server-side checks. Routing another user's review queue should require
   project ownership/admin authority because assignment transfers edit/submit
   and accept authority away from the non-admin creator. This makes direct
   `--request-draft` behave more like batches.
4. **Near term: let outbox events carry intended reviewer intent.**
   Store `"me"` or a concrete user id in the outbox config/event, but honor it
   only through server validation. Shared scheduler workflows should be able to
   route proposals without pretending the scheduler is the scientist.
5. **Longer term: consider reviewer/project-scoped automation tokens.**
   Do this only if shared-machine/HPC workflows prove painful with normal LPATs
   plus reviewer routing.
6. **Default background jobs to batch review.**
   For unattended schedulers, prefer "stage notes now, draft in due batches" over
   per-event draft requests unless the user explicitly asks for immediate drafts.
   Newly installed managed post-commit hooks stage only by default. Explicit
   `lt hooks install --request-draft` opts in to immediate draft requests, and a
   reinstall with no draft flag preserves whatever the existing managed block
   already did.

This keeps the security primitive simple: the API request actor remains the
authorization source, while review ownership and model read scope become explicit
server-validated fields instead of accidental side effects of whichever process
drained an outbox.

## Open Questions

- Should a writable personal access token default note-scoped
  `review_assignee_user_id` to its owner, or should clients pass it explicitly?
- Should direct `--request-draft` be disabled or warned for shared/admin tokens
  unless a reviewer is supplied?
- Should note `created_by_user_id` represent the syncing credential forever, or
  should we add a separate `captured_for_user_id` / `review_owner_user_id`?
- How should auth-disabled development data be displayed so it is not mistaken
  for real per-user attribution?
- Should outbox intended-reviewer validation poison-pill invalid events, silently
  ignore invalid routing, or sync the note while withholding any draft request?
- Do we need first-class, access-controlled token-id/token-label audit fields
  before reviewer-scoped tokens, or is adapter metadata enough?

## Relevant Code And Docs

- `src/lab_tracker_client/client.py`: environment/profile resolution.
- `src/lab_tracker_client/hooks.py`: managed post-commit hook body.
- `src/lab_tracker_client/watch.py`: outbox sync and `--request-draft`.
- `src/lab_tracker_client/repo.py`: repo capture sync and graph draft request.
- `src/lab_tracker_client/hpc.py`: HPC capture sync and graph draft request.
- `src/lab_tracker/mcp_server.py`: Lab Tracker MCP server surface.
- `src/lab_tracker/mcp_tools/write.py`: supervised MCP write tools.
- `src/lab_tracker/services/graph_draft_harness_mcp.py`: scoped harness MCP.
- `src/lab_tracker/routes/review_delivery.py`: scheduled review delivery.
- `src/lab_tracker/app_parts/middleware.py`: token-to-`AuthContext` resolution.
- `src/lab_tracker/auth.py`: principal types and route allowlists.
- `src/lab_tracker/services/shared.py`: actor attribution helpers.
- `src/lab_tracker/services/note_service.py`: note `created_by` fields.
- `src/lab_tracker/services/graph_draft_service.py`: graph draft attribution,
  reviewer fan-out, and interactive accept/commit gates.
- `docs/guided-setup-and-auto-tracking-design.md`
- `docs/watch-folder-capture.md`
- `docs/repo-report-capture.md`
- `docs/hpc-analysis-capture.md`
- `docs/agentic-live-read-tools-design.md`
