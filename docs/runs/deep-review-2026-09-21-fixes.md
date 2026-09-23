# Deep review 2026-09-21: fix status

This records how the findings in [`deep-review-2026-09-21.md`](deep-review-2026-09-21.md)
were addressed on branch `claude/epic-planck-q3shcy` (from `main` at `9858a27`),
in the report's priority order: C1 first, then the highs, the priority
mediums, the remaining mediums, and the lows. Each work package was
implemented test-first in its own worktree, reviewed adversarially, corrected
until the reviewer approved, and then integrated.

## Outcome

| Severity | Findings | Fixed | Already fixed on `main` | Partly fixed | Deferred |
|---|---|---|---|---|---|
| Critical | 1 | 1 | 0 | 0 | 0 |
| High | 10 | 10 | 0 | 0 | 0 |
| Medium | 83 | 82 | 1 (M50; race tests added) | 0 | 0 |
| Low | 131 | 106 | 13 | 6 | 6 |

Every critical, high and medium finding is resolved. The partly fixed and
deferred lows are the ones that need a product or policy decision; each has a
concrete proposal below and in its package's notes.

## Validation on the integrated branch

- Backend, SQLite: 6,291 passed, 147 skipped (Windows-only and Postgres-marked
  tests).
- Backend, Postgres 16 (`-m postgres`): 93 passed.
- Frontend: vitest 469 passed; ESLint and the contract typecheck pass; the
  committed bundle is rebuilt from the merged sources.
- `ruff check`, `mypy`, the OpenAPI-types and skill-reference `--check`
  generators, and `alembic heads` (single head, `0064_orm_schema_parity`)
  pass.
- CI runs on pull requests and on `main` only, so the new CI steps (Docker
  image build, `uv lock --check`) have not run on GitHub yet.

## Changes operators and API clients will notice

- Two new migrations: `0063_user_session_epoch` and `0064_orm_schema_parity`.
  Upgrading past 0063 signs every browser user out once. SQLite migrations
  now run as one all-or-nothing transaction; see the
  [SQLite migration cascade advisory](../advisories/2026-09-sqlite-migration-cascade.md)
  if a SQLite database was upgraded between 26 July 2026 and this fix.
- The app refuses to start against an unmigrated database or one stamped with
  a revision the build does not know (`lab-tracker serve` migrates first).
- Authorization denials are `403 forbidden`; `401 auth_error` now means only a
  missing, invalid or revoked credential. The web app signs out on a 401 from
  a request that carried its session token, and the MCP and `lt` clients no
  longer treat a permission denial as a bad credential.
- The first-admin bootstrap token is never shown in the browser outside
  `LAB_TRACKER_ENVIRONMENT=local` unless `..._DISCLOSURE=first_run` is set.
- The hosted MCP server is read-only unless `LAB_TRACKER_MCP_ALLOW_WRITES` is
  set, and the Compose `mcp` service sits behind the `mcp` profile.
- Request validation is stricter: unknown nested keys and over-long strings
  return 422; questions reach `superseded` only through the refactor command
  and notes reach `archived` only through the archive command.
- Supervision edges are managed by global admins only.
- `GET /batches` returns summaries (`operation_count`, `meeting_note_count`);
  fetch `GET /batches/{change_set_id}` for operations.
