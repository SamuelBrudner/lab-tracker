# Curation states: keeping the graph honest about itself

Lab Tracker's record is only as trustworthy as what you actually reviewed. To
keep the committed graph honest about its own provenance, two distinctions are
recorded durably and exposed through the API.

## How an AI proposal was accepted

Every AI-proposed graph operation carries an **acceptance mode** once it is
accepted, recorded alongside the accepting user and timestamp:

| Mode | Meaning |
| --- | --- |
| `human_selected` | Accepted one operation at a time, after looking at it. |
| `bulk_accepted` | Accepted as part of an "accept all" over the draft. |
| `auto_accepted` | Reserved for any future non-interactive acceptance. |

This matters because a confident, plausible AI suggestion that you clicked
through in a batch is otherwise indistinguishable, in the committed graph, from
an edge you authored and scrutinized. Recording the mode means a later
synthesis (e.g. a progress report) can lean on what was genuinely reviewed
rather than laundering an unreviewed guess into a grant.

### Endpoints

- `PATCH /graph-drafts/{change_set_id}/operations/{operation_id}` with
  `{"status": "accepted"}` records `human_selected`.
- `POST /graph-drafts/{change_set_id}/accept-all` accepts every still-proposed,
  valid operation and records `bulk_accepted`. Operations you already accepted
  by hand keep their `human_selected` mark; invalid operations stay proposed so
  they surface for editing rather than entering the graph silently.
- Re-opening an operation (back to `proposed` or `rejected`) clears the
  acceptance mark, so a re-opened operation never carries a stale record.

The fields appear on each operation in the change-set payload:
`acceptance_mode`, `accepted_by`, `accepted_by_user_id`, `accepted_at`.

## Why a capture was set aside

A captured note is never silently dropped. It stays `staged` (and visible) until
you decide what to do with it. Setting one aside is a first-class action that
always names a reason:

| Reason | Meaning |
| --- | --- |
| `reviewed_not_relevant` | You looked at it and it doesn't belong in the graph. |
| `superseded` | A later capture replaced it. |
| `archived_unreviewed` | Set aside without review (the default). |

So a skipped review degrades **visible coverage** ("37 captures unreviewed since
June 1"), not **silent trust**. You can always tell the difference between a
capture you judged irrelevant and one you simply never got to.

### Endpoint

- `POST /notes/{note_id}/archive` with an optional
  `{"reason": "reviewed_not_relevant"}`. The reason defaults to
  `archived_unreviewed`. The archived note records `archived_reason`,
  `archived_at`, and `archived_by`.
- `GET /projects/{project_id}/coverage` reports how many staged captures are
  still unreviewed, unplaced, or archived unreviewed, so the "37 captures
  unreviewed since June 1" number above is a real read, not a slogan.

## Keep a capture out of scheduled drafting

A staged capture can opt out of the daily review without being set aside:

- `PATCH /notes/{note_id}` with
  `{"metadata": {"scheduled_graph_draft_policy": "exclude"}}` (or the same
  key when the note is created) marks it, and scheduled, run-due, and run-now
  batches skip it. `exclude` is the only admitted value; any other value is
  rejected with `422`.
- The capture stays `staged` and visible, so it still counts as unreviewed
  coverage until it is committed or archived: this is an opt-out from
  drafting, not a judgement about the capture. Note-scoped drafts
  (`POST /notes/{note_id}/graph-drafts`) still work on it.
- Member-onboarding checkpoints carry the same key, set by the guided
  workflow; only `member_onboarding_*` metadata keys are reserved for that
  workflow.
