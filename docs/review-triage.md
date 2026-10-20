# Review triage

Daily reviews open in **Triage**. The screen asks one meaningful question at a
time, showing the proposed change and its source evidence. **Proposals** and
**Narrative** remain available for a full inspection.

Each newly generated draft starts with a short prose explanation of what the
drafter could not resolve: the captures involved, missing or conflicting context,
and the placements or interpretations left open. It pairs these gaps with specific
questions or practical recommendations about what to record next time. When the
context is sufficient, the opening says so briefly. This opening and the account
of the work appear above the controls in every review view.

Future batch and graph-context drafting runs receive recent draft openings,
unresolved fields, and clarification questions with saved human answers, dates,
and attribution. This history is scoped to the same reviewer and current projects,
includes at most five drafts updated in the last 14 days, and has bounded text and
an overall 16,000-character JSON budget. Prior prose remains draft context; it does
not establish a finding or confirm that a suggested workflow change happened.
Answers about past captures apply only where relevant to the new evidence.

## Where attention goes

**Needs your judgment** contains new scientific meaning, consequential changes
to existing records, validation problems, and clarification questions. Decisions,
pivots, dead ends, prediction resolutions, mergers, and retirements are prioritized
within this lane. Model confidence does not rank review importance.

**Quick confirmations** contains note updates that only add concrete targets
while preserving the targets in the captured context. The original targets must
be known; unfamiliar or content-changing updates require judgment. Related link
edits for the same capture can be confirmed together. Conflicting replacement
target lists must be corrected before a group can be kept.

**Can wait** contains optional follow-ups and goals, and decisions the person
explicitly deferred. A deferred operation remains proposed. **Return to review**
clears its deferral without accepting it.

Dependencies expressed through draft `$ref` placeholders form one decision.
Sharing a source capture alone never groups unrelated scientific claims.

## Making a decision

**Keep this decision** or **Confirm these links** explicitly accepts only the
selected group's still-pending edits. Group acceptance is recorded as
`bulk_accepted`; it does not imply individual scrutiny of every supporting edit.
The endpoint checks the group revision and commits the verdict atomically, so an
invalid edit or a stale view cannot partially accept a group or overwrite a newer
correction. Onboarding proposals continue to require individual review.

**Correct** opens the evidence and typed editors. In browsers supporting speech
recognition, **Dictate replacement text** edits the text buffer using the browser's
speech service. Dictation never accepts or commits a proposal. Typed correction
remains available when dictation is unavailable.

Clarifications have **Your answer**, **Save answer**, and **Later** actions. Answers
are dated and attributed in `review_metadata`; answering does not accept a graph
edit or rewrite the source capture. Browser dictation is also available for answers.
Saved answers are included in the recent draft context used by later drafting runs.
The provider's `context_packet` remains the generation input; human review state
and cached queue counts are stored separately.

## Stopping and catching up

**Save progress and return to queue** leaves the review open with its saved
decisions. Contributors send kept decisions to the owner; owners finish by
committing kept changes. The screen distinguishes saved decisions, submitted
reviews awaiting owner commit, and canonical graph commits.

A partial commit of a triaged review creates one follow-up review for unfinished
operations and clarifications. It preserves deferral stamps and source attribution,
and resolves references to records created by the original commit. Creation of the
follow-up and the canonical commit share one transaction. The original review
links to its follow-up; no additional model call or approval happens.

**Triage across pending reviews** combines the person's pending decisions in one
catch-up view. Consequential decisions precede routine confirmations across review
dates. Reviews load in bounded pages; **Include more reviews** extends the set, and
failed pages remain retryable. Each decision still belongs to its original review
and uses its ordinary authorization, acceptance, and owner commit paths.
After the last verdict, **Finish review** stays in the catch-up queue until the
person submits or commits the saved decisions. Owners can close submitted reviews
with no kept edits using **Close review without graph changes**.

Queue cards show actionable decision counts when available. Existing email and
push alerts retain their contentless policy and link into the review. Reminder
timing uses the existing personal cadence settings.

## Understanding participation

Project owners can open **Review participation over the last 30 days** on Daily
review. `GET /projects/{id}/review-engagement` also accepts a timezone-aware
`since` value. It reports assigned reviews, recorded human HTTP opens, triage
starts, submissions, commits, waiting reviews, and pending follow-ups per reviewer.
It exposes median elapsed seconds from first opening to first triage decision and
to submission/commit. These are elapsed times, not active attention measurements.

Opening counts use existing usage-event logging and are unavailable when logging
is disabled. Background catch-up reads use `record_view=false`, so loading several
reviews does not count as opening them. Triage start timestamps begin with this
feature; older manual decisions are not retroactively inferred. Follow-up reviews
are reported separately rather than inflating the assigned-review denominator.

## Upgrade

Run `alembic upgrade head` before serving the new backend. Migration
`0067_graph_draft_review_metadata` adds the non-null JSON review-metadata column;
the frontend bundle and OpenAPI types must be regenerated with backend changes.