- Login and registration have separate rate limiters with a per-client share
  (IPv6 grouped by /64). Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` or
  every client shares one quota.
- Registered local-store health reports `unsupported` until the local-use
  slice lands; Git cache eviction removes only directories the cache created.
- `LAB_TRACKER_GRAPH_DRAFT_PROVIDER=agentic` requires the background worker;
  the Anthropic output budget is configurable (default 16,000 tokens) and its
  timeout default is 300 s.
- Public viewer registration defaults to off outside
  `LAB_TRACKER_ENVIRONMENT=local`, and the hosted templates set it off
  explicitly. `/metrics` is admin-only, and a group that still contains
  projects cannot be deleted.

## Decisions still needed

Each item was left unchanged on purpose because it needs a product or policy
choice; the package notes carry the full proposals.

1. **Registration reveals taken usernames (L43, register half).** Login timing
   no longer leaks account existence, and public viewer registration now
   defaults to off outside `LAB_TRACKER_ENVIRONMENT=local` (L52), but an open
   `/auth/register` still answers 409 for a taken name. Options: invite-only
   registration, or email-verified signup that always answers 202.
2. **Notification email confirmation (L103).** Saving an address marks it
   confirmed. A signed confirmation link needs a new outbox event type, which
   the outbox CHECK constraint blocks without a migration, plus an endpoint.
3. **Spanning goals on project deletion (L100).** A projectless goal that
   links two projects is deleted with either project. Options: refuse the
   delete, relax the goal-scope rule, re-anchor the goal, or delete and audit.
4. **Scaling reads (L86, L61, L88).** Goal visibility is decided per goal in
   Python; the project graph has no node cap; the JSON raw-note preview still
   loads the whole asset (the binary download now streams).
5. **Smaller items:** a configurable source repository URL for client install
   commands (L58); distinct "Save for later" behaviour (L60); unaddressable
   Windows names in recovery scans (L26); helper and path-policy refactors
   (L27, L78); in-memory auth branches used only by tests (L46).

Choices made along the way that you may want to revisit: project membership,
not the global viewer role, decides project write access, and the docs now say
so (L106); a group cannot be deleted while it still contains projects (L107);
`/metrics` is admin-only (L38); supervision edges are admin-managed (M71).

## Other open points found while integrating

- The default `LAB_TRACKER_ANTHROPIC_MODEL` is `claude-3-5-sonnet-latest`, a
  retired alias; choosing a replacement is a cost decision.
- The offline upload queue keeps captures refused with 403 queued for a
  credential retry that cannot succeed. Dropping them would lose the note; a
  "needs access" state would be clearer.
- On SQLite, a visualization upload holds the database write lock while the
  blob is stored (documented; SQLite is the single-process backend).
- Re-drafting a note whose draft was committed returns the committed change
  set (M75 handles only rejected drafts).

## Per-finding status

| Finding | Title | Status | Package |
|---|---|---|---|
| C1 | SQLite batch-mode table rebuilds cascade-delete child rows (0061 wipes graph_change_operations and nulls ch... | Fixed | c1-migrations |
| H1 | Root docker-compose `${VAR:?}` for MCP tokens makes `docker compose up app` and every documented backup/res... | Fixed | release-deploy |
| H2 | Documented fresh-clone install resolves mcp 2.x and breaks `lt-mcp` (and the test suite) at import | Fixed | release-deploy |
| H3 | Cron installer pipes a crontab without a trailing newline; Debian/Ubuntu crontab refuses to install it | Fixed | release-code |
| H4 | Personal access tokens keep their issuance-time role after the user is demoted; admins cannot revoke other... | Fixed | credentials, r4-auth |
| H5 | Import-time Settings()/engine construction in db.py (unused SessionLocal) breaks lt, lt-mcp and the documen... | Fixed | release-code |
| H6 | Offline upload queue can never drain when authentication is disabled (default / LAN --allow-insecure-auth-d... | Fixed | followups-a, frontend |
| H7 | Entity IDs are interpolated unescaped into API URL paths; httpx dot-segment normalization lets any MCP tool... | Fixed | mcp |
| H8 | First-admin bootstrap token is served unauthenticated to any client whose TCP peer is a private address, wh... | Fixed | credentials |
| H9 | `git snapshot` credential stripping keeps bare-token userinfo (`https://<token>@github.com/...`) in evidenc... | Fixed | client, followups-a |
| H10 | `lt repo` capture records the raw remote URL, leaking embedded credentials into staged notes and metadata | Fixed | client, followups-a |
| M1 | .env.example sets LAB_TRACKER_MCP_HOST=127.0.0.1, which makes the compose `mcp` service unreachable when th... | Fixed | release-deploy |
| M2 | Dependabot `pip` ecosystem edits pyproject.toml without regenerating uv.lock, and CI has no lock-freshness... | Fixed | release-deploy |
| M3 | restore-smoke readiness probe uses the Unix socket and can pass during the postgres image's init-only tempo... | Fixed | r3-ops |
| M4 | mcp compose service inherits the Dockerfile HEALTHCHECK on /health, which the MCP ASGI app answers with 401... | Fixed | release-deploy |
| M5 | compose `mcp` service re-resolves and reinstalls the project with `uv run` at container start instead of us... | Fixed | release-deploy |
| M6 | First-admin (non-Docker) setup instructions fail at startup: auth enabled without a signing secret is rejected | Fixed | release-deploy |
| M7 | Hosted GitHub Pages demo breaks on any nested route: 404.html uses relative asset URLs | Fixed | r3-ops |
| M8 | Windows daily-review scheduled task has no way to carry credentials, so the documented auth-enabled flow on... | Fixed | r3-ops, r4-ops, r5-followups |
| M9 | Re-running the cron/launchd daily-review installer without the secret exported silently wipes the persisted... | Fixed | release-code |
| M10 | serve-lan.sh auth-disabled guard only fires for 0.0.0.0/::, so `--host <LAN IP>` serves the graph unauthent... | Fixed | r3-ops |
| M11 | A single dangling symlink anywhere under a recovery root aborts the whole content-hash recovery scan (both... | Fixed | r3-stores |
| M12 | An unreadable subdirectory anywhere under a recovery root makes the whole enumeration DENIED | Fixed | r3-stores |
| M13 | AcquisitionOutputWatcher swallows every registration error silently and retries forever without logging | Fixed | r3-ops, r4-ops |
| M14 | SQLite migrations are not atomic: a failed migration leaves half-applied DDL and a stale _alembic_tmp table... | Fixed | c1-migrations |
| M15 | Per-migration `PRAGMA foreign_keys=ON` re-enable is a silent no-op, leaving FK enforcement off for the rest... | Fixed | c1-migrations |
| M16 | DELETE /projects/{id} always loses its usage-telemetry event because the after-commit insert violates the u... | Fixed | r3-http |
| M17 | App starts and reports /health OK against an unmigrated or stale database; the only schema check is downgra... | Fixed | r3-ops, r4-ops |
| M18 | Multipart file uploads are fully received and spooled to disk before any size limit runs; chunked uploads b... | Fixed | r3-http |
| M19 | Local-store health is unconditionally 'unsupported' while the retained surface and design doc say it is a s... | Fixed | r3-stores |
| M20 | Git cache root/quota bypass Settings, are undocumented, and an invalid LAB_TRACKER_GIT_CACHE_MAX_BYTES sile... | Fixed | r3-ops, r4-ops |
| M21 | next_questions treats rejected/proposed claims as answering a question, hiding questions that still need work | Fixed | r3-context-mcp |
| M22 | Bounded subprocess cleanup depends on PID 1 reaping orphans promptly; the shipped image execs uvicorn as PI... | Fixed | subprocess, release-deploy |
| M23 | ORM metadata and migrated schema have diverged: 25 columns NOT NULL in db_models but nullable in the databa... | Fixed | schema-parity |
| M24 | Decision context resolves the project through a 500-project oldest-first window, producing false anchor_not... | Fixed | r3-context-mcp, r4-provenance, r5-followups |
| M25 | Service worker accepts cross-origin POSTs to /app/share-target and the capture page silently imports them a... | Fixed | r3-frontend, r4-frontend |
| M26 | DailyReviewScheduleForm shows (and would save) a previously selected project's settings when responses arri... | Fixed | followups-a, frontend |
| M27 | Note detail PATCH replaces metadata with a stale snapshot and wipes transcript provenance written by /trans... | Fixed | frontend |
| M28 | Mobile capture has no in-flight guard: a double tap on Save capture creates duplicate notes | Fixed | frontend |
| M29 | Last-used project restore is dead inside the real App: the hook clears the stored selection on mount before... | Fixed | r3-frontend |
| M30 | 401 from a revoked paired-device token ("Invalid device token.") does not match the auth-rejection message... | Fixed | r3-authz |
| M31 | Anthropic client hard-caps output at 4096 tokens and ignores stop_reason, so large batch narratives truncat... | Fixed | r3-graph |
| M32 | Agentic provider is accepted by config but breaks every note-scoped and inline drafting path at first use i... | Fixed | r3-graph |
| M33 | MCP next_questions silently truncates goals, questions, and claims at the oldest 200 rows per list with no... | Fixed | r3-context-mcp |
| M34 | FastMCP auto-enables DNS-rebinding Host validation for the documented default bind 127.0.0.1, so the hosted... | Fixed | mcp |
| M35 | Hosted streamable-http server registers all write tools and local-file-reading tools; nothing verifies the... | Fixed | mcp |
| M36 | Startup open-ADMIN guard fails open: any non-auth API error (unreachable target, 404, 5xx) is swallowed sil... | Fixed | mcp |
| M37 | MCP decision-context tool cannot pass created_by/since/until, so the documented progress_review briefing is... | Fixed | r3-context-mcp, r4-provenance |
| M38 | All MCP tools are synchronous and run inline on the FastMCP event loop, so one slow API call stalls every c... | Fixed | r3-context-mcp |
| M39 | Nested domain models reused inside request schemas silently drop unknown/misspelled keys (extra='ignore'),... | Fixed | r3-invariants |
| M40 | Claim provenance omits visualizations related to the claim unless their analysis is a supporting analysis | Fixed | r3-context-mcp |
| M41 | Claim provenance document overwrites merged prov:Person nodes and drops dataset/analysis-time supervision (... | Fixed | r3-context-mcp, r4-provenance, r5-followups |
| M42 | InMemoryRateLimiter never evicts expired buckets; per-token PAT keys make the dict grow without bound from... | Fixed | credentials (with M43) |
| M43 | InMemoryRateLimiter buckets are never pruned and are keyed by attacker-controlled strings (unbounded userna... | Fixed | r4-auth |
| M44 | Session JWTs cannot be revoked: an admin password reset or role change never terminates existing sessions,... | Fixed | credentials, r4-auth |
| M45 | Authorization denials ("Project contributor access required") are raised as AuthError and mapped to HTTP 40... | Fixed | r3-authz |
| M46 | GET /batches loads every batch change set across all projects, with operations and context packets, then fi... | Fixed | r3-graph |
| M47 | Quick-capture writes the upload to raw note storage before authorization and project-existence checks | Fixed | r3-http |
| M48 | /usage-events/export materializes the entire usage-event table in memory with no bound | Fixed | r3-http |
| M49 | Request schemas leave name/title/description/viz_type/etc. unbounded while ORM columns are String(255)/Stri... | Fixed | r3-invariants |
| M50 | Analysis deletion check skips PROPOSED/TESTING claims and runs before the write, so a concurrent status pro... | Already fixed | r3-services |
| M51 | Claims can be deleted regardless of status or references; cascades strip evidence, logic edges and pivot in... | Fixed | deletion |
| M52 | Claim-edge cycle check is read-then-write with no lock; concurrent edge creates commit a cycle | Fixed | deletion |
| M53 | Inherited group-scoped stores are listed for a project member but GET /data-stores/{id} and /health return... | Fixed | r3-services, r4-locks |
| M54 | Archived datasets that were committed are mutable: question links, manifest and commit hash can be rewritten | Fixed | r3-services |
| M55 | Deleting datasets, sessions, notes, analyses, claims or visualizations leaves note targets (and staged-data... | Fixed | deletion |
| M56 | Dataset delete guard is unlocked against concurrent claim/analysis creation on Postgres | Fixed | deletion |
| M57 | delete_exploration_node has no referrer guard or lock: deleting a node strips a committed PIVOT's exactly-o... | Fixed | deletion |
| M58 | One dangling goal link makes goal listings fail for every caller | Fixed | r3-services, r4-locks, r5-followups |
| M59 | Commit applies member-onboarding-only target rules to every link_note_to_question draft, so $ref or mixed t... | Fixed | r3-graph |
| M60 | Frontend offers 'Revise' on Daily Review (batch) drafts but the backend rejects revision of graph_batch drafts | Fixed | r3-graph |
| M61 | Server-side storage read failures are reported as 422 validation errors with no traceback logged | Fixed | r3-invariants |
| M62 | Note can be archived without a reason (POST/PATCH status=archived) and un-archived leaving stale archived_r... | Fixed | r3-invariants |
| M63 | Note deletion ignores committed dataset manifests and note->note targets, leaving dangling provenance refer... | Fixed | deletion |
| M64 | Project owner can detach a project from its group without any group authorization, severing PI oversight | Fixed | membership |
| M65 | Last group owner can be demoted via PATCH/POST members, leaving a group with no owner although DELETE guard... | Fixed | membership |
| M66 | Offboarding export gate is satisfied by any prior export event, so records created after the export are nev... | Fixed | membership |
| M67 | delete_question guard misses question_refactors CASCADE and superseded links: deleting a refactor replaceme... | Fixed | deletion |
| M68 | Session deletion leaves staged datasets with a dangling manifest source_session_id and cascades captured ac... | Fixed | deletion |
| M69 | Questions can be marked superseded with no replacement pointer via PATCH/POST, and can be created directly... | Fixed | r3-invariants |
| M70 | Dataset commit manifests accept nonexistent and cross-project note_ids | Fixed | r3-invariants |
| M71 | Any global editor can create, edit, delete and list supervision edges between arbitrary users | Fixed | r3-authz |
| M72 | Visualization metadata PATCH rewrites asset_* columns from a stale snapshot without the row lock the upload... | Fixed | r3-services, r4-locks, r5-followups |
| M73 | created_by list filters bind an arbitrary client string to a GUID column, turning a non-UUID value into an... | Fixed | r3-http |
| M74 | Lease-expiry reclaims are unbounded: max_attempts is enforced only when a worker reports failure | Fixed | r3-graph |
| M75 | Re-drafting a note after its draft was rejected returns the rejected change set instead of generating a new... | Fixed | r3-graph |
| M76 | Ownership reassignment leaves experiments, exploration nodes, provenance links, data stores, entity version... | Fixed | membership |
| M77 | Every outbox sync (including the post-commit hook) downloads the project's entire note list to build the ev... | Fixed | r3-client |
| M78 | `lt hpc finish --log` reads the whole log file into memory to extract a 4 KB tail | Fixed | r3-client |
| M79 | 1-second git timeout silently records a dirty working tree as clean (`git_dirty: False`) in repo, HPC and f... | Fixed | client, followups-a |
| M80 | Stale watch events are retried forever and consume the `--limit` budget, starving pending events on schedul... | Fixed | r3-client |
| M81 | No test asserts ORM metadata matches the Alembic head; 32 drift items exist and most unit tests run on crea... | Fixed | schema-parity |
| M82 | Three Postgres race tests lack the `postgres` marker and never run in CI | Fixed | release-code |
| M83 | uv.lock is stale relative to pyproject (mcp specifier) and CI/Docker never detect lock drift | Fixed | release-deploy |
| L1 | CI never builds the Docker image or validates render.yaml / the dedicated compose file | Fixed | low-deploy |
| L2 | CI gate `npm audit --audit-level=high` currently fails at HEAD on a dev-toolchain transitive advisory | Already fixed | low-deploy |
| L3 | docs/runs/ is listed in .gitignore while its review reports are tracked, so new run reports are silently sk... | Fixed | low-docs |
| L4 | AGENTS.md mandates unconditional `git push` at session end, contradicting its own Beads block and CLAUDE.md... | Fixed | low-docs |
| L5 | uv itself is unpinned in the Dockerfile (`pip install uv`) and in CI (`setup-uv@v7` with no version) | Fixed | low-deploy |
| L6 | Entrypoint runs `alembic upgrade head` unconditionally in every replica with no cross-process lock | Fixed | low-deploy |
| L7 | Windows launcher always exits 0 and closes the console on failure (%ERRORLEVEL% expanded at block parse time) | Fixed | low-deploy |
| L8 | Shared-provider overlay hard-codes the MCP healthcheck port to 8000 while the base compose lets LAB_TRACKER... | Already fixed | low-deploy |
| L9 | Root compose default LAB_TRACKER_DATABASE_URL hard-codes the lab_tracker password and does not follow POSTG... | Fixed | low-deploy |
| L10 | mcp container runs the app entrypoint, migrating an orphan SQLite database and minting auth secrets it neve... | Already fixed | low-deploy |
| L11 | configuration.md still says offline queued capture is deferred, while the feature is shipped and described... | Fixed | low-docs |
| L12 | daily-review-email-alerts.md embeds one operator's absolute macOS path and a named private deployment | Fixed | low-docs |
| L13 | Hosted demo is stale relative to main and has no build/deploy automation | Fixed | low-fe-core |
| L14 | Decision-context spec describes behaviour the implementation does not have (cross-project progress_review,... | Fixed | low-docs |
| L15 | MCP authoring spec 'Current State' understates the shipped provenance reads (claim provenance tool exists) | Fixed | low-docs |
| L16 | docs/read-opacity-inventory.md counts and suite list are stale relative to the test-enforced inventory (50... | Fixed | low-docs |
| L17 | Backup/restore commands hard-code Compose volume names that only hold when the checkout directory is named... | Fixed | low-docs |
| L18 | Documented dev install bypasses uv.lock and then hands control to lock-syncing `uv run` | Already fixed | low-deploy |
| L19 | `lt update` documentation omits `.gemini/settings.json` from the list of files it rewrites | Fixed | low-docs |
| L20 | Member-onboarding E2E test is not retry-safe despite CI retries being enabled | Fixed | low-fe-core |
| L21 | package-lock.json nests a stale playwright 1.61.1 under @playwright/test 1.62.1, so `npm ci` installs two P... | Already fixed | low-fe-core |
| L22 | Cache-busting hash ignores manifest.json, icons and app.css, which the service worker serves cache-first fo... | Fixed | low-fe-core |
| L23 | OpenAPI type generator drops `const`, widening required-true acknowledgement to boolean | Fixed | low-fe-core |
| L24 | Skill and MCP docs present deprecated username/password auth as the MCP setup, and the documented .mcp.json... | Fixed | low-docs |
| L25 | Skill and client-config prose still present deprecated username/password as the MCP credential; design doc... | Fixed | low-docs |
| L26 | Windows enumeration rejects NTFS-legal directory entry names and discards the whole scan | Deferred | low-files-platform |
| L27 | Helper modules carry dead entry points and near-duplicate traversal/open routines | Partly fixed | low-files-platform |
| L28 | acquisition_watcher.py duplicates file_watch.py helpers and carries an unused `_is_hidden_relative` | Fixed | low-files-platform |
| L29 | SQLite migrations run with transactional_ddl=False; only two revisions take the BEGIN IMMEDIATE fence, so a... | Already fixed | low-models-tests |
| L30 | notes.archived_by_user_id is declared as a SET NULL foreign key in the model but the migration created it a... | Already fixed | low-models-tests |
| L31 | Route-exposed facade commands bypass the documented once-per-call usage telemetry | Fixed | low-observability-routes |
| L32 | Server-side /app/share-target fallback drops the shared payload and redirects without the from-share=error... | Fixed | low-fe-core |
| L33 | Auth middleware and principal policies match on the un-stripped request path, so a root_path deployment bre... | Fixed | low-auth |
| L34 | Auth path gating uses request.url.path (root_path-prefixed) while admission middleware strips root_path, so... | Fixed | low-auth |
| L35 | A valid PAT is locked out for the whole window after ten policy-denied requests | Fixed | low-auth |
| L36 | /readiness and /metrics return raw SQLAlchemy exception text | Fixed | low-observability-routes |
| L37 | /readiness executes ten full-table COUNT(*) queries per probe instead of a cheap connectivity check | Fixed | low-observability-routes |
| L38 | /readiness and /metrics expose absolute server storage paths, raw database error strings and global entity... | Fixed | low-observability-routes |
| L39 | Local auth-disabled bootstrap swallows database errors with a warning instead of failing startup | Already fixed | low-auth |
| L40 | Provenance documents load every supervision edge in the database (all tenants) on each request | Fixed | low-services-perf |
| L41 | Search `include` filter is not validated: unknown tokens silently return empty results | Fixed | low-observability-routes |
| L42 | Git resolver cache defaults to a predictable directory in the shared system temp dir and honours pre-plante... | Fixed | r3-ops |
| L43 | Public /auth/register and /auth/login reveal whether a username exists | Partly fixed | low-auth |
| L44 | Invited registration over an already-registered username reports 'Invitation has already been used' while t... | Fixed | low-auth |
| L45 | Dead code: InvitationTokenService.consume_invitation_token and upload_security.size_limited_chunks | Fixed | low-auth |
| L46 | InvitationTokenService carries an unused HMAC secret and dead verify/consume/issue helpers; in-memory AuthS... | Partly fixed | low-auth |
| L47 | No administrative revocation or account disable: another user's device tokens and PATs cannot be revoked by... | Already fixed | low-auth |
| L48 | Descendant that escapes the process group leaves non-daemon reader threads blocked and delays interpreter s... | Fixed | low-files-platform |
| L49 | `lab-tracker seed-demo` writes the demo project into whatever database is configured, including hosted/auth... | Fixed | low-cli-client |
| L50 | Managed-block upsert discards all user content after an unpaired BEGIN marker (or before an unpaired END ma... | Fixed | low-cli-client |
| L51 | Settings repr/model_dump include all secrets except store_authority_grants_json; the Settings object is emb... | Fixed | low-observability-routes |
| L52 | Hosted deployment templates leave anonymous viewer self-registration enabled | Fixed | low-auth |
| L53 | Dolt mirror exports authentication tables (PAT/device/invitation token hashes, device enrollments, usage ev... | Fixed | low-cli-client |
| L54 | Metadata sidecar write is not atomic-with-cleanup: a failure after the data blob is renamed leaves an orpha... | Fixed | low-files-platform |
| L55 | app.css is served cache-first by the service worker but is excluded from both the precache list and the con... | Fixed | low-fe-core |
| L56 | Visualization 'Download asset' failure is an unhandled promise rejection with no user feedback; preview fet... | Fixed | low-fe-features |
| L57 | BatchReviewPage.loadBatches has no stale-response guard, so queues for a previously selected project can re... | Fixed | frontend |
| L58 | Client install commands hard-code the upstream GitHub repository regardless of which repository the server... | Deferred | low-fe-features |
| L59 | Cadence select cannot represent server-stored cadences other than 720/1440/10080, so the form displays a va... | Fixed | low-fe-features |
| L60 | Capture composer accessibility: mic control has no accessible name, attachment menu misuses aria-label, and... | Partly fixed | low-fe-features |
| L61 | Project graph re-runs full layout (with an O(n²) collision loop) on every edge hover and has no node cap | Partly fixed | low-fe-features |
| L62 | Question refactor form silently truncates parent/child/note options at 200 items | Fixed | low-fe-features |
| L63 | Question refactor submit has no in-flight guard, allowing duplicate POST /questions/{id}/refactor from a do... | Fixed | low-fe-features |
| L64 | Users page discards the typed replacement password even when the reset request fails | Fixed | low-fe-features |
| L65 | Local note drafts survive session expiry/rejection, so a shared machine offers one person's unsent text to... | Fixed | low-fe-features |
| L66 | Membership lookups silently downgrade the reviewer to read-only on any fetch error (no message), contrary t... | Fixed | low-fe-features |
| L67 | After a successful capture the Save button stays enabled with an empty composer and reports success without... | Fixed | low-fe-features |
| L68 | Mutation hooks clear the form and report failure based on the follow-up refresh, so a successful create can... | Fixed | low-fe-features |
| L69 | Dictation start leaves a dead MediaRecorder in the ref if recorder.start() throws, permanently disabling 'D... | Fixed | low-fe-features |
| L70 | A successful 2xx upload with a malformed envelope (ContractError) is misreported as an offline network fail... | Fixed | low-fe-core |
| L71 | Users and invitations pages silently truncate at 200 rows (no pagination, meta.total ignored) | Fixed | low-fe-core |
| L72 | Shares migrated while the user identity is unknown are enqueued with ownerId "" and become permanently quar... | Fixed | low-fe-core |
| L73 | Production bundle switches to fixture data and a fake signed-in session whenever ?demo=1 is present on any... | Fixed | low-fe-core |
| L74 | Global fetch stub leaks between tests because afterEach only calls vi.restoreAllMocks() | Fixed | low-fe-core |
| L75 | Store-health probes turn every exception into 'unreachable' with no server-side log | Fixed | low-observability-routes |
| L76 | GraphDraftClient protocol docstring still says Anthropic and Google are unimplemented | Fixed | low-docs |
| L77 | HTTP store-health probe follows redirects to any origin and reports HEALTHY for a root that resolution can... | Fixed | low-files-platform |
| L78 | LocalPathPolicy's realpath-based authorization surface is dead code, and the library composition path it fe... | Deferred | low-files-platform |
| L79 | next_questions silently truncates goals/questions/claims at 200 per request and ignores meta.total/has_more | Already fixed | low-models-tests |
| L80 | Admin delivery listing serialises live claim_token values | Fixed | low-email-groups-repo |
| L81 | LocalNoteStorage does not expand '~' in its configured path while LocalFileStorageBackend does | Fixed | low-files-platform |
| L82 | ProcessLock.__enter__ ignores a failed acquire and lets the `with` body run unlocked | Fixed | low-files-platform |
| L83 | Mermaid export escapes double quotes with a backslash, which Mermaid flowchart labels do not support | Fixed | low-models-tests |
| L84 | SMTP credentials are sent over a plaintext connection when tls_mode=none is combined with a username/password | Fixed | low-email-groups-repo |
| L85 | Idempotent-replay status codes are inconsistent across create endpoints | Fixed | low-observability-routes |
| L86 | GET /goals loads all visible goals and runs per-goal authorization queries before paginating in Python | Deferred | low-services-perf |
| L87 | PATCH /groups/{id}/members/{user_id} with an unknown user id returns 500 (unhandled IntegrityError) and ups... | Fixed | low-email-groups-repo |
| L88 | Note raw download reads the entire asset into memory (and base64-encodes it for JSON) instead of streaming... | Partly fixed | low-files-platform |
| L89 | GET /questions/{id}/refactors runs the full unbounded query a second time just to compute total | Fixed | low-observability-routes |
| L90 | Review-link route swallows every exception into a redirect, masking backend failures as invalid links | Fixed | low-observability-routes |
| L91 | Review-link redirect and username attachment swallow all exceptions, including database errors, silently | Fixed | low-observability-routes, integration |
| L92 | POST /review-email/test with an unknown recipient_user_id returns 500 (FK IntegrityError) instead of 422 | Fixed | low-email-groups-repo |
| L93 | Review-link opener swallows every exception, including infrastructure failures, without logging | Fixed | low-observability-routes |
| L94 | `provenance_base_url` fallback raises ValueError (HTTP 500) for /terms and JSON-LD provenance whenever the... | Fixed | low-observability-routes |
| L95 | Download filename header percent-decodes the stored filename, changing names that legitimately contain '%' | Fixed | low-files-platform |
| L96 | Dead and duplicated helpers in the route layer | Fixed | low-observability-routes |
| L97 | list_analyses(question_id=...) loads every dataset in the database to re-apply a filter the repository alre... | Fixed | low-services-perf |
| L98 | Unscoped GET /data-stores omits group-scoped stores that the caller can read and that the project-scoped li... | Already fixed | low-models-tests |
| L99 | Evidence-bundle upload_intent.size_bytes is not bounded by the server upload limit, so a committed bundle c... | Fixed | low-models-tests |
| L100 | Project deletion silently deletes spanning goals that still link other projects' entities | Deferred | low-services-perf |
| L101 | Batch reservation and scheduler tick load every note of the project into memory on each call | Fixed | low-services-perf |
| L102 | _source_notes_for_capture loads every project note (limit=None) and filters capture_bundle_id in Python on... | Fixed | low-services-perf |
| L103 | notification_email_confirmed_at is set without any confirmation; contributors can direct alerts to arbitrar... | Deferred | low-email-groups-repo |
| L104 | Scheduler tick loads every note of a due project and batch execution fetches source notes one by one | Fixed | low-services-perf |
| L105 | Group-read denial branch is untested (require_group_read 'Group access required.') | Fixed | low-models-tests |
| L106 | Global 'viewer' role is not enforced for interactive writes: a viewer with contributor membership can creat... | Fixed | low-auth |
| L107 | Deleting a lab group silently orphans child projects whose only owner access was inherited from the group | Fixed | low-email-groups-repo |
| L108 | Group owner-count check in delete_group_membership is not lock-protected, unlike project memberships | Already fixed | low-email-groups-repo |
| L109 | refactor_question mutates questions without recording entity versions | Fixed | low-services-perf |
| L110 | Deleting a question silently removes it from its children's parent lists with no guard | Already fixed | low-services-perf |
| L111 | record_export_service treats an empty project scope as 'no filter' with `if project_ids:`, inverting the em... | Fixed | low-services-perf |
| L112 | normalize_review_email is not idempotent for quoted local parts; stored address later fails validation and... | Fixed | low-email-groups-repo |
| L113 | promote_operational_session_to_dataset takes no session lock, so it can race delete_session and leave a dan... | Fixed | low-services-perf |
| L114 | Supervision edge create/update accepts a naive started_at with an aware ended_at (or vice versa) and crashe... | Fixed | low-models-tests |
| L115 | Data-store insert/clear_default discard the underlying database exception, so persistence failures are unre... | Fixed | low-email-groups-repo |
| L116 | Graph change-set hydration crashes when any attribution string (created_by/review_assignee/submitted_by/rev... | Fixed | low-email-groups-repo |
| L117 | provenance_links.list_by_project is used by the service layer but is not part of the repository contract | Fixed | low-email-groups-repo |
| L118 | Usage-event retention rollup and export load the entire pre-cutoff event set into memory and issue one SELE... | Fixed | low-services-perf |
| L119 | Published /terms vocabulary definitions cite example values that are not in the corresponding concept schemes | Fixed | low-models-tests |
| L120 | `lab_tracker_client.auth` needs `tomli` on Python 3.10 but it is only declared in the `test` extra | Fixed | low-deploy |
| L121 | Rejected profile token error blames `LAB_TRACKER_ACCESS_TOKEN` even when the token came from `~/.lab-tracke... | Fixed | low-cli-client |
| L122 | Profile `default_project_id` is applied even when the env base URL points at a different server than the pr... | Fixed | low-cli-client |
| L123 | Figure capture prints only the first failure ever; later, different failures are silent on stderr | Fixed | low-cli-client |
| L124 | `lt hooks install` interpolates lt path / base URL / project id into the sh hook without quoting | Fixed | low-cli-client |
| L125 | `lt repo install-hook` falls back to a bare `lt` and the hook then skips capture silently when it is not on... | Fixed | low-cli-client |
| L126 | Client git provenance helpers return '' on any git failure, so captures are recorded without a commit silently | Fixed | low-cli-client |
| L127 | Configuration doc-parity test never scans .env.example (suffix mismatch) and does not cover non-Settings en... | Fixed | low-docs |
| L128 | SQLite transaction-mode pin tests are skipped on every CI Python version | Fixed | low-models-tests |
| L129 | Unscoped GET /questions, /experiments, /exploration-nodes and /provenance-links have no non-member isolatio... | Fixed | low-models-tests |
| L130 | Mapper completeness guard only covers models named in sqlalchemy_mappers.py; 25 ORM models mapped elsewhere... | Fixed | low-models-tests |
| L131 | Tests that call create_app() without a database URL open ./lab_tracker.db in the developer's cwd | Fixed | low-models-tests |
