# SciAgent + Lab-Tracker: Unified Project Specification

**Version:** 0.1
**Status:** Draft
**Date:** 2024-12-09

## Executive Summary

This specification defines a system for capturing scientific reasoning with minimal friction while maintaining formal evidence management for publication. It synthesizes three components:

1. **Lab-Tracker** (exists): Django REST API for formal experiment and evidence management
2. **SciAgent** (to build): Interactive AI assistant for reasoning capture from ELN entries
3. **Run Log Format** (convention): Commit-style structured format for execution-time capture

The key insight: scientists need different tools at different moments. Structured run logs work during execution. Interactive AI helps with informal notes. Both feed into a formal evidence layer for publication.

---

## Problem Statement

Scientific reasoning exists at three levels, each with different capture challenges:

| Level | Example | Current State | Challenge |
|-------|---------|---------------|-----------|
| **Execution** | "Used 3 mice, 50 trials, 5mW laser" | Scattered across notebooks | No standard format, not machine-readable |
| **Reasoning** | "Testing whether PV inhibition broadens tuning" | In scientist's head | Feels like extra work to write down |
| **Evidence** | "Figure 2A supports Claim C-012" | Ad-hoc, reconstructed at paper time | Links not maintained during research |

### The Friction Problem

Existing solutions force a choice:
- **Structured capture** (forms, templates): Complete but high friction → scientists skip it
- **Free-form notes**: Low friction but unstructured → reasoning lost

### Our Approach

Provide multiple entry points at different friction levels, all flowing into a unified evidence graph:

```
Low friction ──────────────────────────────────────▶ High friction

┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Free-form   │    │  Steered     │    │  Structured  │
│    notes     │───▶│  dialogue    │───▶│   run log    │
│              │    │  (SciAgent)  │    │   (commit)   │
└──────────────┘    └──────────────┘    └──────────────┘
        │                  │                    │
        └──────────────────┼────────────────────┘
                           ▼
                  ┌──────────────────┐
                  │   Lab-Tracker    │
                  │  (formal layer)  │
                  │                  │
                  │ Questions,Claims │
                  │ Runs, Evidence   │
                  └──────────────────┘
```

---

## System Architecture

### Component Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           User Interfaces                           │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │
│  │   ELN (native)  │  │  SciAgent Chat  │  │  Lab-Tracker Admin  │ │
│  │                 │  │                 │  │                     │ │
│  │ Markdown/native │  │ React split-    │  │ Django admin or     │ │
│  │ ELN interface   │  │ pane UI         │  │ custom dashboard    │ │
│  └────────┬────────┘  └────────┬────────┘  └──────────┬──────────┘ │
└───────────┼─────────────────────┼─────────────────────┼─────────────┘
            │                     │                     │
            ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Integration Layer                           │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │
│  │   ELN MCP       │  │  SciAgent Core  │  │  Lab-Tracker API    │ │
│  │   Server        │  │                 │  │                     │ │
│  │                 │  │ - LLM reasoning │  │  Django REST        │ │
│  │ - LabArchives   │  │ - Entity lookup │  │  Framework          │ │
│  │ - Markdown      │  │ - Steering      │  │                     │ │
│  │   (future)      │  │ - Confirmation  │  │                     │ │
│  └────────┬────────┘  └────────┬────────┘  └──────────┬──────────┘ │
└───────────┼─────────────────────┼─────────────────────┼─────────────┘
            │                     │                     │
            └──────────┬──────────┴──────────┬──────────┘
                       │                     │
                       ▼                     ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Data Layer                                 │
├─────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐ │
│  │   ELN Storage   │  │  Vector Store   │  │  Lab-Tracker DB     │ │
│  │                 │  │  (ChromaDB)     │  │  (PostgreSQL)       │ │
│  │ LabArchives     │  │                 │  │                     │ │
│  │ cloud / local   │  │ Semantic search │  │ Questions, Claims   │ │
│  │ markdown        │  │ over entries    │  │ Runs, Evidence      │ │
│  └─────────────────┘  └─────────────────┘  └─────────────────────┘ │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Flow

