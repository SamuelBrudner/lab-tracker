# Daily review by email

A scientist can review an assigned daily proposal by replying from their verified
login email. Each message shows one proposed change, the project, the reason for
the change when available, and the deadline for replying.

## Enable review in your inbox

Open **Review proposals by email** in first-run setup or on the **Batches** page.
Ask for a verification code and enter it within 15 minutes. This opts you in to
receiving the contents of assigned proposals across your projects. If you signed
in with a legacy username, you can enter an email address here. After you prove
you own it, that address becomes your login and your current password still works.
You can turn email review off from the same control. Outstanding reply addresses
expire when you opt out.

## Reply to a proposal

Use **Reply** from your verified address and put one line above the quoted email.
These examples show how to review a proposed research question.

| Reply | What Lab Tracker does |
| --- | --- |
| `Approve` | Records your approval for this version of the proposal and sends a confirmation. The usual draft submission and commit permissions still apply. |
| `Correct text: Does the response change after the odor is removed?` | Replaces the proposed wording and emails the edited version for review. The message lists the fields you can edit. |
| `Correct: Link this recording to the question about recovery.` | Records your feedback, sets the proposal aside for revision, and sends a confirmation. |

Each reply address belongs to one version and accepts one decision. A reply to an
older copy cannot approve a newer edit. Use the review link to inspect the full
draft if its assignment, version, or deadline has changed.

## Answer a clarification

If the bucket needs more information, it can email a question on its own.
Reply with one line such as `Answer: I was checking rig calibration` or
`Answer: The recovery question`. Lab Tracker saves your answer in the draft's
review. Reply `Later` to defer the question. Resuming it in the Batches page
sends a fresh reply address.

When captures explicitly name a bench session with no recorded purpose, the
review asks once for that session. Replies such as `Answer: Question 1`,
`Answer: Calibration`, and `Answer: Exploratory` record the scientist's stated
purpose. The **Work purpose** panel on a session or capture shows the
declaration or review confirmation and its date. Choosing a primary question
when starting a scientific session records a declaration. Scientists can also
record purpose directly from that panel.

## Route machine captures

Project owners can use **Unassigned machine captures** on the Batches page to
assign captures from a calendar or shared folder to a current project
contributor. A capture with an explicit session link can use that session's
**Review operator**. An assignment on a capture takes precedence. Overlapping
session times are shown for context and the capture stays in the unassigned
queue until an operator is identified.

Assignment keeps the capture's original source and makes older captures
available to the operator's next batch. A change of operator closes the reply
address for an earlier draft. The new operator receives review emails via their
existing batch cadence and verified mailbox.

## Server setup

