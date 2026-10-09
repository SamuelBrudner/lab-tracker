# Supervised maintenance

The operator-side coordinator connects deployment identity, HTTP health,
installed AI model audits, official OpenAI model notices, and backup evidence.
It persists findings independently of the application database and prepares
review packets with proposed setting changes, validation steps, and rollout
requirements. It makes no production configuration or research-record changes.

Install the server package on a host with Docker access to the monitored
instances. Copy [the inventory example](../deployments/maintenance/inventory.example.json)
to a private operator directory and replace its container, URL, approved source
revision, and backup paths. The zero revision is a placeholder that intentionally
fails identity checks. Paths resolve relative to the inventory file. The
inventory contains no provider keys: metadata checks run inside the actual
container using that instance's existing settings.

Run:

    lab-tracker maintenance run --config /absolute/path/inventory.json --json
    lab-tracker maintenance status --config /absolute/path/inventory.json --json
    lab-tracker maintenance run --config /absolute/path/inventory.json --force --quiet

Exit codes are 0 for no active findings, 1 for attention needed (including an
overlapping run), and 2 for a command/configuration/state error. A not-due run
returns the previous attention state. Inventory changes trigger a fresh pass.
The first successful OpenAI changelog read establishes a baseline; later model
entry changes stay pending until every active OpenAI workload has a later
catalog review. Retirement tables are checked from the first pass, independently
of catalog review dates. Unrecognized or unavailable sources produce findings.
Official source tracking currently covers OpenAI; other providers still receive
the shared catalog review and availability checks.

## Findings and proposals

State lives in an owner-private SQLite database in the configured state directory.
Each complete pass records evidence; the most recent 100 passes are retained.
An immediate transaction serializes overlapping scheduler invocations. New or
changed findings and recoveries create JSON and Markdown packets under
`proposals/`. `--quiet` emits only those changes; repeated failures remain visible
through `status` without producing another packet.

A failed probe never resolves a dependent known incident. For example, an
unreachable provider audit cannot resolve an older-model finding, and an
unavailable retirement document cannot resolve a retirement warning. State
errors fail the command and preserve the existing database.

Review packets identify each affected instance and its expected revision.
Known superseded models get concrete before/after setting proposals. Future
release notices require source review and explicit candidate selection. Packets
remain marked `approval_required` and `rollout_ready=false`; a metadata response
and a newer name do not establish an upgrade's quality.

Container probes select only state, image ID, and revision labels. Commands use
argument vectors with bounded output, per-probe deadlines, and a shared run
deadline. HTTP probes refuse redirects, use a total request deadline, and limit
health payloads to 64 KiB and official source documents to 1 MiB.
HTTP health establishes reachability from this operator host. For a Funnel
deployment, retain the independent public-ingress probe described in
[self-hosted operations](self-hosted-operations.md); a successful private
tailnet route does not establish public ingress health.

## Backup evidence

`backup_root` may name one checkpoint directory or a directory whose immediate
children are checkpoints. The coordinator accepts `MANIFEST.sha256` from the
dedicated backup command, or a JSON manifest mapping `postgres.dump` and
`app-data.tar.gz` to their SHA-256 digests. Partial directories, symlink artifacts,
empty files, malformed manifests, and changed/corrupt files fail verification.
A corrupt newest complete checkpoint cannot be masked by an older good one.

Freshness uses the older artifact's modification time, so touching a manifest
does not refresh old data. Digests establish integrity, not snapshot coherence.
Continue using the existing backup procedure, which quiesces application writes.
The coordinator neither creates backups nor performs restores during ordinary
checks.

Backup reads run in a contained subprocess because a blocked filesystem read
cannot be interrupted by a cooperative timer. `backup_timeout_seconds` defaults
to 60 seconds and can be increased to 600 seconds; the shared run deadline also
applies. A deadline failure remains an unverified-backup finding.

An optional `restore_evidence` file binds a completed scratch restore to the
exact manifest. It must contain `backup_manifest` with the same two digests,
`all_restored_rows_preserved=true`, and `scratch_resources_cleaned=true`.
Missing/mismatched restore evidence produces a separate review finding.
Evidence is an operator attestation, not a new restore test performed by the
coordinator. Generate a fresh attestation after restoring each new checkpoint;
an old proof cannot qualify different artifact digests.

## Model evaluation

`maintenance evaluate` reuses the synthetic golden-day fixture in a fresh
in-memory database for each sample. It compares existing note-link precision
and recall, new-question grounding and placement, specific clarification,
duplicate-question creation, and latency. Empty drafts, invalid metrics,
different prompt versions, quality regression, and excessive latency cannot
pass. By default it alternates three baseline/candidate pairs.

Fixture v2 gives the two bench dose captures and figure a researcher-selected
`declared_question_id` in their supplied capture metadata. The three active
dose-response questions remain in context; routing to a different near-duplicate
still counts as an incorrect link. A separate unassigned dose capture has no
anchor and requires a clarification identifying all three question choices.
Guessing a question for that capture fails even if clarification is also present.
The bare identifier requires a specific clarification of its meaning or animal,
sample, recording, or dataset identity. It need not ask again for session
placement already supplied by context.