```
1. CAPTURE (multiple entry points)
   ┌─────────────────────────────────────────────────────────────┐
   │ a) Structured Run Log    b) SciAgent Chat    c) Direct API  │
   │    (commit format)          (interactive)       (programmatic)│
   └─────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
2. ENTITY RESOLUTION
   ┌─────────────────────────────────────────────────────────────┐
   │ - Match to existing Questions (dropdown / search)           │
   │ - Match to existing Cohorts, Claims                         │
   │ - Create new entities only when needed                      │
   └─────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
3. STORAGE
   ┌─────────────────────────────────────────────────────────────┐
   │ - Entry stored in ELN (with YAML metadata block)            │
   │ - Entities created/linked in Lab-Tracker                    │
   │ - Embeddings stored in vector DB for search                 │
   └─────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
4. QUERY
   ┌─────────────────────────────────────────────────────────────┐
   │ "What experiments tested the PV inhibition hypothesis?"     │
   │ "Show me all runs with unexpected outcomes"                 │
   │ "What's the evidence chain for Claim C-012?"                │
   └─────────────────────────────────────────────────────────────┘
```

---

## Component Specifications

### 1. Lab-Tracker (Existing + Extensions)

#### Current State

Lab-Tracker provides Django models for:
- **Scientific Intent**: Question (hierarchy, hypothesis, status), Claim (statement, evidence links)
- **Execution**: Run, Session, Component (subjects, intervention, recording)
- **Evidence**: Dataset, Analysis, Visualization, Panel, ClaimEvidence

#### Required Extensions

| Extension | Purpose | Priority |
|-----------|---------|----------|
| `Entry` model | Represents an ELN entry (structured or informal) | High |
| `Run.parent_run` | Provenance chain for run sequences | High |
| `Entry.outcome` | supports/refutes/inconclusive/pending | High |
| `Entry.unexpected` | Capture surprising findings | Medium |
| `Entry.follow_ups` | Generated follow-up questions | Medium |
| Search/autocomplete APIs | Support SciAgent dropdowns | High |
| Deviation tracking | Structured capture of execution deviations | Medium |

#### New Model: Entry

```python
class EntryType(models.TextChoices):
    RUN_LOG = "run_log", "Structured Run Log"
    NOTE = "note", "Informal Note"
    OBSERVATION = "observation", "Observation"

class OutcomeStatus(models.TextChoices):
    SUPPORTS = "supports", "Supports Hypothesis"
    REFUTES = "refutes", "Refutes Hypothesis"
    INCONCLUSIVE = "inconclusive", "Inconclusive"
    PENDING = "pending", "Pending Results"

class Entry(TimestampedModel, ELNLinkMixin):
    """
    Represents an ELN entry - either a structured run log or informal note.
    Links to Lab-Tracker entities and stores extracted/confirmed metadata.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)

    # ELN identity
    title = models.CharField(max_length=300)
    entry_type = models.CharField(max_length=20, choices=EntryType.choices)
    entry_date = models.DateField()
    raw_content = models.TextField(help_text="Original ELN content")

    # Entity links (resolved via SciAgent or direct entry)
    questions = models.ManyToManyField(Question, blank=True, related_name="entries")
    run = models.ForeignKey(Run, null=True, blank=True, on_delete=models.SET_NULL)
    claims = models.ManyToManyField(Claim, blank=True, related_name="entries")

    # Extracted/confirmed metadata
    extracted_hypothesis = models.TextField(blank=True)
    outcome = models.CharField(max_length=20, choices=OutcomeStatus.choices, blank=True)
    outcome_summary = models.TextField(blank=True)
    unexpected = models.TextField(blank=True, help_text="Unexpected findings")
    follow_ups = models.JSONField(default=list, help_text="Generated follow-up questions")

    # Extraction metadata
    extraction_confidence = models.FloatField(null=True, blank=True)
    confirmed = models.BooleanField(default=False)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.CharField(max_length=100, blank=True)
```

#### Run Provenance Extension

```python
# Add to Run model
parent_run = models.ForeignKey(
    "self",
    on_delete=models.SET_NULL,
    null=True,
    blank=True,
    related_name="child_runs",
    help_text="Previous run in sequence"
)

commit_message = models.TextField(
    blank=True,
    help_text="Git-style summary of run purpose and findings"
)

deviations = models.JSONField(
    default=list,
    help_text="List of deviations from protocol"
)
```

### 2. SciAgent (To Build)

#### Core Responsibilities

1. **Entry Processing**: Read ELN entries via MCP
2. **Entity Resolution**: Match mentions to existing Lab-Tracker entities
3. **Interactive Steering**: Ask clarifying questions to fill gaps
4. **Confirmation Flow**: Present extracted metadata for user confirmation
5. **Write-back**: Update ELN with metadata block, create/link Lab-Tracker entities

#### Interaction Modes

**Mode A: Passive Extraction**
```
User writes entry in ELN
        │
        ▼
SciAgent reads entry
        │
        ▼
Extracts metadata with confidence scores
        │
        ▼
Presents for confirmation
        │
        ▼
User confirms/edits
```

