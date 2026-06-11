# Build-vs-Buy Boundaries

Validated: June 1, 2026.

This document records the Lab Tracker responsibility boundary decisions from
`lab-tracker-m3l`. It extends the retained-v1 surface rather than replacing it.

## Core Principle

Lab Tracker owns the semantic provenance spine:

`questions -> notes -> sessions -> datasets -> analyses -> claims -> visualizations`

The irreducible product core is:

- Questions as broad-to-atomic inquiry structure.
- Claims as the epistemic layer that links evidence to conclusions and confidence.
- PROV-O / JSON-LD export and ingestion edges that connect artifacts to questions,
  datasets, analyses, claims, and visualizations.

For adjacent responsibilities, use pointer-not-reimplementation:

- Store a reference to the external artifact: canonical URI/key plus content hash.
- Store only the semantic edges the external tool does not model.
- Do not rebuild the external tool's main workflow or UI in Lab Tracker.

The PROV-O export and ingestion vocabulary is the integration seam.

## Decision Table

| Area | Outcome | Boundary |
| --- | --- | --- |
| Auth and RBAC | Keep bespoke retained-v1 auth for now; prepare to offload credential/session/device-grant plumbing later. | Lab Tracker keeps project membership and project-role edges. A future auth provider/library should own credentials, token/session issuance, password resets, and standards-based device authorization. |
| Byte and asset storage | Integrate behind a pluggable object-storage interface; keep local filesystem fallback. | Lab Tracker stores stable object references and checksums. Durability, transfer, lifecycle, and cross-workstation availability belong to S3-compatible/object storage. |
| Dataset versioning | Integrate via optional adapters; prefer DataLad/DANDI for neuroscience datasets, DVC for local/Git-adjacent workflows, and lakeFS for object-store/lakehouse teams. | Dataset bytes and version history stay in the substrate. Lab Tracker ingests manifests and keeps question/claim/session/note edges plus NWB/BIDS metadata. |
| Experiment tracking | Integrate via adapter, with MLflow as the default open-source run reference target and W&B as a supported external reference. Keep bespoke analysis records only as semantic graph activities. | External trackers own run metrics, params, artifacts, and run UI. Lab Tracker stores run URI/id, code/environment pointers, dataset usage, and claim support edges. |
| ELN and lab notebook scope | Link/integrate; do not become an ELN. | ELNs own rich notebook pages, signatures, templates, collaboration, and compliance workflows. Lab Tracker notes remain lightweight graph/evidence notes with optional external ELN entry URI/hash and entity targets. |
| PROV-O external ingestion | Build in-house as a small contract and adapters. | This is part of the semantic core: external artifacts become PROV entities/activities with semantic edges into Lab Tracker's graph. Adapters must stay thin and optional. |

## PROV-O Ingestion Contract

An external artifact reference has this shape:

- `kind`: `entity` or `activity`
- `source_system`: tool/source name, such as `datalad`, `dvc`, `mlflow`,
  `elabftw`, or `s3`
- `uri`: canonical external URI, run URL, object key URL, manifest URI, or ELN
  entry URL
- `content_hash`: stable digest of the artifact or manifest
- `metadata`: tool-native metadata as a JSON-compatible object

For retained-v1, dataset ingestion stores these references in
`DatasetCommitManifest.external_artifacts`. The older
`DatasetCommitManifest.metadata["external_artifacts"]` encoded JSON shape is a
legacy read/export fallback for existing rows. The PROV-O exporter materializes
both shapes as `prov:Entity` or `prov:Activity` nodes and links them from the
dataset commit activity using `prov:used` or `prov:wasInformedBy`.

This lets Lab Tracker track 1TB-scale acquisition runs through canonical URIs,
manifest/content hashes, and semantic graph edges while leaving byte durability,
transfer, lifecycle, and browsing to object stores or data-versioning
substrates.

## Anti-Scope Guardrails

Lab Tracker should not add:

- Identity-provider administration screens, password reset products, or a custom
  standards-stack beyond retained-v1 local auth.
- Object-store lifecycle, replication, or transfer management.
- DVC/DataLad/lakeFS status dashboards or version-control UI.
- MLflow/W&B/Sacred-style run logging, metric charts, artifact browsing, or model
  registry features.
- ELN page editors, wet-lab templates, signatures, inventory, compliance, or rich
  document collaboration.

It may add:

- Adapter configuration.
- Manifest/run/entry import actions.
- Stable external URI and content-hash storage.
- Semantic edges to questions, notes, datasets, analyses, claims, and visualizations.
- PROV-O export/ingestion tests that prove the graph edge is preserved.

## Tool Landscape Validation

Current names were checked against official sources before recording these
decisions:

- OAuth 2.0 Device Authorization Grant: RFC 8628 remains the standards reference:
  https://datatracker.ietf.org/doc/html/rfc8628
- FastAPI Users: official docs now describe the project as maintenance-mode, so it
  is not the preferred long-term target for new auth work:
  https://fastapi-users.github.io/fastapi-users/dev/
- Keycloak: active documentation lists the current 26.6.2 release:
  https://www.keycloak.org/documentation
- authentik: active documentation lists the current 2026.5 release:
  https://docs.goauthentik.io/
- Clerk and Auth0 remain current SaaS auth options:
  https://clerk.com/docs/how-clerk-works/overview and
  https://auth0.com/docs/authentication
- DVC `.dvc` files remain a manifest/pointer substrate for data tracked with Git:
  https://doc.dvc.org/user-guide/project-structure/dvc-files
- DataLad remains an active distributed data-management system:
  https://www.datalad.org/
- DANDI remains the neuroscience archive around NWB/Dandisets:
  https://docs.dandiarchive.org/
- lakeFS remains an object-store/lakehouse versioning option:
  https://docs.lakefs.io/latest/project/
- MLflow and W&B remain active experiment-tracking platforms:
  https://mlflow.org/docs/latest/ml/tracking/ and https://docs.wandb.ai/
- Sacred remains a lighter Python experiment framework but is not the default
  integration target:
  https://sacred.readthedocs.io/en/stable/experiment.html
- S3 and MinIO remain object-storage targets:
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/ and
  https://docs.min.io/community/minio-object-store/
- eLabFTW, Benchling, RSpace, and LabArchives remain active ELN references:
  https://doc.elabftw.net/, https://www.benchling.com/notebook,
  https://www.researchspace.com/features, and https://www.labarchives.com/
