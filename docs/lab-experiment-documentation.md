# Documenting a Wet-Lab Experiment in Lab Tracker

This guide answers a question a bench scientist asked while planning an
experiment: *"I want a per-experiment document with the critical information in
it. What should go in it to make it easy for me to use, and easy for an AI to
use later?"*

It uses one running example — a real experiment a person described out loud —
and maps every artifact that experiment produces onto Lab Tracker's data model.
The point is **not** to move the whole workflow into Lab Tracker. Most of the
artifacts (the Word write-up, the Excel plate map, the protocol PDFs, the
instrument files) should stay where they already live. Lab Tracker's job is to
hold the *reasoning spine* — the question being asked, the evidence that
answers it, the claims that follow — and to keep durable pointers to everything
else so the record is navigable and machine-readable years later.

## The running example: Experiment 001

> Which genes in the TNF-α pathway are differentially regulated in monocytes
> treated with aggregated α-synuclein (at two concentrations), and how does that
> response differ across monocytes, monocyte-derived macrophages, and
> monocyte-derived dendritic cells — all from healthy-control PBMCs?

The scientist's current process, paraphrased from their own description:

1. Create `OneDrive/experiments/001/` and write a templated Word document
   stating the goal, conditions, and day-by-day notes, with links to associated
   files and results.
2. Review shared lab protocols: PBMC prep, monocyte isolation, α-synuclein
   aggregate prep.
3. Walk the lab to check reagent stock and location; photograph bottles for lot
   and catalog numbers; order more if short.
4. Build an Excel plate map (wells → conditions/replicates) and a sample sheet
   (sample number → condition).
5. Print the protocols and the plate design; tape them above the hood; check
   off steps and annotate deviations with a lab pen during the run.
6. Run flow cytometry to validate cell purity — a second Excel sheet for the
   antibody panel (target/fluorophore/location), a sample-to-well sheet, and
   `.fcs` files exported from the core facility computer, uploaded to OneDrive.
7. Stimulate cells; at timepoints collect supernatant and RNA; hand-write the
   actual times reagents were added.
8. Days later: banked samples in a freezer box, annotated protocol printouts,
   handwritten notes, the `.fcs` files, and analysis-software outputs.
9. Photograph the handwritten pages, file them under the experiment, then
   manually transcribe the important parts and update the protocol "in red"
   with what actually happened.

The recurring pain point they named: the old paper notebook **did not link to
the digital artifacts**. They navigated by date. Lab Tracker exists to fix
exactly that — to make the links first-class.

## Guiding principle: pointer, not reimplementation