Same-patch new-question references are graded separately. The meeting supports
partial-agonist saturation and the follow-up supports focal drift versus bath
temperature. Proposal precision counts supported creates and correctly placed
source links; recall requires each supported proposal to be linked to its source.
The originating meeting or follow-up must be cited. Additional motivating dose
captures or drift/temperature observations may be cited within the declared case;
an unrelated capture cannot substitute for or supplement the originating source.
An unrelated source, invented scientific qualifier or numeric dose, duplicate proposal, or link
from an unrelated capture fails this bounded rubric. It accepts declared concept
patterns and a controlled paraphrase vocabulary; this is synthetic grading,
not a general semantic judge. Vocabulary is scoped to each case so combining
unrelated captured concepts does not establish support. Unsupported proposals
do not disappear from scoring.
Link scoring follows the final targets on each existing note. It counts question
targets carried in session or generic updates, and notices later updates that
overwrite earlier links. Reports list missing and unexpected links by stable
synthetic capture/question names.

First exercise the harness without provider calls:

    lab-tracker maintenance evaluate --config /absolute/path/inventory.json \
      --baseline baseline-model --candidate candidate-model --repeat 1 --scripted

For a paid comparison, explicitly opt in and select existing private provider
settings:

    lab-tracker maintenance evaluate --config /absolute/path/inventory.json \
      --baseline gpt-4o-mini --candidate gpt-6.1-sol --repeat 3 --live \
      --provider-env /absolute/path/provider.env

`--reasoning-effort` and `--reasoning-mode` select common OpenAI controls for both
models. Use only controls that both choices support. Compare compatible common
settings first, then additionally validate the planned deployment settings
before rollout.

Use `--candidate-reasoning-effort` and `--candidate-reasoning-mode` to override
controls only for the candidate. For example, comparing the same Sol model with
`--reasoning-effort medium --candidate-reasoning-effort low` measures the effort
tradeoff without sending unsupported reasoning controls to a legacy baseline.
The report records each arm's controls explicitly.

The worker inherits operator environment variables and loads the selected
provider file. Ensure these match the intended instance's endpoint and provider;
the inventory itself does not supply inference credentials. No key is printed
or copied to a packet. The worker has a total process deadline, 600 seconds by
default and at most one hour. `--repeat` allows one through five pairs, so one
invocation makes at most ten synthetic draft calls.
Each sample makes one provider attempt; validation retries are disabled so a
bad first response remains visible and cannot hide extra calls in latency.
Invalid model trials are retained and the other trials still run. Any invalid
trial fails the comparison. An empty provider patch fails even if the server
could add deterministic day-log operations to it.
Telemetry preserves the provider timeout so a slow response retains its generation
lease. Invalid trials include elapsed time and redacted local validation feedback;
provider error bodies are excluded.
Numeric provider response statuses distinguish returned HTTP errors from failures
without a response. Batch prompt v10 includes the API-derived scalar metadata
contract and explains that adding a target must preserve other supported links.
New question links require specific evidence; shared terminology and near-duplicate
questions do not justify adding every possible target.

Reports under `evaluations/` preserve per-run scores, model and prompt identity,
reasoning controls, the configured gates, and failure reasons. Scripted reports
are explicitly ineligible as upgrade evidence. The default thresholds are
precision/recall at least 0.8, at most 0.02 regression against the baseline,
duplicate rate at most 0.05, and at most twice the mean baseline latency; CLI
flags can change these original thresholds. Fixture v2 additionally requires
clarification recall and proposal precision/recall of 1.0 and zero unjustified
ambiguous links. These small, explicitly declared cases require every case to
be correct; historical failed reports are retained unchanged. Reports include
fixture/scorer versions and source hashes, instruction hash, and each supplied
context/returned patch hash. Archived reports from older fixture/scorer versions
remain visible but are ineligible as current upgrade evidence.
Regrading captured outputs after a rubric correction is
diagnostic evidence, marked `post_hoc_rescore` and ineligible for upgrades;
independent trials and broader workload review are required before deployment.
These initial fixture gates are not a claim
that all research workloads are covered. Cost is explicitly unverified because
only numeric OpenAI token usage is captured, without a verified billing rate;
cost and transcription quality need workload-specific review before a production
upgrade.

## Scheduling

Generate a scheduler artifact using the absolute Python path from the installed
operator package environment:

    lab-tracker maintenance schedule --config /absolute/path/inventory.json \
      --python /absolute/path/venv/bin/python --format launchd \
      --output /absolute/path/com.lab-tracker.maintenance.plist

For cron, use `--format cron` instead. Add the generated line to the operator's
crontab, or install/load the generated plist in the operator's LaunchAgents.
Artifact generation does not register a job. Cron invokes the command every
minute; durable state enforces the configured interval. Launchd uses that
interval directly and runs once on load. Both append meaningful changes to
`scheduler.log`; retain or rotate this operator log with normal host log policy.

The operator environment and Docker executable must be on the scheduler's PATH.
If Docker uses Colima, set `docker_context` to `colima` explicitly. Scheduled
checks do not run paid model evaluation. Dependency proposals remain owned by
the weekly Dependabot workflows and CI; the coordinator does not duplicate them.
