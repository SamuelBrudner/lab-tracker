# Scheduled Draft Pass: How Should It Be Triggered?

Companion to the "Scheduled AI graph-draft batch (user-set cadence)" epic
(`lab-tracker-cw3`). This note settled one product question - schedule vs.
user-tap - before the feature shipped.

## What is and isn't at stake

**Not at stake: autonomy.** Drafting never commits on its own. Every proposed
graph operation is queued for human accept / reject / edit through the existing
graph-draft review surface, unless the project owner has since granted
delegated curation ([delegated-curation.md](delegated-curation.md)) — a later
addition to the
graph-draft review surface (`lab-tracker-2dt`). Whether the model runs at
06:00 or when the user taps a button, the next thing that happens is a
human reviewing each proposed op.

**Not at stake (after re-reading): the retained surface.** The deferred
"extraction inbox" workflows in `docs/retained-v1-surface.md` were about
the system deciding what's interesting and queuing unsolicited findings
(the old question-extraction flow). Batch drafting over notes the user
intentionally captured is structuring user-supplied input, not extracting
unsolicited inferences from it. The retained on-demand graph-draft action
already lives at the note level; making it run over a batch of staged
notes is a throughput change, not a contract change.

**At stake: cost and UX shape.** Scheduled drafting means proposals are
waiting when the user opens the app; user-triggered drafting means the
user waits 30–90 s for the model after asking. Scheduled drafting may
also spend model calls on notes the user never gets around to reviewing.

## Two trigger shapes

### A — Scheduled cron batch
Cron runs daily; proposals are pre-drafted and waiting in the review UI.

- **Pro:** zero latency at review time; matches the "daily rhythm" the
  user described.
- **Pro:** the user doesn't have to remember to trigger anything.
- **Con:** model spend on notes that never get reviewed (low-cost to
  mitigate later by skipping un-changed-since-last-run batches).
- **Con:** new operational surface (scheduler, run watermark,
  idempotency, retries). Real but well-trodden.

### B — User-triggered batch
"Draft from today's notes" button in the review UI; click to run.

- **Pro:** no model spend without user intent.
- **Pro:** no scheduler infrastructure.
- **Con:** the user must wait for the model on the same screen.
- **Con:** the "daily rhythm" depends entirely on user habit.

## Recommendation

Build A (scheduled). The user has been clear that the desired UX is
"open the app and find drafts waiting," and the autonomy concern that
would normally argue for B does not apply here because nothing the
scheduler does is irreversible.

Practical sequence inside the epic:

1. `lab-tracker-jdy` — context builder. Pure function from a set of
   staged notes → context packet. Has no opinion about who calls it.
2. `lab-tracker-641` — draft generator. Pure function from packet →
   proposal set. Same property.
3. `lab-tracker-249` — batch review UI. Lists pending batches and lets
   the user accept / reject / edit each proposed op.
4. `lab-tracker-zv9` — scheduler. Calls `jdy` then `641`, persists the
   resulting batch as pending, records run history. Default cadence
   daily; configurable; admin-only "run now" trigger for dev and for
   recovering from a failed run.
5. `lab-tracker-283` — notification. Banner on app open showing how
   many pending batches / unreviewed proposals are waiting. Web push is
   a v2 addition once the PWA service worker lands.

## Decisions (resolved 2026-05-22)

1. **Trigger:** Option A — scheduled cron batch.
2. **Cadence:** daily at a fixed lab-local time. The time itself is a
   configurable setting; default to a morning slot (e.g. 06:00) so
   proposals are ready before the working day.
3. **Empty windows:** skip the batch when no new notes have been
   captured since the last successful run. Run history records the
   skip with reason so it stays observable.

These resolutions are reflected in the bd descriptions for `jdy`,
`641`, `249`, `zv9`, and `283` and need no further amendment.

## Day-log grouping (deterministic stage, added later)

A heavy bench day used to turn into many per-capture proposals. Each batch
draft now ends with one extra, **deterministic** proposal per session that had
at least three short bench captures (typed text, transcribed voice, photo) in
the batch: a `create_note` whose body is a timestamped log (`HH:MM — <first
line of text/transcript, or file name>`) targeting the session, so a reviewer
can accept one legible log instead of N items. The individual capture
proposals stay in the draft.

Design choices:

- **Where it runs.** Inside batch generation, after the model's patch
  validates and before the draft is marked ready, so it lands atomically with
  the draft and the delegated-curation pass sees it like any other proposal
  (admitted by `full`, never by `organize`). It needs no migration: it is an
  ordinary operation row.
- **Honest provenance.** The rationale says "grouped N captures from
  <session>" and that it is a deterministic grouping, not model output; it has
  no confidence score; `source_refs` cite every capture. The change set
  records the appended operation ids under `context_packet.day_logs` (written
  by the server after the model call, so the model cannot claim the label),
  which the review page uses to label it and the applier uses to stamp the
  committed note `origin_provider=lab_tracker`,
  `origin_model=deterministic_day_log` instead of the batch's model.
- **Nothing external.** It reads only the batch's own notes and the project's
  sessions after the provider call; the external-context policy has nothing to
  govern.
- **Idempotent.** The draft is keyed by the batch; a log whose `day_log_key`
  (session plus the exact capture set) already exists as a note is not
  proposed again.
- **Best effort.** A failure is logged and the batch keeps the model's
  proposals. Because the log rides on the batch draft, a batch whose model
  call fails (or a deployment with no provider) gets no day log.

Details, bounds, and the capture rules: [session-suggestions.md](session-suggestions.md).
