# Lab Tracker — Deep Codebase Review

Date: 2026-06-12 · Method: 20 parallel area reviewers + cross-cutting security/integrity/performance sweeps,
adversarial verification of every medium+ finding (critical/high findings: 3-lens majority panel —
correctness, exploitability, severity calibration), completeness critic, targeted gap round.
197 agents total. Severities are post-calibration for a small-lab, LAN-deployed tool.

**Stats:** 122 raw findings → 115 after dedup → 102 adversarially confirmed, 1 refuted, 31 low-severity passed through unverified.

## Quality gates

- **pytest:** PASS — 320 passed, 0 failed, in 149.20s (0:02:29). Note: the `uv` executable is not on PATH in this environment, so the suite was run via the project venv directly (C:\Users\snb6\Documents\GitHub\lab-tracker\.venv\Scripts\pytest.exe -q), which is the same interpreter/env `uv run` would use.
- **ruff:** PASS — "All checks passed!" (0 violations). Run via C:\Users\snb6\Documents\GitHub\lab-tracker\.venv\Scripts\ruff.exe check . for the same reason as above (uv not on PATH).
- **frontend:** PASS — npm run test:frontend (vitest run): 9 test files passed, 55 tests passed, duration 5.91s. npm run lint:frontend (eslint "src/lab_tracker/frontend_src/**/*.{js,jsx}"): exited cleanly with no reported violations. node_modules was already present; npm install was not needed.

## Confirmed findings

### HIGH (12)

#### H1. docker-compose ships a publicly-known auth signing secret as the production default, bypassing the placeholder check in config.py

`docker-compose.yml:15` · security · area: gap:deploy-pipeline

docker-compose.yml defaults LAB_TRACKER_AUTH_SECRET_KEY to 'replace-with-a-strong-secret' while defaulting LAB_TRACKER_ENVIRONMENT to 'production' (line 11) and publishing the app on all interfaces ("8000:8000", line 18). src/lab_tracker/config.py's startup validator (lines 51-59) only rejects the single literal DEFAULT_AUTH_SECRET_KEY = "dev-only-change-me", so the compose placeholder passes validation and becomes the live HMAC-SHA256 signing key for access tokens (src/lab_tracker/auth.py TokenService, lines 227-283). Anyone who can reach port 8000 and knows this repo can forge admin-role bearer tokens for any user id. The same problem applies to .env.example line 7 ('replace-with-a-strong-random-secret'), which also passes the check. The validator's whole purpose is to fail fast on placeholder secrets in non-local environments; the compose default defeats it.

**Fix:** Remove the fallback in compose so the variable is required (LAB_TRACKER_AUTH_SECRET_KEY: ${LAB_TRACKER_AUTH_SECRET_KEY:?set in .env}), and/or extend the config validator to reject a denylist of known placeholders (both 'replace-with-a-strong-secret' variants) or enforce a minimum entropy/length in non-local environments.

*Verification votes: real/high, real/high, real/high*

#### H2. Wheel/Docker image omits sw.js, manifest.json, and PWA icons — /app/sw.js returns 500 and PWA assets 404 in the deployed image

`pyproject.toml:44` · bug · area: gap:deploy-pipeline

Also independently reported as: Wheel/sdist packaging omits sw.js, manifest.json, and all icons — PWA breaks entirely on non-editable installs

[tool.setuptools.package-data] declares only four of the nine files in src/lab_tracker/frontend. The committed bundle directory contains app.css, app.js, icon-180.png, icon-192.png, icon-512.png, index.html, manifest.json, styles.css, and sw.js, and src/lab_tracker/app_parts/frontend.py serves all of them (StaticFiles mount at /app/static, plus a dedicated FileResponse route for /app/sw.js at lines 43-53). I built the wheel from this repo and its lab_tracker/frontend/ contains only app.css, app.js, index.html, styles.css. The Dockerfile (line 17) does a non-editable `uv pip install --system .`, and /app/src is not on sys.path at runtime, so the container serves from site-packages. Result in the supported Docker deployment: index.html (which is packaged) links /app/static/manifest.json, icon-180.png, icon-192.png — all 404; GET /app/sw.js hits Starlette FileResponse on a nonexistent path, which raises at response time → HTTP 500 on every request; service-worker registration, PWA install, offline capture, and the share-target flow all break. tests/test_frontend.py explicitly asserts these routes return 200, so the intent is clearly that they ship.

**Fix:** Replace the explicit list with a glob: lab_tracker = ["frontend/*"] (or enumerate all nine files). Add a packaging test or CI step that builds the wheel and asserts the frontend file set matches the source directory.

*Verification votes: real/high, real/high, real/high*

#### H3. Auth secret validator allows the hardcoded default key while auth is enabled, making admin tokens forgeable

`src/lab_tracker/config.py:51` · security · area: core-app

Also independently reported as: Default published auth secret accepted when auth is enabled in the 'local' environment, allowing token forgery

The model validator only rejects the committed default secret ('dev-only-change-me', config.py:8) when environment != 'local'. But auth can be enabled independently via LAB_TRACKER_AUTH_ENABLED=true while environment stays at its default 'local' (is_auth_enabled(), config.py:38-41). In that supported configuration, TokenService signs HS256 tokens with a secret that is public in the repo. Worse, any database that ever ran in auth-disabled mode contains an admin user at the fixed, publicly-known UUID 00000000-0000-4000-8000-000000000001 (auth.py:24, created by ensure_local_auth_user called from app_parts/runtime.py:48-52). An attacker can mint a token with sub=that UUID using the default key; the auth middleware then loads the user and grants role=admin from the DB row (app_parts/middleware.py:94-98). The docstring on ensure_local_auth_user claims this account 'cannot log in if auth is later enabled' — token forgery with the default secret bypasses login entirely.

**Fix:** Tie the check to auth being enabled rather than to the environment: raise whenever is_auth_enabled() and auth_secret_key == DEFAULT_AUTH_SECRET_KEY (regardless of environment). Optionally also refuse to verify tokens whose sub is LOCAL_AUTH_USER_ID when auth is enabled.

*Verification votes: real/high, real/high, real/high*

#### H4. SQLite foreign keys never enabled, so all FK and CASCADE rules are unenforced on the default backend

`src/lab_tracker/db.py:16` · data-integrity · area: integrity-sweep

The default database is SQLite (config.py:15 `database_url: str = "sqlite+pysqlite:///./lab_tracker.db"`), and SQLite ships with foreign_keys OFF per connection. get_engine()/_connect_args only set check_same_thread; there is no `PRAGMA foreign_keys=ON` event listener anywhere in src/ or tests/. The schema relies entirely on DB-level enforcement: db_models.py declares ondelete="CASCADE"/"SET NULL" on ~25 FKs and the ORM models define no relationships, so SQLAlchemy performs no ORM-level cascade either. Consequences on the default backend: (1) `delete_project` (services/project_service.py:101-106) deletes only the projects row, permanently orphaning every child row (questions, notes, datasets, sessions, claims, goals, memberships, graph change sets, batch settings) while the DELETE /projects route (routes/projects.py:118-160) still deletes their storage blobs after commit, leaving live note/dataset rows pointing at deleted files; (2) FK integrity is not validated at all, so dangling references can be inserted silently; (3) orphaned graph_draft_batch_settings rows for deleted projects make run_due_graph_draft_batches raise NotFoundError on every scheduled invocation. All tests also run on SQLite, so cascade behavior is effectively untested.

**Fix:** Add a connect-event listener for SQLite engines: `@event.listens_for(engine, "connect")` executing `PRAGMA foreign_keys=ON` (or use `listens_for(Engine, "connect")` keyed on dialect). Add a regression test that deletes a project on SQLite and asserts child rows are gone and FK violations raise.

*Verification votes: real/high, real/high, real/high*

#### H5. Decision context 'recent_activity' sections return the OLDEST entities, not the most recent

`src/lab_tracker/decision_context_use_case.py:252` · bug · area: mcp-decision

build_decision_context fetches each section with limit=resolved_limit (default 20, max 100) and labels the results 'recent_activity', and the response meta declares retrieval_policy 'explicit_links_then_search_then_recency'. But every repository list query orders ascending by created_at (e.g. questions: core.py:287 'stmt.order_by(QuestionModel.created_at, QuestionModel.question_id)'; same pattern for notes.py:130, sessions.py:57, datasets.py:118, analyses.py:160, claims (analyses.py:315), visualizations (analyses.py:427)), and pagination takes the FIRST limit rows. So in any project with more than `limit` rows in a section — the normal case for a long-lived project — the decision context contains the oldest, most stale entities and silently drops the newest work, which is exactly what a pre-decision context is supposed to surface. The truncation metadata reports counts but not that the kept sample is the oldest slice.

**Fix:** Have RepositoryDecisionContextReader fetch recency sections ordered by created_at DESC (add an order parameter to the repository queries or reverse via offset=total-limit), or rename the reason/policy if oldest-first is truly intended — currently the labels and behavior contradict each other.

*Verification votes: real/high, real/high, real/high*

#### H6. Stored auth token is destroyed on any /auth/me failure, including network errors — offline app launch signs the user out

`src/lab_tracker/frontend_src/hooks/useAuthSession.js:50` · bug · area: frontend-core

The boot effect calls apiFetch('/auth/me') and, in the catch, clears the token whenever the request fails for any reason. A fetch-level network error (offline, server briefly down) is indistinguishable here from a real 401: setToken("") runs, and the persistence effect at lines 24-30 then executes localStorage.removeItem(TOKEN_STORAGE_KEY), permanently discarding valid credentials. The service worker (frontend/sw.js) deliberately caches the app shell so /app/capture loads offline, and the upload queue exists precisely to capture while offline — but launching the PWA without connectivity now lands on the login form (auth.authEnabled defaults true, token gone), making offline capture impossible and forcing re-login even after connectivity returns.

**Fix:** Only clear the token when the failure is an auth rejection (err.status === 401 or 403 — apiFetch already sets error.status for server-rejected responses and leaves it undefined for network failures, the exact discriminator mobile-capture.jsx:610 already uses). On network failure, keep the token and surface a transient offline notice instead.

*Verification votes: real/high, real/high, real/high*

#### H7. Upload queue awaits IDBTransaction 'success' event that never fires, hanging offline capture and queue drain

`src/lab_tracker/frontend_src/shared/upload-queue.js:47` · bug · area: frontend-core

runRequest() resolves via request.onsuccess, but in createIndexedDbStorage it is also called with an IDBTransaction (`await runRequest(tx)` in add() at line 47 and remove() at line 67). IDBTransaction has no 'success' event (only complete/error/abort), so the promise never settles on the happy path. Consequences with the real IndexedDB adapter: (1) queue.enqueue() never resolves, so mobile-capture's queueRawFileNoteOffline (features/mobile-capture.jsx:594 `await queue.enqueue(...)`) suspends forever — the submit handler's finally never runs, busy stays true, and the user never sees the 'Capture queued' confirmation (the record IS persisted because the transaction auto-commits, so the UI hangs while data silently lands); (2) drain() hangs at `await adapter.remove(item.id)` after the first successful replay, so only one queued item is uploaded per 'online' event/boot, results/notify never fire, and the PendingUploadsBadge never refreshes; (3) migrateIncomingShares (shared/share-target-inbox.js:116 awaits uploadQueue.enqueue, and its own remove() at line 54 has the same `await runRequest(tx)` bug) never completes, so OS share-target captures are never confirmed or drained. All unit tests use the memory adapters (upload-queue.test.jsx uses createMemoryStorage exclusively), so this is untested in CI.

**Fix:** Add a dedicated transaction-completion helper: `function txDone(tx) { return new Promise((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error); }); }` and use it in upload-queue.js add()/remove() and share-target-inbox.js remove(). Add a test against a fake-indexeddb shim so the real adapter is exercised.

*Verification votes: real/high, real/high, real/high*

#### H8. Offline upload queue permanently deletes queued captures on any 4xx, including 401/408/429

`src/lab_tracker/frontend_src/shared/upload-queue.js:169` · data-integrity · area: frontend-features-b

drain() treats every 400-499 response as a permanent client rejection and removes the item from IndexedDB. But 401 (session/device token expired or revoked while the capture sat queued offline), 408, and 429 are retryable; re-auth or waiting would succeed. The queue is the durability layer for offline mobile captures (photos/voice notes) and OS share-sheet handoffs, so the user's only copy of the file is destroyed. Worse, every caller ignores the drain results: mobile-capture.jsx line 361 calls `queue.drain().catch(() => undefined)` and register-sw.js installOfflineRetry calls `queue.drain().catch(() => {})`, so the `rejectedStatus` diagnostic is never surfaced — captures vanish silently.

**Fix:** Only drop on statuses that are provably permanent (e.g. 400/404/413/422); keep 401/403/408/429 queued (optionally with a retry cap), and surface dropped items to the user (e.g. via the existing PendingUploadsBadge/subscribe channel) instead of discarding the diagnostics.

*Verification votes: real/high, real/high, real/high*

#### H9. Downloads crash with 500 for filenames containing non-latin-1 characters

`src/lab_tracker/routes/shared.py:119` · bug · area: routes-entities

safe_attachment_filename strips CR/LF, path separators, and double quotes but passes non-ASCII characters through unchanged. Starlette encodes response headers as latin-1, so building the Content-Disposition header for a filename like '实验数据.csv' raises UnicodeEncodeError — verified against the project venv (Starlette 1.0.0): Response(content=b'x', headers={'Content-Disposition': 'attachment; filename="实验数据.csv"'}) raises UnicodeEncodeError. There is no generic exception handler in routes/errors.py, so this surfaces as an unhandled 500. Uploads happily accept such filenames (notes.py:92, dataset_files.py:81 store file.filename verbatim), so a user can upload a unicode-named note or dataset file and then permanently get 500 from GET /notes/{id}/raw (notes.py:188-194) and GET /datasets/{id}/files/{file_id}/download (dataset_files.py:185-195).

**Fix:** Emit an RFC 5987/6266 header: an ASCII-safe fallback (e.g. percent-encoded or 'download') in filename="..." plus filename*=UTF-8''<percent-encoded> for the real name, or at minimum ASCII-fold/percent-encode the name in safe_attachment_filename so header encoding cannot fail.

*Verification votes: real/high, real/high, real/high*

#### H10. delete_analysis silently strips supporting evidence from SUPPORTED claims via DB cascade

`src/lab_tracker/services/analysis_service.py:175` · data-integrity · area: services-knowledge

delete_analysis performs no referential checks before deleting: committed ("immutable") analyses are deletable, and the claim_analyses link table has ondelete=CASCADE (db_models.py:565-577), so deleting an analysis silently removes supported_by_analysis_ids rows from every claim that cites it. A claim in status SUPPORTED whose sole evidence was that analysis remains SUPPORTED with zero support links — violating the invariant the service itself enforces everywhere else (_ensure_claim_support_links: "Supported claims require supporting datasets or analyses", shared.py:277-283). The visualization cascade is handled deliberately by the route (asset file cleanup in routes/analyses.py:133-150), but nothing guards the claim-evidence side. The research-record integrity this product exists to preserve is corrupted with no warning or audit trail.

**Fix:** In delete_analysis, query claims referencing the analysis (repository.query_claims(analysis_id=...)) and reject the delete (or require explicit detach) when any non-PROPOSED claim would lose its last support link; consider also blocking deletion of COMMITTED analyses to match the immutability rule in update_analysis.

*Verification votes: real/high, real/high, real/high*

#### H11. delete_question performs no referential checks; deleting a referenced question 500s on Postgres and corrupts committed datasets

`src/lab_tracker/services/question_service.py:376` · bug · area: services-entities

QuestionService.delete_question deletes unconditionally with no check for datasets or sessions that reference the question. Both datasets.primary_question_id and sessions.primary_question_id are declared without ondelete (db_models.py lines 144-148 and 439-442, mirrored in alembic/versions/0002_core_entities.py lines 55 and 81), so on an FK-enforcing backend (Postgres) the DELETE /questions/{id} request fails at the request-scope commit with an unhandled IntegrityError -> HTTP 500 (routes/errors.py registers no IntegrityError handler). Separately, dataset_question_links.question_id IS ondelete=CASCADE, so deleting a question that is a secondary link of a COMMITTED dataset silently strips a link row from a dataset the service elsewhere declares immutable ('Committed datasets are immutable.'), invalidating its content-addressed commit_hash. On the default SQLite runtime the delete instead leaves datasets/sessions pointing at a nonexistent question, which later breaks commit flows (update_dataset calls get_question(dataset.primary_question_id) -> NotFoundError on an unrelated PATCH).

**Fix:** In delete_question, query datasets and sessions referencing the question (and committed datasets holding it as a question link) and raise ValidationError/ConflictError before deleting; alternatively add ondelete behavior plus an explicit service-level guard for committed datasets.

*Verification votes: real/high, real/high, real/high*

#### H12. delete_question has no dependency guard; datasets.primary_question_id and sessions.primary_question_id FKs have no ondelete

`src/lab_tracker/services/question_service.py:376` · data-integrity · area: integrity-sweep

DELETE /questions/{question_id} (routes/questions.py:165-171) calls delete_question, which deletes the row with no check for dependent datasets or sessions. DatasetModel.primary_question_id (db_models.py:144-148) and SessionModel.primary_question_id (db_models.py:439-442) are NOT NULL / nullable FKs declared without ondelete. On Postgres, deleting a question that is the primary question of any dataset or session raises IntegrityError at commit time in the middleware (`request_scope.complete_response` -> `commit()`), which is unhandled (only routes/dataset_files.py catches IntegrityError) and surfaces as a 500 instead of a validation error. On the default SQLite backend (FK enforcement off, see separate finding) the delete silently succeeds, leaving datasets/sessions whose primary_question_id points at a nonexistent question; any later update_dataset commit path calls questions.get_question(dataset.primary_question_id) and fails with NotFoundError on an otherwise-valid dataset.

**Fix:** In delete_question, reject deletion with a ValidationError when datasets or sessions reference the question as primary (query datasets/sessions by primary_question_id), mirroring the guards used elsewhere; alternatively define an explicit ondelete policy and an IntegrityError-to-409 handler.

*Verification votes: real/high, real/high, real/high*

### MEDIUM (71)

#### M1. CI never runs the frontend test/lint/build pipeline and never checks the committed bundle for drift

`.github/workflows/ci.yml:7` · testing · area: gap:deploy-pipeline

The only CI job runs ruff and pytest on Python 3.11. package.json declares test:frontend (vitest), lint:frontend (eslint), and build (esbuild via scripts/build-frontend.mjs), and CLAUDE.md names them as required gates when frontend_src or the bundle changes — but no workflow step ever runs npm install or any of them. The served artifact is the committed bundle src/lab_tracker/frontend/app.js (478 KB minified, built by scripts/build-frontend.mjs); nothing verifies it was regenerated after frontend_src changes, so source/bundle drift (or a broken/unbuilt bundle) ships silently and Python tests against the stale bundle keep passing. Minor related gap: pyproject declares requires-python >=3.10 (pyproject.toml line 9) but only 3.11 is ever tested, so the 3.10 support claim is unverified.

**Fix:** Add a CI job: npm ci, npm run lint:frontend, npm run test:frontend, npm run build, then `git diff --exit-code src/lab_tracker/frontend` to fail on bundle drift. Optionally add 3.10 to the Python matrix or raise requires-python to >=3.11.

*Verification votes: real/high*

#### M2. CI installs the package editable only, so packaging and Docker-image regressions are invisible despite tests that cover them

`.github/workflows/ci.yml:19` · testing · area: gap:deploy-pipeline

CI runs `uv pip install -e ".[test,lint]"`, which serves frontend files straight from the source tree, so tests/test_frontend.py (which asserts /app/sw.js, /app/static/manifest.json, and all three icons return 200) passes in CI while the actual shipped artifact — the wheel installed non-editably in the Dockerfile — is missing those exact files (see the package-data finding). CI also never runs `docker build`, so Dockerfile breakage (install failure, entrypoint, missing assets) is only discovered at deploy time. The one failure mode that matters for deployment is structurally untestable under the current editable-only setup.

**Fix:** Add a CI step that builds the wheel, installs it into a clean venv, and runs the frontend-route tests against the installed package (or simply `docker build` the image and curl /app/sw.js and /app/static/manifest.json as a smoke test).

*Verification votes: real/high*

#### M3. Migrations 0014-0024 create many columns nullable that db_models.py declares NOT NULL (systemic schema drift)

`alembic/versions/0022_goals.py:28` · data-integrity · area: migrations

Every migration from 0014 onward creates columns as nullable=True that the ORM models declare non-optional (nullable=False). Examples: goals.status/summary/attributes/created_at/updated_at (0022 lines 27-34) vs GoalModel (db_models.py lines 610-621, e.g. `status: Mapped[str] = mapped_column(String(20), default="planned")`); goal_links.link_status (0022 line 50) vs db_models.py line 650; graph_change_sets.created_at/updated_at/summary (0014 lines 31-32, 0015 line 34) vs db_models.py lines 287, 294-299; question_refactors snapshots/created_at (0016 lines 58-62); project_memberships.created_at/updated_at (0018 lines 26-27); graph_draft_batch_runs.started_at/summary and graph_draft_batch_settings.created_at/updated_at (0024 lines 48-49, 73-78). Production databases are built only from migrations (no create_all in src/lab_tracker), while the test suite builds schemas from Base.metadata.create_all (tests/api_helpers.py:23, tests/test_persistence_flows.py:103, ...), so tests exercise a stricter schema than production. Rows that acquire NULLs in production (raw SQL, the Dolt mirror, future bulk imports, or a bug bypassing the mappers) crash on read: goal_from_model does `status=GoalStatus(row.status)` (sqlalchemy_mappers.py:703) and `_as_utc(row.created_at)` (line 709), where _as_utc (lines 86-89) raises AttributeError on None. It also means `alembic revision --autogenerate` emits a wall of spurious nullability diffs, masking real changes.

