# Marion deployment

This Compose project is the dedicated Lab Tracker instance served at
`https://lab-tracker.tail79f9d8.ts.net:8443`. It is deliberately separate from
the general instance on port 443.

## Local secrets

Copy `deployments/marion/.env.example` to `deployments/marion/.env` and replace
the placeholder with the existing Marion database password:

```dotenv
MARION_POSTGRES_PASSWORD=<random database password>
```

The provider credential remains in the ignored
`deployments/shared-provider/.env`. Neither file belongs in an image or commit.

## Back up and verify

From the repository root, run:

```bash
deployments/marion/backup.sh
deployments/marion/restore-smoke.sh \
  backups/marion/<timestamp-printed-by-backup>
```

The script writes a timestamped Postgres custom-format dump and app-data
archive under `backups/marion/`, validates both archives, and prints their
SHA-256 digests. The restore smoke script then restores both artifacts into
uniquely named disposable Docker resources, checks the migration and user
tables, and removes those scratch resources. A release is blocked until both
commands succeed.

## Build a reviewed image

Use the exact clean commit being released:

```bash
revision="$(git rev-parse HEAD)"
test -z "$(git status --porcelain)"
source_version="$(sed -n 's/^version = "\([^"]*\)"$/\1/p' pyproject.toml)"
git archive --format=tar "${revision}" | docker build \
  --build-arg "LAB_TRACKER_SOURCE_REVISION=${revision}" \
  --build-arg "LAB_TRACKER_SOURCE_VERSION=${source_version}" \
  --tag "lab-tracker-marion:sha-${revision}" \
  -
```

Never reuse `lab-tracker-app:latest` for this deployment. Confirm the image
label before deployment:

```bash
docker image inspect "lab-tracker-marion:sha-${revision}" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

## Deploy

```bash
deployments/marion/release.sh
```

The release script refuses a dirty worktree or an unpushed revision, takes a
fresh backup, runs the disposable restore smoke against that exact backup,
builds a Git archive of the exact commit with revision labels, deploys the
revision-specific image, and checks database migrations, user presence, storage
writes, scheduler/provider readiness, and both local and public deployment
identity. It also verifies that the persisted auth-secret identity did not
change, preserving existing logins and tokens. If a post-cutover gate fails, it
restores the previous app image automatically; the validated backup remains the
recovery point for any database-level rollback. The Compose file requires
`MARION_IMAGE`; an omitted tag fails closed rather than silently selecting a
mutable image.

After deployment, also verify database migration head, scheduler readiness,
setup-readiness revision reporting, and a non-sensitive synthetic graph draft
before issuing an invitation.

The deployment currently supplies a server-side provider credential from the
ignored shared-provider environment. Marion does not need to configure a local
OpenAI key to use Lab Tracker's drafting features. The operator remains
responsible for provider access, cost, and credential rotation; secrets must
never be shown in the setup UI or invitation.