**Mode B: Interactive Steering (preferred)**
```
User starts writing in SciAgent chat
        │
        ▼
SciAgent: "Which question is this addressing?"
        │
        ▼
[Dropdown of existing Questions] or "Create new"
        │
        ▼
User continues writing
        │
        ▼
SciAgent: "You mentioned results - does this support or refute?"
        │
        ▼
User selects outcome
        │
        ▼
SciAgent: "Anything unexpected?"
        │
        ▼
User adds detail or skips
        │
        ▼
Entry saved with full metadata
```

#### Steering Prompts

| Trigger | Agent Response |
|---------|----------------|
| No question linked | "Which question is this addressing? [dropdown]" |
| Mentions hypothesis without linking | "Is this testing: [show matching hypotheses]?" |
| Mentions results without outcome | "Does this support, refute, or is it inconclusive?" |
| Mentions "unexpected" / "surprising" | "What did you expect instead?" |
| Mentions subjects without cohort | "Which cohort? [dropdown]" |
| Mentions prior work | "Should I link this to [suggested run]?" |

#### Entity Lookup APIs Required

```
GET /api/questions/search/?q={text}&limit=5
GET /api/questions/roots/
GET /api/claims/?status=evidence_gathering&search={text}
GET /api/cohorts/available/
GET /api/runs/recent/?limit=10
GET /api/runs/{id}/children/
```

### 3. Run Log Format (Convention)

The commit-style run log is a **convention**, not a separate system. It defines how structured entries should be written in the ELN.

#### Format Specification

```markdown
# Run {run_id}: {title}

**Date:** {YYYY-MM-DD}
**Experimenter:** {name}
**Questions:** {Q-XXX, Q-YYY}
**Parent Run:** {run_id or "none"}

## Intent

{Why this run exists. What hypothesis is being tested.}

## Apparatus

- **Rig:** {identifier}
- **Software:** {repo}@{commit}
- **Calibration:** {date}, {file}

## Subjects

- **Cohort:** {cohort_name}
- **IDs:** {subject_ids}
- **N:** {count}

## Protocol

{What was done, referencing protocol docs}

## Data Products

| Artifact | Path | SHA256 |
|----------|------|--------|
| Raw data | {path} | {hash} |
| Features | {path} | {hash} |
| Quicklook | {path} | {hash} |

## Observations

{Free-form notes about what happened}

## Deviations

- {deviation 1}
- {deviation 2}

## Outcome

**Status:** {supports | refutes | inconclusive | pending}
**Summary:** {one sentence}
**Unexpected:** {if any}

## Follow-ups

- [ ] {follow-up question or task 1}
- [ ] {follow-up question or task 2}

## Commit Message

{Git-style summary: type(scope): description}

---
<!-- lab-tracker metadata -->
```yaml
entry_type: run_log
questions: [Q-012, Q-018]
run_id: a0005
parent_run: a0004
outcome: supports
confirmed: true
```
```

#### YAML Sidecar

For machine consumption, a parallel `{run_id}.yaml` file:

```yaml
run_id: a0005
title: "Intermittent pulses; HRC vs turning"
date: 2024-03-15
experimenter: jsmith
questions:
  - Q-012
  - Q-018
parent_run: a0004

apparatus:
  rig: FLYRIG-03
  software_commit: a1b2c3d
  calibration_date: 2024-03-10

subjects:
  cohort: OR42b-Gal4
  ids: [3412, 3413]
  count: 2

data_products:
  - type: raw
    path: /data/runs/a0005/raw/
    sha256: abc123...
  - type: features
    path: /data/runs/a0005/features.parquet
    sha256: def456...

deviations:
  - "Laser power drifted +4% mid-session; recalibrated"

outcome:
  status: supports
  summary: "Turn latency ~150ms post-pulse confirms prediction"
  unexpected: null

follow_ups:
  - "Test with longer pulse duration"
  - "Repeat with silenced HRC"
```

---

## Integration Patterns

### Pattern 1: Structured Run Log → Lab-Tracker

```
Run log written in ELN
        │
        ▼
ELN MCP server reads entry
        │
        ▼
Parse YAML metadata block
        │
        ▼
Create/update Lab-Tracker entities:
  - Entry (type=run_log)
  - Run (if new)
  - Link to Questions
  - Link to Cohort
  - Create Dataset shells for data products
```

### Pattern 2: Informal Note → SciAgent → Lab-Tracker