**Fix:** Add a tightening migration that backfills NULLs (status='planned', summary='', timestamps from a sentinel or now()) and alters these columns to NOT NULL so the migrated schema matches Base.metadata, mirroring what 0023 already did for goal_links.slot. Going forward, write new columns with the same nullability as the model (early migrations 0001-0010 did this correctly).

*Verification votes: real/high*

#### M4. retained-v1-surface.md omits whole shipped workflows while declaring everything unlisted out-of-surface

`docs/retained-v1-surface.md:31` · docs · area: docs-consistency

The doc declares itself "the single source of truth" and states that anything not listed is "out of the retained v1 surface and should not shape the default runtime, supported docs, or simplified architecture." But the default runtime now registers several first-class workflows the doc never mentions: goals and goal links (src/lab_tracker/routes/goals.py, migrations 0022_goals.py and 0023_goal_link_slot_not_null.py), scheduled graph-draft batches (src/lab_tracker/routes/graph_batches.py, migration 0024_graph_draft_batches.py), device enrollment/auth (src/lab_tracker/routes/device_auth.py, migration 0017_device_tokens.py), the project graph view (src/lab_tracker/routes/project_graph.py), the assistant decision-context endpoint (src/lab_tracker/routes/assistant.py), and provenance export routes (src/lab_tracker/routes/provenance.py). All are wired into register_routes() in src/lab_tracker/routes/__init__.py lines 48-66 and have frontend features (features/goals, features/batches.jsx, features/devices.jsx, features/enroll.jsx, features/project-graph.jsx). README's "Supported workflows in the frontend" list (README.md lines 116-124) is stale the same way. Since other docs and agents are instructed to treat this file as overriding everything else, the omissions actively mislead.

**Fix:** Update the Decision list in docs/retained-v1-surface.md to enumerate the now-shipped workflows (goals/goal links, scheduled graph-draft batches, device enrollment, project graph view, assistant decision context, provenance export), and refresh the README frontend-workflow list to match.

*Verification votes: real/high*

#### M5. Surface doc says graph drafting is "explicitly on-demand and note-scoped; not a standing extraction inbox", but scheduled cadence-based batch drafting ships in the default runtime

`docs/retained-v1-surface.md:47` · docs · area: docs-consistency

The Deferred Workflows section asserts "The retained image-to-graph draft action is explicitly on-demand and note-scoped; it is not a standing extraction inbox." The runtime now ships a scheduled batch-drafting workflow that is neither on-demand-only nor note-scoped: per-project GraphDraftBatchSettings carry cadence_minutes (default 24*60), run_at_local_time, timezone_name, and next_run_at (src/lab_tracker/db_models.py line 368, src/lab_tracker/services/graph_draft_service.py lines 340-375); run_graph_draft_batch_for_project (graph_draft_service.py lines 377-465) sweeps all staged notes in a time window into one batch draft; POST /batches/run-due executes every due project (routes/graph_batches.py lines 161-169); GraphDraftBatchTrigger defaults to SCHEDULED (src/lab_tracker/models.py line 351); and the frontend exposes a pending-batch review queue with a Cadence settings panel (frontend_src/features/batches.jsx). This is functionally the standing extraction inbox the doc claims was deferred. Either the doc's deferral language is stale or the feature contradicts the declared contract — both readings require a maintainer to reconcile the doc.

**Fix:** Amend the Deferred Workflows bullet to describe the actual retained shape: human-gated batch drafting over staged-note windows with user-set cadence and explicit run-now/run-due triggers, distinguishing it from the deferred automatic-extraction inbox.

*Verification votes: real/high*

#### M6. No dependency lock file anywhere in the pipeline — CI and every Docker rebuild float to the newest releases

`pyproject.toml:10` · architecture · area: gap:deploy-pipeline

Runtime dependencies are almost entirely unpinned (alembic, fastapi, pydantic-settings, python-multipart, uvicorn have no constraints at all; the rest are lower-bound only), and the repo has no uv.lock, requirements.txt, or pip-compile output. The Dockerfile resolves dependencies fresh at every build (`uv pip install --system .`), and CI resolves fresh on every run, so the tested environment and the shipped image are both unreproducible: a new FastAPI/SQLAlchemy/pydantic release can break the image with no repo change, and the image CI implicitly validated is never the image that gets built later. npm dependencies are at least range-pinned via package.json, but there is no committed package-lock.json either (npm ci is impossible).

**Fix:** Commit a lock file (uv lock + `uv sync --frozen` in CI, and `uv pip install --system -r` a compiled requirements file or `uv sync` in the Dockerfile), and commit package-lock.json so CI can use npm ci.

*Verification votes: real/high*

#### M7. serve-lan.ps1 binds 0.0.0.0 but does nothing when auth is disabled — the default invocation serves the whole graph unauthenticated to the LAN

`scripts/serve-lan.ps1:101` · security · area: gap:deploy-pipeline

The script's sole purpose is LAN/VPN exposure (it binds uvicorn to 0.0.0.0 and prints firewall-opening instructions), yet it never verifies that authentication is actually on. With no .env and no environment variables set — the literal default invocation `.\scripts\serve-lan.ps1` — Settings.environment defaults to 'local' (src/lab_tracker/config.py line 14), so is_auth_enabled() returns False (config.py lines 38-41) and every endpoint is reachable without credentials from any machine on the LAN. The script only prints an advisory line ('keep authentication enabled and set LAB_TRACKER_AUTH_SECRET_KEY', line 81) and proceeds regardless. docs/lan-shared-graph.md (line 12: 'Keep authentication enabled when serving beyond one trusted local machine') makes clear unauthenticated LAN serving is not the intended design — but the helper's default path produces exactly that, silently. Plain HTTP with bearer tokens and the hardcoded local-Postgres credential (line 25) match the documented LAN design and are not flagged.

**Fix:** Before starting uvicorn, evaluate the effective settings (e.g. run a one-liner against lab_tracker.config.get_settings()) and refuse to bind 0.0.0.0 when auth resolves to disabled, unless an explicit -AllowUnauthenticated switch is passed.

*Verification votes: real/high*

#### M8. Hidden-file check inspects the entire absolute path, silently skipping all files under dot-named ancestors

`src/lab_tracker/acquisition_watcher.py:33` · bug · area: storage

