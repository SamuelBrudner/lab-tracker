# Mobile-First, Multimodal Graph-Aware Note-to-Graph Draft Review

Status: Implemented for the v1 core photo, voice, photo+voice bundle, and text
capture workflow; offline queued capture remains deferred.

Tracking:

- Documentation task: `lab-tracker-dfr`
- Implementation feature: `lab-tracker-mgr`
- Multimodal voice/bundle extension: `lab-tracker-mf7`

This document began as the image-to-graph workflow spec. The current retained
workflow generalizes the same human-approved review path to voice notes and
photo+voice bundles: raw image/audio artifacts remain provenance anchors,
transcripts are editable derived artifacts, graph context is supplied to the
model before drafting, and accepted operations still commit through normal API
validation.

## Summary

Lab Tracker should support a mobile-first capture workflow where a researcher
can take a photo, record a voice note, create a photo+voice bundle, or enter a
quick text note from a phone, then have the system generate a graph-aware draft
entry for human review.

The critical design point is that the AI interpreter should not treat the image
as an isolated document. It should use the existing Lab Tracker graph: active
projects, questions, sessions, datasets, notes, analyses, claims, recent
activity, and known aliases as context when interpreting the image and drafting
graph operations.

The phone should be treated as a first-class hardware interface, not merely as
a smaller screen for the desktop app.

## Motivation

Researchers will not reliably produce rich metadata if the workflow requires
them to sit at a computer and manually compose structured entries after a long
day. The realistic workflow is:

```text
take photo on phone -> AI drafts structured graph update -> researcher reviews/edits -> commit
```

The phone is essential because the relevant context is often created at the
bench, in front of a whiteboard, beside a rig, in a notebook, in a hallway
conversation, or during a meeting. A desktop-first workflow misses the moment
when the metadata is easiest to capture.

This feature should make the easiest behavior also the metadata-preserving
behavior. The researcher should not feel like they are "submitting metadata."
They should feel like they are saving their own memory, organizing their own
work, and avoiding future confusion. Rich dataset metadata should emerge as a
byproduct.

## User Story

As a researcher, I want to take a photo of my notes from my phone and have Lab
Tracker draft a structured graph entry using the current project context, so
that I can approve accurate links to existing questions, sessions, datasets,
notes, analyses, and claims without manually reconstructing context later.

## Product Principle

Lab Tracker should support low-friction research capture first and structured
metadata second.

The system should preserve the raw human note, interpret it in context, and
propose structured graph operations only as a reviewable draft. The model
should never directly mutate the graph.

The core interaction is:

1. Researcher captures a photo on a phone.
2. Lab Tracker stores the raw image as provenance.
3. Lab Tracker builds a compact context packet from the existing graph.
4. The AI proposes draft graph operations.
5. The researcher reviews, edits, accepts, rejects, or defers.
6. Accepted operations are committed through normal validation.

## Proposed Workflow

### 1. Mobile Capture

Add or refine a phone-first capture route, for example:

```text
/app/capture
```

or:

```text
/app/mobile/capture
```

This interface should be optimized for quick phone use:

- open camera;
- take photo or select from camera roll;
- optionally select project;
- optionally select active question;
- optionally select session/dataset;
- optionally add a short text hint;
- upload;
- defer review or immediately request a draft.

The capture UI should not require desktop-style navigation.

### 2. Preserve Raw Note as Provenance

Every uploaded photo should be stored as a raw note artifact before
interpretation.

The image remains the provenance anchor. The AI output is only a draft
interpretation layered on top of the raw human record.

The raw image should retain:

- upload timestamp;
- user;
- selected project/question/session/dataset context, if supplied;
- optional user hint;
- review status;
- links to any accepted graph operations derived from it.

### 3. Build Graph Context Packet

When the user requests an AI draft, the backend should construct a compact
graph context packet from the current Lab Tracker graph.

Context should include, where available:

- selected project;
- selected or recent session;
- selected or active question;
- parent/child question hierarchy;
- recent notes;
- recent datasets;
- recent analyses;
- claims and visualizations;
- entity aliases or labels;
- current user;
- timestamps;
- unresolved recent captures.

The context packet should be bounded and readable by the model.

### 4. Interpret Image Using Graph Context

The model receives:

```text
image + optional user hint + graph context packet + allowed operation schema
```

It should produce draft graph operations, not direct writes.

Example output:

```json
{
  "operations": [
    {
      "op": "create_note",
      "target_type": "session",
      "target_id": "session_2026_05_14_rig2",
      "text": "Rig 2 gradient protocol. Fly 12 tracked well. Turning appeared stronger after pulse onset.",
      "confidence": 0.86
    },
    {
      "op": "link_note_to_question",
      "note_ref": "new_note",
      "question_id": "q_gradient_climbing",
      "confidence": 0.82
    },
    {
      "op": "suggest_followup",
      "text": "Compare turning after pulse onset against smoother plume condition from prior sessions.",
      "linked_question_id": "q_plume_statistics",
      "confidence": 0.61
    }
  ],
  "uncertain_fields": [
    "Exact identity of 'last week's smoother plume condition'",
    "Whether Fly 12 should be represented as a formal subject/entity"
  ]
}
```

### 5. Human Review

The review interface should work on both phone and desktop.

Phone review should support quick actions:

- approve draft;
- edit text;
- change linked question/session/dataset;
- mark uncertain;
- reject draft;
- defer review.

Desktop review can support richer editing and batch cleanup.

The workflow should remain on-demand, note-scoped, and human-approved. A phone
capture flow can create a lightweight "pending review" list, but the feature
should not become a broad automatic extraction inbox.

### 6. Commit Through Existing Validation

Accepted operations should go through normal API validation.

The model should never directly mutate the graph.

All graph writes should remain explicit, validated, and auditable.

## Critical Requirement

The image interpreter must use the existing graph as input context.

This means:

```text
The graph is not only the output of interpretation. It is also the grounding context that helps the model interpret ambiguous notes.
```

For example, a note saying:

```text
same gradient protocol as last week, Rig 2, weird turning after pulse onset
```

is hard to interpret in isolation. But with graph context, the model may know:

- which project is active;
- which questions involve gradient protocols;
- which Rig 2 sessions happened recently;
- which datasets were collected last week;
- which analysis or claim involved turning after pulse onset.

The draft should prefer linking to existing entities over creating duplicates.

## Phone-Specific Requirements

### Mobile-First Capture UI

The capture flow should be designed for phones first, not adapted from desktop.

Acceptance criteria:

- User can capture a photo directly from the phone camera.
- User can upload from camera roll.
- User can attach the image to a project/question/session/dataset with minimal
  taps.
- User can capture without fully resolving context, then review later.
- Capture can happen at the bench, rig, notebook, field site, hallway, or
  whiteboard.
- The UI works in a mobile browser.
- The flow does not require installing a native app for the first version.

### End-of-Day Review

The system should support a natural pattern:

```text
capture throughout the day -> review drafts at the end of the day
```

This does not need to become a broad automatic extraction inbox. It can remain
narrow:

- only user-uploaded image notes;
- only captures where the user requested or queued a draft;
- only note-scoped operations;
- no automatic commits.

### Offline / Unreliable Network Tolerance

Nice-to-have for later, but important for real lab settings:

- allow local queued captures if network drops;
- sync when connection returns;
- preserve timestamp and user;
- show unsynced status clearly.

### Minimal Capture Friction

The phone flow should avoid asking for too much up front.

Good flow:

```text
photo -> optional project/session/question suggestion -> upload -> draft later
```

Bad flow:

```text
fill out metadata form -> choose ontology terms -> upload file -> write description
```

The whole point is to capture the raw note before the context disappears.

## Graph Context Packet

Add a backend service such as:

```python
def build_graph_context_for_note(note_id: str) -> GraphContextPacket:
    """
    Build a compact context packet around an uploaded image note.

    Include selected entities, active project/question/session context,
    recent graph neighborhood, and candidate entity matches.
    """
```

