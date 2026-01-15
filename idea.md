Connected Lab Platform
Project Specification
Version 1.2 • Draft • December 2025

Summary

The Connected Lab Platform addresses a fundamental gap in research data infrastructure: the systematic loss of real-time scientific reasoning and its connection to research data. While labs excel at capturing high-bandwidth data, the semantic context—why data was collected, what was expected, what was observed—remains trapped on paper towels, whiteboards, and in scientists' heads.

This platform ensures that the intent behind every dataset is captured at the moment of collection—and that questions can be captured before any datasets exist.

The Platform:

Ontology: Questions as first-class entities; datasets as evidence linked to questions.

Workflow: Capture → Stage → Commit pipeline with quality gates.

Integration: Hooks into acquisition software and analysis pipelines.

Hardware: Mobile photo upload for the MVP; optional low-cost digital notepads later.

The Outcome: A self-documenting lab where the reasoning behind every experiment persists beyond individual researchers, enabling AI-powered meta-analysis and institutional memory.

Design Philosophy: This platform adopts established standards wherever possible (PROV-O for provenance, DCAT for dataset description, Web Annotation for notes) and introduces a novel ontology only where existing standards do not address the problem: the representation of scientific questions as first-class, hierarchically structured entities.

1. Core Concepts
1.1 The Context Gap

Modern neuroscience labs can acquire data at unprecedented rates. A single rig can produce terabytes of timestamped neural recordings. But these datasets become semantically orphaned when the collector leaves the lab or simply forgets months afterwards—both to lab researchers and to applications like AI-powered meta-analysis.

A file named 2025_12_10_Rig2_session001.nwb tells us When, Where, and What. It does not tell us:

Why: What question was this experiment designed to address?

Expected: What outcome did the scientist anticipate (if any)?

Observed: What actually happened that wasn’t captured in the data file?

Datasets without semantic context are "zombie data"—technically alive but scientifically dead. This leads to Knowledge Loss (when students leave) and AI Blindness (reasoning engines cannot parse unstructured, undocumented data).

1.2 The Solution: Ontology-Driven Science

We invert the typical model where “Question” is optional metadata. Here, Questions are first-class entities.

Methods serve Questions.

Data collection is an implementation of an inquiry.

Questions exist independently of the data collected to address them.

Exploratory ≠ Question-less.
This system explicitly supports descriptive and exploratory science. A “question” is not required to be a well-formed hypothesis test. Examples of valid Questions include:

Descriptive: “What cell types reside in region X under condition Y?”

Baseline characterization: “What is the baseline distribution of X in this strain?”

Parameter sweep / optimization: “Which parameter settings maximize X?”

Method development: “Does protocol variant A reduce motion artifacts vs variant B?”

Investigative follow-up: “Why did we observe a layer-specific effect?”

The requirement is not “predict a p-value”; it’s “articulate the reason we are spending effort.”

1.3 Machine-Readability First, Without Magical Determinism

The ontology is designed to facilitate consumption by AI agents:

Traversable: Agents can follow edges from data to questions and vice versa.

Append-Only: The graph preserves the history of scientific intent, allowing reasoning to evolve without erasing past context.

Auditable Normalization (Assistive, not deterministic):
Free text can be suggested to map onto controlled vocabularies (e.g., NIFSTD/InterLex, OBI, NCIT, NCBI Taxonomy), but this mapping is treated as probabilistic and reviewable, not deterministic.

Principle: Store the raw human record forever; layer machine-readable structure on top with provenance.
Concretely:

Preserve the original text/image/audio.

Generate candidate tags/entities with confidence scores.

Allow user review in staging (or accept automatically only above strict confidence thresholds).

Record who/what performed the mapping, when, and under which model/ruleset version.

Never force users to speak ontology; the system surfaces suggestions.

1.4 The Birth Requirement

The central architectural constraint is the Birth Requirement:

A Dataset cannot be committed without a primary_question_id that references an active Question.

Datasets can address multiple questions; one is designated primary and the rest are secondary links. Outcomes are recorded per question link.

This is enforced at the API level. If you cannot articulate what question your experiment addresses, this system cannot ingest the dataset.

