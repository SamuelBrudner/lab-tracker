# Build-vs-Buy Boundaries

Validated: June 1, 2026. Pipeline and lineage boundary added June 26, 2026.
Agentic-data tool landscape appended July 6, 2026.

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
| Pipeline / workflow orchestration | Integrate via a framework hook; do not build. Kedro is the reference data-pipeline framework; DVC pipelines, Snakemake, and Nextflow are adjacent targets. | The framework owns the data catalog, node/pipeline authoring, runners/execution, and the mechanical file-to-file lineage DAG. Lab Tracker ingests the inputs/outputs a run *declares* (via the hook) as PROV-O `used`/`wasGeneratedBy` edges onto questions, datasets, analyses, and claims. See [Pipeline and Lineage Boundary](#pipeline-and-lineage-boundary). |
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

## Pipeline and Lineage Boundary

Input-to-output lineage is where Lab Tracker most resembles a data-pipeline tool
(Kedro, DVC pipelines, MLflow), so the boundary needs to be explicit. The
distinction is altitude, not subject:

- **Mechanical lineage** — the file-to-file transformation DAG
  (`raw.h5 -> clean.parquet -> features.parquet -> model.pt`), every
  intermediate, execution-shaped. This belongs to the pipeline/versioning tool.
  Lab Tracker does not build a data catalog, a runner, or versioned byte storage
  to reconstruct it.
- **Epistemic lineage** — `question -> dataset -> analysis -> claim`, the
  human-curated edges that terminate on a question or a claim. This is the
  product core and already lives in the graph: datasets name their question,
  claims name their evidence, analyses inherit questions from their data.

Litmus test for "is this edge ours to draw":

> Does the edge terminate on a question or a claim? If yes, it is epistemic —
> Lab Tracker records it. If it is file-to-file with no epistemic node on either
> end, it is mechanical — defer it to the pipeline tool, or ingest it through
> that tool's hook.

The rule is: **defer the mechanical lineage, keep the epistemic lineage, and
bridge the two with the content hash.** When a captured output's bytes later
reappear as an input — the same content hash on a different machine, path, or
run — the link is proposed in review on the hash match alone, with no read
interception and no catalog. The content hash is the cross-tool, cross-machine
join key; the human-gated review turns a match into a `used`/`wasDerivedFrom`
edge.

Consequences for capture:

- **Output capture stays in-house.** Figures and run outputs become staged
  evidence notes tied to questions; no pipeline tool models that.
- **Explicit input *declaration* is a deferred convenience, not core.** An
  `input(path_or_uri)` call at capture time would pre-populate a `used` edge,
  but most of its value is already covered by the content-hash join and the
  existing graph, and it carries the highest catalog-duplication risk. Add it
  only if review-time inference proves insufficient, and only as a dumb,
  fail-soft declaration — never a load-by-name dataset registry.
- **Never auto-intercept reads** (`open()` / audit hooks). Reads are far noisier
  than writes, and interception drifts toward owning I/O, which is the
  framework's job.

The integration shape is a framework hook: Kedro already knows each node's
declared inputs and outputs, so a hook emits Lab Tracker `used`/`wasGeneratedBy`
edges against questions and claims with no double-entry. Labs that run a pipeline
framework get the epistemic layer for free; labs that run ad-hoc scripts get the
same edges from output capture plus the content-hash join.

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
- A data catalog (load/save datasets by name), a pipeline runner, or a mechanical
  file-to-file lineage DAG that duplicates Kedro/DVC pipelines.

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
- Kedro remains the active reference data-pipeline framework (data catalog,
  nodes/pipelines, runners, Kedro-Viz lineage), checked June 26, 2026:
  https://docs.kedro.org/
- S3 and MinIO remain object-storage targets:
  https://docs.aws.amazon.com/AmazonS3/latest/userguide/ and
  https://docs.min.io/community/minio-object-store/
- eLabFTW, Benchling, RSpace, and LabArchives remain active ELN references:
  https://doc.elabftw.net/, https://www.benchling.com/notebook,
  https://www.researchspace.com/features, and https://www.labarchives.com/

### Agentic-Data Tool Landscape (checked July 6, 2026)

Six agentic-data-stack tools were evaluated against the server-resident
drafting design. None was adopted — the deployment topology (SQLite solo,
zero-sidecar desktop launcher) excludes every external engine among them —
but the facts are recorded here and the fit reasoning lives in
`server-resident-agentic-drafting-design.md` ("Adjacent tool landscape").

- PuppyGraph is a proprietary zero-ETL graph query engine (openCypher/Gremlin)
  over relational sources including Postgres; no SQLite connector, no embedded
  mode, Docker-server-only with an 8 GB developer minimum, free developer
  edition capped at two sources:
  https://docs.puppygraph.com/ and https://www.puppygraph.com/pricing
- Upriver is an enterprise agentic data-engineering SaaS over cloud warehouses
  (Snowflake, Databricks, BigQuery; $14M seed announced June 11, 2026); no
  self-hosted or Postgres/SQLite path:
  https://upriverdata.com/
- Compass (Dagster Labs) is a closed-SaaS, Slack-native, governed text-to-SQL
  analyst (GA November 13, 2025); read-only against the warehouse with a
  human-approved context store:
  https://compass.dagster.io/
- Malloy remains MIT-licensed and actively released (core v0.0.421, July
  2026); Malloy Publisher serves semantic models to agents over MCP with
  read-only grounding tools. TypeScript/Node runtime, no SQLite connector,
  and `malloy-py` stale since February 2025 keep it a design reference
  rather than a dependency:
  https://github.com/malloydata/malloy and
  https://github.com/malloydata/publisher
- fenic (typedef) is an Apache-2.0 Python dataframe framework for batch LLM
  inference — typed structured extraction, rate limiting, caching, row-level
  lineage; embeds in-process with no sidecar. Pre-1.0 (v0.10.0, June 25,
  2026) from a seed-stage vendor:
  https://github.com/typedef-ai/fenic and https://docs.fenic.ai/latest/
- Ascend.io is winding down operations as of mid-2026 (site replaced by a
  shutdown notice; changelog stopped April 21, 2026), a year after its
  "agentic data engineering" relaunch and roughly $54M raised. Removed from
  consideration as a pipeline defer target — the Kedro/DVC/Snakemake
  references above are unaffected — and retained as a cautionary example of
  why adapters to venture-backed platforms must stay thin and optional:
  https://www.ascend.io/