Possible context neighborhood:

```text
uploaded note
-> selected project/question/session/dataset
-> active project questions
-> parent/child questions
-> recent sessions
-> recent datasets
-> recent notes
-> analyses/claims/visualizations
-> aliases and labels
```

The context packet should include stable IDs, not only names.

Example:

```json
{
  "project": {
    "id": "project_odor_navigation",
    "label": "Odor-guided navigation"
  },
  "active_questions": [
    {
      "id": "q_gradient_climbing",
      "label": "Can flies climb temporal odor gradients?",
      "status": "active"
    },
    {
      "id": "q_plume_statistics",
      "label": "How do plume statistics shape navigation strategy?",
      "status": "active"
    }
  ],
  "recent_sessions": [
    {
      "id": "session_2026_05_14_rig2",
      "label": "Rig 2 fictive odor session",
      "linked_question_id": "q_gradient_climbing"
    }
  ],
  "recent_datasets": [
    {
      "id": "dataset_2026_05_14_fly12",
      "label": "Fly 12 tracking data, Rig 2",
      "linked_session_id": "session_2026_05_14_rig2"
    }
  ]
}
```

## Model Prompt Requirements

The model prompt should include instructions like:

```text
You are drafting graph operations for Lab Tracker.

Use the provided graph context to resolve ambiguous references in the image.
Prefer linking to existing entities when possible.
Do not invent IDs.
Do not commit changes.
Return only draft operations matching the allowed schema.
Mark uncertainty explicitly.
Preserve the uploaded image note as the provenance source.
```

The model should be required to return structured output that conforms to an
explicit schema.

## Draft Operation Requirements

Draft operations should distinguish between:

1. creating a note;
2. linking the note to an existing entity;
3. proposing a new entity;
4. suggesting a follow-up;
5. flagging uncertainty;
6. requesting user clarification.

The draft should not collapse these into prose.

Example operation types:

```text
create_note
link_note_to_question
link_note_to_session
link_note_to_dataset
link_note_to_analysis
suggest_new_question
suggest_new_dataset
suggest_followup
request_clarification
```

## Acceptance Criteria

- Phone-first capture route exists.
- User can take or upload a photo from a phone.
- User can optionally attach the photo to project/question/session/dataset
  context.
- Raw image is stored as a note/provenance artifact.
- Draft generation uses a graph context packet.
- Draft operations can reference existing entity IDs.
- Model is instructed to prefer existing entities over new entities.
- Review UI shows which graph context was used.
- Review UI distinguishes proposed links to existing entities, proposed new
  entities, and uncertain interpretations.
- User can approve, edit, reject, or defer.
- No operation is committed automatically.
- Accepted operations go through normal API validation.
- The workflow remains note-scoped and human-approved.
- The feature fails loud and fast if graph context cannot be built; it should
  not silently draft against missing context unless the user explicitly requests
  an image-only interpretation.
- The implementation must avoid stubbing or masking functionality.

## Non-Goals

- No fully autonomous metadata generation.
- No automatic graph mutation.
- No requirement that every image produce a structured entry.
- No replacement of the raw human note.
- No broad extraction inbox over all notes.
- No assumption that researchers will sit at computers to create metadata.
- No desktop-first capture design.
- No image-only interpretation as the default path.
- No quiet fallback to generic OCR if graph context is unavailable.

## Validation and Failure Behavior

The feature should fail loud and fast when key assumptions are violated.

Examples:

- If the note ID does not exist, return an explicit error.
- If the image file is unavailable, return an explicit error.
- If graph context cannot be built, notify the user and allow them to choose
  image-only drafting explicitly.
- If model output does not match the expected schema, reject the draft and
  surface the validation error.
- If a proposed operation references an unknown entity ID, reject that
  operation.
- If the model proposes a write outside the allowed operation set, reject it.

The system should not silently drop failed operations or convert them into
generic prose.

## Privacy and Security Considerations