```
User writes informal note
        │
        ▼
SciAgent extracts candidate metadata
        │
        ▼
SciAgent queries Lab-Tracker for matching entities
        │
        ▼
Interactive steering to resolve ambiguities
        │
        ▼
User confirms
        │
        ▼
Create/update:
  - Entry (type=note)
  - Link to existing Questions (or create new)
  - Link to existing Run (if mentioned)
  - Update outcome if results present
```

### Pattern 3: Evidence Assembly (Publication Time)

```
User: "What's the evidence for Claim C-012?"
        │
        ▼
Lab-Tracker query:
  - ClaimEvidence links → Panels, Analyses
  - Panels → Visualizations → Analyses → Datasets
  - Datasets → Runs → Sessions
  - Runs → Entries (for reasoning context)
        │
        ▼
Return full provenance chain with:
  - Formal evidence (figures, stats)
  - Reasoning context (why these experiments)
  - Execution details (what was actually done)
```

---

## Success Criteria

### For Scientists

1. **Capture friction**: Confirming extracted metadata takes <30 seconds
2. **Accuracy**: >70% of extractions confirmed without edits
3. **Discoverability**: Can find related experiments in <3 queries
4. **Provenance**: Can trace any claim to supporting runs and reasoning

### For the System

1. **Entity reuse**: >80% of Question links use existing entities (not new)
2. **Coverage**: >90% of entries have confirmed metadata within 1 week
3. **Consistency**: No orphaned entities, all Claims have evidence paths

### For Automation

1. **Queryability**: Metadata queries return in <500ms
2. **Completeness**: All required fields populated for run logs
3. **Integrity**: All data product hashes verifiable

---

## Scope

### Phase 1: Foundation (This Project)

**In Scope:**
- [ ] Lab-Tracker `Entry` model and extensions
- [ ] Lab-Tracker `Run.parent_run` and provenance fields
- [ ] Search/autocomplete API endpoints
- [ ] Run log format specification (this document)
- [ ] Sample run log entries for testing

**Out of Scope:**
- SciAgent chat UI
- SciAgent LLM integration
- ELN MCP server (exists separately)
- Vector search / ChromaDB
- External API integrations (Semantic Scholar, etc.)

### Phase 2: SciAgent Core

- SciAgent backend (Claude API, tool orchestration)
- Interactive steering logic
- Confirmation flow
- Write-back to ELN and Lab-Tracker

### Phase 3: Full Integration

- SciAgent chat UI (React split-pane)
- Vector search for semantic queries
- Cross-entry linking suggestions
- Automated follow-up surfacing

### Phase 4: Scale

- Multi-lab deployment
- External literature integration
- Institutional knowledge graph

---

## Open Questions

1. **Entry vs. Run granularity**: Should every Entry create a Run, or are they independent? (Current thinking: independent - an Entry may reference a Run but isn't always 1:1)

2. **Confirmation authority**: Who can confirm metadata? Only the author, or any lab member?

3. **Conflict resolution**: If SciAgent extracts metadata that conflicts with existing entity data, how is this resolved?

4. **Versioning**: Should Entry metadata be versioned, or is the current state sufficient?

5. **ELN write-back format**: Append YAML block to markdown? Separate sidecar file? ELN-specific metadata fields?

---

## Appendix: Relationship to Original Documents

### SciAgent POC Spec

This project spec **adopts**:
- The core insight that reasoning extraction is the missing layer
- The metadata schema (question, hypothesis, outcome, unexpected, follow-ups)
- The confirmation flow concept
- The MCP-based architecture

This project spec **extends**:
- Adds interactive steering (not just passive extraction)
- Integrates with existing Lab-Tracker entities (dropdowns, not just creation)
- Defines the Run Log as a parallel structured capture path

This project spec **defers**:
- External API integration (Semantic Scholar, Elicit)
- Full chat UI
- Vector search
- Institutional/library stewardship model

### Commit-Style Run Log

This project spec **adopts**:
- The commit metaphor (runs as version-controlled units)
- The format structure (intent, apparatus, subjects, data products, deviations, outcome)
- Hash-verified artifacts
- Parent/child run chains

This project spec **integrates** the run log as:
- A convention for structured ELN entries
- An entry type within Lab-Tracker (`entry_type=run_log`)
- A format that SciAgent can parse with high confidence

### Lab-Tracker (Existing)

This project spec **preserves**:
- All existing models and relationships
- The evidence graph (Claim → Panel → Visualization → Analysis → Dataset)
- Status workflows
- ELN link fields

This project spec **extends** Lab-Tracker with:
- `Entry` model as the bridge to ELN content
- `Run.parent_run` for provenance chains
- Outcome tracking at the Entry level
- Search/autocomplete APIs for SciAgent integration
