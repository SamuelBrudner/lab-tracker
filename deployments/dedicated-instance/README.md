# Dedicated-instance release workflow

This directory is a reusable release template for one already-provisioned,
authenticated Lab Tracker instance. It deliberately contains no operator
hostname, account identity, database identity, provider credential, or live
model policy.

The application still needs a TLS reverse proxy in front of the loopback-only
host port. Set `DEDICATED_PUBLIC_BASE_URL` to the externally verified HTTPS
origin; do not put an internal container URL there.

## Configure ignored environment files

Copy both examples and restrict them before using any operational script:

```bash
cp deployments/dedicated-instance/.env.example \
  deployments/dedicated-instance/.env
cp deployments/dedicated-instance/provider/.env.example \
  deployments/dedicated-instance/provider/.env
chmod 600 \
  deployments/dedicated-instance/.env \
  deployments/dedicated-instance/provider/.env
```

The repository's `.gitignore` already ignores files named `.env`. The Docker
build context explicitly ignores these two deployment files as defense in
depth.

The deployment env controls the Compose project and app names, HTTPS public
origin, loopback host port, database user/name/password, model and reasoning
settings, provider timeout, and release-gate timing. Replace every example.
For an existing instance, preserve its current database user, database name,
and URL-safe password; a routine app release must not rotate them.

The provider env contains only:

```dotenv
DEDICATED_OPENAI_API_KEY=<provider credential>
```

Compose reads both files for interpolation but does not use a service-level
`env_file`. It injects only `LAB_TRACKER_OPENAI_API_KEY` into the app container,
so unrelated host or provider credentials cannot leak into the service.

Validate interpolation without printing the rendered configuration:

```bash
DEDICATED_IMAGE=lab-tracker-dedicated:configuration-check \
  docker compose \
    --project-directory deployments/dedicated-instance \
    --env-file deployments/dedicated-instance/.env \
    --env-file deployments/dedicated-instance/provider/.env \
    -f deployments/dedicated-instance/docker-compose.yml \
    config --quiet
```

## Back up and verify

Run:

```bash
deployments/dedicated-instance/backup.sh
deployments/dedicated-instance/restore-smoke.sh \
  backups/dedicated-instance/<compose-project>/<timestamp>
```

The backup script discovers the running app's named data volume instead of
assuming an operator-specific volume name. It pauses the app while it captures
the Postgres custom-format dump and app-data archive, then resumes the app
before validating both artifacts and printing their SHA-256 digests. The pair
therefore describes one maintenance-window checkpoint. Work is written to a
`.partial` directory; only after archive validation and creation of
`MANIFEST.sha256` is that directory atomically renamed as a completed backup.
Restore smoke refuses partial directories and verifies the manifest first.

The restore smoke uses uniquely named disposable Docker resources. It waits
until the scratch Postgres accepts TCP connections on the scratch network (the
path `pg_restore` uses, so the image's socket-only init server cannot pass the
gate), retries `pg_restore` a bounded number of times only on connection
failures, restores
the dump with ownership and grants omitted, checks the migration and user
tables, restores the app-data archive, verifies that it is non-empty, and
removes every scratch resource.

Backups contain plaintext research and authentication data. The scripts create
directories with mode `0700` and artifacts with mode `0600`; operators must
also apply encrypted storage, retention, and off-host recovery policies.

## Release a reviewed revision

This workflow is only for an existing instance with exactly one running app
container, at least one administrator, named persistent app/Postgres volumes,
and a recoverable database. It rejects missing, stopped, or duplicate app
containers. Initial provisioning should follow the standard self-hosted setup
first.

Run from a clean branch whose exact commit is its pushed upstream tip:

```bash
deployments/dedicated-instance/release.sh
```

The release:

1. Takes an instance-scoped release lock and refuses permissive or missing env
   files, placeholder/unsafe database values, a dirty worktree, and an unpushed
   revision.
2. Creates a paused-app checkpoint and proves it with a disposable restore.
3. Builds from `git archive` of that exact commit and checks the immutable
   revision label.
4. Captures the previous running image ID and persisted auth-secret digest.
5. Starts the revision-specific image and waits for container health.
6. Runs the in-container deployment probe for app/revision identity, migration
   head, database users/admin, writable storage, provider credential, and
   scheduler readiness.
7. Retries and verifies the same identity through both the loopback and
   configured public health endpoints.

If a post-cutover gate fails, the script restarts the previous immutable image
ID against the current env files and database and waits for it to become
healthy. That automatic recovery works only while the database is still at a
revision the previous image knows, that is, for a release that adds no Alembic
migration. When the new revision adds an Alembic migration, its entrypoint has
already upgraded the database before any gate runs, and the previous image
cannot start on it: its entrypoint fails at `alembic upgrade head` with "Can't
locate revision", and the app itself refuses a database at an Alembic revision
it does not know. The script then reports that the automatic rollback failed
and prints the validated backup directory it took before cutover.

To recover such a release, stop the app, restore the validated backup
(`postgres.dump` into an empty database and `app-data.tar.gz` into the app-data
volume, following the
[restore steps](../../docs/self-hosted-operations.md#restore) with this
instance's compose project and volume names), and then start the previous
image. Plan every release that adds an Alembic migration as one whose rollback
is that full restore. A configuration change the previous image cannot run
with needs the same recovery.

The provider key is server-held and must never be shown in setup, invitation,
or troubleshooting output. Provider authorization, cost controls, and rotation
remain operator responsibilities.