Lab notes may contain unpublished ideas, sensitive data, grant-relevant
information, or human-subject/private information depending on domain.

The feature should therefore make the following explicit:

- where images are stored;
- which model receives the image;
- whether images or outputs are logged;
- whether data are retained by third-party providers;
- who can access raw images and drafts;
- whether project-level permissions apply to drafts;
- whether drafts are visible before approval.

For institutional deployments, model routing and data retention should be
configurable.

## Why This Matters for Dataset Discovery

For research dataset discovery, the upstream capture layer is crucial.
Repository metadata is often sparse because researchers are asked to describe
datasets after the fact, usually for compliance rather than for their own
benefit.

A mobile-first Lab Tracker pattern changes the incentive structure:

```text
researchers capture notes for themselves -> AI drafts graph entries -> humans approve -> rich dataset context becomes a byproduct
```

That makes Lab Tracker a plausible input-side complement to a larger discovery
graph. The discovery graph can harvest metadata from repositories and
aggregators, but a phone-based lab capture interface can preserve the context
that never makes it into those systems in the first place.

The core feature is therefore:

```text
mobile-first photo capture + graph-aware AI interpretation + human-approved graph commit
```

## Implementation Notes

Implemented defaults:

- `/app/capture` is the phone-first capture route.
- Captures are project-scoped and require a project before upload.
- Graph context uses explicit links plus bounded recency, not embeddings.
- Graph-context drafting is the default; image-only drafting is explicit
  fallback only.
- Raw image notes remain the provenance anchor in note storage.
- Draft operations add semantic labels on top of existing create/update commit
  machinery.
- Review is hybrid: typed controls for common operations with JSON as the
  advanced escape hatch.
- Offline local queued capture is deferred.

### Suggested Milestones

#### Milestone 1: Mobile Capture

- Add mobile capture route.
- Support camera/photo upload.
- Store raw image note.
- Allow optional project/question/session/dataset attachment.

#### Milestone 2: Context Packet

- Implement `build_graph_context_for_note`.
- Include bounded graph neighborhood.
- Include stable IDs, labels, and relationship summaries.
- Add tests for context packet construction.

#### Milestone 3: Graph-Aware Draft Generation

- Pass image and graph context to model.
- Enforce structured output schema.
- Reject invalid output.
- Require uncertainty fields.

#### Milestone 4: Review and Commit

- Show draft operations in review UI.
- Support approve/edit/reject/defer.
- Commit accepted operations through existing API validation.
- Keep rejected operations out of the graph.

#### Milestone 5: Evaluation

- Measure time from capture to approved entry.
- Measure proportion of draft operations accepted, edited, rejected, or
  deferred.
- Track duplicate entity creation rate.
- Track whether graph context improves linking accuracy compared with
  image-only interpretation.

### Minimal Evaluation Design

Compare two conditions:

1. image-only interpretation;
2. image + graph-context interpretation.

Metrics:

- correct links to existing entities;
- duplicate entity proposals;
- human edit distance;
- reviewer acceptance rate;
- time to approved entry;
- number of uncertain fields correctly flagged;
- user preference.

The core hypothesis is:

```text
Graph-context interpretation will produce fewer duplicate entities, more correct links, and lower human review burden than image-only interpretation.
```

## Open Questions

- What is the smallest useful graph context packet?
- Should graph context be selected by recency, explicit links, semantic
  similarity, or all three?
- How should the system handle multiple active projects?
- Should capture default to the most recent project/session used by the user?
- How much review should be possible on phone versus desktop?
- Should phone capture create a pending-review queue?
- Should low-confidence drafts be hidden behind a "needs review" state?
- What metadata is safe to send to external model providers?
- Should institutional deployments support local/on-premise models for
  sensitive notes?

## Final Framing

This feature turns Lab Tracker from an image parser into a context-aware
research-record assistant.

The graph is not just something the model writes to. The graph becomes part of
the model's interpretive context.

That is the key step toward a usable input-side research metadata workflow.