Lab Tracker is reasoning- and provenance-centric. It is deliberately **not** an
ELN, an inventory system, a sample/freezer tracker, a data-versioning system,
or an instrument integration. See
[`docs/retained-v1-surface.md`](retained-v1-surface.md) and
[`docs/build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md).

So the rule for this workflow is:

- **Keep authoring where it is.** The Word write-up, Excel plate map, protocol
  documents, and `.fcs`/analysis files stay in OneDrive and the core facility.
- **Capture the spine in Lab Tracker.** The question, the acquisition session,
  the dataset(s), the purity-validation analysis, and the eventual claim live
  as retained entities.
- **Link everything else by reference.** Use
  [`ExternalArtifactReference`](../src/lab_tracker/models.py) (a
  `source_system` + `uri` + `content_hash` + free `metadata`) so the external
  files are addressable from inside the graph without copying them.
- **Declare your store once, then address files relative to it.** The preferred
  form of that reference is now **store-relative**. Register your lab OneDrive
  (or Drive/S3/SFTP) a single time as a `DataStore` named e.g. `lab-onedrive`,
  and every artifact becomes `store://lab-onedrive/experiments/001/flow/sample.fcs`
  + `content_hash` — an `ExternalArtifactReference` with paired `store_name` +
  `locator` fields. This is portable across your laptop, the core-facility PC,
  and an HPC node (the host-specific mount root lives on the *store*, not the
  artifact), and an assistant can pull bounded, hash-verified bytes on demand via
  `lab_tracker_resolve_artifact` (returns `verified` / `drifted` / `unresolved`).
  A synced OneDrive folder registers as `kind=local_fs` with **zero credentials**;
  an unsynced/headless store uses the rclone adapter. See
  [`data-store-registry-design.md`](data-store-registry-design.md).
- **Setup is per-host, and stores are meant to span a fleet.** Real work happens
  on more than one machine — her laptop, an instrument/experiment PC, the shared
  core-facility computer. The store *definition* lives once in Lab Tracker and is
  shared; *reaching* it is per-host, and the health check is always "from **this**
  host." Two practical rules:
  - For a **single machine**, `kind=local_fs` on the synced OneDrive folder is
    easiest (zero credentials).
  - For a **multi-machine lab**, prefer the **rclone form** (`kind=onedrive`,
    `credential_ref` = an rclone remote name each host maps in its own
    `rclone.conf`): the store record is then host-independent and
    `store://lab-onedrive/...` resolves identically on every machine — including
    hosts where OneDrive isn't file-synced. A `local_fs` store's `root` is an
    absolute path, so it only resolves on the machine that registered it.
  - **A host you can't set up (the shared core PC) needs no Lab Tracker setup at
    all:** upload the `.fcs` to OneDrive from it as usual, and your laptop (where
    the store is configured) resolves and hashes the file. The store abstraction
    is exactly what lets a no-agent machine participate.
  *Setup note:* registering a store is currently a `POST /data-stores` call — an
  `lt store` verb (run **on each host**), a web pane, per-host binding, and setup
  auto-detection are in progress (epic `lab-tracker-y5j8`, incl. `lab-tracker-iquh`
  for the per-host binding), so for a first trial have the store pre-registered.

That gives the human a navigable record and gives an AI a typed, linked
structure to reason over — without forcing the scientist to abandon the tools
they already use at the bench.

## What maps onto what

| Workflow artifact | Lab Tracker home | Notes |
| --- | --- | --- |
| The research question (TNF-α genes × α-syn × cell type) | **Question** (`question_type=hypothesis_driven`, with `hypothesis`) | The spine. Everything below points back to it. Can be split into a parent question with atomic children via `parent_question_ids`. |
| Experiment 001 as a run/batch | **Session** (`session_type`, `started_at`/`ended_at`, `primary_question_id`) | One acquisition session for the experiment; close it when the bench work ends. |
| `.fcs` files, raw RNA-seq outputs produced during the run | **AcquisitionOutput** (`file_path`, `checksum`, `size_bytes`) under the session | Large files can stay external; the output record carries the path + content hash. A shared content hash is what lets the daily batch propose `was_derived_from` links later. |
| The committed evidence (purity-validated, condition-mapped samples/data) | **Dataset** (`primary_question_id`, `question_links`, `commit_manifest`) | Promote the session to a dataset, or commit directly. The manifest holds `files`, `external_artifacts`, and string `metadata`. |
| Plate map (well → condition/replicate) and sample sheet | Dataset `commit_manifest.metadata` + the Excel file as an **ExternalArtifactReference** | The plate map is dataset structure, not a first-class entity — record the *actual* layout in metadata and point to the spreadsheet. |
| Flow-cytometry purity validation | **Analysis** (`dataset_ids`, `method_hash`, `code_version`, `external_artifacts`) → optional **Visualization** | The antibody panel and gating outputs are the method/artifacts of this analysis. |
| Protocol PDFs (PBMC prep, monocyte isolation, α-syn aggregation) | **ExternalArtifactReference** on the session/dataset, and/or a **Note** that links them | No protocol entity in v1 — reference the shared document and capture the *as-run* deviations as a Note. |
| Handwritten bench notes, annotated protocol printouts, plate-design printout | **Note** with `raw_asset` (the photo) + `transcribed_text` (you type it) | OCR is deferred (see below); the photo is the durable record, the transcription is a manual edit. |
| Reagent lot/catalog photos, freezer-box location | **Note** (`raw_content` for the location string, `raw_asset` for the bottle photo), attached to the session | Inventory/sample tracking is out of scope as a system; capture it as evidence notes targeted at the experiment. |
| Timepoint actions ("added X to wells 3–6 at 14:20") | **Note** targeted at the Session | Hand-written at the bench, then captured as a timestamped note. |
| A finding ("TNF gene Y is up-regulated at high α-syn in macrophages") | **Claim** (`statement`, `confidence`, `falsification_criteria`, `supported_by_dataset_ids`, `supported_by_analysis_ids`, `answers_question_ids`) | The payoff. A claim must name its evidence and the question it answers. |
| A dead end or design pivot ("dropped DCs — yield too low") | **ExplorationNode** (`decision`/`dead_end`/`pivot`) targeting the question or dataset | Preserves the reasoning trajectory, not just the successful path. |

### Notes attach to anything

A [`Note`](../src/lab_tracker/models.py) carries `targets: list[EntityRef]`, so
the same capture mechanism handles bench notes, reagent photos, timepoint logs,
and protocol deviations — each pointed at the relevant Session, Dataset, or
Question. `raw_asset` holds the photo; `raw_content`/`transcribed_text` hold the
words.

### Two honest limits in v1

- **No automatic transcription.** OCR on note images and automatic audio
  transcription are deferred (`retained-v1-surface.md` → *Deferred Workflows*).
  Voice-note transcription is an explicit, editable, per-note action. So the
  scientist's instinct — *"I should transcribe the important handwritten parts
  myself rather than have AI interpret them"* — matches the tool: the photo is
  preserved as the raw record, and the structured transcription is a deliberate
  human edit. This is a feature, not a gap: the human record stays primary.
- **No protocol / reagent / plate-map entities.** These are intentionally not
  modeled. Reference the external documents and record the *as-run* specifics in
  Notes and dataset metadata.

## The per-experiment document template

This is the answer to *"what goes in the Word doc?"* Keep authoring it in Word
in OneDrive. Structure it as below so a human can read it top-to-bottom and an
AI (or the Lab Tracker graph) can pick out typed, linked fields. The **Lab
Tracker entity** column tells you which field each block becomes when you mirror
the spine into the tool.

```markdown
# Experiment 001 — TNF-α response to aggregated α-synuclein across myeloid lineages

## Metadata
- Experiment ID:        001
- Date(s):              2026-06-28 (setup) … 2026-07-01 (RNA collection)
- Operator:             <name>
- Lab Tracker question: <question_id / URL>      # the spine: Question
- Lab Tracker session:  <session_id / URL>       # Session
- Status:               planned | running | collected | analyzed | written-up

## Question & hypothesis            # → Question.text / Question.hypothesis
Which TNF-α-pathway genes are differentially regulated in monocytes treated
with aggregated α-synuclein (10 / 100 nM), and how does the response differ
across monocytes vs. MDMs vs. MoDCs, from healthy-control PBMCs?
Hypothesis: aggregated α-syn up-regulates TNF-α-pathway genes dose-dependently,
with the strongest response in macrophages.

## Conditions & design              # → Dataset.commit_manifest.metadata (as-run)
- Cell types: monocyte, MDM, MoDC
- Treatments: vehicle, α-syn 10 nM, α-syn 100 nM
- Replicates: n=3
- Plate map (AS-RUN): link to plate_map.xlsx        # ExternalArtifactReference
- Sample sheet (sample → condition): link to samples.xlsx

## Protocols used (as-run)          # → ExternalArtifactReference + deviation Notes
- PBMC prep:           <link to shared protocol> — deviations: <…>
- Monocyte isolation:  <link> — deviations: <…>
- α-syn aggregation:   <link> — deviations: <…>

## Reagents (lot / location)        # → Notes (raw_content + bottle photos)
- <reagent>: cat# … / lot … / location … / photo: <link>

## Day-by-day log                   # → Notes targeted at the Session
- 2026-06-28 14:20 — added α-syn to wells 3–6 (handwritten note photo: <link>)
- …

## Purity validation (flow)         # → Analysis (+ Visualization)
- Antibody panel: link to panel.xlsx
- .fcs files: link to OneDrive/experiments/001/flow/   # AcquisitionOutput
- Gating / result: <summary> + figure link

## Samples banked                   # → Notes (location is not system-tracked)
- RNA, supernatant: freezer <unit> / box <id> / position <…>

## Artifacts index                  # the links the old paper notebook lacked
- Raw data:   store://lab-onedrive/experiments/001/flow/*.fcs   (+ content hashes)
- Analysis:   store://lab-onedrive/experiments/001/analysis/... (software outputs)
- Figures:    store://lab-onedrive/experiments/001/figures/...
  # store-relative locators resolve + hash-verify from any host; a bare URI still works

## Findings                         # → Claim (statement + evidence + question)
- <claim>, confidence <…>, supported by dataset <…> / analysis <…>,
  answers question <…>, falsified if <…>

## Decisions & dead ends            # → ExplorationNode
- <e.g. "dropped MoDC arm — insufficient yield">
```

### Why these fields, specifically

The scientist's real worry was *future usability — for themselves and for AI*.
Each block above earns its place by being something a reader or an agent needs
to reconstruct the experiment without the operator present:

- **Stable IDs and links** (the metadata block + artifacts index) are the whole
  point — they are what the paper notebook never had. They make every external
  file addressable and let `content_hash` matching connect an acquisition output
  to a downstream analysis automatically.
- **Question and hypothesis up top** give both human and AI the *why* before the
  *what*, and map directly to the entity everything else hangs from.
- **As-run, not as-planned.** Record what actually happened (deviations,
  real timepoints, the real plate layout). Planned values are a starting point;
  the as-run values are the evidence. The scientist explicitly wanted the actual
  design captured, "not just what I had planned."
- **Findings phrased as claims with evidence and falsification** match the
  `Claim` shape and force the link from result back to question — the difference
  between a notebook and a reasoning record.

## Suggested order of operations

0. **Once per lab (setup):** register your data store — the lab OneDrive as a
   `DataStore` named `lab-onedrive` (synced folder → `kind=local_fs`), and mark it
   default. From then on every artifact below is addressed as
   `store://lab-onedrive/...` + `content_hash`, so nothing depends on a per-machine
   path. (Trial shortcut: ask whoever is onboarding you to pre-register this.)
1. **Before the bench:** create the **Question** (with hypothesis) and open a
   **Session** for Experiment 001. Put both IDs at the top of the Word doc.
2. **At the bench:** keep working on paper as usual. Photograph handwritten
   pages, reagent bottles, and the plate printout as you go.
3. **After the run:** upload `.fcs`/raw files; register the large ones as
   **AcquisitionOutput**s (or external references). Capture the photos as
   **Notes** targeted at the session, and type the important transcriptions
   yourself.
4. **Commit evidence:** promote the session to (or directly commit) a
   **Dataset**, recording the as-run plate map and sample sheet in
   `commit_manifest.metadata` and linking the spreadsheets as external artifacts.
5. **Validate:** record the flow-cytometry purity check as an **Analysis** over
   that dataset, with an optional **Visualization**.
6. **Conclude:** write the result as a **Claim** that names its supporting
   dataset/analysis and answers the question — and record any **ExplorationNode**
   for arms you dropped.

The Word document stays the human-facing front page — but you increasingly
**don't have to author it by hand.** Because the spine (question → session →
dataset → analysis → claim, plus the linked artifacts) is populated as you work,
an assistant can *compile* the per-experiment write-up from the graph on demand:
`lab_tracker_export_goal_artifact` and `lab_tracker_export_question_subtree`
render a linked subtree, and `get_decision_context` adds a windowed briefing.
Server-resident agentic drafting is the in-progress direction that turns this into
a first-class "generate my experiment doc" flow. Treat the template above as the
*shape* the graph should fill, not a document you keep in sync manually. Either
way — hand-written or generated — Lab Tracker holds the typed spine and the
durable, store-relative links, so months later either the scientist or an
assistant can start from the question and walk to every artifact the experiment
produced, and verify each one still matches what the claim was built on.

## See also

- [`docs/retained-v1-surface.md`](retained-v1-surface.md) — what v1 supports and
  what is deferred (the source of truth).
- [`docs/build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md) — why
  protocols, inventory, and instrument files stay external.
- [`docs/provenance-export.md`](provenance-export.md) — exporting the linked
  record as PROV-O sidecar files that survive without a running instance.
- [`docs/phone-capture-quickstart.md`](phone-capture-quickstart.md) — capturing
  bench photos and voice notes from a phone.