Rationale: Doing is expensive. Struggling for several minutes to formulate your question first is better than collecting data for an hour without any reason.

Operational Sessions (Bypass)

Troubleshooting and rig verification are categorically different from scientific inquiry—they are engineering, not science. The platform supports Operational Sessions that bypass the Birth Requirement. These sessions are logged for provenance (“rig was verified working on date X”) but do not enter the scientific knowledge graph as evidence. They are maintenance records, not datasets. In principle, an Operational Session can be promoted to a Dataset later, preserving provenance.

1.5 Question-First Workflow Requirement

It must be possible—and ideally common—to ingest Questions before any datasets exist.

Example: If a PI and trainee sketch ideas on a whiteboard in a meeting, they should be able to take a photo, have information extracted, and use that as metadata for creating one or more Question entries. Those Questions then become selectable/active for later data collection.

Key design implication: Question capture is not downstream of data capture. It is a peer workflow with its own capture → stage → commit (activate) lifecycle.

2. Data Model Specification

The data model adopts established semantic web standards where applicable and introduces novel structures only where existing ontologies do not address the domain.

2.1 Project

Groups related questions within a research domain.

Alignment: Designed for future alignment with VIVO (the academic research ontology) for institutional integration—linking to grants, personnel, and organizational structure.

Attributes:

project_id, name, description, status

2.2 Scientific Question (Novel Entity)

The atomic unit of scientific reasoning. Persists regardless of experimental outcome. This is the novel contribution of this platform.

Rationale: Existing ontologies (OBI, PROV-O) model experimental designs, activities, and artifacts, but do not treat research questions as first-class, hierarchically structured entities with independent lifecycle. OBI's “objective specification” captures intent, but not an evolving, queryable tree/DAG of inquiry.

Structure: Questions form a directed acyclic graph (DAG), allowing a question to have multiple parent inquiries. Datasets linked to leaf questions automatically inherit the semantic context of the entire lineage. Questions can be committed before any datasets exist and can link to multiple datasets; datasets can link to multiple questions.

Minimal Question Typing (v1.2):
We intentionally avoid an overengineered taxonomy of question types. A small, pragmatic set is sufficient:

descriptive

hypothesis_driven

method_dev

other

This is meant to aid filtering and dashboards—not to constrain scientific thinking.

Attributes:

question_id

text (the question itself)

question_type (descriptive | hypothesis_driven | method_dev | other)

hypothesis (optional) (predicted answer, where applicable)

status (staged | active | answered | abandoned)

parent_question_ids (links to broader parent inquiries)

created_from (manual | meeting_capture | imported | api)

created_at, created_by

2.3 Dataset

Immutable evidence linked to one or more questions.

Alignment: Modeled using DCAT (Data Catalog Vocabulary) for discoverability and PROV-O for provenance. Domain-specific structure follows NWB (neurophysiology) or BIDS (imaging) standards.

PROV-O Mapping: Dataset is a prov:Entity that prov:wasGeneratedBy a data collection prov:Activity.

Attributes:

dataset_id

commit_hash (content-addressed hash of the dataset commit manifest: file checksums, metadata, question links, note refs, extraction provenance; not a Git commit)

primary_question_id (REQUIRED)

question_links (list of {question_id, role=primary|secondary, outcome_status})

2.4 Note

Unstructured context (handwritten, voice, photo) linked to an entity.

Alignment: Modeled using the W3C Web Annotation Data Model. A Note is an oa:Annotation with a target (the entity it describes).

Key v1.2 update: Notes can target Questions as well as Datasets and Sessions (e.g., a whiteboard photo that seeds question creation, or meeting notes that refine a question).

Attributes:

note_id

raw_content (image/audio/file)

transcribed_text

extracted_entities (suggested tags + confidence + provenance)

targets (list of entity refs: Question/Dataset/Analysis/Session)

2.5 Analysis

Computational unit tracking code and data lineage.

Alignment: Modeled as a PROV-O prov:Activity that prov:used one or more Dataset entities and prov:generated Claim and Visualization entities. OBI's “data transformation” classes provide additional specificity if needed.

Attributes:

analysis_id

dataset_ids