_is_hidden checks every component of the path, and _iter_files applies it to the full candidate path (watch root joined with the matched file). If the user passes a watch path that lives anywhere under a dot-prefixed directory (e.g., C:\Users\x\.instrument\runs, or ~/.local/share/... on Linux), every single file is classified hidden and the watcher silently registers nothing — no error, no log. The intent is clearly to skip dotfiles inside the watched tree, not to disqualify the explicitly chosen root. Note also that on Windows (the project's platform) hidden files are marked by attribute, not dot prefix, so the heuristic both over-matches ancestors and under-matches actual hidden files.

**Fix:** Evaluate hidden-ness only on the path relative to the watch root (candidate.relative_to(root)), so explicitly configured roots under dotted directories still work.

*Verification votes: real/high*

#### M9. No browser security headers (nosniff, CSP, X-Frame-Options, Referrer-Policy) on app shell or any response

`src/lab_tracker/app_parts/frontend.py:65` · security · area: gap:upload-serving-xss

The application adds no security response headers anywhere. A grep of the entire backend for X-Content-Type-Options, Content-Security-Policy, X-Frame-Options, Referrer-Policy, and Strict-Transport-Security returns zero matches; there is no header-adding middleware in middleware.py and no FastAPI add_middleware in app.py. The /app HTML shell is served with only cache-control headers, the /app/static mount adds none, and all three file-download responses set only Content-Disposition and Content-Length. Concretely the missing headers would each block a real issue here: (1) X-Content-Type-Options: nosniff would stop MIME-sniffing of the attachment-served dataset/note downloads and reinforce the visualization download; (2) a Content-Security-Policy such as default-src 'self'; script-src 'self' on the /app shell and download responses would neutralize the stored-XSS primitive above even if a malicious content type slips through; (3) X-Frame-Options: DENY / frame-ancestors 'none' would prevent clickjacking the /app shell that authenticates with a localStorage token.

**Fix:** Add a small response-header middleware (or set headers on the relevant responses) emitting X-Content-Type-Options: nosniff on all responses, a restrictive Content-Security-Policy on the /app shell and download endpoints, X-Frame-Options: DENY (or frame-ancestors 'none'), and Referrer-Policy: no-referrer. Apply nosniff in particular to every file-download response.

*Verification votes: real/high*

#### M10. All middleware DB work runs synchronously on the event loop, serializing every concurrent request

`src/lab_tracker/app_parts/middleware.py:77` · performance · area: gap:asgi-lifecycle

Both middlewares are BaseHTTPMiddleware (`@app.middleware("http")`) async dispatchers, so their bodies run on the event loop, not the threadpool. For every authenticated request, auth_middleware performs blocking SQLAlchemy I/O inline: device tokens cost a SELECT + an UPDATE/COMMIT (verify_device_token) + a SELECT (get_user_by_id) = 3 blocking round trips; JWT requests cost 1 (get_user_by_id at line 95). db_session_middleware then calls request_scope.complete_response(response), which runs session.commit()/rollback() (api.py:516-532 -> sqlalchemy_repository_parts/repository.py:75-79) — also on the loop, for every request, since db_session_middleware is registered last and is therefore outermost (app.py:30-31), wrapping even static-asset and 401-rejected requests. Sync route handlers run in the threadpool, but no request reaches it until the loop-blocking auth completes, so N concurrent requests serialize on these calls: total added latency is about N x (auth queries + commit), and any single slow DB call (e.g. a SQLite write waiting up to pysqlite's default 5s busy timeout) freezes the entire server, including in-flight StreamingResponse body pumping which shares the loop. The AuthService/DeviceAuthService sessions themselves are reliably closed (`with self._session_factory() as session:` in auth.py:166, 483) so no leak, but they add 1-2 extra connection checkouts per request on top of the request session. One traced detail worth knowing: the session is committed and closed as soon as call_next yields response headers, i.e. before StreamingResponse bodies (routes/dataset_files.py:191, routes/visualizations.py:198) are iterated — safe today only because storage_backend.iter_chunks reads from disk and never touches the ORM session.

**Fix:** Offload the blocking sections with anyio.to_thread.run_sync (e.g. `principal = await anyio.to_thread.run_sync(verify_device_token, token)` and likewise for get_user_by_id and complete_response), or replace BaseHTTPMiddleware with pure-ASGI middleware that defers DB work to the threadpool. Longer term, cache user lookups for the JWT path (the role is already in the verified claims).

*Verification votes: real/high, real/high, real/high*

#### M11. verify_device_token issues a write transaction (last_used_at + COMMIT) on every device request

`src/lab_tracker/auth.py:489` · performance · area: gap:asgi-lifecycle

Every request authenticated with a device token mutates last_used_at and commits before the request proper even starts. On the default SQLite runtime this takes the database-wide write lock once per request, contending with the main request transaction and with other devices; because it executes inside auth_middleware on the event loop (middleware.py:77), a lock wait here stalls the whole server, and a lock failure raises OperationalError out of the middleware as a plain 500. It also doubles the write volume of read-only GETs from paired devices (the supported phone-PWA capture flow polls via GET).

**Fix:** Throttle the freshness write: only update last_used_at when it is null or older than some window (e.g. 5-15 minutes), turning the hot path into a read-only SELECT. The column only feeds the device-management UI, so coarse granularity is fine.

*Verification votes: real/high*

#### M12. Default JWT signing secret accepted when auth is enabled in the 'local' environment, allowing token forgery

`src/lab_tracker/config.py:43` · security · area: security-sweep

Settings._validate_auth_secret_key only rejects the hard-coded default secret when the environment is NOT local: the guard is `if not is_local and self.is_auth_enabled() and self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY`. But is_auth_enabled() returns True whenever LAB_TRACKER_AUTH_ENABLED is explicitly set to true, regardless of environment (config.py:38-41). So a deployment running with LAB_TRACKER_ENVIRONMENT=local AND LAB_TRACKER_AUTH_ENABLED=true (a supported way to exercise real auth/RBAC on a LAN) constructs TokenService with the published constant DEFAULT_AUTH_SECRET_KEY = 'dev-only-change-me' (config.py:8, runtime.py:56). verify_access_token (auth.py:254-283) trusts any token whose HMAC-SHA256 signature matches that key, so anyone who knows the source-code default can forge a JWT for any user_id and role -- including {"role":"admin"} -- and bypass authentication entirely.

**Fix:** Reject the default secret whenever auth is enabled, irrespective of environment: change the validator condition to `if self.is_auth_enabled() and self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY: raise ValueError(...)`.

*Verification votes: real/high*

#### M13. Foreign keys are never enforced on the default SQLite runtime, so service deletes orphan all child rows

`src/lab_tracker/db.py:22` · data-integrity · area: services-entities

Also independently reported as: SQLite foreign keys never enabled, so all ON DELETE CASCADE/SET NULL rules are silently ignored on the default runtime

Every cascade in the schema is expressed as ondelete="CASCADE" on FK columns (db_models.py); there are no ORM relationship cascades, and repository delete is a plain session.delete(row) (sqlalchemy_repository_parts/common.py lines 92-98). The default database is sqlite+pysqlite (config.py line 15: database_url: str = "sqlite+pysqlite:///./lab_tracker.db"), and get_engine never installs a PRAGMA foreign_keys=ON connect listener — a repo-wide grep for 'foreign_keys' returns nothing. pysqlite leaves FK enforcement OFF per connection, so on the default runtime ProjectService.delete_project orphans every question/note/session/dataset/membership in the project (they remain visible to admins in global list endpoints), SessionService.delete_session orphans acquisition_outputs, and the DELETE /datasets/{id} route deletes the file storage blobs after commit while the dataset_files rows survive, pointing at deleted storage. The entire test suite runs against this non-enforcing configuration, so none of the cascade behavior is actually exercised.

**Fix:** Add an engine connect event listener that executes PRAGMA foreign_keys=ON for sqlite URLs (and a test asserting cascades fire), or implement the cascades explicitly in the service delete methods so behavior does not depend on backend pragmas.

*Verification votes: real/high, real/high, real/high*

#### M14. Deleting a question referenced as a dataset/session primary_question_id is unguarded: 500 on Postgres, dangling reference on SQLite

`src/lab_tracker/db_models.py:144` · data-integrity · area: repo-layer

datasets.primary_question_id (db_models.py 144-148) and sessions.primary_question_id (439-442) are the only FKs to questions without an ondelete rule (confirmed in alembic/versions/0002_core_entities.py lines 55 and 81). question_service.delete_question (lines 376-381) deletes with no referential check, and the only IntegrityError handler in the codebase is in routes/dataset_files.py. On the documented multi-client Postgres runtime, DELETE /questions/{id} for a question that is any dataset's or session's primary question raises an unhandled ForeignKeyViolation -> HTTP 500. On the default SQLite runtime (FKs unenforced, verified: the delete 'COMMITTED (no FK error)'), the dataset keeps a dangling primary_question_id; dataset_service.update_dataset line 195 then calls self.questions.get_question(dataset.primary_question_id) which raises NotFound, making the dataset un-updatable, and dataset_from_model fabricates a QuestionLink to the nonexistent question.

**Fix:** Add a service-level guard in delete_question that rejects deletion (409/validation error) while datasets or sessions reference the question as primary, or define an explicit ondelete policy via a migration; also map IntegrityError to a 409 at the API boundary.

*Verification votes: real/high, real/high, real/high*

#### M15. Scoped users with 2+ project memberships get spurious anchor_not_found when passing an entity anchor without project_id

`src/lab_tracker/decision_context_query.py:26` · bug · area: mcp-decision

When a non-admin actor is a member of two or more projects, accessible_project_ids is a non-None set and the route only auto-resolves project_id when the set has exactly one element (routes/assistant.py:35-36). If such a user calls decision-context with question_id/dataset_id/analysis_id/claim_id/visualization_id but no project_id, build_decision_context looks up the anchor via reader.list_questions(project_id=None, ...). RepositoryDecisionContextReader._project_allowed(None) returns False whenever the accessible set is non-None, so the reader returns an empty list and the use case responds anchor_not_found ('Question ... was not found') even though the entity exists and the user can read it. The global-search fallback handles the scoped project_id=None case correctly (it iterates accessible projects), so anchors are clearly intended to work without project_id.

**Fix:** When project_id is None and the actor is scoped, iterate the accessible project ids for anchor lookups (as reader.search already does), or resolve anchors with direct get-by-id calls followed by an access check on the entity's project_id.

*Verification votes: real/high, real/high, real/high*

#### M16. Anchor resolution scans a 500-row ascending window instead of get-by-id; existing anchors beyond the window are reported not found

`src/lab_tracker/decision_context_use_case.py:57` · bug · area: mcp-decision

Every anchor (project, question, dataset, analysis, claim, visualization) is resolved by listing up to CONTEXT_LOOKUP_LIMIT=500 rows and scanning client-side with find_by_id. Because the underlying queries order ascending by created_at, the NEWEST entities — the ones agents most likely anchor on — fall outside the window first. A project with >500 questions (or a deployment with >500 projects) makes valid IDs return 'anchor_not_found'. The visualization anchor additionally resolves its project by scanning up to 500 analyses (lines 164-176) and silently skips project attribution when the analysis is outside the window, pushing the request into the ambiguous_project path. This also costs five full 500-row fetches with complete entity hydration per request even when anchors are present.

**Fix:** Add get-by-id methods to the DecisionContextReader protocol (the API layer already has api.get_question etc.) and resolve anchors directly, applying the project-access check to the returned entity's project_id.

*Verification votes: real/high*

#### M17. Decision context embeds full note bodies with no per-field truncation — response size is unbounded

`src/lab_tracker/decision_context_use_case.py:329` · performance · area: mcp-decision

The MCP tool promises 'bounded graph context', but the bound is item count only. The notes section merges up to 2x resolved_limit full note dicts (search matches plus recent activity), and Note.raw_content / transcribed_text are unbounded strings (models.py:433-435; NonBlankStr at schemas.py:63 has min_length=1 and no max). Notes are the entity designed to hold full transcripts, so a handful of long notes produces a decision-context payload of hundreds of KB injected straight into an agent's context window. entity_label already truncates note text to 120 chars for labels, but the full entities are returned verbatim alongside seven other sections.

**Fix:** Project note (and claim/question) text down to a bounded excerpt in the decision-context payload (e.g. first N chars plus note_id for follow-up reads), or add a per-response byte budget with truncation flags.

*Verification votes: real/high*

#### M18. Project auto-resolution treats a truncated, order-biased global search as exhaustive and can silently pick the wrong project

`src/lab_tracker/decision_context_use_case.py:192` · bug · area: mcp-decision

When no project or anchor resolves a project, the use case runs one global search capped at resolved_limit (default 20) and auto-selects a project iff exactly one project id appears in the sample. For unscoped actors the search returns the oldest `limit` matches (ascending created_at), so if an older project saturates the sample, matches in newer projects are invisible and the context silently resolves to the old project with no ambiguity flag. For scoped actors the reader concatenates per-project results in project-UUID sort order and then slices `questions[offset : offset + limit]`, so projects sorted later are cut first — same false-uniqueness effect, with the added quirk that offset is applied after per-project queries that each used offset=0.

**Fix:** Disambiguate on distinct project ids rather than raw item samples — e.g. aggregate match counts per project (GROUP BY project_id) or only auto-resolve when the search meta shows the sample was not truncated.

*Verification votes: real/high*

#### M19. Dolt mirror never propagates schema changes; first migration after table creation breaks or silently diverges the sync

`src/lab_tracker/dolt_mirror.py:119` · data-integrity · area: storage

Tables are created in dolt once with `table import -c --pk=...`; every subsequent export uses `table import -r`, which replaces rows but preserves the dolt table's original schema. The live schema is owned by Alembic and changes regularly (this repo adds migrations frequently). After a column is added, the exported CSV no longer matches the frozen dolt schema and the import either errors (raising DoltMirrorError and aborting every future export until someone manually drops the dolt table) or drops the new column, leaving the mirror silently missing data. Renamed/dropped columns break the same way. Tables removed from the model are also never deleted from the mirror, so retired tables persist with stale data in every subsequent commit.

**Fix:** Detect schema drift and recreate the table (drop + import -c) when the CSV header or primary keys differ from the dolt schema, or use dolt's schema-update import mode; also drop dolt tables that are no longer in retained_tables().

*Verification votes: real/high, real/high, real/high*

#### M20. No upload content-type validation or allowlist on any upload route or storage backend

`src/lab_tracker/file_storage.py:138` · security · area: gap:upload-serving-xss

None of the three upload routes (visualizations, dataset files, notes) nor either storage backend validates or restricts the uploaded content type. All three routes accept the client-supplied multipart content_type verbatim and the storage backends only check that filename/content_type are non-empty before persisting raw bytes. This means text/html, image/svg+xml, application/xhtml+xml and other active types are stored unchanged and later echoed back as the response Content-Type, which is the root cause of the inline stored-XSS in the visualization download route and weakens the attachment-served dataset/note downloads (which rely entirely on the browser honoring 'attachment' since nosniff is also absent — see separate finding). store_stream/store inspect only emptiness, never the byte signature or an allowlist.

**Fix:** Add an allowlist (or denylist of active types) at the upload boundary and/or sniff the leading bytes to confirm the declared type. Reject or normalize HTML/SVG/XML uploads, or store a server-derived safe content type rather than trusting file.content_type.

*Verification votes: real/high*

#### M21. Cache-version coordination is unenforced and has already drifted: CACHE_VERSION v16 vs asset ?v=17

`src/lab_tracker/frontend/sw.js:12` · bug · area: gap:pwa-shell-sw

Also independently reported as: app.css is referenced by index.html but absent from the SW precache, so the offline shell loads unstyled

Cache invalidation requires bumping three things in lockstep: the ?v= in index.html (lines 14, 16), the ?v= in SHELL_ASSETS (sw.js:16-17), and CACHE_VERSION (sw.js:12). Nothing enforces this: scripts/build-frontend.mjs only writes app.js, and tests/test_frontend.py:49 asserts only the substring "/app/static/app.js" appears in the SW body — no version comparison anywhere. The drift has already happened: commit 5400a1b bumped ?v= to 17 in both index.html and sw.js but left CACHE_VERSION at v16, so the old ?v=16 entries sit forever in the still-named lab-tracker-shell-v16 cache (the activate handler at sw.js:42 only deletes caches with a *different* name; cache.addAll never removes stale keys). The latent failure mode is worse: regenerating app.js without bumping the ?v= leaves sw.js byte-identical, so the browser never re-installs the SW, and the cache-first /app/static handler (sw.js:82-96) serves the stale bundle to every returning user indefinitely — registration.update() (register-sw.js:60) is a no-op when bytes are unchanged.

**Fix:** Single-source the version: have build-frontend.mjs (or a small script) inject one version string into index.html and sw.js (including CACHE_VERSION), or add a test that parses the ?v= values from index.html and sw.js and asserts they match each other and CACHE_VERSION.

*Verification votes: real/high, real/high, real/high*

#### M22. Text/URL-only shares are silently discarded: manifest advertises title/text/url but the SW persists only file entries and never reads 'url'

`src/lab_tracker/frontend/sw.js:105` · data-integrity · area: gap:pwa-shell-sw

manifest.json (lines 29-39) declares share_target params title, text, url, and files — so the OS offers Lab Tracker as a target for plain text/link shares (e.g. sharing a URL from a browser), not just images/audio. handleShareTarget reads formData fields 'file', 'title', 'text' (sw.js:102-104) — the 'url' field is never read at all — and the only persistence is inside the for-loop over files. A share with no file stores nothing, then redirects to /app/capture as if it succeeded. The page side reinforces the loss: share-target-inbox.js:108-110 deletes any inbox record lacking .file. So link/text shares vanish with no error and no trace.

**Fix:** Either remove title/text/url from manifest params so the OS only offers Lab Tracker for file shares, or store a file-less record (title/text/url) in the inbox and have migrateIncomingShares turn it into a text capture instead of deleting it.

*Verification votes: real/high*

#### M23. Share-inbox write failure is swallowed with a redirect; loss is invisible despite comment claiming otherwise, and nothing consumes from-share=1

`src/lab_tracker/frontend/sw.js:117` · data-integrity · area: gap:pwa-shell-sw

If storeIncomingShare rejects (IndexedDB quota, private-mode restrictions), handleShareTarget logs console.warn — invisible to a phone user — and still issues the same success-looking 303 to /app/capture?from-share=1. The inline comment claims "The lost-share case is rare and visible", but it is not: a repo-wide grep shows the from-share=1 query param appears only at sw.js:122 — no page code reads it, so the app cannot distinguish a successful share landing from a failed one, and migrateIncomingShares simply finds an empty inbox. The server-side fallback in app_parts/frontend.py:55-63 has the same silent-loss shape (file dropped, plain 303), which its comment at least acknowledges. The user's photo/recording is gone with zero feedback.

**Fix:** On failure redirect with a distinct param (e.g. ?from-share=error) and have the capture page read from-share and show a toast — success confirmation when the inbox migration yields 0 items after from-share=1, and an explicit error message on the failure param.

*Verification votes: real/high*

#### M24. Navigation fallback only triggers on network rejection; 5xx responses bypass the cached shell

`src/lab_tracker/frontend/sw.js:74` · bug · area: gap:pwa-shell-sw

The /app/* navigation handler uses fetch(request).catch(...). .catch fires only when the fetch promise rejects (no network); an HTTP error response — 502/503/504 from a reverse proxy while the backend is down or restarting — resolves normally and is rendered as-is. The user sees the proxy error page even though a fully functional cached shell (with the page-side offline upload queue) is sitting in the cache. For an app whose stated purpose is capturing notes when connectivity is unreliable, backend-down-but-proxy-up is exactly the situation the cached shell should cover, and it doesn't.

**Fix:** Check the response status and fall back to the cached shell for 5xx navigations: fetch(request).then((r) => r.ok || r.status < 500 ? r : caches.match("/app").then((c) => c || r)).catch(...).

*Verification votes: real/high*

#### M25. Graph-draft review permissions derived from workspace-selected project, not the draft's project

`src/lab_tracker/frontend_src/app-shell.jsx:431` · bug · area: frontend-features-b

GraphDraftDetailCard (routes graph-draft and batch) receives canWrite={canContributeToProject} and canManageGraph={canManageProjectMembers}, both computed from the membership role in workspaceData.selectedProjectId (app-shell.jsx lines 99-108). A draft/batch can belong to a different project: PendingBatchBanner lists batches across all accessible projects (GET /batches with no project_id) and navigates straight to /app/batches/{id}. A user who is a contributor/owner of the batch's project but only a viewer of the currently selected project gets every Accept/Reject/Save/Submit/Commit control disabled (graph-drafts.jsx lines 202-208) with no explanation; conversely an owner of the selected project viewing another project's draft sees enabled controls that fail with backend 403s.

**Fix:** Inside GraphDraftDetailCard, fetch the viewer's membership for changeSet.project_id (e.g. /projects/{id}/members or a permissions field on the change-set payload) and gate the controls on that, instead of inheriting the workspace-selected project's role.

*Verification votes: real/high*

#### M26. Partial failure in multi-step capture upload re-uploads already-created notes, producing duplicates

`src/lab_tracker/frontend_src/features/mobile-capture.jsx:796` · bug · area: frontend-features-b

uploadCapture performs up to three sequential server calls (photo upload, voice upload, voice transcript) but only persists progress via setUploadedNoteId(noteId) after the whole block succeeds. If the photo POST succeeds and the voice POST (or the transcript POST at line 772, which depends on an external transcription provider) then fails with a server error, the catch at line 830 shows a flash and leaves photoFile/audioFile set with uploadedNoteId still empty. Retrying the send button re-runs uploadOrQueueRawFile for files that were already uploaded, creating duplicate notes in the project. Additionally, the retry path at lines 802-808 unconditionally re-POSTs /notes/{id}/transcript whenever uploadedVoiceNoteId is set, re-running paid transcription and overwriting any transcript the user edited in the meantime.

**Fix:** Persist each successful step immediately (e.g. setUploadedNoteId/setUploadedVoiceNoteId as soon as each note is created and skip re-upload of pieces that already have IDs), and skip the retry transcript POST when the note already has a transcript.

*Verification votes: real/high*

#### M27. Project-graph layout collision loop is quadratic-plus per layer and ReactFlow renders all nodes unvirtualized on unbounded full graphs

`src/lab_tracker/frontend_src/features/project-graph.jsx:236` · performance · area: frontend-features-b

computeNodePositions resolves vertical collisions with a linear scan (`used.some`) inside a while loop inside the per-node forEach: when k nodes in one layer anchor to the same average y (e.g. many datasets/sessions linked to one question), node i performs ~i scan iterations of an i-length array — O(k^2) to O(k^3) work per layer. The backend full view returns every note, session, dataset, analysis, claim, and visualization in the project with `limit=None` (src/lab_tracker/project_graph.py lines 52-68), so a mature project yields thousands of nodes and the layout pass plus the non-virtualized <ReactFlow> render (line 548, no onlyRenderVisibleElements, so all node DOM elements stay mounted) can freeze the tab.

**Fix:** Track occupied y positions per layer in a sorted structure or Set keyed by row index (O(1) collision probe), and pass onlyRenderVisibleElements to ReactFlow so large full-view graphs only mount visible nodes.

*Verification votes: real/high*

#### M28. Session detail card shows stale state after successful close/promote

`src/lab_tracker/frontend_src/features/sessions/SessionDetailCard.jsx:67` · bug · area: frontend-features-a

After 'Close session' or 'Promote to scientific' succeeds on the session detail route, the card keeps rendering the pre-mutation session. The session comes from useSessionDetailData -> useApiResource keyed only on the path `/sessions/${sessionId}` and token (useApiResource.js line 43 deps are [errorMessage, path, token]), so it never refetches, and the handlers discard the updated session returned by onCloseSession/onPromoteSession (app-shell wires these to useSessionActions.handleCloseSession/handlePromoteSession, which only update the workspace sessions list, not this card). The user still sees status 'active', 'Ended: (active)', and the Close button after closing; after promoting, the card still shows 'operational' and the promotion form. Re-clicking re-issues the mutation against an already-closed/promoted session, producing backend validation errors ('Only active operational sessions can be promoted'). The tests in sessions.test.jsx only assert the handler is called, so this is uncovered.

**Fix:** Have useSessionDetailData expose a refresh()/setSession() and apply the payload returned by onCloseSession/onPromoteSession to local state (or bump a refetch counter included in the useApiResource key) so the card reflects the new status/session_type.

*Verification votes: real/high, real/high, real/high*

#### M29. 'Recent Committed' analyses window can omit the most recently committed analysis

`src/lab_tracker/frontend_src/hooks/useAnalysisWorkflow.js:70` · bug · area: frontend-features-a

refreshAnalysisData fetches the last RECENT_COMMITTED_LIMIT committed analyses by computing `offset = total - 5` against the backend list ordering, which is `order_by(AnalysisModel.created_at, AnalysisModel.analysis_id)` ascending (src/lab_tracker/sqlalchemy_repository_parts/analyses.py line 160). That tail is the 5 most recently *created* committed analyses, not the 5 most recently *committed*. The staged->committed workflow explicitly supports committing long-staged analyses, so an analysis created weeks ago and committed today will never appear in 'Recent Committed' once more than 5 newer-created analyses are committed — even though the UI then sorts the window by updated_at desc (sortByRecent), signalling the intent is recency of commit. There is also a TOCTOU between the limit:1 total probe and the second page fetch, which can shift the window by one.

**Fix:** Add a sort/order parameter to the /analyses list endpoint (e.g. order=updated_at.desc) and fetch the first page, or select the recent window client-side by updated_at after fetching committed analyses.

*Verification votes: real/high*

#### M30. MCP get_decision_context masks validation and permission errors as code 'unavailable'

`src/lab_tracker/mcp_api_client.py:352` · api-design · area: mcp-decision

get_decision_context catches every LabTrackerAPIError and httpx.HTTPError and rewrites it as {'error': {'code': 'unavailable', 'message': 'Lab Tracker decision context is unavailable.'}}. The route deliberately returns domain errors (invalid_task_kind, ambiguous_project, anchor_not_found) as HTTP 200 envelopes so they pass through, but HTTP 4xx still reaches this except block: a 422 from AssistantDecisionContextRequest (e.g. limit=200 — the MCP tool signature is an unclamped `limit: int = 20` while the schema enforces le=100; or a malformed UUID anchor) and 403/404 from ensure_project_read all become 'unavailable'. The AGENT_CONSULTATION_POLICY tells agents 'If Lab Tracker is unavailable ... state that explicitly', so an agent that merely passed limit=200 or a non-UUID id will report the tracker as down instead of fixing its input.

**Fix:** Only map connection/5xx failures to 'unavailable'; surface 4xx responses with a distinct code (e.g. invalid_request / forbidden) carrying the API's error message, and clamp or document the limit bound in the MCP tool signature.

*Verification votes: real/high*

#### M31. Project graph full view ships entire note transcripts as node labels

`src/lab_tracker/project_graph.py:221` · performance · area: perf-sweep

_note_label returns note.transcribed_text (or raw_content) with no truncation, and _note_node puts that value directly into ProjectGraphNode.label. In the 'full' view, GET /projects/{id}/graph therefore serializes the complete transcript of every note in the project into the JSON response (and into every line of the mermaid export via project_graph_to_mermaid). Voice transcripts are the primary content type in this app and can be thousands of characters each, so the graph payload scales with total transcript volume even though the frontend renders labels inside 220px-wide nodes (frontend_src/features/project-graph.jsx:269) where only the first few words are visible. Other label helpers in the same builder already truncate (_compact_claim caps at 180 chars in graph_draft_context.py), so this is an inconsistency rather than a design choice.

**Fix:** Truncate note (and claim) labels server-side, e.g. label[:180] with an ellipsis, matching the 180-char convention used by _compact_claim, and keep the node route for navigation to the full note.

*Verification votes: real/high*

#### M32. Visualization asset terms missing from JSON-LD @context, so asset provenance is lost on expansion

`src/lab_tracker/provenance.py:296` · bug · area: routes-knowledge

_visualization_node emits the asset properties as "contentUrl", "fileName", "encodingFormat", "contentSize", and "sha256", but none of these terms are defined in _context (lines 43-81). The context instead defines "filename", "contentType", "sizeBytes", and "checksum" — terms that are used for dataset file nodes but never for visualization assets — which strongly suggests the node builder and the context drifted apart. In JSON-LD processing, terms not mapped in @context (and not IRIs) are dropped during expansion, so any consumer doing real JSON-LD/PROV-O processing of GET /analyses/{id}/provenance loses the visualization filename, content type, size, download URL, and crucially the sha256 checksum — the integrity anchor the provenance export exists to preserve.

**Fix:** Either reuse the already-defined context terms (filename/contentType/sizeBytes/checksum) in _visualization_node, or add the five missing terms to _context (e.g. "contentUrl": {"@id": "lab:contentUrl", "@type": "@id"}, "sha256": "lab:sha256", ...). Add a test that expands the document with a JSON-LD processor or asserts every emitted key is present in @context.

*Verification votes: real/high*

#### M33. Provenance export 500s on malformed external_artifacts dataset metadata

`src/lab_tracker/provenance_ingestion.py:90` · bug · area: routes-knowledge

external_artifacts_from_metadata json.loads()es the reserved 'external_artifacts' key from DatasetCommitManifest.metadata without any error handling, and also raises bare ValueError / pydantic ValidationError on shape mismatches. The manifest metadata is arbitrary user-supplied dict[str,str] (DatasetCommitManifestInput, models.py:243-249) and nothing in DatasetService.create_dataset/commit validates the reserved key at write time. So a user who commits a dataset with metadata={"external_artifacts": "anything not json"} gets a dataset that commits fine but whose GET /datasets/{id}/provenance (routes/provenance.py:27-32 -> provenance.py:169) raises an unhandled json.JSONDecodeError and returns 500. The documented seam (docs/internal-boundaries.md) treats this key as the adapter contract, so it should be validated when written or tolerated when read.

**Fix:** Validate the external_artifacts key in DatasetService when the manifest is created/committed (raising the domain ValidationError -> 400), and/or wrap decoding in the exporter to convert JSONDecodeError/pydantic errors into the domain ValidationError instead of an unhandled 500.

*Verification votes: real/high*

#### M34. No size limit on dataset file and note uploads

`src/lab_tracker/routes/dataset_files.py:97` · security · area: routes-entities

upload_dataset_file streams the multipart body to storage with no maximum-size check at the route, service, or storage layer (LocalFileStorageBackend.store_stream in file_storage.py:131-174 and LocalNoteStorage.store_stream in note_storage.py:36-70 both loop until EOF with no cap; a repo-wide search finds no max-size setting). The same applies to /notes/upload-file and /notes/quick-capture (notes.py:98-102, 134-138). Any authenticated editor can fill the server disk with a single unbounded upload. Additionally, the abstract FileStorageBackend.store_stream default (file_storage.py:36-40) buffers the entire payload into a bytearray in memory, so any future backend that does not override it turns unbounded uploads into unbounded memory growth.

**Fix:** Add a configurable max upload size (settings value), enforce it in the chunk loop (abort and delete the partial object once the limit is exceeded), and reject early when the Content-Length header already exceeds the limit.

*Verification votes: real/high*

#### M35. GET /graph-drafts loads all change sets and returns full context_packet plus all operations per item

`src/lab_tracker/routes/graph_drafts.py:78` · performance · area: perf-sweep

list_graph_drafts calls api.list_graph_change_sets, which queries with limit=None (services/graph_draft_service.py:537-546), so every change set in the table is fetched and hydrated — including every GraphChangeOperationModel row via _operations_for (sqlalchemy_repository_parts/graph_drafts.py:218-234) — before the route slices to the requested page. Worse, the response model is the full GraphChangeSet domain model whose context_packet field (models.py:306) is serialized to clients. Context packets are deliberately large: they embed up to 50 compact questions, 10 of each recent entity type, alias maps, 400-char note previews, full untruncated transcript_text per source artifact (graph_draft_context.py:639), and batch packets cover up to 100 notes — the code even records approximate_size_bytes because the packets are big. The mobile capture screen polls this endpoint with limit=10 on every project selection (frontend_src/features/mobile-capture.jsx:305), so each poll ships ten complete LLM context packets just to render a pending-drafts list.

**Fix:** Pass limit/offset through to repository.query_graph_change_sets (it already supports pagination and indexes ix_graph_change_sets_project_created_at exist), and add a summary read schema for list responses that omits context_packet (and optionally operations), keeping the full payload only on GET /graph-drafts/{id}.

*Verification votes: real/high*

#### M36. All list endpoints load the entire table into memory before paginating

`src/lab_tracker/routes/notes.py:163` · performance · area: routes-entities

Also independently reported as: Every entity list endpoint fetches the whole table then paginates in memory

Every collection endpoint (GET /projects at projects.py:90-96, /questions at questions.py:73-85, /sessions at sessions.py:72-81, /notes at notes.py:163-173, /datasets at datasets.py:65-73) calls the repository with limit=None, offset=0, hydrates every row in the table into domain models, filters visibility in Python via filter_project_scoped_items, then slices in paginate(). The requested limit/offset are never pushed to SQL, so a request for 50 notes costs a full-table scan plus full ORM hydration on every call, and the cost grows linearly with total data volume across all projects — exactly the data (notes, sessions) expected to grow without bound in normal use.

**Fix:** Push the accessible project-id set into the repository query (WHERE project_id IN (...)) so limit/offset and COUNT can run in SQL; for admins (allowed is None) pass limit/offset straight through.

*Verification votes: real/high*

#### M37. PATCH /projects/{id}/members/{user_id} silently creates memberships, including for nonexistent users

`src/lab_tracker/routes/projects.py:226` · data-integrity · area: routes-entities

The PATCH member handler calls upsert_project_membership directly. ProjectService.upsert_project_membership (project_service.py:151-159) creates a brand-new membership when none exists, so PATCH on a user who was never a member returns 200 and adds them instead of returning 404 — wrong partial-update semantics. Worse, unlike the POST path (which resolves the user via _resolve_member_user_id at projects.py:206 and 250-260), neither the PATCH route nor the service validates that user_id corresponds to an existing auth user, so an owner can PATCH any random UUID and persist a membership row pointing at a nonexistent user, which then appears in member listings and in accessible_project_ids bookkeeping.

**Fix:** In the PATCH handler, return 404 when get_project_membership_for_user is None instead of upserting, and validate the user exists (reuse _resolve_member_user_id) before any membership write; alternatively split create/update paths in the service.

*Verification votes: real/high*

#### M38. Project member role-update and removal routes (and the last-owner invariant) are completely untested

`src/lab_tracker/routes/projects.py:215` · testing · area: tests-audit

`PATCH /projects/{project_id}/members/{user_id}` (routes/projects.py:215) and `DELETE /projects/{project_id}/members/{user_id}` (routes/projects.py:234) have zero test references (grep for 'members/' in tests/ returns nothing), and `delete_project_membership` has no service-level test either (grep matches only src). The frontend calls both endpoints (src/lab_tracker/frontend_src/app-shell.jsx:140 and 161), so this is a live supported flow under the retained surface's 'Auth and role-based access control'. The untested logic includes a real invariant: src/lab_tracker/services/project_service.py:184-185 refuses to delete the last owner ('Projects must keep at least one owner.'), plus owner-only authorization for both operations.

**Fix:** Extend test_project_collaboration.py: owner demotes/promotes a member via PATCH, owner removes a member via DELETE and the removed member loses read access, deleting the sole owner returns 422 with the invariant message, and a contributor calling either route gets 401.

*Verification votes: real/high*

#### M39. Create endpoints for questions, sessions, and datasets perform no project-level authorization

`src/lab_tracker/routes/questions.py:43` · security · area: routes-entities

POST /questions, POST /sessions, and POST /datasets never check the actor's project membership. The route handlers call only actor_from_request() (questions.py:43-53, sessions.py:50-57, datasets.py:41-51), and the underlying services (QuestionService.create_question at services/question_service.py:87-88, SessionService.create_session at services/session_service.py:84-85, DatasetService.create_dataset at services/dataset_service.py:89-90) only enforce the global role via require_role(actor, WRITE_ROLES) plus an actor-less projects.get_project(project_id). Any authenticated user with global EDITOR role can therefore create questions, sessions, acquisition pipelines, and datasets inside projects they have no membership in at all (not even viewer). Contrast with POST /notes which calls ensure_project_contributor(request, payload.project_id) at routes/notes.py:63 and re-enforces require_contributor in NoteService.create_note (services/note_service.py:108). Auth/RBAC is in the retained v1 surface, so this is a supported-flow authorization gap.

**Fix:** Call ensure_project_contributor(request, payload.project_id) in the create handlers for questions, sessions, and datasets (or, better, enforce authorization.require_contributor(project_id, actor=actor) inside the three services so every caller is covered, matching NoteService).

*Verification votes: real/high, real/high, real/high*

#### M40. Search offset pagination returns empty/incomplete results for project-scoped queries

`src/lab_tracker/routes/search.py:93` · bug · area: routes-knowledge

When project_ids is not None (any explicit project_id, any non-admin user, or any goal-scoped search), each per-project repository query is issued with limit=limit and offset=0, and the requested offset is applied afterwards by slicing the in-memory list (lines 116-118). Because each query returns at most `limit` rows, the combined list can never contain rows beyond the first `limit` matches per project, so any request with offset >= limit slices past the end and silently returns empty (single project) or wrong (multi-project) results. Example: one project with 50 matching questions, limit=20, offset=20 -> repository returns rows 0-19, then questions[20:40] is []. Page 2 of search is always empty in the most common supported flow. Additionally the meta counts (questions_count/notes_count, line 121) report page sizes, not totals, so clients cannot detect the truncation.

**Fix:** For the single-project case, pass limit/offset straight to the repository query. For the multi-project case, fetch limit+offset rows per project (or query with project_id IN (...) at the SQL level) before slicing, and return real totals in meta.

*Verification votes: real/high, real/high, real/high*

#### M41. Update/delete routes for sessions, datasets, questions, and dataset files gate on read access only — project viewers can mutate and delete

`src/lab_tracker/routes/sessions.py:151` · security · area: routes-entities

All mutating endpoints for sessions (PATCH/DELETE /sessions/{id}, POST .../outputs, POST .../promote, POST .../promote-to-dataset at sessions.py:99, 137, 151, 163, 183), datasets (PATCH/DELETE /datasets/{id} at datasets.py:85, 101), questions (PATCH/DELETE /questions/{id} and /refactor at questions.py:97, 117, 169), and dataset files (POST/DELETE /datasets/{id}/files at dataset_files.py:77, 213) call ensure_project_read, which passes for ProjectMembershipRole.VIEWER (project_authorization.py:12-16, 67-75). The services add only the global require_role(actor, WRITE_ROLES) check, never require_contributor. A user with global EDITOR role who holds only a VIEWER membership on a project can therefore delete its sessions and datasets (including permanent deletion of stored dataset files via the run_after_commit storage cleanup in datasets.py:112-120), rewrite questions, and upload/delete dataset files. Notes routes show the intended model: update/delete/transcribe all use ensure_project_contributor (notes.py:210, 229, 248).

**Fix:** Replace ensure_project_read with ensure_project_contributor on every mutating handler in sessions.py, datasets.py, questions.py, and dataset_files.py (keep ensure_project_read for the GET/list/download endpoints), or push require_contributor into the corresponding service mutation methods.

*Verification votes: real/high, real/high, real/high*

#### M42. Session lifecycle HTTP routes untested: by-link lookup, promote, promote-to-dataset, update, delete

`src/lab_tracker/routes/sessions.py:155` · testing · area: tests-audit

The retained surface names 'closing sessions and promoting eligible sessions into datasets' as a supported flow, but none of these session routes is exercised over HTTP: GET /sessions/by-link/{link_code} (routes/sessions.py:83 — no test contains 'by-link'), POST /sessions/{id}/promote (line 155), POST /sessions/{id}/promote-to-dataset (line 171 — no test contains 'promote-to-dataset' with a client call), PATCH /sessions/{id} (line 95) and DELETE /sessions/{id} (line 147). Promotion and closing are tested only via direct LabTrackerAPI calls (tests/test_core_api.py:573-626, tests/test_acquisition_outputs.py:57-92), which skips request-schema parsing of SessionPromotionRequest/SessionDatasetPromotionRequest (including `commit_manifest` JSON and the `status or DatasetStatus.COMMITTED` default at routes/sessions.py:188) and the per-route ensure_project_read checks. `delete_session` (src/lab_tracker/services/session_service.py:159) has no test at any level.

**Fix:** Add route tests covering: promote-to-dataset with a JSON commit_manifest body and default COMMITTED status, by-link lookup with a normalized (dashed/lowercase) link code, PATCH close, and DELETE, each including a denied non-member case.

*Verification votes: real/high*

#### M43. async visualization file upload blocks the event loop with synchronous storage I/O

`src/lab_tracker/routes/visualizations.py:135` · performance · area: routes-knowledge

upload_visualization_file is declared async def (line 108), so it runs directly on the ASGI event loop, but it drives the storage backend with a synchronous iterator over file.file.read(1024*1024) and a blocking store_stream call. For a large visualization asset, the loop is blocked for the full duration of reading the spooled upload and writing it to the storage backend (disk I/O plus sha256 hashing), stalling every other in-flight request on the server. The non-upload routes in this module are plain def (run in the threadpool), which makes this handler the one that serializes the whole app. The same pattern exists in routes/notes.py and routes/dataset_files.py uploads.

**Fix:** Run the store_stream call off the event loop (e.g. await anyio.to_thread.run_sync / starlette.concurrency.run_in_threadpool), or make the handler a plain def and read the UploadFile via its .file attribute as it already does.

*Verification votes: real/high*

#### M44. commit_analysis silently drops answers_question_ids from submitted claims

`src/lab_tracker/services/analysis_service.py:209` · data-integrity · area: routes-knowledge

POST /analyses/{analysis_id}/commit (src/lab_tracker/routes/analyses.py:105-116) accepts AnalysisCommitRequest.claims as list[ClaimInput], and ClaimInput declares answers_question_ids (src/lab_tracker/models.py:493). But AnalysisService.commit_analysis forwards every ClaimInput field to create_claim EXCEPT answers_question_ids, so question links declared on claims created through the supported analysis-commit flow are silently discarded. The standalone POST /claims route does pass answers_question_ids, so the same payload behaves differently depending on the entry point. Claim-to-question edges are the core of the retained provenance spine, so losing them silently is a data-integrity bug. No test covers answers_question_ids through the commit path (tests only exercise it via create_claim directly).

**Fix:** Pass answers_question_ids=claim_input.answers_question_ids into self.claims.create_claim (create_claim already validates that the questions belong to the project), and add a test asserting question links survive the analysis-commit path.

*Verification votes: real/high, real/high, real/high*

#### M45. commit_analysis drops answers_question_ids from inline ClaimInput payloads

`src/lab_tracker/services/analysis_service.py:209` · bug · area: services-knowledge

POST /analyses/{id}/commit accepts claims as ClaimInput objects (schemas.py:591-594), and ClaimInput declares answers_question_ids (models.py:493). But commit_analysis forwards only statement, confidence, status, supported_by_dataset_ids, and supported_by_analysis_ids to create_claim — answers_question_ids is silently discarded. A user committing an analysis with a claim that answers a question gets a 200 response and a claim with no question link, breaking the question→claim evidence chain with no error. create_claim fully supports the parameter (claim_service.py:62), so this is a one-line omission.

**Fix:** Pass answers_question_ids=claim_input.answers_question_ids in the create_claim call inside commit_analysis, and add a commit test asserting the question link is persisted.

*Verification votes: real/high*

#### M46. Committed-analysis immutability bypassed by archiving first

`src/lab_tracker/services/analysis_service.py:158` · bug · area: services-knowledge

update_analysis blocks environment_hash edits only while status == COMMITTED. But COMMITTED→ARCHIVED is an allowed transition (shared.py:133: `AnalysisStatus.COMMITTED: {AnalysisStatus.COMMITTED, AnalysisStatus.ARCHIVED}`), and once the analysis is ARCHIVED the guard no longer applies, so its environment_hash — provenance recorded at commit time — is freely mutable. Two PATCH requests (status=archived, then environment_hash=<anything>) rewrite the provenance of a record the service explicitly declares immutable ("Committed analyses are immutable."). ARCHIVED is a terminal state, so it should be at least as locked-down as COMMITTED.

**Fix:** Extend the immutability guard to ARCHIVED (e.g. `if analysis.status in {AnalysisStatus.COMMITTED, AnalysisStatus.ARCHIVED}`), or track whether the analysis was ever committed and lock environment_hash from that point.

*Verification votes: real/high*

#### M47. Claim/analysis/visualization writes skip project-scoped authorization that sibling services enforce

`src/lab_tracker/services/claim_service.py:65` · security · area: services-knowledge

ClaimService, AnalysisService, and VisualizationService gate every mutation only with the global role check require_role(actor, WRITE_ROLES), while GoalService, NoteService, ProjectService, and GraphDraftService enforce per-project membership via ProjectAuthorizationPolicy.require_contributor/require_owner. The HTTP routes compound this: POST /claims (routes/claims.py:35-47) and POST /analyses (routes/analyses.py:48-59) perform no project access check at all, and PATCH/DELETE for claims, analyses, and visualizations only call ensure_project_read (e.g. routes/claims.py:84, routes/analyses.py:130). Net effect: any user with the global EDITOR role can create analyses and claims in any project, including projects where they have no membership, and a user with only VIEWER membership on a project can edit and delete its claims, analyses, and visualizations — while the same user is correctly blocked from touching goals or notes in that project (goal_service.py:111 calls self.authorization.require_contributor).

**Fix:** Inject ProjectAuthorizationPolicy into ClaimService, AnalysisService, and VisualizationService and replace require_role(actor, WRITE_ROLES) with authorization.require_contributor(project_id, actor=actor) on all mutating methods, matching GoalService/NoteService. Also add the missing project check to the POST /claims and POST /analyses routes.

*Verification votes: real/high, real/high, real/high*

#### M48. update_claim silently ignores explicit empty lists, so claim support links can never be cleared

`src/lab_tracker/services/claim_service.py:149` · bug · area: services-knowledge

Also independently reported as: update_claim cannot clear support links: empty list is treated as 'keep existing' via `or` fallback

update_claim uses a truthiness fallback (`x or claim.x`) when resolving support links. Passing supported_by_dataset_ids=[] or supported_by_analysis_ids=[] (explicit clearing intent, allowed by the ClaimUpdate schema at schemas.py:466-467 and forwarded verbatim by PATCH /claims/{id} and the graph-draft applier) enters the `is not None` branch but then falls back to the claim's existing links, so the request silently succeeds while changing nothing. The sibling field answers_question_ids at line 154 correctly uses `is not None` semantics and CAN be cleared, proving the asymmetry is unintentional. This also undermines the supported-claim invariant: a caller transitioning a claim to SUPPORTED while clearing its links in the same request passes _ensure_claim_support_links on the strength of links they just asked to remove. No test covers clearing support links to empty.

**Fix:** Use None-aware fallbacks: `supported_by_dataset_ids if supported_by_dataset_ids is not None else claim.supported_by_dataset_ids` (and the same for analysis ids), then add a test that PATCHing [] clears the links and that PROPOSED→SUPPORTED with cleared links is rejected.

*Verification votes: real/high, real/high, real/high*

#### M49. delete_dataset ignores committed-immutability and silently strips evidence from supported claims and committed analyses

`src/lab_tracker/services/dataset_service.py:249` · data-integrity · area: services-entities

Also independently reported as: Entity deletion leaves dangling polymorphic references and silently strips SUPPORTED-claim evidence

update_dataset enforces 'Committed datasets are immutable.' (line 173), but delete_dataset deletes any dataset unconditionally — including COMMITTED ones — with no check for dependents. claim_datasets and analysis_datasets both declare ondelete=CASCADE on dataset_id (db_models.py lines 555-560 and 516-521), so on an FK-enforcing backend deleting a dataset silently removes the supporting-evidence links of SUPPORTED claims and committed analyses. The invariant _ensure_claim_support_links ('Supported claims require supporting datasets or analyses.', services/shared.py lines 282-283) is only checked at claim write time, so a SUPPORTED claim can be left with zero support without any error or status change, destroying exactly the provenance record the product exists to preserve.

**Fix:** In delete_dataset, block deletion (or require an explicit force/archive path) when the dataset is COMMITTED or is referenced by claims/analyses; at minimum query repository.query_claims(dataset_id=...) and query_analyses(dataset_id=...) and raise ConflictError when references exist.

*Verification votes: real/high*

#### M50. Dataset commit silently discards manifest-declared files whenever any uploaded file is attached

`src/lab_tracker/services/dataset_service.py:211` · bug · area: services-entities

In update_dataset's commit path, attached files replace rather than merge with the manifest: files = attached_files or list(base_manifest.files). A staged dataset can legitimately carry manifest-declared files (create_dataset accepts commit_manifest.files while STAGED, and the promote-from-session path injects acquisition outputs into the manifest). If the user then uploads even one file via POST /datasets/{id}/files and commits, every previously declared manifest file silently disappears from the committed provenance manifest — no error, no merge. The commit_hash is recomputed from the truncated manifest so the loss is also invisible to hash validation unless the caller supplies their own commit_hash (in which case they get a confusing mismatch error instead).

**Fix:** Merge attached files with manifest-declared files (raising ValidationError on conflicting paths with different checksums, as _merge_acquisition_outputs already does for the promote path), instead of letting a non-empty attached list wholesale replace the declared manifest.

*Verification votes: real/high*

#### M51. Goal links dangle permanently after their target entity is deleted

`src/lab_tracker/services/goal_service.py:309` · data-integrity · area: services-knowledge

Goal link target integrity is enforced only at link-creation time (_ensure_target_exists). goal_links.entity_id is a bare string column with no foreign key (db_models.py:648), and none of the entity delete paths (claim_service.delete_claim:168-173, analysis_service.delete_analysis:175-180, visualization_service.delete_visualization:121-131, nor the dataset/note/session/question services) remove or mark goal links pointing at the deleted entity. The dangling links remain in every Goal payload (the repository loads links unconditionally in goals_from_rows), can still be promoted to COMMITTED via update_goal_link, and are counted by the search/report surfaces that read query_goal_links (routes/search.py:58). Users see goals claiming evidence/figures that no longer exist.

**Fix:** On entity deletion, delete (or mark DROPPED) goal links whose (entity_type, entity_id) matches the deleted entity — repository.query_goal_links(entity_type=..., entity_id=...) already exists for this; alternatively block deletion while committed goal links reference the entity.

*Verification votes: real/high*

#### M52. Batch windows with more than 100 staged notes silently drop notes 101+ while recording them as covered

`src/lab_tracker/services/graph_draft_context.py:88` · data-integrity · area: graph-drafts

build_batch_graph_context truncates the model input to batch_note_limit (=_BATCH_NOTE_LIMIT=100, graph_draft_service.py:56), but create_batch_graph_draft records ALL note ids in source_note_ids (line 244: source_note_ids=note_ids) and _attach_batch_source_traceability stamps all of them onto operations; run_graph_draft_batch_for_project reports note_count=len(notes) and, on success, the window watermark advances to window_end. Notes beyond the cap were never shown to the model, yet the next scheduled window starts after their created_at, so they are never drafted in any later batch. The only trace is a warning string inside context_summary; the run status is READY and the change set claims full source coverage, so the omission is effectively invisible.

**Fix:** When len(notes) > _BATCH_NOTE_LIMIT, either cap window_end at the created_at of note #100 so the watermark only advances past notes actually drafted (letting the next run pick up the remainder), or loop over chunks; at minimum set source_note_ids to the truncated set and surface truncation on the run record.

*Verification votes: real/high*

#### M53. Graph draft context builder loads every entity in the project to keep the 10 most recent

`src/lab_tracker/services/graph_draft_context.py:309` · performance · area: perf-sweep

build_graph_context_packet (and build_batch_graph_context, lines 119-152, which repeats this per project in the batch) calls list_notes/list_sessions/list_datasets/list_analyses/list_claims/list_visualizations with no filters or limit, fully hydrating every entity in the project — every note with full transcript text and a targets IN-query, every claim with three child-map queries — then sorts in Python and keeps only _RECENT_CONTEXT_LIMIT = 10 items. The same pattern drives run_project_batch (services/graph_draft_service.py:396-400), which lists all project notes unfiltered even though query_notes supports a status= pushdown for the staged-only filter applied right after. This runs on every interactive graph-draft creation (POST /notes/{id}/graph-drafts) and on every scheduled batch run, so draft latency grows linearly with project history while the output stays fixed at 10 items per category. Composite indexes (ix_notes_project_created_at, ix_sessions_project_started_at, etc.) already exist to serve ORDER BY ... DESC LIMIT 10 directly.

**Fix:** Add ordered, limited query support (order_by created_at DESC, limit N) to the repository query methods, or service-level list_recent_* helpers, and use status pushdown in run_project_batch (list_notes(project_id=..., status=NoteStatus.STAGED)) instead of filtering in Python.

*Verification votes: real/high*

#### M54. Concurrent commits of the same graph draft double-apply all accepted operations

`src/lab_tracker/services/graph_draft_service.py:799` · data-integrity · area: graph-drafts

commit_graph_change_set is a check-then-act sequence with no concurrency control: it reads the change set, checks status is READY/SUBMITTED, applies every accepted operation (each entity service generates a fresh uuid4, e.g. question_service.create_question line 90), then writes status COMMITTED. Each HTTP request runs in its own transaction (LabTrackerRequestScope.complete_response commits on status < 400, api.py:516-521), and the repository read is a plain session.get with no SELECT ... FOR UPDATE and no version column (sqlalchemy_repository_parts/graph_drafts.py:266-271, 285-292). Two concurrent commit requests (e.g. a double-clicked Commit button, or two reviewers) both observe READY, both apply, and both successfully write COMMITTED — every created question/note/claim/dataset is inserted twice into the research graph with distinct IDs, and update operations run twice.

**Fix:** Guard the commit with a conditional UPDATE that transitions status from ready/submitted to a transient 'committing' state (UPDATE ... WHERE status IN (...) and check rowcount), or SELECT the change-set row FOR UPDATE / add a version column, before applying operations.

*Verification votes: real/high, real/high, real/high*

#### M55. Orphaned batch settings for a deleted project permanently break run-due scheduling and re-spend LLM calls every tick

`src/lab_tracker/services/graph_draft_service.py:480` · bug · area: graph-drafts

run_due_graph_draft_batches iterates due settings with no per-project error isolation: run_graph_draft_batch_for_project is not wrapped in try/except in the loop, and it calls self.projects.get_project(project_id) (line 389) which raises NotFoundError. The graph_draft_batch_settings FK declares ondelete="CASCADE" (db_models.py:362-366), but the default backend is SQLite (config.py:15: database_url = "sqlite+pysqlite:///./lab_tracker.db") and the codebase never issues PRAGMA foreign_keys=ON (grep finds no match), so deleting a project (project_service.delete_project, line 101) leaves its settings row behind. Settings rows are auto-created for any viewed project (get_graph_draft_batch_settings), so this is easy to hit. Once one orphaned row is due, every /batches/run-due call raises 404, the request-scoped transaction rolls back ALL completed runs and next_run_at advances for healthy projects processed earlier in the loop — after their paid draft-model calls already executed — and the same projects are re-run (with new batch_keys, since window_end = utc_now() changes) on every subsequent tick, forever.

**Fix:** Wrap each iteration in try/except, record a FAILED run (or disable/delete the orphaned settings row) and continue; additionally enable SQLite foreign-key enforcement or have delete_project explicitly remove dependent batch settings/runs.

*Verification votes: real/high, real/high, real/high*

#### M56. Scheduled graph-draft batch runner (run-due) has zero test coverage, including its cadence/timezone math

`src/lab_tracker/services/graph_draft_service.py:467` · testing · area: tests-audit

The scheduled-batch feature completed in the most recent commit (f71ea24 'complete scheduled graph-draft batches') is entirely untested. `run_due_graph_draft_batches` (graph_draft_service.py:467) and its HTTP route `POST /batches/run-due` (src/lab_tracker/routes/graph_batches.py:161) have no test references anywhere in tests/ (grep for 'run_due' and 'run-due' matches only src). Untested behaviors include: the admin-only gate (which raises ValidationError, i.e. HTTP 422 not 401, for non-admins), per-project iteration with draft-client close on failure, rescheduling via `_next_run_at`, and `repository.list_due_graph_draft_batch_settings`. The `_next_run_at` cadence/run_at_local_time/timezone arithmetic (graph_draft_service.py:1038-1054, including the `while candidate <= current: candidate += cadence` loop and DST-sensitive ZoneInfo conversion) is asserted only as truthy in tests (test_graph_draft_batches.py:267 and 283: `assert defaults.json()["data"]["next_run_at"]`), so any wrong next-run computation passes the suite.

**Fix:** Add route tests for POST /batches/run-due: admin runs due projects (settings with next_run_at in the past), non-admin is rejected, next_run_at is advanced to the expected concrete datetime, and a draft-client failure for one project still reschedules. Add direct unit tests for _next_run_at with fixed `now` values covering cadence rollover and a non-UTC timezone.

*Verification votes: real/high, real/high, real/high*

#### M57. run-now accepts a future `until` (and since > until), permanently excluding notes from all later batch windows

`src/lab_tracker/services/graph_draft_service.py:390` · data-integrity · area: graph-drafts

Also independently reported as: Scheduler tick racing a manual run-now produces overlapping duplicate batch drafts for the same notes

GraphDraftBatchRunRequest (schemas.py:367-371) has no validation on since/until, and run_graph_draft_batch_for_project uses them verbatim: window_end = _as_utc(until or utc_now()). A manual run with `until` in the future (user error or client clock skew) produces a READY or SKIPPED run whose window_end is in the future. latest_successful_for_project orders by window_end DESC and counts SKIPPED as successful (sqlalchemy_repository_parts/graph_batches.py:210-226), so all subsequent runs use that future timestamp as window_start. Notes staged between now and that future instant have created_at <= window_start, and _staged_notes_in_window requires start < created_at (line 1074), so they are silently never included in any batch window — they are skipped forever with no error or warning. There is also no check that since <= until.

**Fix:** Clamp window_end to utc_now() (or reject until > now with a ValidationError) and validate since < until in run_graph_draft_batch_for_project.

*Verification votes: real/high*

#### M58. httpx transport errors bypass the batch retry loop and strand a DRAFTING change set that permanently occupies its batch_key

`src/lab_tracker/services/graph_draft_service.py:264` · bug · area: graph-drafts

create_batch_graph_draft saves the change set with default status DRAFTING (line 260) before calling the model, and the retry loop catches only GraphDraftingError. The OpenAI client calls self._client.post("/responses", ...) (graph_drafting.py:340) with no handling of httpx transport exceptions (ConnectError, ReadTimeout, etc.), so the most common transient failure class (a) gets zero retries despite the _BATCH_RETRY_ATTEMPTS loop existing exactly for transient model failures, and (b) propagates to run_graph_draft_batch_for_project's broad `except Exception` (line 441), which marks the run FAILED and returns normally — the request commits, persisting the change set stuck at DRAFTING forever. DRAFTING is excluded from _PENDING_BATCH_STATUSES so it is invisible in the default batch list, yet the existing-change-set lookup (lines 226-231) returns it for any status, so a retry of the same explicit window returns the dead DRAFTING change set instead of re-drafting.

**Fix:** Wrap transport calls in the drafting clients with `except httpx.HTTPError as exc: raise GraphDraftingError(...)` so transient network failures take the retry/FAILED path; also filter the batch_key existing-lookup to non-FAILED, non-DRAFTING statuses (or mark the change set FAILED in the runner's exception handler).

*Verification votes: real/high*

#### M59. GET batch-settings endpoint persists enabled-by-default scheduling as a side effect, gated only by read access

`src/lab_tracker/services/graph_draft_service.py:331` · api-design · area: graph-drafts

get_graph_draft_batch_settings requires only require_read but, when no row exists, creates and saves default settings with enabled=True and a computed next_run_at. A project only participates in scheduled batch drafting if a settings row exists (list_due selects from the table), so a mere GET of /projects/{id}/graph-draft-batch-settings by any read-only viewer flips a project from never-scheduled to scheduled daily LLM drafting — while the PATCH that is supposed to control scheduling requires require_owner (line 350). This also makes a GET endpoint mutating, so read-only users trigger DB writes attributed to them via updated_by.

**Fix:** Return ephemeral defaults from GET without persisting (create the row lazily on PATCH or on first batch run), or require owner role before materializing enabled-by-default settings.

*Verification votes: real/high*

#### M60. run_due_graph_draft_batches executes N project runs plus LLM calls in one request transaction; a late failure rolls back already-completed run records

`src/lab_tracker/services/graph_draft_service.py:467` · data-integrity · area: integrity-sweep

Under the FastAPI middleware all writes in a request share one session committed only in request_scope.complete_response (api.py:516-521), and RepositoryUnitOfWork skips commit when request-managed (services/base.py:55-57). run_due_graph_draft_batches loops over all due projects, making external LLM calls and saving GraphDraftBatchRun/GraphChangeSet rows per project, but nothing is durable until the whole request succeeds. The loop has no per-project exception handling (only `finally` for client close), so if project N raises before run_graph_draft_batch_for_project's internal try (e.g. NotFoundError from `self.projects.get_project`, or an authorization error), the route returns >=400 and the middleware rolls back every completed run, change set, and next_run_at update for projects 1..N-1 — after the irreversible model calls were already paid for. On the next invocation window_end differs, producing a new batch_key, so the batch_key idempotency dedup (lines 408-410, 226-231) does not prevent re-drafting the same notes. The RUNNING-then-FAILED/READY save sequence inside run_graph_draft_batch_for_project (lines 422-465) similarly assumes incremental durability that request-managed mode does not provide.

**Fix:** Wrap each project iteration in try/except so one project's failure records a FAILED run and continues, and either commit per project (explicit repository.commit() for this admin endpoint) or move the scheduled runner out of the request-scoped transaction so completed runs stay durable.

*Verification votes: real/high*

#### M61. upsert_project_membership can demote the last owner, violating the at-least-one-owner invariant

`src/lab_tracker/services/project_service.py:160` · data-integrity · area: services-entities

delete_project_membership explicitly enforces 'Projects must keep at least one owner.' (lines 184-185), but upsert_project_membership — reachable via both POST and PATCH /projects/{id}/members — applies a role change with no owner-count check. The sole OWNER can demote themselves (or be demoted) to viewer/contributor, leaving the project with zero owners. After that, require_owner fails for every member, so memberships, project updates, and project deletion are unmanageable by anyone except a global ADMIN; the delete-side guard becomes unreachable dead protection.

**Fix:** In upsert_project_membership, when an existing OWNER membership is being changed to a non-owner role, count remaining owners and raise the same 'Projects must keep at least one owner.' ValidationError if it is the last one.

*Verification votes: real/high*

#### M62. Project-membership write controls bypassed for questions, sessions, datasets, analyses, claims, and visualizations (cross-project IDOR / privilege escalation)

`src/lab_tracker/services/question_service.py:87` · security · area: auth

Also independently reported as: Project-scoped write authorization is enforced only in NoteService; question/session/dataset writes bypass membership checks

Write operations on questions, sessions, datasets, analyses, claims, and visualizations enforce only the GLOBAL role (require_role(actor, WRITE_ROLES) where WRITE_ROLES = {ADMIN, EDITOR} in services/shared.py:33). They never check the per-project ProjectMembershipRole (viewer/contributor/owner). The HTTP routes for these write paths gate only on ensure_project_read (e.g. routes/questions.py:97 update, routes/sessions.py:99 update, routes/datasets.py:85 update, routes/analyses.py:96 update, routes/claims.py:84 update, routes/visualizations.py:125), and the create routes (routes/questions.py create_question, routes/sessions.py:50, routes/claims.py:35, routes/analyses.py:48) do no project check at all. Consequences: (1) a user granted the global EDITOR role but who is NOT a member of project B can create questions/sessions/datasets/analyses/claims in project B, because create_* only calls require_role + projects.get_project with no membership check; (2) a user who is only a project VIEWER of project B but holds global EDITOR can update/delete those entities in B, since the route's ensure_project_read accepts a VIEWER membership and the service's require_role accepts EDITOR. This defeats the read-only intent of VIEWER membership and the contributor boundary. Note and goal services do this correctly via self.authorization.require_contributor (note_service.py:108), proving the intended pattern; the listed domains diverge from it.

**Fix:** Route all writes for questions/sessions/datasets/analyses/claims/visualizations through ProjectAuthorizationPolicy.require_contributor(project_id, actor=...) (and require_owner where appropriate) in the service layer, instead of the global require_role(WRITE_ROLES). On create paths, resolve project_id and call require_contributor before persisting. Keep require_role only as a coarse pre-check, not the sole write gate.

*Verification votes: real/high, real/high, real/high*

#### M63. Analysis/claim/visualization writes authorize on global role, not project membership (cross-project write + in-project viewer escalation)

`src/lab_tracker/services/visualization_service.py:43` · security · area: security-sweep

Write operations for analyses, claims, and visualizations gate on the GLOBAL role axis via require_role(actor, WRITE_ROLES) where WRITE_ROLES = {Role.ADMIN, Role.EDITOR} (services/shared.py:33). Every other domain (notes, questions, datasets, graph-drafts) instead uses self.authorization.require_contributor(project_id, actor=actor), which is project-scoped. In ProjectAuthorizationPolicy, has_global_write returns True only for ADMIN (project_authorization.py:31-32), so for a non-admin EDITOR require_contributor demands project CONTRIBUTOR/OWNER membership. Because these three services bypass that policy: (1) the CREATE routes perform NO project authorization at all -- create_visualization (routes/visualizations.py:61-71), create_analysis (routes/analyses.py:48-59), create_claim (routes/claims.py:35-47) call only the service, which only checks the global role -- so any authenticated user with global role EDITOR can inject analyses, claims, and visualizations into ANY project, including projects they have zero membership in (cross-project write / data integrity). (2) For UPDATE/DELETE/upload-file the routes add only ensure_project_read (e.g. routes/visualizations.py:125,209,226; routes/analyses.py:96,109; routes/claims.py:84), a read-level check, so a user who is merely a project VIEWER (read-only member) but global EDITOR can mutate/delete these records and replace visualization asset files -- a viewer-to-writer escalation within a project. Device tokens cannot reach these POSTs (device_principal_can_access blocks non-note POSTs), but ordinary EDITOR user sessions can.

**Fix:** Give VisualizationService/AnalysisService/ClaimService a ProjectAuthorizationPolicy and replace require_role(actor, WRITE_ROLES) with authorization.require_contributor(project_id, actor=actor) on every create/update/delete (resolving project_id from the analysis/claim/visualization's project), mirroring note_service and graph_draft_service. At minimum add ensure_project_contributor to the create_analysis/create_claim/create_visualization routes.

*Verification votes: real/high, real/high, real/high*

#### M64. Graph change set, batch run/settings, and project membership mappers skip the _as_utc normalization every other mapper applies, leaking naive timestamps

`src/lab_tracker/sqlalchemy_repository_parts/graph_drafts.py:199` · bug · area: repo-layer

All mappers in sqlalchemy_mappers.py normalize DB datetimes with _as_utc() because SQLite returns naive datetimes for DateTime(timezone=True) columns (e.g. note_from_model: created_at=_as_utc(row.created_at)). But change_set_from_model (graph_drafts.py lines 199-209), operation_from_model (lines 91-92), settings_from_model and run_from_model (graph_batches.py lines 58-70, 112-130), and SQLAlchemyProjectMembershipRepository._from_row (core.py lines 101-102) pass row datetimes through unchanged. On the default SQLite runtime, GraphChangeSet/GraphDraftBatchRun/ProjectMembership API responses serialize timestamps without a UTC offset while every other entity serializes with one; JS Date parsing of offset-less ISO strings interprets them as local time, shifting all displayed graph-draft and review timestamps by the client's UTC offset. graph_draft_service defends some comparisons with its own _as_utc (line 392), but anything comparing change_set.updated_at or run.started_at against an aware datetime elsewhere will raise TypeError on naive values.

**Fix:** Apply the shared as_utc helper (sqlalchemy_mapper_parts/common.py) to every datetime field in change_set_from_model, operation_from_model, settings_from_model, run_from_model, and SQLAlchemyProjectMembershipRepository._from_row.

*Verification votes: real/high*

#### M65. Note query silently drops the target filter when only target_entity_type (or only target_entity_id) is supplied

`src/lab_tracker/sqlalchemy_repository_parts/notes.py:118` · bug · area: repo-layer

SQLAlchemyNoteRepository.query only applies the NoteTargetModel join when BOTH target_entity_type and target_entity_id are non-None. The HTTP route (routes/notes.py list_notes, lines 151-170) exposes both as independent optional query params and performs no both-or-neither validation, so GET /notes?target_entity_type=dataset ignores the filter entirely and returns every visible note as if unfiltered, instead of notes targeting datasets or a 422. The same half-filter pattern exists in SQLAlchemyGoalRepository.query (goals.py line 112), though the goals HTTP route does not currently expose those params.

**Fix:** Either support filtering by entity_type alone (apply each predicate independently on the joined table) or reject half-specified filters with a ValidationError at the route/service layer.

*Verification votes: real/high*

#### M66. LTRecord.id returns the analysis UUID for visualization records

`src/lab_tracker_client/client.py:45` · data-integrity · area: client-cli

LTRecord.id walks _ID_FIELDS in order and returns the first key present. For records from list_visualizations(), the server's Visualization model (src/lab_tracker/models.py:553-555) serializes both viz_id and analysis_id, and analysis_id appears earlier in _ID_FIELDS than viz_id/visualization_id. Empirically verified: LTRecord({'viz_id': 'VIZ-UUID', 'analysis_id': 'ANALYSIS-UUID'}).id returns 'ANALYSIS-UUID'. Any consumer script using viz.id (e.g. to build an EntityRef('visualization', viz.id) note target or to fetch /visualizations/{viz_id}) silently operates on the parent analysis UUID instead of the visualization's own id.

**Fix:** Reorder _ID_FIELDS so entity primary keys are checked before link-field names (move "viz_id"/"visualization_id" ahead of "analysis_id" and "claim_id"), or better, resolve the id by the record's own type rather than first-match key ordering, and add a regression test for visualization records.

*Verification votes: real/high, real/high, real/high*

#### M67. Client reads LAB_TRACKER_BASE_URL but every documented setup only defines LAB_TRACKER_MCP_BASE_URL

`src/lab_tracker_client/client.py:145` · api-design · area: client-cli

LabTracker.from_env() deliberately falls back to the documented MCP credential vars (LAB_TRACKER_MCP_USERNAME/LAB_TRACKER_MCP_PASSWORD) but not to LAB_TRACKER_MCP_BASE_URL. LAB_TRACKER_BASE_URL is referenced nowhere else in the repo: .env.example:24, the repo .mcp.json, docs/lab-tracker-mcp-skills.md, skills/lab-tracker/SKILL.md, and the `lab_tracker init` scaffolding (src/lab_tracker/cli.py:105) all configure only LAB_TRACKER_MCP_BASE_URL. In the documented LAN/Tailscale consumer-repo flow, the MCP tools reach the remote server while the scaffolded scripts/lt.py shim and `lt` CLI silently send requests, including the MCP service-account credentials via /auth/login, to the hardcoded default http://127.0.0.1:8000.

**Fix:** Add the same fallback for the base URL: os.getenv("LAB_TRACKER_BASE_URL") or os.getenv("LAB_TRACKER_MCP_BASE_URL") or DEFAULT_BASE_URL, and document LAB_TRACKER_BASE_URL (and LAB_TRACKER_USERNAME/PASSWORD/PROJECT_ID) in .env.example or the consumer scaffolding.

*Verification votes: real/high*

#### M68. list_* methods reinterpret limit as a page size and always fetch the entire collection

`src/lab_tracker_client/client.py:556` · api-design · area: client-cli

Every public list method (list_projects, list_questions, list_notes, ...) exposes limit/offset parameters that mirror the server's bounded pagination contract, but _list_all loops until current_offset >= total, using limit only as the per-request page size. list_projects(limit=10) returns all projects, not 10. Beyond the surprising semantics, this makes every upsert O(collection size): upsert_note/find_note_by_marker downloads every note in the project on each call (client.py:465-470), so batch imports are O(N^2) HTTP requests, and there is no way for a consumer to actually cap a listing.

**Fix:** Either honor limit as a maximum result count (stop once len(items) >= limit) and add a separate fetch_all/paginate-all helper, or rename the parameter to page_size and document the fetch-all behavior. For the upsert helpers, prefer server-side filtering (e.g. /questions?search=) over downloading the full collection.

*Verification votes: real/high*

#### M69. Transport errors bypass the LTError hierarchy and the CLI prints raw tracebacks

`src/lab_tracker_client/client.py:597` · api-design · area: client-cli

The client defines LTError/LTAPIError/LTValidationError as its failure contract, but self._client.request(...) is never wrapped, so httpx.ConnectError, ReadTimeout, and other transport failures propagate as httpx exceptions; consumers catching LTError per the exported exception types miss the most common failure (server not running). The CLI (src/lab_tracker_client/cli.py:18-27) has no exception handling at all, so `lt health` against a stopped server, a bad --status enum, a missing --file path, or malformed --metadata JSON all dump full tracebacks. Additionally, `lt note --metadata '[1,2]'` (valid JSON, non-object) crashes with AttributeError because _validate_metadata calls .items() on the parsed value without checking it is a mapping (client.py:821).

**Fix:** Wrap the two _client.request calls and the login post in try/except httpx.HTTPError and re-raise as LTAPIError; in cli.main(), catch LTError and print a one-line error with a nonzero exit code; have _validate_metadata reject non-Mapping input with LTValidationError.

*Verification votes: real/high*

#### M70. All list-endpoint project scoping is tested only with the always-authorized admin fixture

`tests/conftest.py:59` · testing · area: tests-audit

The only auth fixture in conftest is `admin_auth_headers` (Role.ADMIN). For an admin, `accessible_project_ids` returns None, which makes `filter_project_scoped_items` (src/lab_tracker/routes/shared.py:89-93) return all items unfiltered: `if allowed is None: return items`. Every HTTP test that calls GET /sessions, /datasets, /analyses, /claims, /notes, /visualizations, /goals, /batches, /batches/runs uses admin headers (verified across test_http_db_integration.py, test_persistence_flows.py, test_goals.py, test_dataset_file_routes.py), so the per-route membership filter on those list endpoints is never executed with a restricted actor. The only non-admin HTTP coverage (tests/test_project_collaboration.py) exercises GET /projects, POST /notes, /search and /assistant/decision-context — each of which uses different scoping code. Deleting the `filter_project_scoped_items` call from any of the resource list routes (a cross-project data leak) would pass the entire suite.

**Fix:** Add a conftest fixture that registers a non-admin member of one project, then one parametrized test asserting that GET /sessions, /datasets, /analyses, /claims, /notes, /visualizations, /goals and /batches return only that project's records when a second admin-owned project has sibling records.

*Verification votes: real/high, real/high, real/high*

#### M71. Hardcoded 2026 window dates make three batch tests start failing after 2026-12-30

`tests/test_graph_draft_batches.py:159` · testing · area: tests-audit

test_batch_run_is_idempotent_for_same_window (lines 159-160), test_batch_retry_and_dead_letter_paths_are_persisted (lines 184 and 218-219) pass absolute `since`/`until` values ('2026-12-30T00:00:00Z', '2026-12-31T00:00:00Z') while the notes they expect to be batched are created at real wall-clock `utc_now()`. The window filter `_staged_notes_in_window` (src/lab_tracker/services/graph_draft_service.py:1075-1079) keeps only notes with `start < created_at <= end`, so once the real date passes 2026-12-30 the freshly created notes fall outside the window, the run is SKIPPED, and assertions such as `len(fake_client.calls) == 1` (line 170) and `assert retry_response.json()["data"]["status"] == "ready"` (line 189) fail. This is a guaranteed time-bomb CI failure about six months out.

**Fix:** Compute `since`/`until` relative to datetime.now(timezone.utc) (e.g. now - 1 day / now + 1 day) or freeze utc_now via monkeypatch as test_auth_routes.py already does.

*Verification votes: real/high*

### LOW (19)

#### L1. 0023 upgrade aborts with UNIQUE violation on databases containing duplicate unslotted goal links

`alembic/versions/0023_goal_link_slot_not_null.py:14` · data-integrity · area: migrations

The migration converts NULL slots to '' so they participate in uq_goal_links_goal_entity_relation_slot, but it does not deduplicate first. NULLs are distinct under unique constraints on both SQLite and Postgres, so during the regression window this migration was written to fix (0022 shipped in commit 5400a1b on 2026-06-05; the uniqueness fix and 0023 landed together in commit 6000db7 on 2026-06-10, 'fix goal validation and link uniqueness regressions'), the app could insert multiple rows with identical (goal_id, entity_type, entity_id, relation) and slot=NULL. On any such database, the blind UPDATE collapses those rows onto the same key tuple and violates the unique constraint, aborting `alembic upgrade head` and leaving the deployment stuck below head until someone manually deduplicates.

**Fix:** Before the UPDATE, delete (or re-slot) duplicates, e.g. keep the earliest created_at per (goal_id, entity_type, entity_id, relation) among slot-IS-NULL rows: run a DELETE using a self-join/correlated subquery on link_id, then perform the NULL->'' rewrite and the NOT NULL alter.

*Verification votes: real/high*

#### L2. Watcher run() loop dies permanently on any transient API/DB error

`src/lab_tracker/acquisition_watcher.py:85` · bug · area: storage

scan() calls api.register_acquisition_output with no exception handling, and run() (lines 100-109) loops over scan() with no try/except either. Any transient failure — a database hiccup (the engine uses pool_pre_ping but reconnection still raises mid-statement), the session being closed/deleted (services/session_service.py line 176: self.get_session(session_id) raises NotFoundError), or a PermissionError outside the two narrowly-caught spots — propagates out and terminates the polling loop. A long-running watcher meant to monitor an entire acquisition session silently stops recording outputs after the first error, and one bad file also aborts registration of all remaining files in that scan pass.

**Fix:** Catch and log per-file registration failures inside scan() so one file does not abort the pass, and catch transient exceptions in run() with backoff so the loop survives DB/API blips; only let unrecoverable errors (e.g., session deleted) escape.

*Verification votes: real/high, real/high, real/high*

#### L3. Stat-then-hash race registers mismatched checksum/size that never self-heals

`src/lab_tracker/acquisition_watcher.py:63` · data-integrity · area: storage

scan() stats the file (line 64), then hashes it (line 75) with no stability check, so a file an instrument is actively writing is registered immediately. If the file grows between stat() and _hash_file() — the normal case for acquisition outputs written incrementally — the API record gets checksum(content_v2) paired with size_bytes(content_v1). On the next scan the stat differs from the fingerprint, the file is re-hashed, and the checksum now matches fingerprint.checksum, so the branch at lines 78-84 updates only the in-memory fingerprint and `continue`s without calling the API. The database permanently keeps a size_bytes that does not correspond to the stored checksum; register_acquisition_output's upsert (session_service.py lines 183-196) would fix it, but it is never invoked again for that file.

**Fix:** Re-stat after hashing (or require size/mtime stable across two polls before registering), and in the checksum-match branch still call register_acquisition_output when the recorded size differs, since the API already upserts size_bytes.

*Verification votes: real/high*

#### L4. SW scope /app/ never controls the bare /app URL that the root redirect targets, so browser entry sessions get no offline support

`src/lab_tracker/app_parts/frontend.py:39` · bug · area: gap:pwa-shell-sw

The SW is registered from /app/sw.js with no explicit scope (register-sw.js:57-58), so its scope defaults to "/app/". Service-worker scope matching is string-prefix based: the URL "/app" (no trailing slash) does not start with "/app/", so navigations to exactly /app — the target of the root redirect and the documented entry URL (http://127.0.0.1:8000/app) — are never controlled. Consequences: (1) offline navigation to /app or / shows the browser error page despite "/app" being precached in SHELL_ASSETS (sw.js:15) and handled by the nav fallback's startsWith("/app") check (sw.js:72) — that code path is unreachable for the /app navigation itself; (2) a page loaded at /app is uncontrolled for its whole lifetime, so none of its asset fetches go through the SW (no runtime caching, no cache-first). Only entries at /app/... paths (e.g. the manifest start_url /app/capture) get SW behavior.

**Fix:** Redirect / to /app/ (and have the /app route redirect to /app/ as well, or serve index only at /app/), or move the SW to be served at /sw.js-equivalent scope via a Service-Worker-Allowed header so the scope covers /app exactly.

*Verification votes: real/high*

#### L5. Unauthenticated /readiness and /metrics leak filesystem paths, raw SQLAlchemy error text, and store contents

`src/lab_tracker/app_parts/observability.py:128` · security · area: core-app

Both endpoints are in the auth middleware's public allowlist (app_parts/middleware.py:28-29), so they are reachable without credentials even when auth is enabled. /readiness returns each storage check's absolute resolved path plus details like 'parent directory not writable: <parent>' (observability.py:41-80), and on DB failure returns the raw stringified SQLAlchemy exception, which typically embeds the failed SQL statement and driver connection details (host/user/database for Postgres OperationalError). /metrics additionally exposes row counts for every entity table, environment name, and uptime (observability.py:143-163, 196-204). For a deployed, auth-enabled instance this is meaningful reconnaissance data handed to anonymous clients.

**Fix:** When auth is enabled, either require credentials for /metrics and /readiness (drop them from _PUBLIC_PATHS) or reduce the unauthenticated payload to a bare ok/fail status, logging the detailed diagnostics server-side instead of returning them.

*Verification votes: real/high*

#### L6. Enrollment offer consumption is a check-then-act race; one offer can mint multiple device tokens

`src/lab_tracker/auth.py:433` · security · area: auth

DeviceAuthService.consume_enrollment loads the enrollment row, checks 'enrollment.consumed_at is not None', and only later sets consumed_at and commits, with no row-level lock (e.g. with_for_update) and no unique constraint preventing reuse. The /auth/devices/consume endpoint is unauthenticated/public (middleware.py:33), and the offer token travels in a QR/URL. Two concurrent POSTs carrying the same valid offer token can both pass the consumed_at check before either commits, each issuing a separate long-lived device token (auth.py:437-454). The single-use guarantee promised in the class docstring ('short-lived single-use grants') is therefore not enforced under concurrency, so an offer that should yield one device credential can yield several that are not tracked back to a single enrollment.

**Fix:** Make consumption atomic: either lock the enrollment row (select(...).with_for_update()) before the consumed_at check, or perform a conditional UPDATE (UPDATE ... SET consumed_at=now WHERE enrollment_id=? AND consumed_at IS NULL) and only issue the device token if exactly one row was affected.

*Verification votes: real/high*

#### L7. SQLite engine has no WAL/busy_timeout configuration for the multi-client runtime; commit-time lock errors bypass the JSON error contract

`src/lab_tracker/db.py:24` · bug · area: gap:asgi-lifecycle

create_engine uses SQLAlchemy 2.0 defaults, which for a file-based SQLite URL means QueuePool handing multiple connections across threadpool threads (check_same_thread=False makes that legal). Journal mode stays the rollback default (no WAL), so any writer blocks all readers for the duration of its transaction; only pysqlite's implicit 5-second connect timeout stands between contention and sqlite3.OperationalError 'database is locked'. With per-request device-token commits (auth.py:489-490) plus the per-request middleware commit, multi-client LAN use makes contention routine. When the lock error fires at commit time it is raised from db_session_middleware (middleware.py:118 -> api.py:527 repository.commit()), which sits ABOVE FastAPI's ExceptionMiddleware where the ErrorEnvelope handlers (routes/errors.py) live — so the client gets Starlette's plain-text 500 instead of the documented JSON error envelope, and the wait itself happens on the event loop (see the loop-blocking finding). pool_pre_ping=True is appropriate for the Postgres path and harmless for SQLite; default Postgres pool sizing (5+10) is fine for this scale.

**Fix:** On the SQLite branch, add a connect-event listener executing PRAGMA journal_mode=WAL and PRAGMA busy_timeout=<n ms> (and foreign_keys=ON per the FK finding), or pass connect_args={"timeout": ...} explicitly. Alternatively document SQLite as single-client-only and require Postgres for the LAN runtime.

*Verification votes: real/high*

#### L8. Export reads tables without snapshot isolation, producing referentially inconsistent mirror commits

`src/lab_tracker/dolt_mirror.py:83` · data-integrity · area: storage

export_tables iterates retained_tables() over a single connection but never establishes a snapshot-consistent transaction. Under Postgres's default READ COMMITTED, each per-table SELECT sees a fresh snapshot, so a write occurring mid-export (the mirror is documented to run against the live runtime) can yield a dolt commit where, e.g., a notes row references a project that did not exist when the projects table was dumped, or a parent exists without children that were committed between the two reads. The resulting dolt history records states the live database never had, which defeats the purpose of an auditable mirror.

**Fix:** Run the whole export inside one REPEATABLE READ (or SERIALIZABLE) transaction, e.g. engine.connect().execution_options(isolation_level="REPEATABLE READ") with an explicit begin(), so all tables are dumped from a single snapshot.

*Verification votes: real/high*

#### L9. Failed streaming upload leaks orphan temp files in storage shard directories

`src/lab_tracker/file_storage.py:150` · bug · area: storage

LocalFileStorageBackend.store_stream creates a NamedTemporaryFile with delete=False and no try/finally cleanup. The chunks iterable comes straight from the HTTP upload stream (routes/dataset_files.py line 98: iter(lambda: file.file.read(1024 * 1024), b"")), so a client disconnect or read error mid-upload raises out of the for-loop, the with-block closes the handle, and the partially written tmp* file is left in the shard directory forever. Nothing ever scans for or removes these, so aborted uploads accumulate unbounded garbage alongside real .bin/.json objects. _atomic_write_bytes (lines 101-109) has the same delete=False pattern with no cleanup if os.replace fails. Additionally, if the metadata sidecar write at line 170 fails after os.replace at line 160 succeeded, a .bin file exists with no .json sidecar and no DB row, again unreclaimed.

**Fix:** Wrap the temp-file write and os.replace in try/except that unlinks tmp_name on any failure (same for _atomic_write_bytes), e.g. try: ... except BaseException: os.unlink(tmp_name); raise.

*Verification votes: real/high*

#### L10. Shared visualizationRequestRef across analysis ids permanently strands earlier panels in 'Loading visualizations...'

`src/lab_tracker/frontend_src/hooks/useAnalysisWorkflow.js:137` · bug · area: frontend-core

loadVisualizations uses one monotonic ref for all analyses. Expanding analysis B while analysis A's fetch is in flight bumps the ref, so when A's response arrives the guard `visualizationRequestRef.current !== requestId` returns early without writing state — A's entry stays `{loading: true, loaded: false}` forever. The per-analysis state is keyed by analysisId, so the two requests do not actually conflict; the cancellation is spurious. Worse, recovery is impossible through the UI: AnalysisVisualizationSection's toggle (features/analysis/AnalysisPanel.jsx:91) only refetches when `!state.loaded && !state.loading`, and loading is stuck true, so the panel shows the loading message until the project or token changes resets visualizationStates.

**Fix:** Track request ids per analysis (e.g. a Map keyed by analysisId, or store the requestId inside the per-analysis state entry) so a load for one analysis cannot cancel another's. The same generation-counter-vs-keyed-state mismatch is worth avoiding anywhere per-entity caches are loaded concurrently.

*Verification votes: real/high*

#### L11. Shared visualizationRequestRef strands a section in permanent 'Loading visualizations...'

`src/lab_tracker/frontend_src/hooks/useAnalysisWorkflow.js:154` · bug · area: frontend-features-a

loadVisualizations uses a single hook-wide visualizationRequestRef even though results are stored per analysisId. If the user clicks 'Load visualizations' on committed analysis A and then on analysis B before A's fetch resolves, B increments the ref, so when A's fetch completes the guard `visualizationRequestRef.current !== requestId` is true and A's state update is skipped — A's entry stays {loading: true, loaded: false} forever. AnalysisVisualizationSection.handleToggle only re-loads when `!state.loaded && !state.loading` (AnalysisPanel.jsx line 91), so collapsing/re-expanding never retries; the section shows 'Loading visualizations...' until the project is switched. The same stale-guard exists in the catch path. Since requests for different analyses write to disjoint keys, the cross-analysis guard is unnecessary.

**Fix:** Track request ids per analysisId (e.g. a Map keyed by analysisId) or drop the guard entirely since state is keyed by analysisId and last-write-wins per key is already correct.

*Verification votes: real/high*

#### L12. No 401 handling or token refresh: sessions silently break after the 60-minute token TTL

`src/lab_tracker/frontend_src/hooks/useAuthSession.js:32` · api-design · area: frontend-core

Access tokens expire after 60 minutes by default (src/lab_tracker/auth.py:230 `ttl_minutes: int = 60`) and the backend exposes POST /auth/refresh (src/lab_tracker/routes/auth.py:77), but the frontend never calls it and nothing in frontend_src reacts to a 401 (the only err.status check anywhere is mobile-capture.jsx:610, which tests for undefined). The /auth/me validation runs only when `token` changes, so after expiry mid-session every API call fails — each interaction flashes 'Authentication required.'-style errors while the workspace stays rendered with stale data, and the login form never reappears. The user must manually Sign out or reload to recover; an unsaved capture form's submit just errors.

**Fix:** Either schedule a call to /auth/refresh before expires_at (the login payload already includes expires_at, currently discarded), or centrally detect err.status === 401 in the api layer and clear the token so the AuthForm reappears immediately.

*Verification votes: real/high*

#### L13. Every MCP tool call constructs a new client and performs a fresh /auth/login round-trip

`src/lab_tracker/mcp_tools/read.py:12` · performance · area: mcp-decision

All 30 MCP tools follow the pattern `client = client_from_env(); try: ... finally: client.close()`. LabTrackerAPIClient caches the bearer token only on the instance (self._access_token), and client_from_env() builds a brand-new client per invocation, so with auth enabled every single tool call issues a POST /auth/login (server-side password hashing) before the actual request, plus a new TCP/TLS connection. An agent following the consultation policy makes many small read calls per decision; each one pays double round-trips and the auth endpoint absorbs one password verification per tool call.

**Fix:** Share one module-level client (httpx.Client is connection-pooling and the 401-retry already handles token expiry), or cache the access token in a module-level variable keyed by base_url/username so logins are amortized across tool calls.

*Verification votes: real/high*

#### L14. Analyses/claims list endpoints load the entire table before paginating in memory

`src/lab_tracker/routes/analyses.py:74` · performance · area: routes-knowledge

list_analyses (routes/analyses.py:74-83) and list_claims (routes/claims.py:62-72) always call the repository with limit=None, offset=0, hydrate every row (including the per-batch child link maps for dataset/analysis/question ids), then filter and slice in Python via filter_project_scoped_items + paginate. The repository layer fully supports SQL-side pagination and the accessible-project filter could be expressed as project_id IN (...). As written, every GET /analyses or GET /claims without a project_id filter loads and maps the whole table per request, and even project-filtered requests load the project's full history to serve one page.

**Fix:** Add an accessible_project_ids filter to query_analyses/query_claims (WHERE project_id IN (...)) so limit/offset can be pushed into SQL, and only fall back to in-memory filtering for the unrestricted-admin case where no filter is needed at all.

*Verification votes: real/high*

#### L15. Search issues two sequential queries per accessible project, unbounded when goal-filtered

`src/lab_tracker/routes/search.py:93` · performance · area: perf-sweep

For any non-admin user searching without an explicit project_id, the route loops over every project the user can access and issues a separate query_questions and query_notes call per project — 2P sequential ILIKE scans for P memberships, each fetching up to `limit` rows that are then merged and re-sliced in Python (lines 116-118), so most of the fetched rows are discarded. When goal_id is supplied, limit becomes None (lines 74, 84, 99, 108), so every question/note in the project matching the substring is fetched and hydrated just to be intersected in Python with the goal-link id set. The repository query methods only accept a single project_id, which forces this loop; decision_context_query.py:118-141 duplicates the same per-project loop for assistant search.

**Fix:** Add a project_ids IN (...) filter to query_questions/query_notes so one query covers all accessible projects with LIMIT applied in SQL. For the goal-filtered path, push the linked id set into the query (question_id IN (...)) instead of fetching unbounded results and intersecting in Python.

*Verification votes: real/high*

#### L16. GET /visualizations issues 3 queries per visualization over an unbounded result set

`src/lab_tracker/routes/visualizations.py:247` · performance · area: routes-knowledge

list_visualizations queries with limit=None (line 85-91), then _filter_visualizations_for_access loops over every returned visualization calling request_api.get_analysis (SELECT analysis + SELECT analysis_datasets per call) and ensure_project_read (SELECT project_membership per call for non-admins, see ProjectAuthorizationPolicy.membership_role at src/lab_tracker/services/project_authorization.py:61-65). With no project_id filter this loads every visualization in the database and runs ~3 extra queries per row on every list request. Even when project_id IS provided, the route has already verified read access once (line 84) and the SQL join already scopes rows to that project, so the per-item rechecks are pure duplicated work. Other list routes resolve accessible_project_ids once and filter in memory; this one re-queries per item.

**Fix:** Batch-load the analyses (repository.analyses already supports batched hydration) or select viz_id -> project_id with a single join, then intersect with accessible_project_ids_from_request (one membership query). Skip the recheck entirely when project_id was already validated.

*Verification votes: real/high*

#### L17. Stored XSS: visualization asset served inline with client-controlled Content-Type and no allowlist

`src/lab_tracker/routes/visualizations.py:192` · security · area: gap:upload-serving-xss

The visualization asset download route serves uploaded bytes with Content-Disposition: inline and media_type taken verbatim from the attacker-controlled multipart upload, with no content-type allowlist or sniffing anywhere in the upload path (routes, services, or storage backends). An authenticated writer can upload an asset whose content_type is text/html (or image/svg+xml) containing <script>; the download endpoint then renders it as an HTML document in the application's own origin. Because the response is served same-origin to /app, executed script can read the bearer token in localStorage (key 'lab_tracker_access_token', constants.js line 1) or, in the default auth-disabled mode, drive the admin API directly. Exploitability: the SPA itself only renders assets through <img>/<audio> (VisualizationDetailCard.jsx lines 88-94), which neutralizes script, and in auth-enabled deployments a top-level browser navigation to the download URL carries no Bearer header so the middleware returns 401 — so there it is a latent primitive. BUT the documented default runtime is environment='local' (config.py lines 13, 38-41) where auth is disabled and every request gets Role.ADMIN (middleware.py lines 50-51, 69-71); in that mode opening the asset URL directly renders the malicious text/html inline and runs script with full same-origin admin API access. The 'inline' disposition is also simply the wrong design for a file-download endpoint.

**Fix:** Serve user-uploaded assets with Content-Disposition: attachment (never inline) and add X-Content-Type-Options: nosniff; do not echo the client content-type into media_type for active types — either force a safe download type (application/octet-stream) or restrict the served Content-Type to a vetted allowlist (e.g. image/png, image/jpeg). For SVG specifically, sanitize or refuse it. Validate/allowlist content types at upload time as well.

*Verification votes: real/high, real/high, real/high*

#### L18. Last-owner invariant in delete_project_membership is check-then-act with no DB backing

`src/lab_tracker/services/project_service.py:179` · data-integrity · area: integrity-sweep

The 'projects must keep at least one owner' rule is enforced only by counting owners in Python before deleting. Two concurrent DELETE membership requests (each in its own request-scoped session/transaction, default READ COMMITTED on Postgres) targeting the two remaining owners both observe owner_count == 2, both pass the guard, and both deletes commit, leaving the project with zero owners. Nothing at the DB level (constraint or trigger) backs the invariant, and there is no row locking (SELECT ... FOR UPDATE) on the memberships involved. Once a project has no owner, owner-gated operations (membership management, reviews, project settings) become impossible for non-admin users.

**Fix:** Lock the project's membership rows (with_for_update on project_memberships filtered by project_id) before counting, or re-verify the invariant with a guarded DELETE (delete only if another owner row exists) so concurrent removals cannot both succeed.

*Verification votes: real/high*

#### L19. Importing lab_tracker_client instantiates a client and can crash at import time

`src/lab_tracker_client/client.py:687` · bug · area: client-cli

Module level code runs client = client_from_env() on import, which reads environment variables and constructs an httpx.Client. Verified: with LAB_TRACKER_HTTP_TIMEOUT=abc, `import lab_tracker_client` raises ValueError('could not convert string to float') from from_env (line 152), so even `lt ids` or `python -m lab_tracker_client health` crash before argument parsing. The module-level client is also never closed, env changes after import are ignored, and every CLI invocation builds a second client (cli.py main() calls LabTracker.from_env() again) while the import-time one leaks.

**Fix:** Make the module-level singleton lazy (e.g. a _get_client() memoized accessor used by the module-level helper functions), and wrap the timeout parse in a clear LTValidationError. This removes the import-time crash and the leaked httpx.Client.

*Verification votes: real/high*

## Low-severity findings (reported by a single reviewer, not adversarially verified)

- **Readiness DB check runs 10 full-table COUNT(*) queries and throws the results away** — `src/lab_tracker/app_parts/observability.py:132` (performance, core-app)
  - _database_check delegates to _store_counts_from_database, which executes SELECT COUNT(*) against all ten entity tables (observability.py:111-129), then discards the counts and only inspects the error. Readiness endpoints are polled repeatedly by orchestrators/monitors, and COUNT(*) is a table scan on both SQLite and Postgres, so liveness probing cost grows with data volume for no benefit. A single SELECT 1 proves connectivity.
  - Fix: Replace the count sweep in _database_check with a trivial probe (e.g. session.execute(select(1))) and keep the full count sweep only for /metrics, where the numbers are actually used.
- **Production auth middleware ships a test-only bypass for any path under /_test/** — `src/lab_tracker/app_parts/middleware.py:61` (security, core-app)
  - _is_public_path unconditionally exempts every path starting with /_test/ from authentication. The only routes under that prefix live in tests/test_db_wiring.py (test fixtures register /_test/fail, /_test/repository, etc.); no production route uses it. Shipping a wildcard auth-bypass prefix in production middleware purely so test fixtures can skip credentials is a latent hole: any future route mounted under /_test/ (or a debugging router left enabled) silently becomes unauthenticated.
  - Fix: Remove "/_test/" from the production allowlist and have the wiring tests either disable auth (environment=local default already does this) or register their fixture routes under a path they explicitly allow in the test app.
- **Unused module-level SessionLocal builds a second engine and instantiates Settings as an import side effect** — `src/lab_tracker/db.py:46` (dead-code, core-app)
  - SessionLocal is referenced nowhere in the codebase (grep finds only the definition), yet it executes at import time of lab_tracker.db — which everything imports transitively via `from lab_tracker.db import Base` in db_models, including alembic/env.py:10. Each import therefore constructs Settings() (reading .env from the CWD and running the auth-secret model validator, which can raise ValueError and turn a config problem into an ImportError for migrations/tests) and creates a second Engine pointed at the env-derived database_url. That engine is separate from the one the app builds in build_app_runtime and is never disposed by make_lifespan (app_parts/runtime.py:80-88).
  - Fix: Delete the module-level SessionLocal (callers that need a factory already use get_session_factory(settings)/build_app_runtime), removing the import-time Settings/engine side effects.
- **User enumeration: member-create resolves user existence before owner authorization** — `src/lab_tracker/routes/projects.py:206` (security, routes-entities)
  - In create_project_member, _resolve_member_user_id runs before upsert_project_membership, but the owner check (authorization.require_owner) lives inside upsert_project_membership (project_service.py:149). So any authenticated user — with no relationship to the project at all — can POST to /projects/{id}/members and distinguish responses: 404 'User does not exist.' when the probed username/user_id is absent versus 401 'Project owner access required.' when it exists. This lets non-owners enumerate valid usernames and user IDs.
  - Fix: Call ensure_project_owner(request, project_id) at the top of create_project_member before resolving the target user, so unauthorized callers get the same 401 regardless of whether the user exists.
- **Project graph 'questions' view runs five unbounded entity queries whose results are discarded** — `src/lab_tracker/project_graph.py:52` (performance, routes-knowledge)
  - build_project_graph unconditionally executes unbounded (limit=None) queries for datasets, analyses, claims, and visualizations — each of which also hydrates its child link maps — before checking the view. For view="questions", none of those results are used: nodes are only added inside the `if view_value in {"evidence", "full"}` block (lines 74-88) and _add_evidence_edges is likewise gated (line 91-92). So GET /projects/{id}/graph?view=questions pays the full evidence-view query cost (5 unbounded queries plus link-map queries) for output built from questions alone.
  - Fix: Move the dataset/analysis/claim/visualization queries inside the existing `if view_value in {"evidence", "full"}` guard so the questions view only queries questions.
- **Accepting an operation that $ref's a rejected operation passes review validation but fails the whole commit** — `src/lab_tracker/services/graph_draft_validation.py:288` (bug, graph-drafts)
  - Review-time validation substitutes any {"$ref": ...} with a placeholder UUID (_payload_for_review_validation), so update_graph_change_operation happily lets a reviewer ACCEPT an operation whose payload references the client_ref of an operation they REJECTED (or left PROPOSED). At commit time only ACCEPTED operations populate ref_map (graph_draft_service.py:804-820), so resolve_refs raises ValidationError('Unknown graph draft ref: ...') and the entire commit 400s with no indication of which accepted operation depends on which rejected one. The reviewer is allowed to construct a state that can never commit, and the only diagnostic is an opaque ref name.
  - Fix: In update_graph_change_operation, when an operation is rejected, demote (or flag) accepted operations whose payloads $ref its client_ref; or validate ref-dependency consistency before allowing commit and report the dependent operation ids in the error.
- **create_project splits project and owner-membership writes across two independently committed units of work** — `src/lab_tracker/services/project_service.py:52` (architecture, services-entities)
  - create_project saves the project in one unit_of_work and the creator's OWNER membership in a second one. RepositoryUnitOfWork.__exit__ commits at the end of each block when the context is not request-managed (services/base.py lines 55-57: 'if not self._context.is_request_managed(): self._repository.commit()'). Under the HTTP middleware everything shares one request transaction, but for any direct/standalone use of the service (the supported construction path ServiceContext(repository=...) without request_context, as exercised by tests and embedding code), a failure between the two commits leaves a committed project with no owner membership — the exact state delete_project_membership's last-owner guard exists to prevent, and one that locks non-admin creators out of their own project.
  - Fix: Save the project and the owner membership inside a single unit_of_work block so the create is atomic in both request-managed and standalone modes.
- **list_analyses question filter loads every dataset in the database to re-apply a filter SQL already performed** — `src/lab_tracker/services/analysis_service.py:133` (performance, services-knowledge)
  - AnalysisService.list_analyses passes question_id to repository.query_analyses, which already filters via a SQL join on DatasetQuestionLinkModel (sqlalchemy_repository_parts/analyses.py:135-153). The service then re-filters the result in Python by building a map of self.datasets.list_datasets() with no project scoping — every dataset row plus its question links and manifest is hydrated on each question-filtered call, with identical semantics to the SQL filter (_analysis_has_question_link checks the same dataset question links). The project_id and dataset_id re-filters at lines 129-132 are similarly redundant. This path is exercised by LabTrackerAPI.list_analyses consumers and scales O(total datasets) per call.
  - Fix: Trust the repository filters and drop the in-Python re-filtering, or at minimum scope the dataset fetch to the analyses' project ids (datasets.list_datasets(project_id=...)).
- **Dead membership mapper pair in sqlalchemy_mappers.py duplicates (and diverges from) the live mapping in core.py** — `src/lab_tracker/sqlalchemy_mappers.py:120` (dead-code, repo-layer)
  - project_membership_from_model (lines 120-133) and apply_project_membership_to_model (lines 136-145) are exported but never called: SQLAlchemyProjectMembershipRepository defines its own _from_row for reads and applies fields inline via apply_project_membership_to_model only in save() — actually save() does import apply_project_membership_to_model, but the read path uses the private _from_row, so the exported from-model function with correct _as_utc normalization is dead while the live read path (core.py _from_row) lacks normalization. Two parallel implementations of the same mapping have already drifted (tz handling), which is exactly the failure mode dead duplicates create.
  - Fix: Delete _from_row in core.py and reuse project_membership_from_model (extending it to accept the joined UserModel), or remove the unused mapper; keep exactly one membership mapping implementation.
- **daily_graph_reviews tables are orphaned schema: created by 0017, unused by any code, never dropped** — `alembic/versions/0017_daily_graph_reviews.py:16` (dead-code, migrations)
  - 0017_daily_graph_reviews creates daily_graph_reviews and daily_graph_review_change_sets (with two indexes each and FKs into graph_change_sets), but there is no corresponding model in src/lab_tracker/db_models.py and zero references to either table anywhere in src/lab_tracker (grep for 'daily_graph' matches only alembic/, tests/test_migrations.py, and .beads). The feature was superseded by graph_draft_batch_settings/graph_draft_batch_runs in 0024 (per the 'Reframe graph-draft batch beads' commit), yet no migration drops the dead tables, and tests/test_migrations.py:130-131 asserts they exist at head, cementing the drift. Metadata-built test databases (Base.metadata.create_all) do not contain them, so migrated and test schemas permanently diverge.
  - Fix: Add a new head migration that drops daily_graph_review_change_sets and daily_graph_reviews (and their indexes), and update test_alembic_upgrade_head_creates_expected_tables to assert their absence alongside dataset_reviews/note_tag_suggestions.
- **batch_key uniqueness object mismatch: 0024 creates unique index ix_graph_change_sets_batch_key, model declares constraint uq_graph_change_sets_batch_key** — `alembic/versions/0024_graph_draft_batches.py:26` (architecture, migrations)
  - GraphChangeSetModel declares `UniqueConstraint("batch_key", name="uq_graph_change_sets_batch_key")` (db_models.py lines 256-258), but migration 0024 instead created a unique index named ix_graph_change_sets_batch_key. Uniqueness is enforced either way, but the two schema lineages now carry differently-named (and differently-typed: index vs constraint) objects: migrated databases have ix_graph_change_sets_batch_key and no uq_* constraint, while metadata-built test databases have the uq_* constraint and no ix_* index. Any future migration that does drop_constraint('uq_graph_change_sets_batch_key', ...) or drop_index('ix_graph_change_sets_batch_key', ...) will succeed against one lineage and fail against the other, and autogenerate will perpetually flag both objects.
  - Fix: Add a migration that drops ix_graph_change_sets_batch_key and creates the named unique constraint uq_graph_change_sets_batch_key (via batch_alter_table for SQLite), so migrated databases match the model metadata; or change the model to declare Index("ix_graph_change_sets_batch_key", "batch_key", unique=True) instead of the UniqueConstraint.
- **CSV serialization conflates NULL with empty string, losing fidelity and masking changes** — `src/lab_tracker/dolt_mirror.py:153` (data-integrity, storage)
  - _serialize maps None to "" while a genuine empty-string column value also serializes to "". The dolt mirror therefore cannot distinguish NULL from '' in any nullable text column: a live-DB change from NULL to '' (or vice versa) produces an identical CSV, so `dolt status` shows no change and no commit is created, and the mirrored history misrepresents the actual values. Booleans similarly round-trip as the Python reprs "True"/"False" rather than SQL booleans.
  - Fix: Emit a distinct NULL sentinel and pass dolt's null-value import option (dolt table import supports treating a designated string as NULL), and serialize booleans as 0/1 or true/false consistently.
- **_response_error discards field-level 422 'issues', leaving agents with only 'Request validation failed.'** — `src/lab_tracker/mcp_api_client.py:869` (api-design, mcp-decision)
  - The API's RequestValidationError handler returns code 'request_validation_error' with the generic message 'Request validation failed.' and puts the actionable per-field details in error.issues (routes/errors.py:48-55). _response_error extracts only error.message, so every MCP tool that hits Pydantic validation (extra fields are forbidden by RequestModel, malformed UUIDs, out-of-range limits) surfaces a LabTrackerAPIError whose text gives the calling agent no indication of which parameter was wrong — making self-correction a guessing game.
  - Fix: In _response_error, append a compact rendering of error.issues (field: message pairs) to the returned string so agents can correct the offending parameter.
- **Expired LAB_TRACKER_ACCESS_TOKEN produces a misleading username/password error** — `src/lab_tracker_client/client.py:625` (bug, client-cli)
  - On a 401, _request discards the configured access token and demands a password login. If the client was configured only via LAB_TRACKER_ACCESS_TOKEN (a constructor/env option the client explicitly supports) and that token is expired or revoked, the user gets 'LAB_TRACKER_USERNAME and LAB_TRACKER_PASSWORD are required when the Lab Tracker API has authentication enabled' with no mention that a token was supplied and rejected, sending them down the wrong debugging path. The server's actual 401 body (e.g. 'Invalid token.') is also discarded.
  - Fix: When a 401 occurs and an access token had been supplied but no username/password is available, raise an error stating the access token was rejected (including the server's error message) instead of the generic credentials message.
- **refreshProjectMembers has no stale-response guard; rapid project switching can apply the wrong project's membership/permissions** — `src/lab_tracker/frontend_src/app-shell.jsx:79` (bug, frontend-core)
  - Unlike the data hooks (which all use requestRef counters), the members fetch in App has no sequencing or cancellation. Switching the active project twice in quick succession fires two overlapping GETs; if the first project's response resolves last, projectMembers holds project A's members while selectedProjectId is project B. The derived canContributeToProject and canManageProjectMembers (lines 104-108) then gate note capture, member management, and the canManageGraph prop passed to BatchReviewPage/GraphDraftDetailCard based on the wrong project until the next refresh. The backend still enforces authorization, so impact is incorrect UI gating (actions hidden when allowed, or shown then failing), not privilege escalation.
  - Fix: Apply the same requestRef pattern used in useProjectNoteData/useProjectSessionData (capture a request id before the await and discard the response if superseded), or set a canceled flag in the effect cleanup.
- **Sign-in success flash is wiped immediately by the token-change effect** — `src/lab_tracker/frontend_src/hooks/useAuthSession.js:36` (bug, frontend-core)
  - handleAuthSubmit sets the token and then setFlash('Signed in successfully.') (lines 89-96). The token change re-runs the /auth/me effect, which unconditionally executes `if (token) { setFlash("", ""); }` right after the commit — erasing the success message within the same frame, so the user never sees the 'Signed in successfully.' / 'Viewer account created. You are signed in.' confirmation. (Sign-out's 'Signed out.' message survives because the effect skips setFlash when token is empty.)
  - Fix: Only clear the error half of the flash in the revalidation effect (or skip the clear entirely on the initial post-login validation), e.g. drop the setFlash call and let actual /auth/me failures set the error in the catch.
- **Stale session primary question keeps 'Start session' enabled when no active questions exist** — `src/lab_tracker/frontend_src/hooks/useProjectWorkspaceForms.js:27` (bug, frontend-features-a)
  - When the active-question list becomes empty but the previously selected primary question still exists in the project with a non-active status (e.g. the only active question gets superseded or answered and refreshProjectData runs), the effect keeps the stale sessionPrimaryQuestionId because it only clears the id when the question no longer exists at all. SessionPanel then renders the question select disabled and blank (the stale id matches no option) with the 'Commit at least one question...' warning, yet the submit button stays enabled because its guard is `sessionType === "scientific" && !sessionPrimaryQuestionId` (SessionPanel.jsx lines 118-123) and the id is truthy. Submitting posts the non-active question id and fails server-side with 'Primary question must be active for scientific sessions' (session_service.py line 96).
  - Fix: Clear sessionPrimaryQuestionId whenever it is not among the current active questions (mirror the hasCurrent check), or additionally disable the Start button when primaryQuestionOptions.length === 0.
- **QuestionDetailCard hard-caps project questions and targeted notes at 200 without pagination** — `src/lab_tracker/frontend_src/features/questions/QuestionDetailCard.jsx:37` (bug, frontend-features-a)
  - Unlike the rest of the app, which paginates with fetchAllPages (e.g. useProjectWorkspaceData.refreshProjectData, useSessionDetailData), QuestionDetailCard fetches project questions and targeted notes through useApiResource with a fixed limit of 200 and offset 0. For projects with more than 200 questions, the refactor form's 'Replacement parents' options, 'Move child questions' checkboxes, parent-name resolution, and superseded-by/supersedes lookups are silently truncated; same for 'Move note targets' beyond 200 notes. A refactor submitted from this truncated view will silently fail to reparent children that fell outside the first page.
  - Fix: Fetch these lists with fetchAllPages (as the workspace and session detail hooks do) instead of a single capped page.
- **DatasetDetailCard treats archived datasets as staged ('staged' pill, live files instead of commit manifest)** — `src/lab_tracker/frontend_src/features/datasets/DatasetDetailCard.jsx:27` (bug, frontend-features-a)
  - DatasetStatus includes 'archived' (src/lab_tracker/models.py line 79), but the detail card only special-cases 'committed': for any other status it fetches /datasets/{id}/files and renders the pill as 'staged N' (line 117: `{dataset.status === "committed" ? "committed" : "staged"}`). An archived dataset that was previously committed therefore shows the mutable attached-files listing rather than its immutable commit_manifest, and is labeled 'staged' in the Files header even though the status pill at the top says 'archived'. Reachable by navigating to /app/datasets/{id} for any archived dataset.
  - Fix: Branch on the presence of commit_manifest (or on status !== "staged") so archived-after-commit datasets render their commit manifest, and label the files header with the actual dataset status.
- **Mobile capture 'Pending review' lists committed/rejected drafts and can bury pending ones** — `src/lab_tracker/frontend_src/features/mobile-capture.jsx:305` (bug, frontend-features-b)
  - The capture page's Pending review section fetches `/graph-drafts?project_id=X&limit=10` with no status filter. Unlike GET /batches — which defaults to pending statuses when status is None (graph_batches.py lines 65-70) — GET /graph-drafts returns change sets of every status (graph_drafts.py lines 66-90). So committed, rejected, and failed drafts render under 'Pending review', and because only the 10 most recent are fetched, recently committed drafts push genuinely pending ones out of the list entirely.
  - Fix: Request only reviewable statuses (e.g. status=ready, plus changes_requested) or filter client-side before rendering, mirroring the pending-status defaulting that /batches already applies.
- **Accept all silently skips operations that fail to save, then allows committing without them** — `src/lab_tracker/frontend_src/features/graph-drafts.jsx:319` (bug, frontend-features-b)
  - acceptAll awaits saveOperation per operation, but saveOperation swallows failures (invalid JSON payload returns early; server rejection is caught and only setFlash'd at lines 308-312). The loop continues, and each subsequent success overwrites the flash with 'Graph draft operation updated.', hiding the failure. The user sees a success message, the failed operation stays in 'proposed', and 'Commit accepted changes' then commits the partial set without any indication one proposal was dropped.
  - Fix: Have saveOperation return success/failure, aggregate results in acceptAll, and finish with a single flash that reports how many operations were accepted vs failed (stopping or highlighting the failed ones).
- **tests/test_smoke.py is a no-op test** — `tests/test_smoke.py:1` (dead-code, tests-audit)
  - The file contains only `def test_smoke(): assert True`. It verifies nothing (not even that the package imports) and inflates the passing-test count.
  - Fix: Delete the file, or make it a real smoke test (e.g. import lab_tracker and create_app()).
- **No rate limiting or lockout on authentication endpoints; unauthenticated viewer self-registration** — `src/lab_tracker/routes/auth.py:71` (security, security-sweep)
  - /auth/login (and the other auth endpoints) are in _PUBLIC_PATHS (middleware.py:30-32) and have no rate limiting, throttling, or account lockout, so credential brute-forcing is unbounded by the application; PBKDF2 at 120k iterations slows each guess but does not cap attempt volume. Separately, register_auth permits unauthenticated account creation for the VIEWER role: when payload.role == Role.VIEWER the authorization branch is skipped entirely (routes/auth.py:44-67), so any anonymous caller can create accounts without limit, which combined with the missing rate limit allows trivial user-table flooding.
  - Fix: Add per-IP/per-account rate limiting and exponential backoff or temporary lockout on /auth/login and /auth/register, and gate viewer self-registration behind an explicit deployment flag if open signup is not intended.
- **Unauthenticated /metrics endpoint discloses store row counts and auth configuration** — `src/lab_tracker/app_parts/observability.py:196` (security, security-sweep)
  - The /metrics route is registered with no authentication and is listed in _PUBLIC_PATHS (middleware.py:28). It returns exact row counts for every store (projects, questions, datasets, notes, sessions, analyses, claims, visualizations, etc.) plus the deployment's auth-enabled flag. For a non-local deployment this leaks the size and shape of the research record and whether auth is on to any unauthenticated network client.
  - Fix: Require authentication (or a separate metrics token / network restriction) for /metrics when auth is enabled, or reduce the payload to a non-sensitive health signal without per-entity counts.
- **get_graph_draft_batch_settings writes default settings during a read-authorized request and races on the project unique constraint** — `src/lab_tracker/services/graph_draft_service.py:325` (data-integrity, integrity-sweep)
  - The settings getter requires only read access but performs a write: when no row exists it creates and saves default settings. Besides being a side-effecting GET (a project reader, not owner, triggers persistent state creation attributed via updated_by), it is a check-then-insert race: two concurrent first reads of the same project's settings both observe None and both insert, and the second commit violates uq_graph_draft_batch_settings_project (db_models.py:354), producing an unhandled IntegrityError 500. The same check-then-insert shape exists for batch runs (lines 408-423 against uq_graph_draft_batch_runs_batch_key) and batch change sets (lines 226-231 against uq_graph_change_sets_batch_key); the unique constraints keep data consistent but concurrent callers get 500s instead of the existing record.
  - Fix: Return computed defaults without persisting them on read (persist only on update), or catch IntegrityError on the insert and re-read the winning row.
- **register_acquisition_output scans every output of the session per file and races to IntegrityError instead of upserting** — `src/lab_tracker/services/session_service.py:60` (performance, integrity-sweep)
  - _find_existing_acquisition_output loads all acquisition outputs for the session (limit=None) and linearly scans them in Python to find one (session_id, file_path) pair, even though uq_acquisition_outputs_session_path (db_models.py:455-461) gives an indexed direct lookup. The AcquisitionOutputWatcher calls register_acquisition_output once per new/changed file on a 1-second poll loop (acquisition_watcher.py:85-91), so a session with N outputs costs O(N) rows fetched per registration, O(N^2) per burst of new files. Additionally the check-then-insert is racy: two concurrent registrations of the same path (e.g. two watcher instances, or watcher plus manual API call) both miss the existing row and the second insert fails with an unhandled IntegrityError instead of converging on the update path the code already implements.
  - Fix: Add a repository method that selects directly on (session_id, file_path) — the unique constraint already indexes it — and catch IntegrityError on insert to fall back to the update path.
- **list_question_refactors runs the full listing query twice just to compute total** — `src/lab_tracker/routes/questions.py:156` (performance, perf-sweep)
  - The endpoint first fetches the requested page, then calls list_question_refactors a second time with limit=None to fetch every refactor row solely to take len() for the pagination total. The repository's query_question_refactors already executes a dedicated COUNT statement and returns (rows, total) (sqlalchemy_repository_parts/core.py:355-357); the service layer (services/question_service.py:216-223 via query_from_repository) discards the total, forcing the route into the redundant unbounded second query — three queries and a full-table hydration where one paginated query plus the existing count suffices.
  - Fix: Expose the (items, total) tuple from the service (or add a count method) so the route can return the repository's already-computed total without re-fetching all rows.
- **internal-boundaries.md links to useProjectWorkspaceActions.js, which does not exist** — `docs/internal-boundaries.md:100` (docs, docs-consistency)
  - The Frontend Data Loading section documents three workspace hooks, but the third file is gone: src/lab_tracker/frontend_src/hooks/ contains useProjectWorkspaceData.js and useProjectWorkspaceForms.js, while mutations/refresh behavior now lives in useProjectActions.js, useNoteActions.js, useQuestionActions.js, useSessionActions.js, useAnalysisWorkflow.js, and useDatasetWorkflow.js. The only reference to useProjectWorkspaceActions anywhere in the repo is this doc line, so the markdown link is dead. The same doc's Repository Layout list (lines 34-40) is also stale: sqlalchemy_repository_parts/ now additionally contains goals.py, graph_drafts.py, and graph_batches.py, which are not listed.
  - Fix: Replace the dead link with the actual per-resource action hooks (useProjectActions.js, useNoteActions.js, useQuestionActions.js, useSessionActions.js, etc.), and add goals.py, graph_drafts.py, and graph_batches.py to the Repository Layout module list.
- **README claims OpenAI key is required for graph drafts and omits the implemented multi-provider config (Anthropic/Google, graph_draft_provider, bootstrap token, public base URL)** — `README.md:146` (docs, docs-consistency)
  - README's Configuration section states "LAB_TRACKER_OPENAI_API_KEY: required for graph draft generation and voice-note transcription" and the feature blurb (line 11) says capture "asks GPT for reviewable draft operations." The code now supports three drafting providers selected by LAB_TRACKER_GRAPH_DRAFT_PROVIDER: src/lab_tracker/config.py lines 22-36 define graph_draft_provider (default "openai"), anthropic_api_key/anthropic_model/anthropic_base_url/anthropic_timeout_seconds, and google_* equivalents, and src/lab_tracker/graph_drafting.py implements AnthropicGraphDraftClient (line 410) and GoogleGraphDraftClient (line 553). So the OpenAI key is only required for the default provider (it remains required for voice transcription, since Anthropic drafting raises on audio). The Configuration list also omits LAB_TRACKER_GRAPH_DRAFT_PROVIDER, all LAB_TRACKER_ANTHROPIC_* and LAB_TRACKER_GOOGLE_* vars, LAB_TRACKER_PUBLIC_BASE_URL, and LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN, all of which are live settings.
  - Fix: Document LAB_TRACKER_GRAPH_DRAFT_PROVIDER and the Anthropic/Google variable sets (plus LAB_TRACKER_PUBLIC_BASE_URL and LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN), and reword the OpenAI-key bullet to "required when graph_draft_provider=openai, and always required for voice transcription."
- **Entrypoint retries a deterministically failing migration 30 times, then compose restart policy loops the container forever** — `docker-entrypoint.sh:8` (bug, gap:deploy-pipeline)
  - The until-loop retries `alembic upgrade head` up to MIGRATION_MAX_ATTEMPTS (30) with a 2s sleep regardless of why it failed. The retry exists for the DB-not-ready race, but a genuinely broken migration (failing DDL, bad revision chain, multiple heads) is re-executed identically 30 times (~60s) before the container exits — and docker-compose.yml then restarts it (restart: unless-stopped, line 21), so a bad migration becomes an infinite restart loop hammering Postgres rather than a clear fast failure. The compose depends_on already gates on postgres service_healthy, so the connection-race window the loop targets is largely covered for the compose path.
  - Fix: Separate readiness from migration: poll DB connectivity first (e.g. a short python -c psycopg connect loop), then run `alembic upgrade head` exactly once and exit immediately on failure so the error surfaces on the first attempt.
- **Container runs the app as root — no USER directive in the Dockerfile** — `Dockerfile:27` (security, gap:deploy-pipeline)
  - The image never creates or switches to an unprivileged user, so uvicorn, the migration step, and everything reachable through an app compromise run as root inside the container, with root ownership of the mounted note_storage volume (/app/data/note_storage). For a network-exposed FastAPI service (published 0.0.0.0:8000 by compose) this is a standard, cheap hardening gap: any code-execution bug in the app or its dependencies immediately yields container-root.
  - Fix: Add a non-root user (e.g. `RUN useradd --create-home --uid 10001 appuser`), chown /app/data, and add `USER appuser` before ENTRYPOINT. The entrypoint and uvicorn need no root privileges (port 8000 is unprivileged).

## Refuted findings

- **Re-pairing a device can race useAuthSession into deleting the freshly stored device secret** — `src/lab_tracker/frontend_src/features/enroll.jsx` (frontend-features-b)
  - Why refuted: The quoted code is accurate but the claimed race is unreachable. The destructive path exists: useAuthSession.js lines 50-58 call setToken("") when /auth/me rejects with a truthy boot token, and the effect at lines 24-30 runs localStorage.removeItem(TOKEN_STORAGE_KEY); a revoked device secret does yield 401 (src/lab_tracker/auth.py line 487 returns None for revoked tokens; middleware.py line 79 raises AuthError). However, app-shell.jsx line 288 gates rendering on auth.authChecked: while !authChecked, only WorkflowCoverageCard renders — EnrollPage (line 296) mounts only after authChecked flips true. authChecked is set true exclusively in the .finally (useAuthSession.js lines 60-64) of the same /auth/me promise whose .catch performs setToken(""), and .catch runs strictly before .finally. So in the re-pair scenario the ordering is forced: stale-token /auth/me settles → setToken("") → authChecked=true → EnrollPage mounts → consume POST is sent → setItem(new secret) in its .then (enroll.jsx line 59). The removeItem therefore always executes before the consume request is even dispatched, removing only the stale secret. Even in the tightest interleaving (token clear and authChecked batched into one commit, child mount effect firing the POST before the parent removeItem effect), the consume .then is an async callback that cannot run until the synchronous effect flush containing removeItem completes — setItem strictly follows removeItem. After the clear, token state is ""; the re-fired token-less /auth/me rejection hits the `if (token)` guard (line 54) and performs no removal, and grep confirms useAuthSession.js line 29 is the only production remover of TOKEN_STORAGE_KEY. There is no reachable interleaving in which a truthy-token /auth/me rejection lands after the new secret is stored: SPA navigation to /app/enroll does not re-fire /auth/me (effect deps [setBusy, setFlash, token] are stable), a QR scan is a fresh page load that goes through the same gated boot, and there is no StrictMode double-invocation (app.jsx uses plain createRoot). Actual re-pair behavior is benign: stale secret cleared, transient "Invalid device token." flash, pairing succeeds, new secret survives the forced reload. Finding is not real; severity adjusted to low only as a formality since the structured field requires a value — there is no user-visible defect beyond the transient flash message.

## Completeness critic

The review's coverage of the Python service layer, routes, migrations, and frontend_src is thorough, but four concrete blind spots remain. (1) Nothing covered the deployment/packaging/CI pipeline: Dockerfile, docker-compose.yml, docker-entrypoint.sh, .github/workflows/ci.yml, pyproject packaging metadata, package.json, scripts/build-frontend.mjs, scripts/serve-lan.ps1. I verified a real bug here already: [tool.setuptools.package-data] in pyproject.toml ships only index.html/app.js/app.css/styles.css, so the Docker image (`uv pip install --system .`) is missing sw.js, manifest.json, and all icons that app_parts/frontend.py serves — and CI runs zero frontend checks and no bundle-freshness check, while docker-compose defaults LAB_TRACKER_AUTH_SECRET_KEY to 'replace-with-a-strong-secret' under environment=production. (2) The ASGI request lifecycle was never examined as a unit: app_parts/middleware.py does synchronous DB lookups inside async middleware on every authenticated request (event-loop blocking), _is_public_path() exempts any path prefixed /app/, /docs/, /redoc/, or /_test/ (no /_test/ routes exist), and db.py creates SQLite engines with check_same_thread=False and no busy-timeout while the db-session middleware's request_scope/complete_response interplay with StreamingResponse bodies is unreviewed. (3) The instruction 'do NOT review the bundle' excluded src/lab_tracker/frontend entirely, but sw.js, index.html, manifest.json, and styles.css there are hand-maintained source — scripts/build-frontend.mjs generates only app.js. The service worker's manually coordinated CACHE_VERSION/?v= scheme (currently v16 vs v17) and its share-target/IndexedDB handler got zero review despite the offline-PWA story being a major product surface. (4) The security sweep missed the browser-facing uploaded-content serving lens: routes/visualizations.py serves user-uploaded assets with Content-Disposition: inline and the client-supplied content type (stored XSS via text/html or scripted SVG upload), there is no X-Content-Type-Options/CSP anywhere in the backend, and the frontend keeps the bearer token in localStorage — a same-origin XSS steals it.

## Per-area assessments

### Application core, config, startup

The application core is generally well built: startup wiring is explicit (build_app_runtime → app.state), the per-request DB session middleware correctly commits on <400 / rolls back on >=400 and on unhandled exceptions via the request-scope context manager, deferred after-commit/rollback actions are isolated so cleanup failures cannot flip a request outcome, the lifespan disposes the engine, error handlers return crafted domain messages without stack traces, and static/SPA route ordering (mount before catch-all, no-store on the app shell and service worker) is correct. The notable problems are configuration/exposure issues rather than lifecycle bugs: the auth-secret validator's environment-scoped check permits the committed default HMAC key while auth is enabled (combined with the fixed-UUID local admin row this allows token forgery), and the unauthenticated observability endpoints leak internal paths, raw SQLAlchemy errors, and store counts, plus a few smaller items (test-path auth bypass prefix, wasteful readiness COUNT sweep, dead import-time engine in db.py).

### Authentication and authorization

The token/credential primitives are mostly sound: passwords use PBKDF2-SHA256 with per-user salt and hmac.compare_digest, JWT verification uses constant-time signature comparison and always re-signs with its own HMAC key (no alg-confusion), device secrets are stored only as SHA-256 hashes, and the bootstrap-admin flow is gated correctly. The significant problems are in authorization rather than authentication: write paths for questions, sessions, datasets, analyses, claims, and visualizations enforce only the coarse global role and skip the per-project membership check that notes/goals use, enabling cross-project writes and project-VIEWER privilege escalation; secondary issues are the default signing secret being accepted when auth is enabled under the 'local' environment and a check-then-act race in device-enrollment consumption.

### Entity routes

Entity route handlers are structurally consistent (envelope responses, shared pagination helpers, service-level validation), but project-scoped authorization is enforced unevenly: notes correctly require contributor membership for writes, while questions, sessions, and datasets can be created in any project by any global editor and mutated or deleted by read-only project viewers. Beyond authorization, the file-handling paths have a reproducible 500 on download of non-latin-1 filenames, no upload size limits, and every list endpoint loads the full table into memory before paginating.

### Knowledge routes

The knowledge routes are structurally sound: search is injection-safe (parameterized ilike with escaped %/_ wildcards via substring_pattern), goal/link validation is thorough (service-side contributor/owner checks, same-project target validation, duplicate-link detection), and the repositories batch child-row loading correctly. The real problems are correctness and scaling at the route/seam layer: /search offset pagination silently returns empty or truncated pages for any project-scoped query, the analysis-commit flow silently drops answers_question_ids from submitted claims, the provenance export both 500s on malformed external_artifacts metadata and emits visualization-asset terms absent from its JSON-LD @context (losing checksums under expansion), and the analyses/claims/visualizations list endpoints fetch unbounded result sets with /visualizations adding a 3-queries-per-row access recheck.

### Graph drafts and batches

The graph drafts/batches subsystem has solid single-request semantics — the request-scoped transaction (api.py LabTrackerRequestScope) makes draft commits atomic over HTTP, and batch_key unique constraints plus the run-key dedupe give reasonable idempotency for identical windows. The real problems are concurrency and scheduling-lifecycle gaps: nothing prevents two concurrent commits of the same change set from double-applying operations, the run-due loop has no per-project error isolation (an orphaned settings row for a deleted project on the default SQLite backend, where FK cascades are unenforced, permanently wedges the scheduler while re-spending LLM calls each tick), unvalidated manual run windows and the 100-note truncation can silently advance the watermark past notes that are then never drafted, and unwrapped httpx transport errors both defeat the batch retry loop and strand DRAFTING change sets that poison their batch_key.

### Entity services

The entity services are cleanly structured (consistent unit-of-work usage, good status-transition tables, careful manifest hashing), but enforcement of cross-entity invariants is uneven: delete paths perform no referential checks and lean entirely on DB-level cascades that either 500 (Postgres, missing ondelete on datasets/sessions.primary_question_id) or never fire at all (default SQLite runtime lacks PRAGMA foreign_keys=ON), so question/dataset/project deletion can orphan or silently corrupt committed provenance records. Authorization is also inconsistent — only NoteService enforces project-membership writes while question/session/dataset writes accept any global editor — and a few invariants (last project owner, committed-dataset immutability, manifest file preservation on commit) are enforced on some code paths but bypassable on adjacent ones.

### Knowledge services

The knowledge services (analysis, claim, visualization, goal) have clear invariant-driven design, and the recent goal-link uniqueness fix is complete and consistent on the goal side: the in-memory _ensure_unique_goal_links check, the slot normalization (None <-> ''), and the new DB unique constraint all agree, including the update_goal_link relation/slot-collision path. The remaining problems cluster around the older trio of services: claim/analysis/visualization writes bypass the project-membership authorization that goals and notes enforce (any global editor can write to any project), update_claim cannot clear support links due to a truthiness fallback, delete_analysis silently strips evidence from SUPPORTED claims via DB cascade, commit_analysis drops inline answers_question_ids, goal links dangle after their target entities are deleted, and the committed-analysis immutability rule is bypassable by archiving first.

### Persistence layer

The persistence layer is generally well built: every repository batch-loads child rows (parent maps, link maps, target maps) so there are no N+1 patterns, counts/pagination are computed correctly with mirrored count statements, search input is properly escaped for ILIKE, and unique constraints (goal links with NULL-slot normalization, batch keys, usernames) match what the services assume. The two significant problems are deletion integrity — SQLite (the default and documented single-client runtime) never gets PRAGMA foreign_keys=ON so every ondelete CASCADE/SET NULL rule the delete flows depend on is a no-op (empirically verified orphans), and questions referenced as a dataset/session primary_question_id can be deleted with no guard (500 on Postgres, dangling references on SQLite) — plus a timezone-normalization drift where the graph-draft, batch-run, and membership mappers skip the _as_utc conversion all other mappers apply.

### Alembic migrations

The migration chain itself is healthy: 25 revisions resolve to the single head 0024_graph_draft_batches, the 0017_daily_graph_reviews/0017_device_tokens fork is correctly reconciled by the no-op 0019 merge revision, dialect-sensitive operations (boolean CASE backfills in 0008, batch_alter_table for SQLite, the Postgres-only alembic_version widening in 0015 that lands just in time for the >32-char revision ids) are handled, and tests enforce both the single head and the fork-upgrade path. The genuine problems are drift and one fragile data migration: migrations from 0014 onward systematically create columns nullable that the ORM declares NOT NULL (so production schemas are looser than the metadata-built schemas the test suite runs against), the dead daily_graph_reviews tables are created but never dropped and have no model, batch_key uniqueness is a differently-named index vs constraint across the two schema lineages, and 0023's NULL-slot rewrite can abort with a unique violation on databases that accumulated duplicate unslotted goal links during the very regression window it fixes.

### File storage, watcher, dolt mirror

The storage layer is structurally sound (UUID-sharded paths, no user-controlled filenames in paths, temp-file-then-rename writes), but it leaks orphan temp files on failed uploads and the metadata sidecar is non-transactional with the data write. The acquisition watcher and dolt mirror have weaker operational stories: the watcher loop dies permanently on any API/DB error and can persist a checksum/size pair from two different file states, while the dolt mirror never reconciles schema changes after a table is first created and exports without snapshot isolation, so a live database can produce referentially inconsistent or permanently failing mirror syncs.

### MCP tools and decision context

The MCP tool layer is a thin, mostly clean pass-through to the HTTP API, but the decision-context subsystem has real selection-logic defects: every "recent_activity" section is actually oldest-first (the repository orders ascending by created_at), anchor resolution is a bounded 500-row list scan that fails for scoped multi-project users and for entities beyond the window, and project auto-resolution trusts a truncated search sample. On the MCP side, the get_decision_context wrapper flattens validation and permission errors into a misleading "unavailable" code, field-level 422 details are discarded for all tools, and the per-call client construction forces a fresh /auth/login round-trip on every tool invocation.

### Client library and CLI

The client library's HTTP contract is in good shape: every path (/auth/login, /projects, /questions, /notes, /notes/quick-capture, /sessions, /datasets, /analyses, /claims, /visualizations, /health, /readiness), query parameter, payload field, and envelope shape was verified against the FastAPI routes and schemas, and the build/serve/icon scripts are correct. The real issues are internal to the client: LTRecord.id returns the wrong UUID for visualization records, the package performs fallible env-dependent work at import time, the client reads a base-URL env var that no documented setup defines (while falling back to the documented MCP credential vars), list-method limit/offset semantics silently diverge from the server contract, and transport errors bypass the LTError hierarchy so the CLI dumps raw tracebacks.

### Frontend core

The frontend core is generally well-structured — most data hooks use request-id guards and cancellation flags, the API layer matches backend routes (envelope shape, pagination caps, auth contract all verified), and no XSS sinks exist in this area. However, the offline/PWA machinery has a serious defect (awaiting a `success` event on an IndexedDB transaction, which never fires, hanging the offline capture flow), and the auth session hook both destroys the stored token on transient network failures — defeating the offline-first design — and has no handling for the 60-minute token expiry.

### Frontend features (sessions/datasets/questions/analysis)

The sessions/datasets/questions/analysis features are generally well built — request cancellation, per-project state resets, and pagination via fetchAllPages are handled carefully, and panel/detail prop wiring in app-shell.jsx and WorkspaceHome.jsx is consistent. The main problems are mutation/view desyncs: the session detail card never reflects a successful close or promote, the analysis visualization loader's shared request counter can permanently strand a section in a loading state, and the "Recent Committed" analyses window is computed from created_at ordering so a just-committed old analysis can be silently omitted.

### Frontend features (goals/drafts/capture/graph/misc)

The reviewed frontend features are generally well-structured (clean cancellation patterns, memoized graph layout, device flows that match the backend device_auth routes), but the capture/upload path has real data-integrity gaps: the offline queue permanently drops captures on any 4xx including auth/rate-limit responses, and multi-step capture uploads duplicate notes on partial failure. Secondary issues include role gating for graph-draft review being derived from the wrong project, an O(k^2+) layout collision loop combined with non-virtualized ReactFlow rendering on full graphs that the backend returns unbounded, and a localStorage race that can wipe a freshly paired device secret.

### Test suite audit

The suite is generally strong — auth, graph drafts, dataset/visualization file storage, migrations, and the React shell all have substantive behavioral tests, including good negative cases (token expiry, raw-asset rollback, outsider download denial). The biggest weaknesses are that the newest feature (scheduled graph-draft batch runs, POST /batches/run-due and its cadence/timezone scheduling) shipped with zero coverage, almost every list endpoint is tested only through an always-authorized admin fixture that bypasses the project-scoping filter, supported session-lifecycle and member-management HTTP routes are unexercised, and three batch tests carry hardcoded 2026 dates that will start failing after 2026-12-30.

### Cross-cutting security sweep

The backend is largely solid on classic injection and file-handling vectors: SQL goes through SQLAlchemy with properly escaped LIKE patterns, file storage paths derive from server-generated UUIDs (no path traversal), attachment filenames are sanitized, JWT verification ignores the client alg header (no alg-confusion), device-token and enrollment secrets use 256-bit entropy with SHA-256 hashing, and the only subprocess use (Dolt mirror) takes no user input. The significant issue is authorization: analysis, claim, and visualization write paths gate on the global EDITOR role instead of project-scoped contributor membership, enabling cross-project writes and in-project viewer-to-writer escalation, alongside a config gap that permits the default JWT secret when auth is enabled locally.

### Cross-cutting integrity sweep

The service layer is generally disciplined: HTTP requests run in a single request-scoped transaction via middleware, multi-entity flows like question refactors persist through one unit of work, and most app-level uniqueness checks (batch keys, goal links, usernames) are backed by real DB unique constraints. The genuine weaknesses are at the DB-enforcement boundary and in delete flows: SQLite (the default backend) never gets PRAGMA foreign_keys=ON so every FK/CASCADE rule is unenforced, primary_question_id FKs lack ondelete policies and delete_question has no guard, entity deletions strand polymorphic note/goal references and silently strip SUPPORTED-claim evidence, and a handful of check-then-act spots (last-owner deletion, settings/output upserts, the multi-project scheduled batch runner with external LLM calls inside one transaction) can lose or duplicate work under concurrency or partial failure.

### Cross-cutting performance sweep

The data layer is in good shape — repositories batch child collections via IN-clause maps, hot filter columns have composite indexes, and the frontend hooks/project-graph are well-memoized with stale-request guards and no per-item fetch loops or render storms. The systemic problem is the HTTP route layer: nearly every list endpoint (notes, questions, sessions, datasets, analyses, claims, visualizations, projects, graph-drafts) passes limit=None to the repository and paginates in memory after Python-side access filtering, which combines with the frontend's fetchAllPages into quadratic-cost reads, and is amplified by large-payload offenders (graph-draft context packets in list responses, untruncated transcripts as graph node labels) and per-project query loops in search and the graph-draft context builder.

### Docs vs reality

Setup and command documentation is in good shape: I executed or inspected every documented command path (uv/pip/alembic invocations, python -m alembic on Windows venv, npm scripts, docker compose services, scripts/serve-lan.ps1 -UsePostgres, lab_tracker init / lt-mcp / dolt_mirror entry points in pyproject and cli.py, MCP env vars, bd bootstrap/init/update flags against bd 1.0.4, .beads/config.yaml keys) and all match reality, as do CLAUDE.md's migration-fork claims (0017 fork merged by 0019) and internal-boundaries' request-scope lifecycle (verified in app_parts/middleware.py and routes/shared.py with no ContextVar or root-API bypasses). The real drift is in the authority docs themselves: docs/retained-v1-surface.md no longer describes the shipped default runtime (goals, scheduled graph-draft batches, device enrollment, project graph, assistant context, provenance routes are all registered but unlisted, and the batches feature directly contradicts the "on-demand, not a standing inbox" deferral language), and internal-boundaries.md/README contain stale references (a dead link to useProjectWorkspaceActions.js, an env-var list missing the multi-provider drafting configuration).