Use the SMTP transport and configure a distinct plus-addressed reply mailbox.
Point the IMAP poller at that inbox and enable the in-process ticker or schedule
the integrations poll. The receiving mail server must authenticate senders and
strip untrusted copies of its own authentication headers. Set its identifier in
`LAB_TRACKER_REVIEW_EMAIL_AUTHSERV_ID`. The reply lifetime is set by
`LAB_TRACKER_REVIEW_EMAIL_LINK_TTL_MINUTES` and defaults to 24 hours. See the
[mail settings](configuration.md#daily-review-email-alerts) and
[inbound mailbox settings](configuration.md#server-capture-channels).

## Content-free alerts

A scientist can also enable a short cue when an assigned daily review is ready:

> A Lab Tracker review is ready.
>
> Open Lab Tracker to review it: `<signed link>`

This per-project alert contains only a link and a delivery reference. It is
available with SMTP or the external worker. Account-level proposal emails take
precedence for enrolled scientists when the reply channel is enabled.

## Delivery guarantees

The final `GRAPH_BATCH → READY` update and its recipient-scoped outbox row are
written in one database transaction. A deterministic unique key prevents the
same review from being queued twice. A worker leases one row, commits the
lease, contacts the provider, and then records provider acceptance or a
sanitized failure in a new transaction. Expired leases can be reclaimed after
a worker crash; retryable failures use exponential backoff and a bounded
attempt count. Every lease counts as an attempt, so a delivery whose worker
keeps dying before it reports a result is marked `failed` once its lease
expires with `LAB_TRACKER_REVIEW_EMAIL_MAX_ATTEMPTS` attempts used. The next
claim logs a warning naming each delivery it marks failed this way.

`accepted` means the provider accepted the submission. It does not prove inbox
delivery; that would require provider delivery webhooks.

## Recipient setup

On the daily-review schedule, select a per-user schedule, enter that user's
mailbox, and enable **Email me when my review is ready**. Project-level default
rows cannot receive email because they have no recipient identity. The app
rechecks the preference and address immediately before each provider attempt,
so opting out or changing the address cancels an already queued stale
delivery.

## Signed links

Every real review alert contains a recipient- and delivery-bound HMAC token
with a short expiry. The `/r/<token>` route verifies it and redirects to the
review page. The token is not a login token and never grants project access:
the browser must still sign in, and the ordinary batch API enforces project
authorization.

## Transport choices

With `LAB_TRACKER_REVIEW_EMAIL_TRANSPORT=smtp`, the app's background worker
sends through the configured SMTP account. TLS modes and network timeouts are
explicit, failures are redacted before persistence, and retries reuse a
deterministic `Message-ID`.

With `external`, Lab Tracker owns only the outbox. In Docker Compose, invoke
the bridge inside the already-running **primary** app container so it inherits
the live Postgres configuration and persisted signing secret:

```bash
docker compose \
  -p lab-tracker \
  -f /path/to/lab-tracker/docker-compose.yml \
  exec -T app \
  python -m lab_tracker.review_email_external_worker claim
```

A bare host invocation is safe only when it is explicitly configured with the
same production database URL, public URL, email settings, and authentication
signing secret. Otherwise it may open a local SQLite database or create links
that the live app cannot verify.

The JSON response contains fixed `subject` and `text_content` fields plus an
opaque `dedupe_marker`. Before sending, the mailbox worker searches Sent Items
for that marker; if it already exists, the worker records the existing message
as accepted instead of sending again. The marker is stable across retries and
contains no graph or project identifier.

After the mailbox provider accepts the message, the worker acknowledges it
with `accepted --delivery-id ... --claim-token ... --provider-message-id ...`.
On failure it calls `failed` with the same IDs and a small non-secret error
code. This mode keeps mailbox OAuth credentials out of the Lab Tracker
container.

The root Compose file also provides a default-off, primary-only
`review-email-external` control profile. It mounts the primary app's durable
runtime secret read-only, joins only the primary network, receives no OpenAI or
mailbox credentials, and can be used when a one-shot container is preferable:

```bash
docker compose \
  -p lab-tracker \
  -f /path/to/lab-tracker/docker-compose.yml \
  --profile review-email-external \
  run --rm --no-deps review-email-control claim
```

Replace `claim` with the full `accepted ...`, `failed ...`, or `test --to ...`
argument list as needed. Replace `/path/to/lab-tracker` with the primary
checkout, and `lab-tracker` after `-p` with the primary instance's Compose
project name: the checkout directory name unless `COMPOSE_PROJECT_NAME` or
`-p` set another (`docker compose ls` lists them). The explicit project name
and root Compose path prevent this helper from attaching to another Compose
project on the same host, such as a separate dedicated instance with its own
database and signing secret.

Admins can enqueue a fixed, non-graph diagnostic via
`POST /review-email/test`. The diagnostic is visibly labeled as a test and
creates no graph record. The local bridge exposes the same operation without
an application password. Under Docker, run it through the primary app:

```bash
docker compose \
  -p lab-tracker \
  -f /path/to/lab-tracker/docker-compose.yml \
  exec -T app \
  python -m lab_tracker.review_email_external_worker test --to user@example.org
```

## Unattended external delivery

Do **not** schedule `claim` by itself: a claim acquires a lease and must be
followed by a Sent Items lookup plus `accepted` or `failed`. Schedule the whole
mailbox transaction on a short cadence:

1. Run the primary `claim` command above.
2. Stop when its JSON contains `"delivery": null`.
3. Search the sender's Sent Items for the exact `dedupe_marker`.
4. If found, call `accepted` with that existing provider message ID.
5. Otherwise send the fixed `subject` and `text_content` to `to`, then call
   `accepted` with the new provider message ID.
6. On a provider failure, call `failed`; add `--retryable` only for a transient
   failure.
7. Repeat until the queue is empty, with a conservative per-run cap.

The application and Compose profile deliberately cannot grant mailbox access.
The remaining external input is a mailbox identity (for example the connected
Outlook account) whose scheduled automation may search/read its Sent Items and
send messages. The automation also needs local permission to run the primary
Compose command. No SMTP password, OAuth token, or model key belongs in Lab
Tracker's Compose file.