method_hash

code_version (Git commit)

environment_hash (optional; future)

executed_by, executed_at

2.6 Claim (Open Design Question; Minimal v1.2)

An empirical statement (e.g., “PV inhibition broadens tuning”) with associated confidence and status.

Intent: Avoid reinventing the wheel; align with an existing ontology if possible.

Candidate Alignments:

Nanopublications (minimal publishable units of scientific assertions with formal provenance)

SEPIO (Scientific Evidence and Provenance Information Ontology)

v1.2 stance: Claims exist as lightweight first-class entities with PROV provenance; deeper semantic formalism remains an open collaboration item.

Attributes:

claim_id

statement

confidence

status (proposed | supported | rejected)

supported_by (analysis_id(s), dataset_id(s))

2.7 Visualization

Evidence artifact (plot, table) supporting a claim.

Alignment: Modeled as a PROV-O prov:Entity that prov:wasGeneratedBy an Analysis.

Attributes:

viz_id

type

file_path

caption

related_claim_ids

2.8 Ontology Alignment Summary
Entity	Standard	Notes
Dataset	DCAT + PROV-O	DCAT for discovery; PROV-O for lineage; NWB/BIDS for domain structure
Note	Web Annotation	W3C standard; Note targets Questions/Datasets/Sessions/Analyses
Analysis	PROV-O Activity	prov:Activity that used Datasets and generated outputs
Visualization	PROV-O Entity	prov:wasGeneratedBy an Analysis
Claim	TBD (lightweight v1.2)	Candidates: Nanopublications, SEPIO (open design question)
Question	Novel	Core contribution; no existing standard addresses this as first-class DAG
Project	VIVO-aligned	Future integration with institutional research information systems
2.9 Controlled Vocabularies (Assistive Tagging)

Entity tagging uses established domain vocabularies rather than custom terms:

NIFSTD / InterLex: Neuroanatomy, cell types, brain regions

OBI: Experimental methods, assay types, study designs

NCIT: General biomedical terms

Species/strain: NCBI Taxonomy

Important: In v1.2 these vocabularies are used via assistive suggestion + review, not “deterministic automation.”

3. The Workflow

The platform implements a Capture → Stage → Commit workflow inspired by version control systems. This provides both flexibility during work and rigor at the moment of commitment.

3.1 Workflow A: Questions (Capture → Stage → Commit (Activate))

Questions are intended to be captured early (often before data exists).

Phase Q1: Question Capture

Capture must be frictionless. Supported capture sources:

Meeting/Whiteboard Photo: Take a picture of a whiteboard or notebook page.

Typed Entry: Quick form entry of question text, type, and optional hypothesis.

Import: From documents, project plans, or existing trackers.

Phase Q2: Question Staging

Captured question candidates land in a Question Staging Inbox:

Extracted candidate questions are shown as suggestions.

User confirms/edits:

question text

minimal question_type (descriptive/hypothesis_driven/method_dev/other)

parent question links (optional)

tags (optional; suggested)

The original source photo/note remains linked.

Phase Q3: Commit (Activate)

Once committed, a Question:

receives a stable question_id

becomes selectable as an “active question” for data collection

can be referenced by datasets via the Birth Requirement

Questions can be committed before any datasets exist.

3.2 Workflow B: Data (Capture → Stage → Commit → Knowledge Graph)
Phase 1: Context Capture

Capture must be frictionless. Two primary modes:

Mode A: Photo Upload (Mobile) (MVP)
A mobile web app supports photographing pages, linking to an active question, and uploading for extraction.

Mode B: Optional Lab-Pad (Future Hardware)
A low-cost ($50) digital notepad at every bench allowing real-time handwriting and sketching with zero latency. See Appendix A for future hardware details.

Capture: Scientist writes in physical notebook.

Snap: Uses mobile web app to photograph the page.

Link: Selects the active question on the phone (or creates one if needed).

Upload: Image is sent to the ingestion server.

Process: Image is routed through the extraction engine, returning text + segments to staging.

Phase 2: Staging

Notes land in the Staging Inbox:

Scientist reviews extraction results.

Confirms links (Question, dataset/session).

Approves suggested tags/entities (optional).

