# Experiment Walkthrough — Coverage & Gap Audit

A bench scientist talked through one full experiment out loud (the
α-synuclein / TNF-α myeloid experiment described in
[`lab-experiment-documentation.md`](lab-experiment-documentation.md)). This
document audits **every artifact-generation step they named** against what Lab
Tracker can actually capture, so we know where a first-time product tester will
hit friction *before* they do.

The headline finding: most gaps are **by design** — Lab Tracker is the reasoning
spine plus link layer, not an ELN, inventory system, sample tracker, or
instrument integration (see [`retained-v1-surface.md`](retained-v1-surface.md)
and [`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md)). That makes the
tester's experience primarily an **expectations problem**: it goes smoothly if
they arrive understanding Lab Tracker links to their OneDrive/Excel/protocols
rather than replacing them, and roughly if they arrive expecting an ELN.

> **Update (2026-07-02) — two of the ranked gaps below have moved.** Since this
> audit was first written, the **data-store registry shipped** (the `DataStore`
> entity, migrations `0049`/`0050`, `POST`/`GET /data-stores` + `/health`,
> `store://<name>/<path>` resolution through `local_fs` and an rclone adapter
> that covers OneDrive, and the `lab_tracker_resolve_artifact` MCP read tool —
> see [`data-store-registry-design.md`](data-store-registry-design.md) and
> `src/lab_tracker/artifact_resolution.py`). That changes the OneDrive story
> from "opaque `uri` + hash" to a portable, hash-verified, agent-resolvable
> `store://lab-onedrive/experiments/001/...` locator, which **softens G7**.
> Separately, the "write-up has no home" problem (G1) is being answered by
> **agent-composed write-ups from the graph** — an agent can already compile a
> goal/question subtree via the `lab_tracker_export_goal_artifact` /
> `lab_tracker_export_question_subtree` / `get_decision_context` read tools, and
> server-resident agentic drafting is the in-progress direction that makes this
> first-class. The **remaining** OneDrive friction is now a *setup* problem, not
> a data-model one — tracked as epic
> [`lab-tracker-y5j8`](#the-real-remaining-onedrive-gap-is-setup-not-data-model),
> not the rows below. G1 and G7 are annotated inline.

## Coverage legend

- **Full** — a first-class entity holds this with its structure intact.
- **Partial** — captured only as an opaque image or external pointer; the
  *structure* (well layout, sample→condition map, lot number) is lost unless a
  human retypes it into metadata.
- **None** — no home; the artifact has nowhere to land in Lab Tracker.

## Every artifact-generation step in the walkthrough

| # | Step the scientist described | Artifact produced | Lab Tracker home | Coverage |
| --- | --- | --- | --- | --- |
| 1 | Create `OneDrive/experiments/001/` | Folder = the "experiment" unit | Project / Session / Dataset (no single "experiment" handle) | **Partial** |
| 2 | Templated Word doc: goal, conditions, day-by-day, links, scratch notes | The per-experiment write-up | — (no document entity; only atomic Notes) | **None** |
| 3 | Pull shared protocols (PBMC prep, monocyte isolation, α-syn aggregation) | Selected protocol set | ExternalArtifactReference only | **Partial** |
| 4 | Walk lab, check reagent stock/location | Handwritten location note | Note (`raw_content`) | **Partial** |
| 5 | Photograph reagent bottle for lot/catalog # | Image w/ lot+catalog | Note `raw_asset` (image); number **not** extracted (OCR deferred) | **Partial** |
| 6 | Order more reagent if short | Purchase order | — (procurement out of scope) | **None** |
| 7 | Excel plate map: well → condition/replicate | Plate design | Dataset `commit_manifest.metadata` + external ref | **Partial** |
| 8 | Print plate, hand-write blocks, photograph | Annotated plate image | Note `raw_asset` | **Partial** |
| 9 | Excel sample sheet: sample # → condition/replicate | Sample manifest | external ref / metadata string (sample tracking deferred) | **Partial** |
| 10 | Print protocol + design, tape above hood | Physical working copy | — (physical) | n/a |
| 11 | Check off steps / pen-annotate deviations during run | **As-run protocol** | Note photo + manual transcription; no execution model | **None** |
| 12 | Flow antibody Excel: target / fluorophore / location | Panel sheet | external ref (no panel entity) | **Partial** |
| 13 | Flow sample sheet: which samples ran on the machine | Run manifest | external ref (sample tracking deferred) | **Partial** |
| 14 | `.fcs` files from the cytometer | Raw instrument data | **AcquisitionOutput** (`file_path`, `checksum`) | **Full** |
| 15 | Upload `.fcs` from core PC → OneDrive → experiment subfolder | File movement | Manual; `lt watch` can't run on a shared core PC | **Partial** |
| 16 | Flow analysis-software outputs | Derived analysis files | **Analysis** `external_artifacts` | **Full** |
| 17 | Set up wells with stimulations (2 concentrations) | Experimental manipulation | Dataset metadata (conditions) | **Partial** |
| 18 | Add reagent to wells at timepoints; note the time (handwritten) | Timed intervention log | Note targeted at Session; no structured timepoint log | **Partial** |
| 19 | Collect supernatant + RNA at timepoints | Banked aliquots | — (sample tracking deferred) | **None** |
| 20 | Samples in a freezer box, with location | Physical sample + location | — (no sample/freezer tracking) | **None** |
| 21–22 | Photograph handwritten bench notes → OneDrive/experiment folder | Image of paper record | Note `raw_asset` | **Full** |
| 23 | Manually transcribe the important parts | Structured text | Note `transcribed_text` (manual — matches their instinct) | **Full** |
| 24 | Copy protocol, annotate "in red" what differed | As-run protocol doc | — (no protocol versioning) | **None** |
| 25 | Final notebook page: date, title, steps/pasted protocol, plate image, sample list | Synthesized record | — (no document entity) | **None** |
| 26 | Record metadata about the artifacts and links | Linkage metadata | external refs + manifest + provenance links | **Full** |

## The gaps that will actually hurt a tester (ranked)

### G1 — The experiment write-up document has no home *(reframed: generate it)*
Their **primary** artifact is the templated Word doc — it *is* their lab
notebook. Lab Tracker has no document/write-up entity; it has atomic Notes and a
typed graph. A tester's first question will be *"where does my experiment
write-up go?"* The original honest answer was *"it stays in Word; Lab Tracker
links to it."* The **better** answer, now that the spine is populated, is *"you
don't hand-author the write-up — an assistant composes it from the graph."* An
agent with the Lab Tracker MCP can already compile a goal or question subtree
(`lab_tracker_export_goal_artifact`, `lab_tracker_export_question_subtree`) plus
a windowed `get_decision_context` into a per-experiment document on demand, and
**server-resident agentic drafting** is the in-progress direction that makes
this a first-class flow rather than an assemble-it-yourself trick. So the write-up
becomes a *generated view* of the graph, not a document the tester has to keep in
sync by hand. **Mitigation:** still lead onboarding with the
[experiment-document template](lab-experiment-documentation.md#the-per-experiment-document-template)
so the tester knows which fields the graph should hold, but frame the Word/Markdown
page as something the assistant renders from the spine, not a second source of truth.

### G2 — There is no "experiment" as a unit
Their mental model is *"experiment 001."* Lab Tracker offers Project → Question →
Session → Dataset, and one experiment spans setup + several collection days +
flow validation — i.e. potentially several Sessions and Datasets. Nothing says
"all of this is experiment 001." **Mitigation:** adopt a convention —
*one experiment = one Session, plus the Question it serves and the Dataset(s) it
commits* — and put the Session ID at the top of the Word doc so the grouping is
explicit. Worth tracking whether testers want a lightweight "experiment"
grouping entity; it would be additive to the spine.

### G3 — Sample tracking and the sample→condition map *(scientifically load-bearing)*
The walkthrough generates sample sheets, timepoint aliquots, freezer locations,
and "which sample number ran on the flow machine." Sample tracking is explicitly
deferred. Yet the sample→condition mapping is exactly what makes the downstream
RNA/flow data interpretable — lose it and the dataset is noise. Today it survives
only as an Excel file referenced as an external artifact plus a metadata string.
**Mitigation:** tell testers to commit the sample sheet as an
ExternalArtifactReference on the Dataset and mirror the key map into
`commit_manifest.metadata`; be explicit that this is the current ceiling so they
don't expect per-aliquot tracking.

### G4 — Protocols: selection *and* as-run deviations
No protocol entity (steps 3, 11, 24). The as-run annotated protocol — arguably
the most important record of *what actually happened* — collapses to a photo plus
manual transcription. The scientist explicitly wants the **actual** design
captured, "not just what I had planned." Lab Tracker stores the committed
(as-run) dataset state but models no protocol/checklist execution.
**Mitigation:** capture deviations as Notes targeted at the Session and record
as-run conditions in dataset metadata; set expectations that the protocol
document itself lives external.

### G5 — Structured spreadsheets stored as opaque blobs
Plate map (7,8), antibody panel (12), sample/run sheets (9,13) are all
*structured* artifacts that Lab Tracker keeps only as external files + free-text
metadata. Their structure — and therefore its readability by a future AI — is
lost unless a human retypes it. This directly undercuts the scientist's stated
goal of making the record "easy for AI to use."

### G6 — Reagent lot/catalog capture is image-only
They photograph bottles specifically to retain lot and catalog numbers (for
reproducibility and recalls). The image lands in a Note, but the structured
number is not extracted (OCR is deferred — see the restoration ledger in
[`retained-v1-surface.md`](retained-v1-surface.md), which already sketches an
on-demand OCR-assist shape). So lot numbers are present but not queryable.

### G7 — Capture volume vs. the review gate, and the shared core PC *(softened)*
A heavy bench day generates many phone photos; each becomes a *staged* note that
needs human-gated graph review before it means anything in the graph. That
review burden is real and worth pre-warning. The **shared core PC** half of this
gap is now softened by the data-store registry: `lt watch` still can't run on a
machine the scientist doesn't control, but she no longer needs it there. Once the
lab OneDrive is registered as a `DataStore`, the `.fcs` exported to it (step 15)
is addressable as `store://lab-onedrive/experiments/001/flow/sample.fcs` +
`content_hash` — resolvable and hash-verifiable from *her* laptop or an agent, with
no agent on the core PC. The residual friction is (a) **something still has to
compute the initial `content_hash`** — the resolver only *verifies* a supplied hash
([`artifact_resolution.py`](../src/lab_tracker/artifact_resolution.py)), tracked as
[`lab-tracker-16x2`](#the-real-remaining-onedrive-gap-is-setup-not-data-model) —
and (b) **the store must be registered first**, which today means a raw
`POST /data-stores` (see the new setup gap below).

### G8 — "Easy for AI in the future" has a ceiling
The scientist's explicit goal. An AI can traverse the typed spine (question →
session → dataset → analysis → claim) cleanly. But the *content-rich* artifacts —
handwritten-note photos, plate maps, antibody panels, the Word write-up — are
opaque to it: no OCR, no spreadsheet parsing, no semantic/vector search (all
deferred). So an agent can navigate the structure but cannot *read* most of the
substance unless a human has transcribed it into Notes/metadata. The goal is
**partially** met today, and the gap is exactly the deferred-extraction surface.

## The real remaining OneDrive gap is *setup*, not data-model

The data-store registry means the OneDrive linking problem is solved at the
data-model layer — but it shipped **orphaned from the setup flow**. The
guided-setup path
([`guided-setup-and-auto-tracking-design.md`](guided-setup-and-auto-tracking-design.md),
`src/lab_tracker/setup_guide.py`) wires watch folders and git hooks; nothing in
it discovers or registers a `DataStore`. And there is no store-registration verb
or web pane: `create_data_store` exists only as `POST /data-stores`
([`routes/data_stores.py`](../src/lab_tracker/routes/data_stores.py)), with **no
`lt` verb, no `/app` UI, and — by deliberate design choice — no MCP setup tool**
(the guided-setup design rejected MCP setup tools so consent stays on the CLI's
command-approval gate). So the honest current answer to *"how do I point Lab
Tracker at my OneDrive?"* is *"someone runs a raw API call for you,"* which is a
poor first-run experience for a bench scientist on `/app` + phone.

The fix is to make setup **auto-hydrate from the tools she already uses** —
detect the synced OneDrive folder (or an existing rclone remote), propose it, run
the health check, and set it as her default store, with the same
`--dry-run`/approve choreography as `lt watch add`.

**Setup is a fleet, not a server.** The machine that runs the API is not the only
one that matters — watch folders and store access live wherever the work happens:
her laptop, an instrument/experiment PC, the shared core-facility computer. The
setup profile is already per-host (`~/.lab-tracker/config.json`, repo-local
`watch.json`, per-repo hooks) and `check_store_health` is explicitly "from this
host," so hydration must run **per host** and confirm *that* host's reachability —
never assume one machine. Two consequences: a shared store should prefer the
**rclone form** (a remote name each host maps locally) over a host-pinned
`local_fs` path so it resolves across the fleet (tracked as `lab-tracker-iquh`);
and a host she **can't** set up (the shared core PC) needs no setup at all — it
uploads to the store and another host resolves + hashes the file, which is the
mechanism that softens G7. Tracked as epic
[`lab-tracker-y5j8`](#) with children: the `lt store` verb + detection spine
(`lab-tracker-pwsb`), setup-awareness coverage (`lab-tracker-5zui`), a web-app
data-store pane for non-CLI users (`lab-tracker-bs24`), content-hash minting on
store-artifact registration (`lab-tracker-16x2`), and capture clients writing
`store://<default>/...` locators automatically once a default exists
(`lab-tracker-iarb`). Until at least the spine lands, **pre-register the tester's
`lab-onedrive` store for her before the trial** so the `store://` path is live on
day one.

## What Lab Tracker covers well (so onboarding can lead with strength)

Steps 14, 16, 21–23, 26 are genuinely well served, and they include the
scientist's own stated pain point — *the old paper notebook "didn't link to the
artifacts."* Lab Tracker's external artifact references, dataset manifest, and
content-hash provenance links are precisely the fix: every external file becomes
addressable, and a shared content hash lets the daily batch propose
`was_derived_from` links from an acquisition output to a downstream analysis.
Their instinct to **transcribe handwriting themselves rather than trust AI to
interpret it** also matches the tool exactly (OCR deferred; transcription is a
deliberate human edit) — frame that as intentional, not a limitation.

## Recommendations for a smooth tester experience

1. **Set the boundary on day one.** "Lab Tracker is the reasoning spine and link
   layer over your existing OneDrive, Excel, and protocols — not a replacement
   for them." This single sentence prevents G1, G3, G4, G7 from reading as bugs.
2. **Hand them the experiment-document template** as the bridge artifact, and the
   *one experiment = one Session* convention (G2).
3. **Pre-warn the four friction points:** the Word doc stays external (G1), the
   sample sheet is the current ceiling for sample tracking (G3), the core PC is a
   manual upload (G7), and handwriting/lot numbers are images you transcribe
   (G6, G8).
4. **Collect signal, don't pre-build.** Watch whether testers reach for: a
   lightweight "experiment" grouping (G2), structured plate/sample maps (G3, G5),
   or on-demand OCR (G6). The restoration ledger already records the *shape* any
   of these should take if reintroduced — capture demand against those shapes
   rather than expanding scope reactively.

## See also

- [`lab-experiment-documentation.md`](lab-experiment-documentation.md) — the
  positive mapping and the experiment-document template.
- [`retained-v1-surface.md`](retained-v1-surface.md) — supported vs deferred
  (the source of truth) and the restoration ledger for deferred ideas.
- [`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md) — why protocols,
  inventory, samples, and instruments stay external.
