# Daily-review cue: data-egress defaults

The daily review notifies a reviewer that an edition is ready by sending a
**cue** — an email or push knock — not a digest. This page documents the egress
defaults that make a cue safe to send off-app. It is the narrow, shippable
subset of the broader daily-review privacy policy
(`lab-tracker-jptw`); it covers **only** the metadata-grade payload a cue carries
and leaves the full photo/voice/TTS multimodal-egress policy for later.

> **The rule in one line:** a cue carries a *cardinality and an opaque signed
> link* — never any science. There is deliberately no accept/reject affordance
> in the cue itself; every decision still happens in the authenticated app.

## What a cue may contain

A cue is built from the `GET /batches/editions-ready` read model. Each ready
edition exposes only:

| Field | In the cue? | Notes |
| --- | --- | --- |
| `change_set_id` | encoded inside the signed link only | opaque UUID |
| `decidable_count` | yes, unless suppressed | how many items still await a decision |
| `deep_link` | yes | signed, short-TTL link into the in-app queue |
| `review_assignee_user_id` / `review_assignee_username` | routing only | used to address the cue; never rendered into third-party-visible body text |
| `project_name` | **no, by default** | `null` unless the caller passes `include_project_name=true` |
| `sensitivity_suppressed` | drives suppression | see below |
| edition summary, proposed operations, note text, excerpts | **never** | not present in the read model at all |

The read model has no field that carries proposed graph edges, note content, or
excerpts. There is nothing to accidentally render.

## The four egress defaults

1. **Generic lockscreen / subject; project name suppressed by default.** A
   project name such as “CRISPR-XYZ knockout toxicity” is itself unpublished
   science, and a lockscreen or an email subject line is shoulder-surfable. The
   read model returns `project_name = null` unless the caller explicitly opts in
   with `include_project_name=true` (allowed only where the user chose to surface
   it — e.g. their own inbox, never a shared channel). The fixed cue phrasing is
   generic: *“Your Lab Tracker daily review is ready.”*

2. **Count suppressed on sensitivity-tagged editions.** If any source note in an
   edition is tagged sensitive
   (`metadata["sensitivity"] == "sensitive"`; see
   `SENSITIVITY_METADATA_KEY` / `is_sensitive_note` in
   `src/lab_tracker/services/shared.py`), the edition returns
   `decidable_count = null` and `sensitivity_suppressed = true`, so even activity
   *volume* for a sensitive program cannot be inferred off-app. The cue degrades
   to *“Your daily review is ready”* with no number.

3. **Delivery is off until a channel is explicitly enabled.** Lab Tracker sends
   nothing itself: it holds no SMTP or push credentials and owns no transport.
   The cue is sent by the user’s own run-due routine through the user’s own
   mailbox (see [scheduled-daily-review.md](scheduled-daily-review.md)). No
   surprise egress: a channel only carries cues once the operator wires it up.

4. **The deep link is signed and short-TTL, so a leaked cue is not itself an
   egress vector.** The link
   (`src/lab_tracker/review_links.py`) is an HMAC-SHA256 signature over
   `change_set_id | exp` using the app auth secret, TTL-bounded by
   `LAB_TRACKER_REVIEW_LINK_TTL_HOURS` (default 72h). Following it only lands the
   recipient on `/app/batches/<id>` inside the app, where Lab Tracker’s normal
   authentication still gates every read and the human review gate still governs
   every accept/reject. `GET /r/{token}` is public but only ever 302-redirects;
   an expired or tampered link bounces to “open your Read”, never a silent
   commit and never a data disclosure.

### Deliberate deviation: re-clickable, not single-use

The originating design (bead `lab-tracker-udv1.10`) described the deep link as
“single-use.” The shipped link is instead **re-clickable within its TTL**. The
reasoning, recorded here so it is not lost:

- The link grants **no capability**. Auth is enabled on any deployed instance, so
  following the link still requires the recipient to be signed in as themselves;
  the token is not an access grant, only a signed pointer.
- Single-use links are routinely burned by email clients that **prefetch** links
  for previews/safety scanning, and by a user simply opening the message twice.
  A single-use link would frequently be dead on first human click.
- Because the link confers nothing, single-use would add fragility without adding
  a security guarantee. The TTL bounds leakage; the signature bounds forgery; app
  auth bounds access. Those three are the real guarantees.

If a future channel ever needs a true one-shot link (e.g. a capability-bearing
link), that would require server-side consume state (a table) and is out of scope
here.

## Honestly-named residual

A cue still crosses a per-edition tuple of `{exists, timestamp, count}` to the
device — and, on the email path, to a third-party mailbox. That is
**metadata-grade** egress, mitigated by count-suppression on sensitive editions
and by project-name suppression, but it is not literally zero. Sending any cue is
an affirmative choice to move that much.

## Where this is enforced in code

- Read model + suppression: `GraphDraftService.list_ready_editions`
  (`src/lab_tracker/services/graph_draft_service.py`), admin-gated like
  `run-due`.
- Contentless response shape: `ReadyEdition` (`src/lab_tracker/models.py`).
- Signed link: `src/lab_tracker/review_links.py`.
- Public landing route + admin read endpoint: `routes/review_delivery.py`.
- Sensitivity tag vocabulary: `services/shared.py`.