Phase 3: Commit (Quality Gate)

The Commit bundles:

The Data Files (from the rig)

The Context Notes (from photo uploads; optional Lab-Pad in future)

The Scientific Question link(s) (primary required)

Birth Requirement enforcement: dataset commit fails without primary_question_id unless it is explicitly an Operational Session.

Phase 4: Knowledge Graph

Once committed, the dataset becomes part of the lab’s permanent, queryable knowledge graph.

3.3 PI Review Loop (Configurable Policy)

To ensure data quality, the platform supports a review workflow analogous to code review:

PIs receive a “Dataset PR” notification.

They can Approve, Request Changes, or Reject.

Policy is lab-configurable:
Just as in some GitHub projects you can push directly to main, in others you need review, PIs can choose:

no review required

review required for some categories (e.g., specific projects)

review required for all dataset commits

This turns data management into a structured mentorship opportunity when desired, without forcing a single lab culture.

4. Integration with Collection Workflows

Data acquisition happens on specialized systems (SpikeGLX, ScanImage). The platform integrates without disrupting these tools.

4.1 Integration Patterns

QR Code Linking: Mobile app (and optional Lab-Pad) displays code; rig camera snaps it. Zero software changes required.

Wrapper Script: Thin script runs before/after acquisition to register session.

File Watcher: Daemon watches output folders and links new files to active session.

Native Integration: Embed metadata directly in NWB/BIDS headers.

5. Integration with Analysis Workflows
5.1 The Analysis Commit Workflow

Just as data collection has a Capture → Stage → Commit loop, analysis has an Explore → Analyze → Commit loop:

Explore: Scientist uses Jupyter notebooks to query datasets and generate temporary plots.

Commit Analysis: Significant results are frozen. Code version is recorded. Claims are generated.

Knowledge Graph: Analysis, Claims, and Visualizations are registered, linking back to Questions and Datasets.

5.2 Analysis Automation Pattern

While the core requirement is that analysis artifacts must be registered in the graph, implementation should be low-friction.

One effective pattern uses a Python client library with decorators to wrap existing analysis functions, automatically capturing inputs, outputs, and Git commit hashes.

Known limitation: Maintaining first-class client libraries across all languages used in labs is unrealistic; therefore v1.2 prioritizes:

excellent Python integration

a stable HTTP API for everyone else

6. Deployment Architecture
6.1 Software Stack
Component	Technology	Rationale
API Server	FastAPI (Python)	Async, typed, OpenAPI documentation
Database	PostgreSQL	Mature, concurrent writes, institutional support
Vector Store	ChromaDB	Semantic search for notes and questions
Web UI	React + TypeScript	Rich interactions, type safety
OCR/AI	Extraction engine (pluggable)	Mixed handwriting/diagrams; graceful fallback supported
6.2 Server Requirements

Minimum: Raspberry Pi 5 or NUC (4 cores, 8GB RAM)

Recommended: Small lab server (8 cores, 16GB RAM, 2TB SSD)

6.3 Backup Strategy

Database: Hourly snapshots

Raw Notes: Permanent sync to institutional storage

Configuration: Git-backed

7. Risks and Mitigations
7.1 Adoption Risks
Risk	Mitigation	Notes
Scientists don’t capture notes	Frictionless hardware or seamless photo upload	Reduce friction to near-zero
Commit feels like extra work	Configurable PI review loop + visible value in retrieval/search	Incentivize quality and reuse
Questions are low quality	Mentorship + lightweight review norms	Make question articulation a skill
Exploratory work blocked	Descriptive questions + minimal question types	Exploratory is supported; “question-less” is not
Meeting-generated questions don’t get used	Promote question-first workflow and “commit (activate)” step	Make active questions easy to select at rig time
7.2 Technical Risks
Risk	Mitigation	Notes
Extraction accuracy too low	Fall back to image-only notes; human correction in staging	Graceful degradation
Data loss	Redundant storage; immutable raw files	Defense in depth
Overpromising on ontology mapping	Assistive suggestions + provenance + review	Avoid “deterministic automation” claims
8. Open Questions for Collaboration

The data model presented here is intentionally minimal. The following design questions require collaborative exploration:

8.1 Ontology Design

To what extent should the data model extend existing ontologies (OBI, PROV-O) versus define domain-specific primitives?

How should the Question entity relate to OBI's “objective specification” concept?

What is the appropriate formalism for the Claim entity (Nanopublications vs SEPIO vs lightweight local model)?

How do claims evolve with new evidence? Probability update vs superseding claims?

8.2 Question Lifecycle

What happens when questions are answered or abandoned? Do they remain queryable?

Can questions themselves be versioned as understanding evolves?

How do we model the relationship between questions that are refined, split, or merged over time?

8.3 Institutional Integration

How does this platform relate to existing ELN systems (LabArchives)? Complementary layer? Eventual replacement?

What is the data export pathway for publication and sharing? How do we ensure NWB/BIDS compliance?

What authorization model is appropriate? Who can see what across lab members?

8.4 Meeting Capture and Extraction

What extraction fidelity is “good enough” for converting meeting photos into question candidates?

What UI patterns best support “one photo → multiple questions” flows?

How do we preserve the original meeting context while still producing clean Question entities?

Appendix A: Edge Hardware (Lab-Pad)

For labs desiring a dedicated capture device, the Lab-Pad V1 specification follows. Note: this hardware is optional and not part of the current MVP (photo upload only).

A.1 Design Philosophy

The hardware serves one purpose: ubiquitous, frictionless context capture. It must be:

Cheap enough to deploy one per person, not one per lab

Simple enough that there’s no learning curve

Durable enough for daily bench use

A.2 Hardware Configuration
Component	Specification	Rationale
Compute	Raspberry Pi Zero 2 W or Pi 3A+	Sufficient for display + network; low cost
Display	7" capacitive touch (1024×600)	Large enough for handwriting
Input	Fine-tip capacitive stylus	No pressure sensitivity needed; commodity
Connectivity	Wi‑Fi 2.4 GHz	Ubiquitous; sufficient for stroke data
Power	5V USB	Standard phone charger; easy replacement
Enclosure	3D-printed or commercial Pi case	Tilted for comfortable writing angle

Target cost: $50–70 USD per unit (all-in)

A.3 Offline Behavior

The Lab-Pad must function when the server is temporarily unreachable:

Scenario	Behavior
Server unreachable at boot	Load cached question list; show “Offline” indicator; allow note capture
Connection lost mid-session	Continue capturing; queue pages for sync; show “Offline” indicator
Connection restored	Background sync of queued pages; update question list
No cached questions	Show warning; allow “uncontexted” notes requiring manual linking later

Offline storage budget: 100 pages (~50 MB) before oldest pages are at risk.

Appendix B: Example Scenarios
Scenario 0: The Question-First Meeting

PI + trainee write 3 candidate questions on a whiteboard.

Trainee takes a photo via the mobile app.

System extracts candidate questions into Question Staging.

Trainee confirms text, sets minimal types, and commits (activates) two questions.

Those questions become selectable at the rig the next day.

Scenario 1: Standard Experiment Day

8:00 AM: Scientist selects “Does PV inhibition broaden tuning?” in the mobile app (or web UI).

Acquisition: Records data. Jots notes in a physical notebook.

5:00 PM: Uploads photos, reviews notes in Staging. Commits “Dataset + Notes + Question”.

Scenario 2: The Paper Notebook User

Acquisition: Scientist writes notes in physical notebook.

Upload: Snaps photo via phone web app, selects question.

Commit: Same end-of-day workflow. Extraction indexing makes paper notes searchable.

Scenario 3: Mid-Experiment Pivot

10:30 AM: Scientist sees unexpected layer-specific effect.

Action: Creates new question: “Why is effect layer-specific?”

Result: Subsequent notes link to new question. Old notes stay with original.

Scenario 4: Troubleshooting (Operational Session)

9:00 AM: Rig producing artifacts. Scientist starts Operational Session.

Troubleshooting: Notes capture diagnostic steps without requiring a scientific question.

Resolution: Session logged as “Rig 2 verified working 2025‑12‑10” in operational records, not the scientific knowledge graph.

— End of Specification —
